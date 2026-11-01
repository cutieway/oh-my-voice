from __future__ import annotations

import re
from collections import defaultdict


def normalize_name(value: str | None) -> str:
    return "".join(ch.casefold() for ch in (value or "") if ch.isalnum())


def canonical_id(npc_id: int) -> str:
    return f"npc_{int(npc_id)}"


def unresolved_id(voice_id: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", voice_id.casefold()).strip("_")
    return "unresolved_" + (safe or "speaker")


def identity_links(speakers: list[dict], associations: list[dict]) -> dict[str, set[int]]:
    links: dict[str, set[int]] = defaultdict(set)
    for speaker in speakers:
        if speaker.get("npc_id") is not None:
            links[str(speaker["voice_id"])].add(int(speaker["npc_id"]))
    for association in associations:
        if association.get("npc_id") is not None:
            links[str(association["speaker_id"])].add(int(association["npc_id"]))
    return links


def label_name_index(speakers: list[dict]) -> dict[str, str]:
    return {
        str(row["voice_id"]): str(row.get("name") or row["voice_id"])
        for row in speakers
    }
