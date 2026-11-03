from __future__ import annotations

from typing import Any

try:
    import blueprint as blueprint_renderer
except ModuleNotFoundError:
    from tools import blueprint as blueprint_renderer

from .cache import object_hash
from .casting import RULES_VERSION
from .evidence import creative_evidence, make_evidence


# Only fields with a validation story may reach a profile. `diction_style` and
# `contraction_frequency` failed the benchmark and stay out until they pass.
VALIDATED_STYLE_FIELDS = (
    "register",
    "lexical_complexity",
    "sentence_complexity",
    "colloquialism",
    "discourse_style",
)

IDENTITY_FIELDS = (
    "presentation",
    "perceived_age",
    "perceived_vocal_size",
    "vocal_weight",
    "pitch",
    "pitch_range",
    "timbre_primary",
    "timbre_secondary",
    "timbre_preferences",
    "resonance",
    "articulation",
    "speaking_pace",
    "intonation",
    "distinctive_traits",
)

PROVENANCE_KEYS = (
    "rule",
    "evidence_type",
    "rule_version",
    "prior_score",
    "casting_strength",
    "facts",
    "factors",
    "rationale",
    "sources",
    "source",
    "quote",
    "note",
)

_EVIDENCE_LEVEL = {
    "game_fact": "explicit",
    "lore_fact": "supported_inference",
    "explicit_audio": "explicit",
    "creative_selection": "creative_selection",
    "default": "default",
    "unknown": "unknown",
}


def _evidence_level(decision: dict) -> str:
    level = decision.get("level")
    if level:
        return str(level)
    return _EVIDENCE_LEVEL.get(str(decision.get("evidence_type")), "creative_selection")


def _provenance(decision: dict) -> dict:
    record = {
        key: decision[key]
        for key in PROVENANCE_KEYS
        if decision.get(key) not in (None, [], {})
    }
    record.setdefault("rule_version", decision.get("rule_version") or RULES_VERSION)
    record.setdefault("evidence_type", decision.get("evidence_type") or "unknown")
    return record


def _evidence_note(field: str, decision: dict) -> str:
    rule = decision.get("rule") or "no_rule"
    evidence_type = decision.get("evidence_type") or "unknown"
    rule_version = decision.get("rule_version") or RULES_VERSION
    parts = [
        decision.get("rationale")
        or decision.get("note")
        or f"Deterministic casting rule `{rule}` applied to verified game facts.",
        f"rule={rule}",
        f"evidence_type={evidence_type}",
        f"rule_version={rule_version}",
    ]
    if decision.get("prior_score") is not None:
        parts.append(f"prior_score={decision['prior_score']}")
    if decision.get("casting_strength") is not None:
        parts.append(f"casting_strength={decision['casting_strength']}")
    facts = decision.get("facts") or {}
    compact_facts = {
        key: value for key, value in facts.items()
        if value not in (None, "", [], {})
    }
    if compact_facts:
        parts.append(
            "facts=" + ", ".join(f"{key}={value}" for key, value in compact_facts.items())
        )
    sources = decision.get("sources") or ([decision["source"]] if decision.get("source") else [])
    if sources:
        parts.append("sources=" + "; ".join(str(source) for source in sources))
    return f"{field}: " + " | ".join(parts)


