from __future__ import annotations

import hashlib
import pathlib
from typing import Any

from .cache import read_json, write_content_addressed
from .config import BLUEPRINTS, DEFAULT_OUT

ARCHETYPE_DIR = BLUEPRINTS / "archetypes"


def load_archetypes(directory: pathlib.Path = ARCHETYPE_DIR) -> list[dict]:
    profiles = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        profile = read_json(path)
        if profile.get("profile_kind") == "archetype":
            profiles.append(profile)
    return profiles


def _presentation_for(candidate: dict, classification: dict | None) -> str:
    """Presentation comes from the game's own sex field."""
    sex = str((candidate or {}).get("sex") or "").strip().casefold()
    return {"male": "masculine", "female": "feminine"}.get(sex, "neutral")


def select_archetype(
    candidate: dict,
    archetypes: list[dict] | None = None,
    *,
    classification: dict | None = None,
) -> dict:
    catalog = archetypes if archetypes is not None else load_archetypes()
    if not catalog:
        raise RuntimeError("no shared synthetic archetypes are available")
    presentation = _presentation_for(candidate, classification)
    matching = [
        profile for profile in catalog
        if profile.get("identity", {}).get("presentation") == presentation
    ]
    if not matching:
        matching = [
            profile for profile in catalog
            if profile.get("identity", {}).get("presentation") == "neutral"
        ]
    if not matching:
        matching = catalog
    matching.sort(key=lambda profile: profile["character"]["id"])
    digest = hashlib.sha256(
        f"{candidate.get('canonical_id')}:{presentation}:archetype-catalog-1.0".encode()
    ).digest()
    selected = matching[int.from_bytes(digest[:8], "big") % len(matching)]
    return {
        "profile_id": selected["character"]["id"],
        "profile_kind": "archetype",
        "selection_method": "stable-hash-with-presentation-filter",
        "presentation_context": presentation,
        "catalog_version": "1.0",
        "status": "draft",
    }


def build_assignment(
    candidate: dict,
    classification: dict | None,
    *,
    profile_id: str | None,
    reason: str,
    confidence: float | None,
    status: str,
) -> dict[str, Any]:
    return {
        "canonical_id": candidate["canonical_id"],
        "npc_id": candidate.get("npc_id"),
        "speaker_ids": sorted(set(candidate.get("speaker_ids") or [])),
        "aliases": sorted(set(candidate.get("aliases") or [])),
        "normalized_names": sorted(set(candidate.get("normalized_names") or [])),
        "profile_id": profile_id,
        "assignment_reason": reason,
        "assignment_confidence": confidence,
        "assignment_status": status,
        "classification_status": (classification or {}).get("status", "needs_review"),
        "human_approval": None,
    }


def write_assignment_snapshot(assignments: list[dict], out: pathlib.Path | str = DEFAULT_OUT) -> pathlib.Path:
    body = {
        "schema_version": "1.0",
        "catalog_version": "1.0",
        "assignments": sorted(assignments, key=lambda item: item["canonical_id"]),
    }
    return write_content_addressed(pathlib.Path(out) / "assignments", "registry", body)


def load_assignment_snapshot(path: pathlib.Path | str) -> dict:
    payload = read_json(pathlib.Path(path))
    if payload.get("schema_version") != "1.0" or not isinstance(payload.get("assignments"), list):
        raise ValueError("invalid synthetic voice assignment registry")
    return payload


def resolve_assignment(voice_id: str, registry: dict) -> dict | None:
    matches = [
        item for item in registry.get("assignments", [])
        if voice_id in item.get("speaker_ids", [])
    ]
    profile_ids = {item.get("profile_id") for item in matches}
    if len(profile_ids) > 1:
        raise ValueError(f"voice_id {voice_id!r} resolves to multiple synthetic profiles")
    return matches[0] if matches else None


def resolve_effective_profile(
    voice_id: str,
    registry: dict,
    *,
    profile_roots: tuple[pathlib.Path, ...] = (
        BLUEPRINTS / "characters",
        BLUEPRINTS / "archetypes",
        DEFAULT_OUT / "profiles",
    ),
) -> dict | None:
    assignment = resolve_assignment(voice_id, registry)
    if not assignment or not assignment.get("profile_id"):
        return None
    profile_id = assignment["profile_id"]
    candidates = []
    for root in profile_roots:
        if not root.exists():
            continue
        paths = {root / f"{profile_id}.json", *root.glob("*.json")}
        for path in paths:
            if not path.is_file():
                continue
            try:
                profile = read_json(path)
            except (OSError, ValueError):
                continue
            if profile.get("character", {}).get("id") == profile_id:
                history = profile.get("manual_history") or []
                last_human_action = max(
                    (str(row.get("timestamp") or "") for row in history if isinstance(row, dict)),
                    default="",
                )
                timestamp = last_human_action or str(
                    (profile.get("classification") or {}).get("timestamp") or ""
                )
                candidates.append((timestamp, int(profile.get("profile_version", 1)), path.stat().st_mtime_ns, profile))
    if not candidates:
        raise FileNotFoundError(f"assignment points to missing profile {profile_id!r}")
    candidates.sort(key=lambda row: row[:3])
    profile = candidates[-1][3]
    return {
        "assignment": assignment,
        "profile": profile,
        "ready_for_generation": (
            profile.get("lifecycle", {}).get("status") == "approved"
            and bool(profile.get("lifecycle", {}).get("approval"))
        ),
    }
