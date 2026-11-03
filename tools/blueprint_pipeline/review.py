from __future__ import annotations

import copy
import datetime as dt
import hashlib
import pathlib
import wave
from typing import Any


class ReviewError(ValueError):
    pass


def utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_sha256(path: pathlib.Path | str) -> str:
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_details(path: pathlib.Path | str) -> dict:
    ref = pathlib.Path(path)
    if not ref.is_file():
        raise ReviewError(f"reference audio does not exist: {ref}")
    try:
        with wave.open(str(ref), "rb") as audio:
            seconds = audio.getnframes() / float(audio.getframerate())
    except (wave.Error, EOFError) as exc:
        raise ReviewError("approval reference must be a readable WAV file") from exc
    if not 8 <= seconds <= 18:
        raise ReviewError(f"reference duration {seconds:.2f}s is outside the 8-18 second acceptance range")
    return {"sha256": file_sha256(ref), "seconds": round(seconds, 3)}


def mark_auditioned(
    profile: dict,
    *,
    reference_file: pathlib.Path | str,
    design_seed: int,
    by: str,
    note: str | None = None,
    reference_transcript: str | None = None,
    design_model: str | None = None,
    identity_prompt_hash: str | None = None,
    style_profile_hash: str | None = None,
    combined_instruct_hash: str | None = None,
    casting_rules_version: str | None = None,
    renderer_version: str | None = None,
    instruct: str | None = None,
    taxonomy_version: str | None = None,
    classifier_version: str | None = None,
    prompt_version: str | None = None,
    sampler_version: str | None = None,
    corpus_hash: str | None = None,
    sample_manifest_hash: str | None = None,
) -> dict:
    if not by.strip():
        raise ReviewError("auditioning reviewer name is required")
    details = reference_details(reference_file)
    updated = copy.deepcopy(profile)
    timestamp = utc_timestamp()
    previous_pin = copy.deepcopy(updated.get("reference_pin"))
    new_pin = {
        "design_seed": int(design_seed),
        "reference_sha256": details["sha256"],
        "ref_seconds": details["seconds"],
        "pinned_on": timestamp,
    }
    if reference_transcript is not None:
        new_pin["reference_transcript"] = reference_transcript
        new_pin["reference_transcript_sha256"] = hashlib.sha256(
            reference_transcript.encode("utf-8")
        ).hexdigest()
    for key, value in (
        ("design_model", design_model),
        ("identity_prompt_hash", identity_prompt_hash),
        ("style_profile_hash", style_profile_hash),
        ("combined_instruct_hash", combined_instruct_hash),
        ("casting_rules_version", casting_rules_version),
        ("renderer_version", renderer_version),
        ("instruct", instruct),
        ("taxonomy_version", taxonomy_version),
        ("classifier_version", classifier_version),
        ("prompt_version", prompt_version),
        ("sampler_version", sampler_version),
        ("corpus_hash", corpus_hash),
        ("sample_manifest_hash", sample_manifest_hash),
    ):
        if value is not None:
            new_pin[key] = value
    updated["reference_pin"] = new_pin
    updated.setdefault("lifecycle", {})["status"] = "auditioned"
    updated["lifecycle"]["approval"] = None
    updated["lifecycle"]["manual_override"] = True
    metadata = updated.get("archetype_metadata")
    if isinstance(metadata, dict):
        metadata["audition_status"] = "auditioned"
    updated.setdefault("qc", {})["reference_duration_ok"] = True
    updated["qc"]["identity_approved"] = False
    updated.setdefault("manual_history", []).append({
        "action": "audition",
        "timestamp": timestamp,
        "by": by,
        "changes": {"reference_pin": {"from": previous_pin, "to": new_pin}},
        "note": note,
    })
    return updated


def approve_profile(
    profile: dict,
    *,
    by: str,
    reference_sha256: str | None = None,
) -> dict:
    if not by.strip():
        raise ReviewError("human approver name is required")
    lifecycle = profile.get("lifecycle") or {}
    if lifecycle.get("status") != "auditioned":
        raise ReviewError("a profile must be auditioned before it can be approved")
    pin = profile.get("reference_pin")
    if not pin or not pin.get("reference_sha256"):
        raise ReviewError("the profile has no pinned reference clip")
    expected = pin["reference_sha256"]
    if reference_sha256 is not None and reference_sha256 != expected:
        raise ReviewError("approval hash does not match the pinned reference clip")
    updated = copy.deepcopy(profile)
    timestamp = utc_timestamp()
    updated["lifecycle"]["status"] = "approved"
    updated["lifecycle"]["approval"] = {
        "approved_by": by,
        "approved_at": timestamp,
        "reference_sha256": expected,
    }
    updated["lifecycle"]["manual_override"] = True
    updated.setdefault("qc", {})["identity_approved"] = True
    metadata = updated.get("archetype_metadata")
    if isinstance(metadata, dict):
        metadata["audition_status"] = "approved"
    updated.setdefault("manual_history", []).append({
        "action": "approve",
        "timestamp": timestamp,
        "by": by,
        "changes": {"lifecycle.status": "approved", "reference_sha256": expected},
        "note": None,
    })
    return updated


