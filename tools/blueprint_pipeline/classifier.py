from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import Any

from . import CLASSIFIER_CONFIG_VERSION
from .cache import object_hash
from .config import (
    DEFAULT_THRESHOLDS,
    DIALOGUE_SAMPLER_VERSION,
    SPEECH_STYLE_SCHEMA_VERSION,
    STYLE_CLASSIFIER_VERSION,
    STYLE_EVIDENCE_POLICY_VERSION,
    STYLE_FIELDS,
    STYLE_PROMPT_VERSION,
    STYLE_TAXONOMY_VERSION,
)
from .dialogue import (
    evidence_sufficiency,
    independent_context_key,
    sample_id,
    sample_manifest_hash,
)
from .jev import (
    GUARDRAILS,
    build_questions,
    compact_state,
    request_fingerprint,
)


class ClassifierProvider(ABC):
    name = "base"
    model_id = "unknown"

    @abstractmethod
    def classify(self, dossier: dict, questions: dict[str, dict]) -> dict:
        raise NotImplementedError





class RulesProvider(ClassifierProvider):
    """Explicitly selected deterministic baseline. It never acts as a fallback."""

    name = "rules"
    model_id = "rules-3.0"

    def __init__(self, vocabulary: dict):
        self.vocabulary = vocabulary

    def classify(self, dossier: dict, questions: dict[str, dict]) -> dict:
        analysis = (dossier.get("dialogue_analysis") or {}).get("interpretation") or {}
        answers = {}
        for question_id, question in questions.items():
            if question["type"] != "choice" or not question_id.startswith("speech_style."):
                raise ValueError(f"rules baseline does not answer {question_id}")
            field = question_id.split(".", 1)[1]
            value = analysis.get(field)
            criteria = question["criteria"]
            if value not in criteria:
                value = None
            answers[question_id] = {
                "type": "choice",
                "choice": value,
                "supporting_line_ids": [],
                "counterexample_line_ids": [],
                "rationale": (
                    f"rules baseline selected {value} from deterministic dialogue measurements"
                    if value is not None
                    else "rules baseline produced no supported label"
                ),
            }
        return {
            "model": self.model_id,
            "answers": answers,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }


def utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _has_verified_voice_evidence(dossier: dict) -> bool:
    evidence = dossier.get("voice_evidence")
    if isinstance(evidence, dict):
        return bool(evidence.get("verified"))
    if isinstance(evidence, list):
        return any(isinstance(item, dict) and item.get("verified") for item in evidence)
    return False


def _identity_gate(dossier: dict) -> str | None:
    identity = dossier.get("identity") or {}
    if identity.get("resolution_status") != "eligible":
        return "identity failed the conservative synthetic-candidate gate"
    if identity.get("npc_id") is None:
        return "identity has no canonical game NPC ID"
    if (
        identity.get("has_recorded_voice")
        or identity.get("voice_evidence_verified")
        or _has_verified_voice_evidence(dossier)
    ):
        return "identity has verified recorded voice evidence; use the official audio path"
    if identity.get("has_se_voice_reference"):
        return "identity has an unverified SE voice reference; verify the speaker-to-audio relationship before synthetic design"
    if identity.get("identity_ambiguous"):
        return "identity is ambiguous"
    return None


