from __future__ import annotations

from typing import Any

try:
    import blueprint as blueprint_renderer
except ModuleNotFoundError:
    from tools import blueprint as blueprint_renderer

from .cache import object_hash
from .evidence import creative_evidence, make_evidence, source_evidence


def _voice_evidence_for(dossier: dict, field: str, value: str, vocabulary: dict) -> dict | None:
    if not value:
        return None
    if field == "distinctive_traits" or isinstance(value, (list, dict)):
        return None
    field_spec = vocabulary["identity"][field]
    values = field_spec.get("values", {})
    if field == "timbre_secondary":
        values = vocabulary["identity"][field_spec["ref"]]["values"]
    rendered = str(values.get(value, {}).get("render") or value).casefold()
    tokens = [token for token in rendered.split() if len(token) > 2]
    for evidence in dossier.get("voice_evidence") or []:
        quote = str(evidence.get("quote") or "")
        quote_words = quote.casefold()
        if tokens and all(token in quote_words for token in tokens) and evidence.get("source"):
            return source_evidence(
                field,
                str(evidence.get("source_type") or "wiki_lore"),
                str(evidence["source"]),
                evidence.get("revision_id"),
                quote,
                level=str(evidence.get("level") or "explicit"),
            )
    return None


def build_individual_profile(
    dossier: dict,
    classification: dict,
    archetype_profile: dict,
    vocabulary: dict,
) -> dict:
    identity_context = dossier.get("identity") or {}
    archetype_identity = archetype_profile["identity"]
    cast = classification.get("acoustic_casting") or {}
    identity = {}
    evidence = {}
    identity_fields = (
        "presentation", "perceived_age", "pitch", "pitch_range", "timbre_primary",
        "timbre_secondary", "resonance", "articulation", "speaking_pace",
        "intonation", "distinctive_traits",
    )
    for field in identity_fields:
        decision = cast.get(field) or {}
        value = decision.get("value")
        if value is None:
            value = archetype_identity.get(field)
        if field not in {"perceived_age", "pitch_range", "timbre_secondary", "resonance", "distinctive_traits"}:
            if value is None:
                value = vocabulary["classification"]["defaults"].get(field)
        identity[field] = value
        if value is None or value == []:
            continue
        source = _voice_evidence_for(dossier, field, value, vocabulary)
        if source is not None:
            evidence[field] = source
        elif field == "accent":
            continue
        else:
            evidence[field] = creative_evidence(
                field,
                "Deliberate synthetic casting choice; this is not a claim about an original actor or recording.",
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
        value = decision.get("value")
        if value is not None:
            speech_style[field] = value
    analysis = dossier.get("dialogue_analysis") or {}
    confidence_values = [
        float(answer.get("confidence"))
        for answer in (classification.get("answers") or {}).values()
        if answer.get("type") in {"choice", "score"} and answer.get("confidence") is not None
    ]
    confidence = min(confidence_values) if confidence_values else None
    source_url = (
        (dossier.get("wiki_page") or {}).get("url")
        or f"game:ENpcResident/{identity_context.get('npc_id')}"
    )
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
            "lore_age": identity_context.get("lore_age"),
            "role": identity_context.get("occupation"),
            "source_url": source_url,
            "applies_to": identity_context.get("speaker_ids", []),
        },
        "identity": identity,
        "speech_style": speech_style or None,
        "speech_style_analysis": analysis or None,
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
        "pack_archon": "neutral",
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
