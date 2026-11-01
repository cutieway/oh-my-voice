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

from .config import (
    DEFAULT_JEV_ENDPOINT,
    DEFAULT_JEV_KEY_ENV,
    DEFAULT_JEV_MODEL,
    DEFAULT_USER_AGENT,
    HTTP_TIMEOUT_SECONDS,
    MAX_QUESTION_COUNT,
    MAX_REQUEST_BYTES,
    MAX_RETRIES,
    MAX_STATE_BYTES,
)


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


def compact_state(dossier: dict, *, max_bytes: int = MAX_STATE_BYTES) -> dict:
    state = {
        "identity": {
            key: (dossier.get("identity") or {}).get(key)
            for key in (
                "canonical_id", "npc_id", "name", "aliases", "race", "clan",
                "sex", "occupation", "faction", "location", "resolution_status",
                "has_se_voice_reference", "identity_ambiguous",
            )
        },
        "statistics": dossier.get("statistics") or {},
        "dialogue_analysis": dossier.get("dialogue_analysis") or {},
        "dialogue_samples": [
            {
                "text": sample.get("text", ""),
                "source": sample.get("source"),
                "sheet": sample.get("sheet"),
            }
            for sample in dossier.get("dialogue_samples") or []
        ],
        "lore_evidence": dossier.get("lore_evidence") or [],
        "voice_evidence": dossier.get("voice_evidence") or [],
        "wiki_resolution": dossier.get("wiki_resolution") or {},
        "fact_conflicts": dossier.get("fact_conflicts") or [],
        "classification_guardrails": [
            "This is synthetic casting; there is no original recording to discover.",
            "Race, clan, sex, body, occupation, and personality are not acoustic evidence.",
            "Dialogue text can support speech style, but cannot establish pitch or timbre.",
            "Never invent source facts or citations.",
        ],
    }
    while len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > max_bytes:
        samples = state["dialogue_samples"]
        if not samples:
            raise ValueError("dossier state exceeds configured Jev request limit")
        samples.pop()
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
            if choice not in allowed:
                raise JevResponseError(f"{question_id} selected an invalid option")
            probabilities = _check_distribution(
                answer.get("probabilities"), allowed, question_id
            )
            confidence = _finite_probability(answer.get("confidence"), f"{question_id}.confidence")
            if probabilities[choice] + 1e-9 < max(probabilities.values()):
                raise JevResponseError(f"{question_id} choice is not the most probable option")
            answers[question_id] = {
                "type": expected_type,
                "choice": choice,
                "probabilities": probabilities,
                "confidence": confidence,
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
    ):
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
        api_key = os.environ.get(self.api_key_env)
        if not api_key and not self.local_endpoint:
            raise JevConfigurationError(
                f"Jev API key missing; set {self.api_key_env} in the environment "
                "(a loopback endpoint such as http://127.0.0.1:18081/v1/systemone needs none)"
            )
        state = compact_state(dossier)
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
