from __future__ import annotations

import json
import math
import os
import time
import datetime as dt
import email.utils
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .cache import object_hash
from .config import (
    DEFAULT_JEV_ENDPOINT,
    DEFAULT_JEV_KEY_ENV,
    DEFAULT_JEV_MODEL,
    DEFAULT_USER_AGENT,
    DIALOGUE_SAMPLER_VERSION,
    HTTP_TIMEOUT_SECONDS,
    MAX_QUESTION_COUNT,
    MAX_REQUEST_BYTES,
    MAX_RETRIES,
    MAX_STATE_BYTES,
    STYLE_PROMPT_VERSION,
    STYLE_STATUSES,
    STYLE_TAXONOMY_VERSION,
)
from .dialogue import evidence_sufficiency, sample_id, sample_manifest_hash


class JevError(RuntimeError):
    pass


class JevConfigurationError(JevError):
    pass


class JevResponseError(JevError):
    pass


def _description(key: str, entry: dict) -> str:
    return str(entry.get("classification_description") or entry.get("render") or key.replace("_", " "))


def build_questions(vocabulary: dict) -> dict[str, dict]:
    questions = vocabulary["classification"]["questions"]
    result = {}
    for question_id, spec in questions.items():
        qtype = spec["type"]
        question = {"type": qtype, "instructions": spec["instructions"]}
        if qtype == "choice":
            source = spec["values"]
            allowed = _resolve_values(vocabulary, source)
            question["criteria"] = {
                key: _description(key, entry)
                for key, entry in allowed.items()
            }
            question["evidence_required"] = True
            question["max_supporting"] = 4
            question["max_counterexamples"] = 3
            question["min_supporting"] = 2
            question["rationale_required"] = True
        elif qtype == "score":
            question["criteria"] = list(spec["levels"])
        elif qtype == "noul" and spec.get("criteria"):
            question["criteria"] = dict(spec["criteria"])
        result[question_id] = question
    if len(result) > MAX_QUESTION_COUNT:
        raise ValueError(
            f"classification defines {len(result)} questions; local conservative limit is {MAX_QUESTION_COUNT}"
        )
    return result


def _resolve_values(vocabulary: dict, path: str) -> dict:
    if path.startswith("identity."):
        field = path.split(".", 1)[1]
        return vocabulary["identity"][field]["values"]
    if path.startswith("speech_style."):
        field = path.split(".", 1)[1]
        return vocabulary["speech_style"]["dimensions"][field]["values"]
    raise ValueError(f"unsupported question vocabulary path: {path}")


# What the model is told about its own evidence base. A 24-line sample of 1,552
# lines is 1.5%; saying so is what keeps a confident-sounding answer honest.
GUARDRAILS = (
    "Classify only the recurring written-dialogue style of one character.",
    "Do not infer race, gender, age, body, occupation, status, personality, or expected archetype.",
    "Dialogue text cannot establish pitch, timbre, resonance, accent, loudness, speed, pauses, or intonation.",
    "Judge recurring patterns across ordinary lines; a dramatic scene is not the default.",
    "Use mixed when multiple patterns genuinely recur, and abstain when the sample is too sparse.",
    "Cite only supplied line IDs and give a short rationale tied to those lines. Never invent an ID, quote, or source fact.",
    "Model self-reported confidence is diagnostic only and is not calibrated.",
)