def _decision_for_field(
    *,
    field: str,
    answer: dict,
    question: dict,
    provider: ClassifierProvider,
    sample_ids: set[str],
    sample_contexts: dict[str, str | None],
    tier: dict,
    analysis: dict,
    model_id: str,
) -> dict:
    value = answer.get("choice")
    method = "rules_baseline" if provider.name == "rules" else "jev"
    diagnostics = answer.get("diagnostics") or {}
    base = {
        "method": method,
        "provider": provider.name,
        "model_id": model_id,
        "evidence_tier": tier["tier"],
        "evidence": tier,
        "model_reported_status": answer.get("model_reported_status") or answer.get("status"),
        "diagnostics": diagnostics,
    }
    if tier["tier"] == "insufficient":
        return {
            **base,
            "value": None,
            "status": "insufficient_evidence",
            "supporting_line_ids": [],
            "counterexample_line_ids": [],
            "patterns": [],
            "citation_balance": None,
            "rationale": "The sample is below the minimum evidence floor.",
            "rules_baseline_estimate": (
                analysis.get(field) if method == "rules_baseline" else None
            ),
        }
    if value is not None and value not in question["criteria"]:
        return {
            **base,
            "value": None,
            "status": "rejected",
            "supporting_line_ids": [],
            "counterexample_line_ids": [],
            "patterns": [],
            "citation_balance": None,
            "rationale": f"Classifier returned an unsupported label {value!r}.",
        }

    # RulesProvider is a deterministic offline baseline. It supplies no JEV
    # citations; its status still comes from the same request-visible tier.
    if method == "rules_baseline":
        if value is None:
            return {
                **base,
                "value": None,
                "status": "unresolved",
                "supporting_line_ids": [],
                "counterexample_line_ids": [],
                "patterns": [],
                "citation_balance": None,
                "rationale": "Rules baseline produced no supported label.",
            }
        return {
            **base,
            "value": value,
            "status": "provisional" if tier["tier"] == "provisional" else "supported",
            "supporting_line_ids": [],
            "counterexample_line_ids": [],
            "patterns": [],
            "citation_balance": None,
            "rationale": answer.get("rationale") or "",
        }

    supporting = list(answer.get("supporting_line_ids") or [])
    counterexamples = list(answer.get("counterexample_line_ids") or [])
    patterns = list(answer.get("patterns") or [])
    pattern_ids = [
        line_id for pattern in patterns for line_id in pattern.get("line_ids") or []
    ]
    cited = supporting + counterexamples + pattern_ids
    invalid = [line_id for line_id in cited if line_id not in sample_ids]
    if invalid:
        return {
            **base,
            "value": None,
            "status": "rejected",
            "supporting_line_ids": supporting,
            "counterexample_line_ids": counterexamples,
            "patterns": patterns,
            "citation_balance": None,
            "rationale": f"Classifier cited line IDs absent from the request: {invalid[:3]}.",
        }
    if value is None:
        return {
            **base,
            "value": None,
            "status": "unresolved",
            "supporting_line_ids": supporting,
            "counterexample_line_ids": counterexamples,
            "patterns": patterns,
            "citation_balance": None,
            "rationale": answer.get("rationale") or "Classifier abstained.",
        }

    minimum = int(question.get("min_supporting", 3))
    if value == "mixed":
        minimum_patterns = int(question.get("min_mixed_patterns", 2))
        minimum_pattern_support = int(question.get("min_pattern_support", 3))
        allowed_pattern_labels = set(question["criteria"]) - {"mixed"}
        if len(patterns) < minimum_patterns:
            reason = "A mixed value requires at least two recurring patterns."
        elif any(pattern.get("label") not in allowed_pattern_labels for pattern in patterns):
            reason = "A mixed pattern label is not valid for this field."
        elif len(pattern_ids) != len(set(pattern_ids)):
            reason = "A line ID cannot be cited by more than one mixed pattern."
        elif any(len(pattern.get("line_ids") or []) < minimum_pattern_support for pattern in patterns):
            reason = f"Each mixed pattern requires at least {minimum_pattern_support} cited lines."
        elif set(pattern_ids) & set(counterexamples):
            reason = "A mixed pattern line cannot also be a counterexample."
        elif set(pattern_ids) & set(supporting):
            reason = "A mixed pattern line cannot also be a supporting line."
        else:
            context_keys = {
                sample_contexts.get(line_id) for line_id in pattern_ids
            }
            context_keys.discard(None)
            if len(context_keys) < 2:
                reason = "A mixed value requires its cited pattern lines to span at least two independent contexts."
            else:
                reason = None
        if reason:
            return {
                **base,
                "value": None,
                "status": "rejected",
                "supporting_line_ids": pattern_ids,
                "counterexample_line_ids": counterexamples,
                "patterns": patterns,
                "citation_balance": None,
                "rationale": reason,
            }
        return {
            **base,
            "value": "mixed",
            "status": "provisional" if tier["tier"] == "provisional" else "supported",
            "supporting_line_ids": pattern_ids,
            "counterexample_line_ids": counterexamples,
            "patterns": patterns,
            "citation_balance": None,
            "rationale": answer.get("rationale") or "",
        }

    if len(supporting) < minimum:
        return {
            **base,
            "value": None,
            "status": "rejected",
            "supporting_line_ids": supporting,
            "counterexample_line_ids": counterexamples,
            "patterns": [],
            "citation_balance": None,
            "rationale": f"Supported Style fields require at least {minimum} cited lines.",
        }
    total_citations = len(supporting) + len(counterexamples)
    citation_balance = (
        round(len(supporting) / total_citations, 4) if total_citations else None
    )
    return {
        **base,
        "value": value,
        "status": "provisional" if tier["tier"] == "provisional" else "supported",
        "supporting_line_ids": supporting,
        "counterexample_line_ids": counterexamples,
        "patterns": [],
        "citation_balance": citation_balance,
        "rationale": answer.get("rationale") or "",
    }