def reject_profile(profile: dict, *, by: str, reason: str) -> dict:
    if not by.strip() or not reason.strip():
        raise ReviewError("a human reviewer and rejection reason are required")
    updated = copy.deepcopy(profile)
    timestamp = utc_timestamp()
    previous_lifecycle = copy.deepcopy(updated.get("lifecycle") or {})
    updated.setdefault("lifecycle", {})["status"] = "rejected"
    updated["lifecycle"]["approval"] = None
    updated["lifecycle"]["manual_override"] = True
    updated["lifecycle"]["rejection_reason"] = reason
    updated.setdefault("qc", {})["identity_approved"] = False
    metadata = updated.get("archetype_metadata")
    if isinstance(metadata, dict):
        metadata["audition_status"] = "rejected"
    updated.setdefault("manual_history", []).append({
        "action": "reject",
        "timestamp": timestamp,
        "by": by,
        "changes": {"lifecycle": {"from": previous_lifecycle, "to": "rejected"}},
        "note": reason,
    })
    return updated


def _set_path(target: dict, dotted_path: str, value: Any) -> None:
    parts = dotted_path.split(".")
    if not parts or parts[0] not in {"identity", "speech_style", "notes"}:
        raise ReviewError("manual edits may only change identity, speech_style, or notes")
    if parts[0] == "notes":
        if len(parts) != 1 or not isinstance(value, str):
            raise ReviewError("notes must be a string")
        target["notes"] = value
        return
    current = target.setdefault(parts[0], {})
    for part in parts[1:-1]:
        current = current.setdefault(part, {})
    if len(parts) < 2:
        raise ReviewError("manual edit needs a field path")
    current[parts[-1]] = value


def apply_manual_changes(
    profile: dict,
    changes: dict[str, Any],
    *,
    by: str,
    note: str | None = None,
) -> dict:
    if not by.strip() or not changes:
        raise ReviewError("a reviewer and at least one change are required")
    updated = copy.deepcopy(profile)
    before_identity = copy.deepcopy(updated.get("identity") or {})
    before = {
        path: _read_path(updated, path)
        for path in changes
    }
    for path, value in changes.items():
        _set_path(updated, path, value)
    identity_changed = before_identity != updated.get("identity")
    if identity_changed:
        updated["profile_version"] = int(updated.get("profile_version", 1)) + 1
        metadata = updated.get("archetype_metadata")
        if isinstance(metadata, dict):
            metadata["audition_status"] = "draft"
    lifecycle = updated.setdefault("lifecycle", {})
    old_status = lifecycle.get("status")
    old_approval = lifecycle.get("approval")
    lifecycle["manual_override"] = True
    lifecycle["status"] = "ready_for_audition" if identity_changed else (
        old_status if old_status in {"auditioned", "approved", "rejected"} else "needs_review"
    )
    lifecycle["approval"] = None if identity_changed else old_approval
    if identity_changed:
        pin = updated.pop("reference_pin", None)
        if pin:
            updated.setdefault("manual_history", []).append({
                "action": "edit",
                "timestamp": utc_timestamp(),
                "by": by,
                "changes": {"retired_reference_pin": pin},
                "note": "The previous pin is preserved in history because acoustic identity changed.",
            })
        qc = updated.setdefault("qc", {})
        for key in ("reference_duration_ok", "reference_transcript_verified", "no_reference_leak", "identity_approved", "no_drift"):
            qc[key] = False
    timestamp = utc_timestamp()
    updated.setdefault("manual_history", []).append({
        "action": "edit",
        "timestamp": timestamp,
        "by": by,
        "changes": {path: {"from": before[path], "to": value} for path, value in changes.items()},
        "note": note,
    })
    return updated


def _read_path(target: dict, dotted_path: str) -> Any:
    current: Any = target
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return copy.deepcopy(current)


def preserve_manual_profile(existing: dict | None, generated: dict, *, force: bool = False) -> dict:
    if existing is None or force:
        return generated
    lifecycle = existing.get("lifecycle") or {}
    if lifecycle.get("manual_override") or lifecycle.get("status") in {
        "auditioned", "approved", "rejected"
    }:
        return existing
    return generated