def _coverage(statistics: dict, samples: list[dict]) -> dict:
    unique = int(statistics.get("unique_line_count") or statistics.get("dialogue_count") or 0)
    sampled = len(samples)
    source_counts = {}
    contexts = set()
    for sample in samples:
        source = str(sample.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        contexts.add((source, str(sample.get("sheet") or "unknown")))
    return {
        "rows": statistics.get("dialogue_count"),
        "unique_lines": unique,
        "sampled_lines": sampled,
        "sampled_words": sum(len(str(sample.get("text") or "").split()) for sample in samples),
        "coverage_of_unique_lines": round(sampled / unique, 4) if unique else None,
        "source_distribution": statistics.get("source_distribution") or {},
        "sampled_sources": dict(sorted(source_counts.items())),
        "sampled_source_count": len(source_counts),
        "sampled_context_count": len(contexts),
        "warning": (
            "The sample is a deterministic subset. Treat it as evidence, not as the full corpus."
            if unique and sampled / unique < 0.25 else None
        ),
    }


def _sample_records(dossier: dict) -> list[dict]:
    canonical_id = str((dossier.get("identity") or {}).get("canonical_id") or "unknown")
    records = []
    for sample in dossier.get("dialogue_samples") or []:
        text = str(sample.get("text") or "")
        if not text.strip():
            continue
        identifier = sample.get("id") or sample_id(canonical_id, sample)
        records.append({
            "id": str(identifier),
            "text": text,
            "source": sample.get("source"),
            "sheet": sample.get("sheet"),
            "length_bucket": sample.get("length_bucket"),
        })
    return records


def _state_without_samples(
    dossier: dict,
    samples: list[dict],
    *,
    include_measurements: bool,
    budget_reduction: dict | None = None,
    vocabulary: dict | None = None,
) -> dict:
    statistics = dossier.get("statistics") or {}
    canonical_id = str((dossier.get("identity") or {}).get("canonical_id") or "unknown")
    manifest = dossier.get("sample_manifest") or {}
    state = {
        "task": (
            "Classify the character's stable, recurring written-dialogue style from the supplied "
            "anonymous lines. Do not infer voice acoustics or identity."
        ),
        "dialogue_samples": [
            {"id": sample["id"], "text": sample["text"]}
            for sample in samples
        ],
        "dialogue_coverage": _coverage(statistics, samples),
        "evidence_sufficiency": evidence_sufficiency(
            statistics, samples, vocabulary=vocabulary,
        ),
        "sample_manifest_hash": sample_manifest_hash(
            canonical_id, samples, corpus_hash=manifest.get("corpus_hash"),
        ),
        "taxonomy_version": STYLE_TAXONOMY_VERSION,
        "prompt_version": STYLE_PROMPT_VERSION,
        "sampler_version": DIALOGUE_SAMPLER_VERSION,
        "guardrails": list(GUARDRAILS),
    }
    if include_measurements:
        state["dialogue_measurements"] = (
            (dossier.get("dialogue_analysis") or {}).get("measurements") or {}
        )
    if budget_reduction is not None:
        state["budget_reduction"] = budget_reduction
    return state


def _encode_size(state: dict) -> int:
    return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))


def _trim_to_budget(
    dossier: dict,
    samples: list[dict],
    *,
    max_bytes: int,
    include_measurements: bool,
    vocabulary: dict | None = None,
) -> tuple[list[dict], dict]:
    original = len(samples)
    # Stable order makes budget reduction reproducible. The lowest opaque IDs
    # are kept first; coverage and hashes are recomputed after trimming.
    ordered = sorted(samples, key=lambda sample: sample["id"])
    kept = list(ordered)
    while True:
        reduction = {
            "applied": len(kept) != original,
            "original_samples": original,
            "final_samples": len(kept),
            "reason": "max_state_bytes" if len(kept) != original else None,
        }
        state = _state_without_samples(
            dossier, kept, include_measurements=include_measurements,
            budget_reduction=reduction, vocabulary=vocabulary,
        )
        if _encode_size(state) <= max_bytes:
            return kept, reduction
        if not kept:
            raise ValueError("dossier state exceeds configured Jev request limit")
        kept.pop()


def compact_state(
    dossier: dict,
    *,
    max_bytes: int = MAX_STATE_BYTES,
    include_measurements: bool = False,
    vocabulary: dict | None = None,
) -> dict:
    """Build the identity-blind state the model sees.

    The primary request contains only stable opaque IDs, dialogue text, minimal
    coverage metadata, and version tags. It deliberately omits identity,
    appearance, voice evidence, rules interpretations, and regex signal labels.
    """
    samples = _sample_records(dossier)
    if not samples:
        state = _state_without_samples(
            dossier, [], include_measurements=include_measurements,
            vocabulary=vocabulary,
        )
        if _encode_size(state) > max_bytes:
            raise ValueError("dossier state exceeds configured Jev request limit")
        return state
    kept, reduction = _trim_to_budget(
        dossier, samples, max_bytes=max_bytes, include_measurements=include_measurements,
        vocabulary=vocabulary,
    )
    state = _state_without_samples(
        dossier, kept, include_measurements=include_measurements,
        budget_reduction=reduction, vocabulary=vocabulary,
    )
    if _encode_size(state) > max_bytes:
        raise ValueError("dossier state exceeds configured Jev request limit")
    return state