def _profile_allocation(
    dossier: dict,
    decisions: dict[str, dict],
    policy: dict,
) -> dict:
    identity = dossier.get("identity") or {}
    statistics = dossier.get("statistics") or {}
    lines = int(statistics.get("unique_line_count") or statistics.get("dialogue_count") or 0)
    supported = [
        field for field, decision in decisions.items()
        if decision.get("status") == "supported" and decision.get("value") is not None
    ]
    provisional = [
        field for field, decision in decisions.items()
        if decision.get("status") == "provisional" and decision.get("value") is not None
    ]
    voice_route = (
        "official_audio"
        if identity.get("has_recorded_voice")
        or identity.get("voice_evidence_verified")
        or _has_verified_voice_evidence(dossier)
        else "synthetic_candidate"
    )
    minimum = int(policy.get("min_dialogue_lines_for_individual", 50))
    minimum_fields = int(policy.get("min_style_fields_for_individual", 2))
    if voice_route == "official_audio":
        decision = "official_audio"
        reason = "Official voice/audio linkage takes precedence over synthetic design."
    elif lines < minimum:
        decision = "shared_archetype"
        reason = f"Dialogue volume {lines} is below the individual-design floor {minimum}."
    elif len(supported) < minimum_fields:
        decision = "shared_archetype"
        reason = (
            f"Only {len(supported)} supported Style field(s); at least {minimum_fields} "
            "are required for an individual design."
        )
    else:
        decision = "individual_design"
        reason = (
            f"{len(supported)} supported Style fields and {lines} dialogue lines justify "
            "an individual synthetic design."
        )
    return {
        "decision": decision,
        "reason": reason,
        "voice_route": voice_route,
        "dialogue_lines": lines,
        "supported_style_fields": sorted(supported),
        "provisional_style_fields": sorted(provisional),
        "minimum_dialogue_lines": minimum,
        "minimum_style_fields": minimum_fields,
    }


