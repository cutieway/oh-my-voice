from __future__ import annotations

from typing import Any

try:
    import blueprint as blueprint_renderer
except ModuleNotFoundError:
    from tools import blueprint as blueprint_renderer

from .cache import object_hash
from .evidence import creative_evidence, make_evidence


# Only fields with a validation story may reach a profile. `diction_style` and
# `contraction_frequency` failed the benchmark and stay out until they pass.
VALIDATED_STYLE_FIELDS = (
    "register",
    "vocabulary_complexity",
    "sentence_complexity",
    "colloquialism",
    "rhetorical_style",
)


def build_individual_profile(
    dossier: dict,
    classification: dict,
    casting: dict,
    vocabulary: dict,
) -> dict:
    """Compose an NPC profile: acoustics cast from game data, style classified."""
    identity_context = dossier.get("identity") or {}
    identity = {}
    evidence = {}
    identity_fields = (
        "presentation", "perceived_age", "pitch", "pitch_range", "timbre_primary",
        "timbre_secondary", "resonance", "articulation", "speaking_pace",
        "intonation", "distinctive_traits",
    )
    for field in identity_fields:
        decision = casting.get(field) or {}
        value = decision.get("value")
        identity[field] = value
        if value is None or value == []:
            continue
        level = decision.get("level") or "creative_selection"
        rule = decision.get("rule")
        facts = decision.get("facts") or {}
        note = decision.get("note") or (
            "Deterministic casting rule "
            f"`{rule}` applied to game facts "
            f"({', '.join(f'{key}={value}' for key, value in facts.items() if value not in (None, ''))}). "
            "This describes the synthetic voice we create, not the original actor."
        )
        if level == "default":
            evidence[field] = make_evidence(field, "default", note=note)
        else:
            evidence[field] = creative_evidence(
                field, note, source=f"casting:{rule or 'rule'}"
            )

    defaults = vocabulary["classification"]["defaults"]
    # Required identity fields always get a deliberate value. The defaults are
    # explicit creative fallbacks and are not inferred from physical metadata.
    for field in ("presentation", "pitch", "timbre_primary", "articulation", "speaking_pace", "intonation"):
        if identity.get(field) is None:
            identity[field] = defaults[field]
            evidence[field] = creative_evidence(
                field,
                "Project fallback for a synthetic cast; not a claim about an original recording.",
            )
    accent_default = vocabulary["identity"]["accent"]["default"]
    identity["accent"] = dict(accent_default)
    evidence["accent"] = make_evidence(
        "accent",
        "default",
        note="Project-wide accent fallback; replace only with source-backed evidence or an explicit creative decision.",
    )

    speech_style = {}
    for field, decision in (classification.get("speech_style") or {}).items():
        if field not in VALIDATED_STYLE_FIELDS:
            continue
        value = decision.get("value")
        if value is not None:
            speech_style[field] = value
    style_analysis = {}
    for field, decision in (classification.get("speech_style") or {}).items():
        if field not in VALIDATED_STYLE_FIELDS or decision.get("value") is None:
            continue
        style_analysis[field] = {
            "value": decision.get("value"),
            "confidence": decision.get("confidence"),
            "probabilities": decision.get("probabilities"),
            "method": decision.get("method") or classification.get("provider"),
        }
    analysis = (
        {
            "source": "blueprint_pipeline.classify",
            "provider": classification.get("provider"),
            "model_id": classification.get("model_id"),
            "classification": classification.get("canonical_id"),
            "dossier_hash": classification.get("dossier_hash"),
            "fields": style_analysis,
        }
        if style_analysis
        else None
    )
    confidence_values = [
        float(answer.get("confidence"))
        for answer in (classification.get("answers") or {}).values()
        if answer.get("type") in {"choice", "score"} and answer.get("confidence") is not None
    ]
    confidence = min(confidence_values) if confidence_values else None
    source_url = f"game:ENpcResident/{identity_context.get('npc_id')}"
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
        "speech_style": speech_style or None,
        "speech_style_analysis": analysis,
        "classification": {
            "provider": classification.get("provider", "unknown"),
            "model_id": classification.get("model_id", "unknown"),
            "dossier_hash": classification.get("dossier_hash") or "",
            "configuration_hash": classification.get("configuration_hash") or "",
            "source_audit_hash": object_hash(classification),
            "timestamp": classification.get("timestamp", ""),
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
        "notes": "Synthetic voice draft. Acoustic fields are creative casting decisions unless their field evidence includes a source quote. Human audition and approval are still required.",
    }
    return profile


def validate_generated_profile(profile: dict, vocabulary: dict) -> list[str]:
    return blueprint_renderer.validate_profile(
        profile, vocabulary, profile.get("character", {}).get("id", "<generated>")
    )
