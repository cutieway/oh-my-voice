from __future__ import annotations

import ipaddress
import json
import math
import os
import time
import datetime as dt
import email.utils
from collections import Counter
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
    SPEECH_STYLE_SCHEMA_VERSION,
    STYLE_CLASSIFIER_VERSION,
    STYLE_EVIDENCE_POLICY_VERSION,
    STYLE_FIELDS,
    STYLE_PROMPT_VERSION,
    STYLE_STATUSES,
    STYLE_TAXONOMY_VERSION,
)
from .dialogue import (
    known_context_part,
    independent_context_count,
    independent_context_key,
    prompt_evidence_sufficiency,
    sample_id,
    sample_manifest_hash,
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
        if not question_id.startswith("speech_style."):
            raise ValueError(f"JEV may only answer Style questions, not {question_id!r}")
        field = question_id.split(".", 1)[1]
        if field not in STYLE_FIELDS:
            raise ValueError(f"JEV received a non-core Style question {question_id!r}")
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
            question["max_supporting"] = 6
            question["max_counterexamples"] = 3
            question["min_supporting"] = 3
            question["min_mixed_patterns"] = 2
            question["min_pattern_support"] = 3
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
    if not path.startswith("speech_style."):
        raise ValueError(f"JEV vocabulary paths must be Style-only: {path}")
    field = path.split(".", 1)[1]
    if field not in STYLE_FIELDS:
        raise ValueError(f"JEV vocabulary path is not a core Style field: {path}")
    return vocabulary["speech_style"]["dimensions"][field]["values"]


# What the model is told about its own evidence base. A representative sample
# remains a fraction of the corpus; saying so keeps a confident-sounding answer
# honest.
GUARDRAILS = (
    "Classify only the recurring written-dialogue style of one character.",
    "Do not infer race, gender, age, body, occupation, status, personality, or expected archetype.",
    "Dialogue text cannot establish pitch, timbre, resonance, accent, loudness, speed, pauses, or intonation.",
    "Judge recurring patterns across ordinary lines; a dramatic scene is not the default.",
    "Use the value `mixed` only when two or more patterns genuinely recur; list both patterns and their line IDs.",
    "Do not assign an evidence status. The application derives status from the submitted sample and your cited lines.",
    "Abstain with a null choice when the sample is too sparse.",
    "Cite only supplied line IDs and give a short rationale tied to those lines. Never invent an ID, quote, or source fact.",
    "Model self-reported confidence is diagnostic only and is not calibrated.",
)


def _coverage(statistics: dict, samples: list[dict]) -> dict:
    unique = int(statistics.get("unique_line_count") or statistics.get("dialogue_count") or 0)
    sampled = len(samples)
    sources = {
        source for sample in samples
        if (source := known_context_part(sample.get("source"))) is not None
    }
    contexts = independent_context_count(samples)
    return {
        "rows": statistics.get("dialogue_count"),
        "unique_lines": unique,
        "sampled_lines": sampled,
        "sampled_words": sum(len(str(sample.get("text") or "").split()) for sample in samples),
        "coverage_of_unique_lines": round(sampled / unique, 4) if unique else None,
        "sampled_source_count": len(sources),
        "sampled_context_count": contexts,
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
        # Kept locally for context-stratified trimming; `_state_without_samples`
        # strips source/sheet before anything is sent.
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
        "evidence_sufficiency": prompt_evidence_sufficiency(
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


_FORBIDDEN_STATE_KEYS = {
    "identity",
    "appearance",
    "voice_evidence",
    "voice_claims",
    "lore",
    "lore_quotes",
    "source",
    "sheet",
    "source_distribution",
    "canonical_id",
    "npc_id",
}


def _assert_identity_blind_state(value: Any, path: str = "state") -> None:
    """Reject identity, lore, and local provenance from a JEV request state."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _FORBIDDEN_STATE_KEYS:
                raise JevConfigurationError(
                    f"JEV state must not include {path}.{key}"
                )
            _assert_identity_blind_state(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_identity_blind_state(item, f"{path}[{index}]")


def _encode_size(state: dict) -> int:
    return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))


def _trim_length_bucket(sample: dict) -> str:
    return str(sample.get("length_bucket") or "unknown")


def _trim_context(sample: dict) -> str | None:
    return independent_context_key(sample)


def _trim_reduction(
    kept: list[dict],
    *,
    original_samples: int,
    original_lengths: set[str],
    original_contexts: set[str],
) -> dict:
    current_lengths = {_trim_length_bucket(sample) for sample in kept}
    current_contexts = {
        context for sample in kept
        if (context := _trim_context(sample)) is not None
    }
    original_sources = {
        context.split("::", 1)[0]
        for context in original_contexts
        if not context.startswith("*::")
    }
    current_sources = {
        context.split("::", 1)[0]
        for context in current_contexts
        if not context.startswith("*::")
    }
    return {
        "applied": len(kept) != original_samples,
        "original_samples": original_samples,
        "final_samples": len(kept),
        "reason": "max_state_bytes" if len(kept) != original_samples else None,
        "strategy": "balanced_length_and_context",
        "length_buckets_retained": sorted(current_lengths),
        "lost_length_buckets": sorted(original_lengths - current_lengths),
        "original_source_count": len(original_sources),
        "final_source_count": len(current_sources),
        "lost_source_count": len(original_sources - current_sources),
        "original_context_count": len(original_contexts),
        "final_context_count": len(current_contexts),
        "lost_context_count": len(original_contexts - current_contexts),
    }


def _trim_candidate_index(samples: list[dict]) -> int:
    """Pick the next sample to drop without collapsing a small stratum.

    Candidates that leave every length bucket and source/sheet context
    populated are preferred. Among those, the largest groups lose a sample
    first, so cycling through the strata keeps the sample balanced. The opaque
    ID is the final tie-break, preserving determinism.
    """
    length_counts = Counter(_trim_length_bucket(sample) for sample in samples)
    context_counts = Counter(
        context for sample in samples
        if (context := _trim_context(sample)) is not None
    )

    def key(index: int):
        sample = samples[index]
        length = _trim_length_bucket(sample)
        context = _trim_context(sample)
        lost = int(length_counts[length] == 1)
        if context is not None:
            lost += int(context_counts[context] == 1)
        return (
            lost,
            -length_counts[length],
            -context_counts.get(context, 0),
            sample["id"],
        )

    return min(range(len(samples)), key=key)


def _trim_to_budget(
    dossier: dict,
    samples: list[dict],
    *,
    max_bytes: int,
    include_measurements: bool,
    vocabulary: dict | None = None,
) -> tuple[list[dict], dict]:
    original = len(samples)
    # Stable order makes the trimmed result reproducible. Samples are removed
    # in a balanced order, not truncated from the ID-sorted tail.
    ordered = sorted(samples, key=lambda sample: sample["id"])
    kept = list(ordered)
    original_lengths = {_trim_length_bucket(sample) for sample in ordered}
    original_contexts = {
        context for sample in ordered
        if (context := _trim_context(sample)) is not None
    }
    while True:
        reduction = _trim_reduction(
            kept,
            original_samples=original,
            original_lengths=original_lengths,
            original_contexts=original_contexts,
        )
        state = _state_without_samples(
            dossier, kept, include_measurements=include_measurements,
            budget_reduction=reduction, vocabulary=vocabulary,
        )
        if _encode_size(state) <= max_bytes:
            return kept, reduction
        if not kept:
            raise ValueError("dossier state exceeds configured Jev request limit")
        kept.pop(_trim_candidate_index(kept))


def compact_state(
    dossier: dict,
    *,
    max_bytes: int = MAX_STATE_BYTES,
    include_measurements: bool = False,
    vocabulary: dict | None = None,
) -> dict:
    """Build the identity-blind state the model sees.

    The primary request contains only stable opaque IDs, dialogue text,
    aggregate numeric coverage/evidence metadata, and version tags. It omits
    identity, appearance, voice evidence, rules interpretations, regex signal
    labels, and source/sheet names; those names stay in application provenance.
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


def _patterns(value: Any, allowed: list[str], limit: int) -> list[dict]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise JevResponseError("patterns must be a list")
    patterns = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise JevResponseError(f"patterns[{index}] must be an object")
        label = item.get("label")
        if label not in allowed or label == "mixed":
            raise JevResponseError(f"patterns[{index}].label is invalid")
        ids = _line_id_list(
            item.get("line_ids"), f"patterns[{index}].line_ids", limit,
        )
        if not ids:
            raise JevResponseError(f"patterns[{index}].line_ids must not be empty")
        if seen_ids & set(ids):
            raise JevResponseError("a line ID is cited by more than one pattern")
        seen_ids.update(ids)
        patterns.append({"label": label, "line_ids": ids})
    return patterns


# --------------------------------------------------------------------------
# Evidence pass
#
# A local System One endpoint answers a value question with a distribution and
# nothing else: it returns no citations. The documented Style gate, however,
# requires cited lines before a field may be called supported. Rather than
# invent citations or waive the gate, the application runs a second, explicitly
# separate pass that asks the model to rank the exact supplied lines. Every
# citation recorded afterwards is model output; `evidence_source` says so, and
# nothing implies the value question produced it.
# --------------------------------------------------------------------------

EVIDENCE_PASS_VERSION = "jev-evidence-pass-1.0"
# Four questions over <=40 line options fit the default 8192-token local
# context; ten do not. Each question re-states the full option list.
EVIDENCE_QUESTIONS_PER_REQUEST = 4
EVIDENCE_SUPPORT_TOP_K = 3
EVIDENCE_COUNTER_TOP_K = 2
EVIDENCE_MIXED_PATTERN_LIMIT = 3
# A flat distribution over N options is 1/N. Anything below this floor is not
# a preference and must never become a citation.
EVIDENCE_MIN_PROBABILITY = 0.08

EVIDENCE_SOURCE = "model_line_choice"


def _evidence_instructions(field: str, value: str, kind: str) -> str:
    if kind == "support":
        return (
            f"Which supplied line most clearly exemplifies the value `{value}` "
            f"for the field {field}? Judge only the wording, not who might say it."
        )
    if kind == "counter":
        return (
            f"Which supplied line is most atypical of the value `{value}` for the "
            f"field {field} and would count as a counterexample? Judge only the wording."
        )
    return (
        f"Which supplied line belongs to the recurring pattern `{value}` for the "
        f"field {field}? Judge only the wording."
    )


def _mix_pattern_labels(question: dict, answer: dict) -> list[str]:
    """The concrete labels a mixed answer plausibly splits into."""
    probabilities = (answer.get("diagnostics") or {}).get("raw_probabilities") or {}
    labels = [
        label for label in question["criteria"]
        if label != "mixed" and label in probabilities
    ]
    labels.sort(key=lambda label: (-probabilities[label], label))
    return labels[:EVIDENCE_MIXED_PATTERN_LIMIT]


def evidence_question_specs(
    answers: dict[str, dict],
    questions: dict[str, dict],
    line_ids: list[str],
) -> dict[str, dict]:
    """Line-ranking questions for the fields that produced a concrete value.

    Returns request-ready question specs keyed by evidence question ID. An
    empty result means no field produced a value worth citing.
    """
    if not line_ids:
        return {}
    criteria = {line_id: None for line_id in line_ids}
    specs: dict[str, dict] = {}
    for field in STYLE_FIELDS:
        question = questions.get(f"speech_style.{field}")
        answer = answers.get(f"speech_style.{field}")
        if not question or not isinstance(answer, dict):
            continue
        value = answer.get("choice")
        if not value:
            continue
        if value == "mixed":
            for label in _mix_pattern_labels(question, answer):
                specs[f"evidence.{field}.pattern.{label}"] = {
                    "type": "choice",
                    "instructions": _evidence_instructions(field, label, "pattern"),
                    "criteria": dict(criteria),
                }
            continue
        for kind in ("support", "counter"):
            specs[f"evidence.{field}.{kind}"] = {
                "type": "choice",
                "instructions": _evidence_instructions(field, value, kind),
                "criteria": dict(criteria),
            }
    return specs


def _ranked_line_ids(answer: dict, limit: int) -> list[str]:
    """Highest-probability line IDs above the flat-distribution floor."""
    diagnostics = answer.get("diagnostics") or {}
    probabilities = diagnostics.get("raw_probabilities") or {}
    if probabilities:
        ranked = [
            line_id for line_id, probability in sorted(
                probabilities.items(), key=lambda item: (-item[1], item[0]),
            )
            if probability >= EVIDENCE_MIN_PROBABILITY
        ]
        return ranked[:limit]
    choice = answer.get("choice")
    return [choice] if isinstance(choice, str) and choice else []


def select_evidence(
    answers: dict[str, dict],
    questions: dict[str, dict],
    responses: dict[str, dict],
    *,
    failures: dict[str, str] | None = None,
) -> dict:
    """Turn the evidence answers into per-field citations.

    Nothing here decides a status. The citations are injected into the value
    answer and the existing evidence gate still decides whether they are
    sufficient, so a weak line ranking can only reject a field, never promote
    one.
    """
    fields: dict[str, dict] = {}
    diagnostics: dict[str, dict] = {}
    for field in STYLE_FIELDS:
        value_answer = answers.get(f"speech_style.{field}")
        question = questions.get(f"speech_style.{field}")
        if not isinstance(value_answer, dict) or not question:
            continue
        value = value_answer.get("choice")
        if not value:
            continue
        if value == "mixed":
            patterns = []
            claimed: set[str] = set()
            for label in _mix_pattern_labels(question, value_answer):
                answer = responses.get(f"evidence.{field}.pattern.{label}")
                if not isinstance(answer, dict):
                    continue
                line_ids = [
                    line_id for line_id in _ranked_line_ids(
                        answer, EVIDENCE_SUPPORT_TOP_K + len(claimed),
                    )
                    if line_id not in claimed
                ][:EVIDENCE_SUPPORT_TOP_K]
                diagnostics[f"evidence.{field}.pattern.{label}"] = _evidence_diagnostic(answer)
                if not line_ids:
                    continue
                claimed.update(line_ids)
                patterns.append({"label": label, "line_ids": line_ids})
            fields[field] = {
                "supporting_line_ids": [],
                "counterexample_line_ids": [],
                "patterns": patterns,
                "citation_balance": None,
            }
            continue
        support = responses.get(f"evidence.{field}.support")
        counter = responses.get(f"evidence.{field}.counter")
        support_ids = (
            _ranked_line_ids(support, EVIDENCE_SUPPORT_TOP_K)
            if isinstance(support, dict) else []
        )
        counter_ids = [
            line_id for line_id in (
                _ranked_line_ids(counter, EVIDENCE_COUNTER_TOP_K)
                if isinstance(counter, dict) else []
            )
            if line_id not in support_ids
        ]
        if isinstance(support, dict):
            diagnostics[f"evidence.{field}.support"] = _evidence_diagnostic(support)
        if isinstance(counter, dict):
            diagnostics[f"evidence.{field}.counter"] = _evidence_diagnostic(counter)
        total = len(support_ids) + len(counter_ids)
        fields[field] = {
            "supporting_line_ids": support_ids,
            "counterexample_line_ids": counter_ids,
            "patterns": [],
            "citation_balance": (
                round(len(support_ids) / total, 4) if total else None
            ),
        }
    return {
        "fields": fields,
        "diagnostics": diagnostics,
        "failures": dict(failures or {}),
    }


def _evidence_diagnostic(answer: dict) -> dict:
    """Model diagnostics for one evidence question; never an evidence status."""
    diagnostics = answer.get("diagnostics") or {}
    probabilities = diagnostics.get("raw_probabilities") or {}
    ranked = sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))[:5]
    return {
        "choice": answer.get("choice"),
        "top_lines": [
            {"line_id": line_id, "probability": probability}
            for line_id, probability in ranked
        ],
        "raw_confidence": diagnostics.get("raw_confidence"),
        "model_self_reported_confidence": diagnostics.get("model_self_reported_confidence"),
    }


def _evidence_chunks(question_ids: list[str], option_count: int) -> list[list[str]]:
    """Chunk evidence questions so one request fits the endpoint context.

    Every question restates the whole option list, so the cost is roughly
    questions x options. Measured on an 8192-token local context: four
    questions over 24 options fit, ten do not.
    """
    size = EVIDENCE_QUESTIONS_PER_REQUEST if option_count <= 24 else 2
    return [question_ids[index:index + size] for index in range(0, len(question_ids), size)]


def run_evidence_pass(
    provider: Any,
    state: dict,
    answers: dict[str, dict],
    questions: dict[str, dict],
    line_ids: list[str],
) -> dict:
    """Ask the model to rank the supplied lines for each proposed value.

    Only providers that accept an explicit request state can run this pass; the
    offline rules baseline cannot and reports no citations, which leaves the
    existing gate to reject its uncited values.
    """
    specs = evidence_question_specs(answers, questions, line_ids)
    result = {
        "version": EVIDENCE_PASS_VERSION,
        "source": EVIDENCE_SOURCE,
        "questions": specs,
        "fields": {},
        "diagnostics": {},
        "failures": {},
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    if not specs:
        return result
    if not hasattr(provider, "classify_state"):
        result["failures"] = {
            question_id: "provider cannot run an explicit-state evidence pass"
            for question_id in specs
        }
        return result

    responses: dict[str, dict] = {}

    def ask(chunk: list[str]) -> None:
        chunk_questions = {question_id: specs[question_id] for question_id in chunk}
        try:
            response = provider.classify_state(state, chunk_questions)
        except JevError as exc:
            # A context or body limit is per-request, not per-question: split
            # rather than lose every citation at once.
            if len(chunk) > 1:
                middle = len(chunk) // 2
                ask(chunk[:middle])
                ask(chunk[middle:])
                return
            result["failures"][chunk[0]] = str(exc)
            return
        responses.update(response.get("answers") or {})
        usage = response.get("usage") or {}
        for key in ("input_tokens", "output_tokens"):
            result["usage"][key] += int(usage.get(key) or 0)
        result["model"] = response.get("model")
        result["model_revision"] = response.get("model_revision")

    for chunk in _evidence_chunks(list(specs), len(line_ids)):
        ask(chunk)

    selected = select_evidence(
        answers, questions, responses, failures=result["failures"],
    )
    result["fields"] = selected["fields"]
    result["diagnostics"] = selected["diagnostics"]
    return result


def apply_evidence(answers: dict[str, dict], evidence: dict) -> dict[str, dict]:
    """Copy value answers and attach the separately elicited citations."""
    applied = {}
    for question_id, answer in answers.items():
        if question_id.startswith("speech_style."):
            field = question_id.split(".", 1)[1]
            citations = (evidence.get("fields") or {}).get(field)
            if citations and answer.get("choice"):
                answer = dict(answer)
                answer["supporting_line_ids"] = citations["supporting_line_ids"]
                answer["counterexample_line_ids"] = citations["counterexample_line_ids"]
                answer["patterns"] = citations["patterns"]
                diagnostics = dict(answer.get("diagnostics") or {})
                diagnostics["evidence_source"] = evidence.get("source")
                diagnostics["evidence_pass_version"] = evidence.get("version")
                diagnostics["evidence_questions"] = {
                    key: value for key, value in (evidence.get("diagnostics") or {}).items()
                    if key.split(".")[1:2] == [field]
                }
                failures = {
                    key: value for key, value in (evidence.get("failures") or {}).items()
                    if key.split(".")[1:2] == [field]
                }
                if failures:
                    diagnostics["evidence_failures"] = failures
                answer["diagnostics"] = diagnostics
        applied[question_id] = answer
    return applied


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
            # JEV status is diagnostic input only; it never authorizes a
            # decision. Keep it solely so a malformed model status can be
            # audited without entering the decision path.
            reported_status = answer.get("status")
            if reported_status is not None:
                if reported_status not in STYLE_STATUSES:
                    raise JevResponseError(f"{question_id} has an invalid status")
            if choice is not None and choice not in allowed:
                raise JevResponseError(f"{question_id} selected an invalid option")
            probabilities = None
            if answer.get("probabilities") not in (None, {}):
                probabilities = _check_distribution(
                    answer.get("probabilities"), allowed, question_id
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
                int(question.get("max_supporting", 6)),
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
            patterns = _patterns(
                answer.get("patterns"), allowed, int(question.get("max_supporting", 6)),
            )
            pattern_ids = {
                line_id for pattern in patterns for line_id in pattern["line_ids"]
            }
            if pattern_ids & set(supporting):
                raise JevResponseError(
                    f"{question_id} repeats a pattern line in supporting_line_ids"
                )
            if pattern_ids & set(counterexamples):
                raise JevResponseError(
                    f"{question_id} repeats a pattern line in counterexample_line_ids"
                )
            if choice != "mixed" and patterns:
                raise JevResponseError(
                    f"{question_id} patterns are only valid for a mixed choice"
                )
            rationale = answer.get("rationale")
            if rationale is not None and not isinstance(rationale, str):
                raise JevResponseError(f"{question_id}.rationale must be a string")
            answers[question_id] = {
                "type": expected_type,
                "choice": choice,
                "model_reported_status": reported_status,
                "supporting_line_ids": supporting,
                "counterexample_line_ids": counterexamples,
                "patterns": patterns,
                "rationale": rationale,
                "diagnostics": {
                    "raw_probabilities": probabilities,
                    "raw_confidence": confidence,
                    "model_self_reported_confidence": diagnostic,
                },
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
                "diagnostics": {
                    "raw_probabilities": probabilities,
                    "raw_confidence": confidence,
                    "model_self_reported_confidence": None,
                },
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
    revision = payload.get("model_revision") or payload.get("model_build")
    return {
        "model": payload["model"],
        "model_revision": str(revision) if revision is not None else None,
        "answers": answers,
        "usage": normalized_usage,
    }


def endpoint_identity(endpoint: str) -> str:
    """Stable, credential-free endpoint identity for cache fingerprints."""
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise JevConfigurationError(f"invalid JEV endpoint: {endpoint}") from exc
    if not parsed.scheme or not host:
        raise JevConfigurationError(f"invalid JEV endpoint: {endpoint}")
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme.casefold()}://{authority}{parsed.path or '/'}"


def is_loopback_endpoint(endpoint: str) -> bool:
    try:
        host = (urlsplit(endpoint).hostname or "").casefold()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def request_fingerprint(
    *,
    provider: str,
    endpoint: str | None,
    model: str,
    state: dict | None,
    questions: dict[str, dict],
    vocabulary: dict,
    resolved_model_revision: str | None = None,
    guardrails: tuple[str, ...] = GUARDRAILS,
) -> str:
    """Hash the exact request contract, excluding credentials."""
    return object_hash({
        "provider": provider,
        "endpoint": endpoint_identity(endpoint) if endpoint else None,
        "model_requested": model,
        "model_revision": resolved_model_revision,
        "state_hash": object_hash(state) if state is not None else None,
        "questions_hash": object_hash(questions),
        "guardrails_hash": object_hash(list(guardrails)),
        "taxonomy_version": STYLE_TAXONOMY_VERSION,
        "classifier_version": STYLE_CLASSIFIER_VERSION,
        "prompt_version": STYLE_PROMPT_VERSION,
        "schema_version": SPEECH_STYLE_SCHEMA_VERSION,
        "sampler_version": DIALOGUE_SAMPLER_VERSION,
        "evidence_policy_version": STYLE_EVIDENCE_POLICY_VERSION,
        "vocabulary_hash": object_hash(vocabulary),
    })


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
        allow_remote: bool = False,
    ):
        self.vocabulary = vocabulary
        self.endpoint = endpoint
        self.endpoint_identity = endpoint_identity(endpoint)
        self.local_endpoint = is_loopback_endpoint(endpoint)
        self.allow_remote = bool(allow_remote)
        self.model = model
        self.model_id = model
        self.resolved_model_revision: str | None = None
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or urlopen
        self.sleeper = sleeper

    @property
    def endpoint_class(self) -> str:
        return "local" if self.local_endpoint else "remote"

    def describe(self) -> dict:
        return {
            "endpoint": self.endpoint_identity,
            "endpoint_class": self.endpoint_class,
            "model_requested": self.model,
            "model_revision": self.resolved_model_revision,
        }

    def classify(self, dossier: dict, questions: dict[str, dict]) -> dict:
        return self.classify_state(
            compact_state(dossier, vocabulary=self.vocabulary), questions,
        )

    def classify_state(self, state: dict, questions: dict[str, dict]) -> dict:
        _assert_identity_blind_state(state)
        if not self.local_endpoint and not self.allow_remote:
            raise JevConfigurationError(
                "Remote JEV endpoints are disabled by default. Use a loopback "
                "endpoint or opt in explicitly with --allow-remote."
            )
        api_key = os.environ.get(self.api_key_env)
        if not api_key and not self.local_endpoint:
            raise JevConfigurationError(
                f"Jev API key missing; set {self.api_key_env} in the environment "
                "(a loopback endpoint such as http://127.0.0.1:8081/v1/systemone needs none)"
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
                normalized = validate_response(decoded, questions)
                self.resolved_model_revision = normalized.get("model_revision")
                return normalized
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
