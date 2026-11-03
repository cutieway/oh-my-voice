from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from .cache import json_files, read_json, write_content_addressed
from .dialogue import sample_id


def _style_analysis(classification: dict | None, profile: dict | None) -> dict:
    if classification and isinstance(classification.get("speech_style_analysis"), dict):
        return classification["speech_style_analysis"]
    if profile and isinstance(profile.get("speech_style_analysis"), dict):
        return profile["speech_style_analysis"]
    return {}


def _request_metadata(classification: dict | None) -> dict:
    if not classification:
        return {
            "available": False,
            "note": "No classification record is available for this NPC.",
        }
    analysis = _style_analysis(classification, None)
    configuration = classification.get("configuration") or {}
    return {
        "available": True,
        "provider": classification.get("provider"),
        "model_id": classification.get("model_id") or analysis.get("model_id"),
        "model_revision": analysis.get("model_revision"),
        "endpoint": analysis.get("endpoint"),
        "endpoint_class": analysis.get("endpoint_class"),
        "taxonomy_version": analysis.get("taxonomy_version"),
        "classifier_version": analysis.get("classifier_version"),
        "prompt_version": analysis.get("prompt_version"),
        "schema_version_contract": analysis.get("schema_version_contract"),
        "sampler_version": analysis.get("sampler_version"),
        "evidence_policy_version": analysis.get("evidence_policy_version"),
        "request_fingerprint": analysis.get("request_fingerprint"),
        "response_fingerprint": analysis.get("response_fingerprint"),
        "sample_manifest_hash": analysis.get("sample_manifest_hash"),
        "state_hash": configuration.get("state_hash"),
        "questions_hash": configuration.get("questions_hash"),
        "guardrails_hash": configuration.get("guardrails_hash"),
        "vocabulary_hash": configuration.get("vocabulary_hash"),
        "budget_reduction": analysis.get("budget_reduction"),
        "evidence": analysis.get("evidence") or {},
        "classification_status": classification.get("status"),
        "classification_timestamp": classification.get("timestamp"),
        "cached": classification.get("cache_key") is not None,
    }


def _sample_provenance(dossier: dict, sample: list[dict]) -> dict:
    """Map opaque request IDs to local source/sheet provenance for humans only."""
    canonical_id = str((dossier.get("identity") or {}).get("canonical_id") or "")
    wanted = {str(row.get("id")) for row in sample if row.get("id")}
    provenance = {}
    for row in dossier.get("dialogue_samples") or []:
        identifier = str(sample_id(canonical_id, row))
        if identifier not in wanted:
            continue
        provenance[identifier] = {
            "source": row.get("source"),
            "sheet": row.get("sheet"),
            "row_id": row.get("row_id"),
            "voice_line_id": row.get("voice_line_id"),
            "dialog_id": row.get("dialog_id"),
        }
    return provenance


def review_report(
    dossier: dict,
    classification: dict | None,
    profile: dict | None,
    renderer,
    vocabulary: dict,
    assignment: dict | None = None,
) -> dict:
    identity = dossier.get("identity") or {}
    analysis = _style_analysis(classification, profile)
    stored_sample = [
        {"id": str(row.get("id")), "text": row.get("text")}
        for row in analysis.get("representative_sample") or []
        if row.get("id") and isinstance(row.get("text"), str)
    ]
    plan = renderer.build_plan(profile, vocabulary) if profile else None
    report = {
        "canonical_id": identity.get("canonical_id"),
        "name": identity.get("name"),
        "game_metadata": {
            key: identity.get(key)
            for key in ("npc_id", "race", "clan", "sex")
        },
        "identity_facts": identity,
        "identity_resolution": {
            "status": identity.get("resolution_status"),
            "has_se_voice_reference": identity.get("has_se_voice_reference"),
            "ambiguous": identity.get("identity_ambiguous"),
            "reasons": identity.get("review_reasons", []),
        },
        "voice_evidence": dossier.get("voice_evidence", []),
        "dialogue_statistics": dossier.get("statistics", {}),
        "dialogue_analysis": dossier.get("dialogue_analysis", {}),
        "dialogue_samples": dossier.get("dialogue_samples", []),
        "classification": classification,
        "archetype_assignment": assignment,
        "proposed_blueprint": profile,
        "reference_pin": (profile or {}).get("reference_pin"),
        "jev_request": _request_metadata(classification),
        "jev_sample": stored_sample,
        "jev_sample_status": (
            "recorded_exact_request"
            if stored_sample
            else "not_recorded; this classification predates exact sample capture, so the report does not reconstruct one"
        ),
        "jev_sample_local_provenance": _sample_provenance(dossier, stored_sample),
        "jev_audit_features": analysis.get("experimental") or {},
        "identity_instruction": plan["identity_prompt"] if plan else None,
        "style_instruction": plan["style_prompt"] if plan else None,
        "combined_instruction": plan["instruct"] if plan else None,
        "identity_prompt_hash": plan["identity_prompt_hash"] if plan else None,
        "style_profile_hash": plan["style_profile_hash"] if plan else None,
        "combined_instruct_hash": plan["combined_instruct_hash"] if plan else None,
        "reference_text": plan["reference"]["text"] if plan else None,
        "design_model": plan["design_model"] if plan else None,
        "design_seed": plan["seed"] if plan else None,
        "human_approval": None,
    }
    return report