def build_individual_profile(
    dossier: dict,
    classification: dict,
    casting: dict,
    vocabulary: dict,
) -> dict:
    """Compose an NPC profile: casting decisions plus independently classified style.

    Every non-null identity field carries both a blueprint evidence record and a
    structured provenance record. Null fields are omitted from the prompt, never
    replaced with arbitrary defaults.
    """
    identity_context = dict(dossier.get("identity") or {})
    if dossier.get("voice_evidence"):
        identity_context["voice_evidence"] = dossier.get("voice_evidence")
    identity: dict[str, Any] = {}
    evidence: dict[str, dict] = {}
    provenance: dict[str, dict] = {}

    for field in IDENTITY_FIELDS:
        decision = casting.get(field) or {}
        value = decision.get("value")
        identity[field] = value
        if value is None or value == []:
            continue
        level = _evidence_level(decision)
        source = decision.get("source")
        quote = decision.get("quote")
        if level in {"explicit", "supported_inference"} and not (source and quote):
            level = "creative_selection"
        note = _evidence_note(field, decision)
        if level == "default":
            evidence[field] = make_evidence(field, "default", note=note, source=source)
        elif level in {"explicit", "supported_inference"}:
            evidence[field] = make_evidence(
                field,
                level,
                source_type=str(decision.get("evidence_type") or "game_fact"),
                source=source,
                quote=quote,
                note=note,
            )
        else:
            evidence[field] = creative_evidence(
                field,
                note,
                source=source or f"casting:{decision.get('rule') or 'rule'}",
            )
        provenance[field] = _provenance(decision)

    # Cached pre-2.0 classifications predate statuses; their labels are kept
    # readable for migration. A current record that omits status is unresolved.
    legacy_classification = not classification.get("schema_version")

    def normalized_style_decision(decision):
        # Schema 2.0 used `mixed` as both a value and a status. Migrate only
        # the unambiguous case; a mixed status with any other value was the
        # conflation this schema removes, so it stays unresolved.
        value = decision.get("value")
        status = decision.get("status") or (
            "supported" if legacy_classification else "unresolved"
        )
        if status == "mixed":
            status = "supported" if value == "mixed" else "unresolved"
        return {**decision, "value": value, "status": status}

    def accepted(decision):
        normalized = normalized_style_decision(decision)
        return normalized["value"] is not None and normalized["status"] == "supported"

    speech_style = {}
    for field, decision in (classification.get("speech_style") or {}).items():
        if field not in VALIDATED_STYLE_FIELDS:
            continue
        normalized = normalized_style_decision(decision)
        if accepted(normalized):
            speech_style[field] = normalized["value"]
    full_analysis = classification.get("speech_style_analysis") or {}
    dimensions = {
        field: normalized_style_decision(decision)
        for field, decision in (full_analysis.get("dimensions") or {}).items()
        if field in VALIDATED_STYLE_FIELDS
    }
    if not dimensions:
        dimensions = {
            field: normalized_style_decision(decision)
            for field, decision in (classification.get("speech_style") or {}).items()
            if field in VALIDATED_STYLE_FIELDS and accepted(decision)
        }
    analysis = (
        {
            **{key: value for key, value in full_analysis.items() if key != "dimensions"},
            "source": "blueprint_pipeline.classify",
            "classification": classification.get("canonical_id"),
            "dossier_hash": classification.get("dossier_hash"),
            "fields": dimensions,
        }
        if dimensions
        else None
    )
    # Model self-reported confidence is diagnostic only. Classification
    # strength is carried by evidence_tier, never promoted to profile confidence.
    confidence = None
    source_url = f"game:ENpcBase/{identity_context.get('npc_id')}"
    profile = {
        "schema_version": vocabulary["schema_version"],
        "profile_version": 1,
        "profile_kind": "character",
        "character": {
            "id": identity_context["canonical_id"],
            "name": identity_context.get("name") or identity_context["canonical_id"],
            "npc_id": identity_context.get("npc_id"),
            "race": identity_context.get("race"),
            "clan": identity_context.get("clan"),
            "sex": identity_context.get("sex", "unknown"),
            "role": identity_context.get("occupation"),
            "source_url": source_url,
            "applies_to": identity_context.get("speaker_ids", []),
        },
        "identity": identity,
        "identity_provenance": provenance,
        "casting": {
            "rules_version": RULES_VERSION,
            "warnings": casting.get("warnings") or [],
        },
        "speech_style": speech_style or None,
        "speech_style_analysis": analysis,
        "classification": {
            "provider": classification.get("provider", "unknown"),
            "model_id": classification.get("model_id", "unknown"),
            "dossier_hash": classification.get("dossier_hash") or "",
            "configuration_hash": classification.get("configuration_hash") or "",
            "source_audit_hash": object_hash(classification),
            "timestamp": classification.get("timestamp", ""),
            "taxonomy_version": (classification.get("configuration") or {}).get("taxonomy_version"),
            "classifier_version": (classification.get("configuration") or {}).get("classifier_version"),
            "prompt_version": (classification.get("configuration") or {}).get("prompt_version"),
            "sampler_version": (classification.get("configuration") or {}).get("sampler_version"),
        },
        "lifecycle": {
            "status": "draft",
            "classification_confidence": confidence,
            "manual_override": False,
            "approval": None,
        },
        "evidence": evidence,
        "qc": {
            "reference_duration_ok": False,
            "reference_transcript_verified": False,
            "no_reference_leak": False,
            "identity_approved": False,
            "no_drift": False,
        },
        "notes": "Synthetic voice draft. Acoustic fields are creative casting decisions unless their evidence includes a verified audio source. Null optional fields are omitted from the prompt; human audition and approval are still required.",
    }
    return profile


def validate_generated_profile(profile: dict, vocabulary: dict) -> list[str]:
    return blueprint_renderer.validate_profile(
        profile, vocabulary, profile.get("character", {}).get("id", "<generated>")
    )