def _finite_probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevResponseError(f"{label} must be a number")
    cooked = float(value)
    if not math.isfinite(cooked) or not 0 <= cooked <= 1:
        raise JevResponseError(f"{label} must be between 0 and 1")
    return cooked


def _check_distribution(probabilities: Any, expected_keys: list[str], label: str) -> dict[str, float]:
    if not isinstance(probabilities, dict):
        raise JevResponseError(f"{label}.probabilities must be an object")
    cooked = {str(key): _finite_probability(value, f"{label}.probabilities.{key}")
              for key, value in probabilities.items()}
    if set(cooked) != set(expected_keys):
        raise JevResponseError(f"{label}.probabilities has invalid option keys")
    if abs(sum(cooked.values()) - 1.0) > 0.02:
        raise JevResponseError(f"{label}.probabilities must sum to 1")
    return cooked


def _line_id_list(value: Any, label: str, limit: int) -> list[str]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise JevResponseError(f"{label} must be a list of non-empty strings")
    if len(value) > limit:
        raise JevResponseError(f"{label} has too many IDs")
    if len(set(value)) != len(value):
        raise JevResponseError(f"{label} contains duplicate IDs")
    return list(value)


def validate_response(payload: dict, questions: dict[str, dict]) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        raise JevResponseError("response is missing the answering model ID")
    answer_values = payload.get("answers")
    if not isinstance(answer_values, dict):
        raise JevResponseError("response is missing answers")
    if set(answer_values) != set(questions):
        missing = sorted(set(questions) - set(answer_values))
        extra = sorted(set(answer_values) - set(questions))
        raise JevResponseError(f"answer IDs mismatch (missing={missing}, extra={extra})")
    answers = {}
    for question_id, question in questions.items():
        answer = answer_values[question_id]
        expected_type = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != expected_type:
            raise JevResponseError(f"{question_id} has the wrong answer type")
        if expected_type == "choice":
            allowed = list(question["criteria"])
            choice = answer.get("choice")
            # A missing status is not evidence. Keep the model's choice for
            # audit, but record the answer as unresolved so aggregation cannot
            # promote it to supported.
            status = answer.get("status") or "unresolved"
            if status not in STYLE_STATUSES:
                raise JevResponseError(f"{question_id} has an invalid status")
            if status in {"insufficient_evidence", "rejected"}:
                if choice is not None:
                    raise JevResponseError(
                        f"{question_id} must not select a label when status is {status}"
                    )
            elif status in {"supported", "mixed", "provisional"}:
                if choice not in allowed:
                    raise JevResponseError(f"{question_id} selected an invalid option")
            probabilities = None
            if choice is not None:
                probabilities = _check_distribution(
                    answer.get("probabilities"), allowed, question_id
                )
                if probabilities[choice] + 1e-9 < max(probabilities.values()):
                    raise JevResponseError(
                        f"{question_id} choice is not the most probable option"
                    )
            confidence = answer.get("confidence")
            if confidence is not None:
                confidence = _finite_probability(confidence, f"{question_id}.confidence")
            diagnostic = answer.get("model_self_reported_confidence")
            if diagnostic is not None:
                diagnostic = _finite_probability(
                    diagnostic, f"{question_id}.model_self_reported_confidence"
                )
            supporting = _line_id_list(
                answer.get("supporting_line_ids"),
                f"{question_id}.supporting_line_ids",
                int(question.get("max_supporting", 4)),
            )
            counterexamples = _line_id_list(
                answer.get("counterexample_line_ids"),
                f"{question_id}.counterexample_line_ids",
                int(question.get("max_counterexamples", 3)),
            )
            if set(supporting) & set(counterexamples):
                raise JevResponseError(
                    f"{question_id} cites the same line as support and counterexample"
                )
            rationale = answer.get("rationale")
            if rationale is not None and not isinstance(rationale, str):
                raise JevResponseError(f"{question_id}.rationale must be a string")
            answers[question_id] = {
                "type": expected_type,
                "choice": choice,
                "status": status,
                "probabilities": probabilities,
                "confidence": confidence,
                "model_self_reported_confidence": diagnostic,
                "supporting_line_ids": supporting,
                "counterexample_line_ids": counterexamples,
                "rationale": rationale,
            }
        elif expected_type == "score":
            levels = question["criteria"]
            allowed = [str(index) for index in range(len(levels))]
            probabilities = _check_distribution(
                answer.get("probabilities"), allowed, question_id
            )
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise JevResponseError(f"{question_id}.score must be numeric")
            score = float(score)
            if not math.isfinite(score) or score < 0 or score > len(levels) - 1:
                raise JevResponseError(f"{question_id}.score is outside its level range")
            confidence = _finite_probability(answer.get("confidence"), f"{question_id}.confidence")
            answers[question_id] = {
                "type": expected_type,
                "score": score,
                "legend": answer.get("legend"),
                "probabilities": probabilities,
                "confidence": confidence,
            }
        elif expected_type == "noul":
            answers[question_id] = {
                "type": expected_type,
                "noul": _finite_probability(answer.get("noul"), f"{question_id}.noul"),
            }
        else:
            raise JevResponseError(f"unsupported request question type {expected_type!r}")

    usage = payload.get("usage") or {}
    normalized_usage = {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise JevResponseError(f"usage.{key} must be a nonnegative integer")
        normalized_usage[key] = value
    return {
        "model": payload["model"],
        "answers": answers,
        "usage": normalized_usage,
    }


class JevProvider:
    """HTTP adapter for TypeSafe System One / Jev.

    Duck-typed against ``ClassifierProvider``: ``classifier`` imports
    ``build_questions`` from this module, so subclassing it here would cycle.
    """

    name = "jev"

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_JEV_ENDPOINT,
        model: str = DEFAULT_JEV_MODEL,
        api_key_env: str = DEFAULT_JEV_KEY_ENV,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        retries: int = MAX_RETRIES,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        vocabulary: dict | None = None,
    ):
        self.vocabulary = vocabulary
        self.endpoint = endpoint
        self.model = model
        self.model_id = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or urlopen
        self.sleeper = sleeper

    @property
    def local_endpoint(self) -> bool:
        """True when the endpoint is a loopback server that needs no API key."""
        host = urlsplit(self.endpoint).hostname or ""
        return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

    def classify(self, dossier: dict, questions: dict[str, dict]) -> dict:
        return self.classify_state(
            compact_state(dossier, vocabulary=self.vocabulary), questions,
        )

    def classify_state(self, state: dict, questions: dict[str, dict]) -> dict:
        api_key = os.environ.get(self.api_key_env)
        if not api_key and not self.local_endpoint:
            raise JevConfigurationError(
                f"Jev API key missing; set {self.api_key_env} in the environment "
                "(a loopback endpoint such as http://127.0.0.1:18081/v1/systemone needs none)"
            )
        payload = {"model": self.model, "state": state, "questions": questions}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        state_bytes = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        if state_bytes > MAX_STATE_BYTES:
            raise JevError("state exceeds configured context safety limit")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise JevError("request exceeds configured HTTP body safety limit")
        request = Request(
            self.endpoint,
            data=encoded,
            headers={
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            method="POST",
        )
        for attempt in range(self.retries + 1):
            try:
                response = self.opener(request, timeout=self.timeout)
                status = getattr(response, "status", 200)
                body = response.read()
                if status < 200 or status >= 300:
                    if status == 429 or status >= 500:
                        headers = getattr(response, "headers", {})
                        raise _RetryableHttp(status, headers.get("Retry-After"))
                    raise JevError(f"Jev returned HTTP {status}")
                try:
                    decoded = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JevResponseError("Jev returned invalid JSON") from exc
                return validate_response(decoded, questions)
            except HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    if attempt >= self.retries:
                        raise JevError(f"Jev request failed with HTTP {exc.code}") from exc
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    self.sleeper(self._retry_delay(attempt, retry_after))
                    continue
                raise JevError(f"Jev request failed with HTTP {exc.code}") from exc
            except _RetryableHttp as exc:
                if attempt >= self.retries:
                    raise JevError(f"Jev request failed with HTTP {exc.status}") from exc
                self.sleeper(self._retry_delay(attempt, exc.retry_after))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.retries:
                    raise JevError(f"Jev request failed: {type(exc).__name__}") from exc
                self.sleeper(min(30.0, 0.5 * (2 ** attempt)))
        raise JevError("Jev request exhausted retries")

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(120.0, float(retry_after)))
            except ValueError:
                try:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    return max(0.0, min(120.0, (parsed - dt.datetime.now(dt.timezone.utc)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(30.0, 0.5 * (2 ** attempt))


class _RetryableHttp(Exception):
    def __init__(self, status: int, retry_after: str | None = None):
        self.status = status
        self.retry_after = retry_after