def build_pipeline_report(
    inventory: dict,
    cache_dir: Path | str,
    out_dir: Path | str,
) -> tuple[dict, Path, Path]:
    cache_dir = Path(cache_dir)
    out_dir = Path(out_dir)
    classifications = {}
    for path in json_files(cache_dir / "classifications"):
        try:
            item = read_json(path)
        except (OSError, ValueError):
            continue
        canonical_id = item.get("canonical_id")
        if canonical_id:
            classifications[str(canonical_id)] = item

    assignments = []
    for path in json_files(out_dir / "assignments", "registry.*.json"):
        try:
            body = read_json(path)
        except (OSError, ValueError):
            continue
        assignments.extend(body.get("assignments") or [])
    latest_assignment = {}
    for assignment in assignments:
        latest_assignment[assignment.get("canonical_id")] = assignment

    status_counts = Counter(item.get("status", "unknown") for item in classifications.values())
    provider_counts = Counter(item.get("provider", "unknown") for item in classifications.values())
    usage = Counter()
    for item in classifications.values():
        for key, value in (item.get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] += value
    assignment_counts = Counter(
        (item.get("profile_kind") or "unassigned", item.get("assignment_status") or "unknown")
        for item in latest_assignment.values()
    )
    report = {
        "schema_version": "1.0",
        "inventory_counts": inventory.get("counts", {}),
        "classification_count": len(classifications),
        "classification_status_counts": dict(sorted(status_counts.items())),
        "provider_counts": dict(sorted(provider_counts.items())),
        "jev_usage": dict(sorted(usage.items())),
        "assignment_count": len(latest_assignment),
        "assignment_counts": [
            {"profile_kind": kind, "status": status, "count": count}
            for (kind, status), count in sorted(assignment_counts.items())
        ],
        "review_candidates": [
            {
                "canonical_id": item.get("canonical_id"),
                "name": item.get("name"),
                "status": item.get("status"),
                "reasons": item.get("review_reasons", []),
            }
            for item in classifications.values()
            if item.get("status") == "needs_review"
        ],
        "limits": [
            "Classification snapshots are immutable and resumable.",
            "Jev usage is reported when the provider returns token counts; this report does not estimate provider billing.",
            "Assignments are provisional until a human auditions and approves the linked profile.",
        ],
    }
    summary_lines = [
        "# Synthetic blueprint pipeline report",
        "",
        f"- Eligible synthetic NPC identities: {inventory.get('counts', {}).get('eligible_synthetic_npc_identities', 0)}",
        f"- Identity groups needing review: {inventory.get('counts', {}).get('by_status', {}).get('review', 0)}",
        f"- Unresolved speaker labels: {inventory.get('counts', {}).get('unresolved_review_labels', 0)}",
        f"- Cached classifications: {len(classifications)}",
        f"- Provisional assignments: {len(latest_assignment)}",
        f"- Review routed classifications: {status_counts.get('needs_review', 0)}",
        "",
        "## Classification by status",
        "",
    ]
    summary_lines.extend(f"- {key}: {value}" for key, value in sorted(status_counts.items()))
    summary_lines += ["", "## Provider token usage", ""]
    summary_lines.extend(f"- {key}: {value}" for key, value in sorted(usage.items()))
    summary_lines += ["", "## Review queue", ""]
    summary_lines.extend(
        f"- {item.get('canonical_id')} — {item.get('name')}: {', '.join(item.get('reasons') or [])}"
        for item in report["review_candidates"]
    )
    if not report["review_candidates"]:
        summary_lines.append("- Empty")
    json_path = write_content_addressed(out_dir / "reports", "pipeline", report)
    # Keep markdown as a real .md artifact without replacing any existing report.
    raw_md = "\n".join(summary_lines) + "\n"
    md_name = f"pipeline.{hashlib.sha256(raw_md.encode()).hexdigest()[:16]}.md"
    md_target = out_dir / "reports" / md_name
    md_target.parent.mkdir(parents=True, exist_ok=True)
    if not md_target.exists():
        with md_target.open("x", encoding="utf-8") as handle:
            handle.write(raw_md)
    markdown_path = md_target
    return report, json_path, markdown_path