def classify_dossier(
    dossier: dict,
    provider: ClassifierProvider,
    vocabulary: dict,
    *,
    model_id: str | None = None,
    thresholds: dict | None = None,
    request_state: dict | None = None,
) -> dict:
    policy = dict(DEFAULT_THRESHOLDS)
    policy.update(vocabulary.get("classification", {}).get("thresholds", {}))
    if thresholds:
        policy.update(thresholds)
    identity = dossier.get("identity") or {}
    canonical_id = str(identity.get("canonical_id") or "")
    gate_reason = _identity_gate(dossier)
    if gate_reason:
        return {
            "schema_version": "2.0",
            "canonical_id": canonical_id,
            "npc_id": identity.get("npc_id"),
            "name": identity.get("name"),
            "dossier_hash": dossier.get("input_hash"),
            "provider": provider.name,
            "model_id": model_id or provider.model_id,
            "status": "needs_review",
            "assignment": None,
            "profile_allocation": {
                "decision": (
                    "official_audio"
                    if (
                        identity.get("has_recorded_voice")
                        or identity.get("voice_evidence_verified")
                        or _has_verified_voice_evidence(dossier)
                    )
                    else "shared_archetype"
                ),
                "reason": gate_reason,
                "voice_route": (
                    "official_audio"
                    if (
                        identity.get("has_recorded_voice")
                        or identity.get("voice_evidence_verified")
                        or _has_verified_voice_evidence(dossier)
                    )
                    else "synthetic_candidate"
                ),
            },
            "speech_style": {},
            "speech_style_analysis": None,
            "review_reasons": list(identity.get("review_reasons") or []) + [gate_reason],
            "decisions": {},
        }

    questions = build_questions(vocabulary)
    statistics = dossier.get("statistics") or {}
    samples = dossier.get("dialogue_samples") or []
    sent_state = None
    if request_state is not None or provider.name == "jev":
        # One prepared state is used for sending, citations, evidence,
        # manifest and the cache fingerprint. The caller may pass the exact
        # state it already hashed even for the offline baseline.
        sent_state = request_state or compact_state(dossier, vocabulary=vocabulary)
        sent_ids = {
            str(sample.get("id"))
            for sample in sent_state.get("dialogue_samples") or []
            if sample.get("id")
        }
        samples = [
            sample for sample in samples
            if str(sample.get("id") or sample_id(canonical_id, sample)) in sent_ids
        ]
    sample_ids = {
        str(sample.get("id") or sample_id(canonical_id, sample))
        for sample in samples
    }
    sample_contexts = {
        str(sample.get("id") or sample_id(canonical_id, sample)): independent_context_key(sample)
        for sample in samples
    }
    analysis = (dossier.get("dialogue_analysis") or {}).get("interpretation") or {}
    if sent_state is not None and hasattr(provider, "classify_state"):
        response = provider.classify_state(sent_state, questions)
    else:
        response = provider.classify(dossier, questions)
    answers = dict(response.get("answers") or {})
    resolved_model_id = response.get("model") or model_id or provider.model_id
    resolved_model_revision = response.get("model_revision")

    decisions: dict[str, dict] = {}
    decision_checks: dict[str, dict] = {}
    field_tiers: dict[str, dict] = {}
    for field in STYLE_FIELDS:
        question_id = f"speech_style.{field}"
        question = questions[question_id]
        tier = evidence_sufficiency(
            statistics, samples, vocabulary=vocabulary, field=field,
        )
        field_tiers[field] = tier
        answer = answers.get(question_id)
        if not isinstance(answer, dict):
            decisions[field] = {
                "value": None,
                "status": "rejected",
                "method": "rules_baseline" if provider.name == "rules" else "jev",
                "provider": provider.name,
                "model_id": resolved_model_id,
                "evidence_tier": tier["tier"],
                "evidence": tier,
                "supporting_line_ids": [],
                "counterexample_line_ids": [],
                "patterns": [],
                "citation_balance": None,
                "rationale": "Classifier response omitted this field.",
                "diagnostics": {},
                "model_reported_status": None,
            }
        else:
            decisions[field] = _decision_for_field(
                field=field,
                answer=answer,
                question=question,
                provider=provider,
                sample_ids=sample_ids,
                sample_contexts=sample_contexts,
                tier=tier,
                analysis=analysis,
                model_id=resolved_model_id,
            )
        decision = decisions[field]
        decision_checks[field] = {
            "accepted": decision["status"] == "supported",
            "status": decision["status"],
            "evidence_tier": decision["evidence_tier"],
        }

    usage = {
        "input_tokens": int((response.get("usage") or {}).get("input_tokens") or 0),
        "output_tokens": int((response.get("usage") or {}).get("output_tokens") or 0),
    }
    allocation = _profile_allocation(dossier, decisions, policy)
    supported_fields = allocation["supported_style_fields"]
    rejected_fields = [
        field for field, decision in decisions.items() if decision["status"] == "rejected"
    ]
    provisional_fields = allocation["provisional_style_fields"]

    review_reasons = []
    if not supported_fields:
        review_reasons.append("no Style field met the supported evidence tier")
    if provisional_fields:
        review_reasons.append(
            "provisional Style fields are stored but not rendered: "
            + ", ".join(provisional_fields)
        )
    if rejected_fields:
        review_reasons.append(
            "Style fields rejected for invalid or missing evidence: "
            + ", ".join(sorted(rejected_fields))
        )
    if allocation["decision"] == "shared_archetype":
        review_reasons.append(allocation["reason"])
    if allocation["decision"] == "official_audio":
        review_reasons.append(allocation["reason"])
    if allocation["dialogue_lines"] < int(policy["min_dialogue_lines_for_review"]):
        review_reasons.append("very little dialogue is available")

    if not supported_fields or rejected_fields:
        status = "needs_review"
    else:
        status = "ready_for_audition"

    manifest = dossier.get("sample_manifest") or {}
    measurements = (dossier.get("dialogue_analysis") or {}).get("measurements") or {}
    prepared_manifest_hash = (
        (sent_state or {}).get("sample_manifest_hash")
        if sent_state is not None
        else sample_manifest_hash(
            canonical_id, samples, corpus_hash=manifest.get("corpus_hash"),
        )
    )
    provider_description = (
        provider.describe() if hasattr(provider, "describe") else {}
    )
    request_key = request_fingerprint(
        provider=provider.name,
        endpoint=provider_description.get("endpoint"),
        model=model_id or provider.model_id,
        state=sent_state,
        questions=questions,
        vocabulary=vocabulary,
        guardrails=GUARDRAILS,
    )
    response_key = request_fingerprint(
        provider=provider.name,
        endpoint=provider_description.get("endpoint"),
        model=model_id or provider.model_id,
        state=sent_state,
        questions=questions,
        vocabulary=vocabulary,
        resolved_model_revision=resolved_model_revision,
        guardrails=GUARDRAILS,
    )
    analysis_record = {
        "schema_version": SPEECH_STYLE_SCHEMA_VERSION,
        "method": provider.name,
        "provider": provider.name,
        "model_id": resolved_model_id,
        "model_revision": resolved_model_revision,
        "endpoint": provider_description.get("endpoint"),
        "endpoint_class": provider_description.get("endpoint_class"),
        "request_fingerprint": request_key,
        "response_fingerprint": response_key,
        "taxonomy_version": STYLE_TAXONOMY_VERSION,
        "classifier_version": STYLE_CLASSIFIER_VERSION,
        "prompt_version": STYLE_PROMPT_VERSION,
        "schema_version_contract": SPEECH_STYLE_SCHEMA_VERSION,
        "sampler_version": DIALOGUE_SAMPLER_VERSION,
        "evidence_policy_version": STYLE_EVIDENCE_POLICY_VERSION,
        "corpus_hash": dossier.get("input_hash"),
        "sample_manifest_hash": prepared_manifest_hash,
        "budget_reduction": (
            (sent_state or {}).get("budget_reduction")
        ),
        "evidence": {
            "raw_lines": measurements.get("raw_line_count") or statistics.get("dialogue_count"),
            "unique_usable_lines": statistics.get("unique_line_count"),
            "unique_words": measurements.get("word_count"),
            "sampled_lines": len(samples),
            "sampled_words": sum(len(str(sample.get("text") or "").split()) for sample in samples),
            "sources_covered": len({
                key.split("::", 1)[0]
                for sample in samples
                if (key := independent_context_key(sample)) is not None
                and not key.startswith("*::")
            }),
            "contexts_covered": len({
                key for sample in samples
                if (key := independent_context_key(sample)) is not None
            }),
            "evidence_tier": evidence_sufficiency(
                statistics, samples, vocabulary=vocabulary,
            )["tier"],
            "field_tiers": {
                field: tier["tier"] for field, tier in field_tiers.items()
            },
        },
        "dimensions": decisions,
        "experimental": {
            "speech_habits": {
                "value": analysis.get("speech_habits") or [],
                "method": "rules_baseline_evidence_collection",
                "note": "Not a production Style field; retained for habit verification only.",
            },
            "lexical_features": {
                "taxonomy": analysis.get("lexical_features") or {},
                "contraction_frequency": (
                    "high" if float(measurements.get("contraction_rate_per_word") or 0) >= 0.055
                    else "moderate" if float(measurements.get("contraction_rate_per_word") or 0) >= 0.02
                    else "low"
                ),
                "contraction_rate_per_word": measurements.get("contraction_rate_per_word"),
                "average_word_length": measurements.get("average_word_length"),
                "vocabulary_diversity": measurements.get("vocabulary_diversity"),
                "honorific_and_form_of_address_count": measurements.get("honorific_and_form_of_address_count"),
                "question_count": measurements.get("question_count"),
                "exclamation_count": measurements.get("exclamation_count"),
                "method": "deterministic_text_counts",
                "note": "Audit-only. Not a production Style field and never rendered into the voice instruction.",
            },
            "verbal_habits": {
                "value": analysis.get("verbal_habits") or analysis.get("speech_habits") or [],
                "method": "deterministic_text_counts",
                "note": "Candidate textual mannerisms; a human must confirm they are recurring and character-specific.",
            },
        },
        "validated": False,
        "validation_note": "Experimental until human gold benchmark and VoiceDesign A/B gates pass.",
    }
    configuration = {
        "config_version": CLASSIFIER_CONFIG_VERSION,
        "thresholds": policy,
        "question_count": len(questions),
        "model_requested": model_id or provider.model_id,
        "model_revision": resolved_model_revision,
        "endpoint": provider_description.get("endpoint"),
        "endpoint_class": provider_description.get("endpoint_class"),
        "request_fingerprint": request_key,
        "state_hash": object_hash(sent_state) if sent_state is not None else None,
        "questions_hash": object_hash(questions),
        "guardrails_hash": object_hash(list(GUARDRAILS)),
        "vocabulary_hash": object_hash(vocabulary),
        "taxonomy_version": STYLE_TAXONOMY_VERSION,
        "classifier_version": STYLE_CLASSIFIER_VERSION,
        "prompt_version": STYLE_PROMPT_VERSION,
        "schema_version_contract": SPEECH_STYLE_SCHEMA_VERSION,
        "sampler_version": DIALOGUE_SAMPLER_VERSION,
        "evidence_policy_version": STYLE_EVIDENCE_POLICY_VERSION,
    }
    return {
        "schema_version": "2.0",
        "canonical_id": canonical_id,
        "npc_id": identity.get("npc_id"),
        "name": identity.get("name"),
        "speaker_ids": identity.get("speaker_ids", []),
        "aliases": identity.get("aliases", []),
        "dossier_hash": dossier.get("input_hash"),
        "configuration_hash": object_hash(configuration),
        "configuration": configuration,
        "provider": provider.name,
        "model_id": resolved_model_id,
        "timestamp": utc_timestamp(),
        "usage": usage,
        "questions": questions,
        "answers": answers,
        "decision_checks": decision_checks,
        "distinctive": allocation["decision"] == "individual_design",
        "profile_allocation": allocation,
        "speech_style": decisions,
        "speech_style_analysis": analysis_record,
        "voice_claims": [
            {
                "field": evidence.get("field"),
                "source": evidence.get("source"),
                "revision_id": evidence.get("revision_id"),
                "quote": evidence.get("quote"),
                "evidence_level": evidence.get("level", "explicit"),
            }
            for evidence in dossier.get("voice_evidence") or []
        ],
        "status": status,
        "review_reasons": sorted(set(review_reasons)),
        "human_approval": None,
    }
