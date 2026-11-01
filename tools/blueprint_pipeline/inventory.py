from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from .config import LANGUAGE
from .database import association_rows, iter_dialogue_rows, read_db, speaker_rows
from .game_data import game_metadata
from .identity import canonical_id, identity_links, label_name_index, normalize_name, unresolved_id


def expansion_bucket(source: str, sheet: str) -> str:
    parts = (sheet or "").replace("\\", "/").split("/")
    if source == "cutscene" and len(parts) > 1 and parts[0].casefold() == "cut_scene":
        # The DB keeps Lumina sheet namespaces, not expansion IDs. Preserve the
        # exact game-data group without guessing its marketing expansion name.
        return f"cut_scene/{parts[1]}"
    if source in {"custom", "quest", "raid", "content", "warp", "transport", "opening"}:
        return "quest_sheet_group_unmapped"
    return "source_group_unmapped"


def _display_name(npc_id: int, labels: list[str], names: dict[str, str], game_index: dict) -> str:
    metadata = game_metadata(npc_id, game_index)
    if metadata.get("name"):
        return str(metadata["name"])
    label_names = [names[label].strip() for label in labels if names.get(label, "").strip()]
    if not label_names:
        return f"NPC {npc_id}"
    counts = Counter(label_names)
    highest = max(counts.values())
    return sorted(name for name, count in counts.items() if count == highest)[0]


def build_inventory(
    db: Path | str,
    *,
    lang: str = LANGUAGE,
    game_index: dict[int, dict] | None = None,
) -> dict:
    game_index = game_index or {}
    with read_db(db) as connection:
        speakers = speaker_rows(connection)
        associations = association_rows(connection, lang)
        names = label_name_index(speakers)
        speaker_by_id = {str(row["voice_id"]): row for row in speakers}
        links = identity_links(speakers, associations)
        active = set()
        voiced_dialogue_npcs = set()
        lines: dict[str, dict] = {}
        for row in iter_dialogue_rows(connection, lang):
            assoc_id = row.get("associated_speaker_id")
            voice_id = str(assoc_id or row.get("speaker_id") or "")
            if voice_id:
                active.add(voice_id)
            candidate_npc = row.get("associated_npc_id")
            if candidate_npc is None and voice_id in speaker_by_id:
                candidate_npc = speaker_by_id[voice_id].get("npc_id")
            if candidate_npc is None:
                candidate_npc = row.get("npc_id")
            if candidate_npc is not None:
                links[voice_id].add(int(candidate_npc))

            if row.get("has_scd"):
                if candidate_npc is not None:
                    voiced_dialogue_npcs.add(int(candidate_npc))
                continue
            speaker = speaker_by_id.get(voice_id)
            if not voice_id or speaker is None or speaker.get("has_scd"):
                continue
            if candidate_npc is None:
                continue
            key = canonical_id(int(candidate_npc))
            info = lines.setdefault(key, {
                "row_ids": set(),
                "line_ids": set(),
                "source_rows": defaultdict(set),
                "source_ids": defaultdict(set),
                "expansion_rows": defaultdict(set),
            })
            info["row_ids"].add(row["id"])
            info["source_rows"][row["source"]].add(row["id"])
            if row.get("voice_line_id"):
                info["line_ids"].add(row["voice_line_id"])
                info["source_ids"][row["source"]].add(row["voice_line_id"])
            info["expansion_rows"][expansion_bucket(row["source"], row["sheet"])].add(row["id"])

        voiced_ids = {
            int(row["npc_id"]) for row in speakers
            if row.get("has_scd") and row.get("npc_id") is not None
        }
        voiced_ids.update(voiced_dialogue_npcs)
        voiced_names = defaultdict(set)
        for row in speakers:
            if row.get("has_scd"):
                norm = normalize_name(row.get("normalized_name") or row.get("name"))
                if norm:
                    voiced_names[norm].add(
                        int(row["npc_id"]) if row.get("npc_id") is not None else None
                    )

        ids_to_labels: dict[int, set[str]] = defaultdict(set)
        for voice_id, npc_ids in links.items():
            for npc_id in npc_ids:
                ids_to_labels[npc_id].add(voice_id)

        all_candidate_ids = set(ids_to_labels)
        name_to_ids = defaultdict(set)
        for npc_id, label_ids in ids_to_labels.items():
            for label_id in label_ids:
                speaker = speaker_by_id.get(label_id, {})
                norm = normalize_name(speaker.get("normalized_name") or names.get(label_id))
                if norm:
                    name_to_ids[norm].add(npc_id)

        candidates = []
        for npc_id in sorted(all_candidate_ids):
            label_ids = sorted(ids_to_labels[npc_id])
            label_rows = [speaker_by_id[label] for label in label_ids if label in speaker_by_id]
            if not label_rows or all(row.get("has_scd") for row in label_rows):
                continue
            reasons = []
            ambiguous_links = [label for label in label_ids if len(links[label]) > 1]
            if ambiguous_links:
                reasons.append("speaker label is associated with multiple NPC identities")
            if any("ambiguous" in str(row.get("speaker_match", "")) for row in label_rows):
                reasons.append("game speaker match is ambiguous")
            if npc_id in voiced_ids or any(row.get("has_scd") for row in label_rows):
                reasons.append("same canonical NPC identity has an SE voice reference")

            norms = sorted({
                normalize_name(row.get("normalized_name") or row.get("name"))
                for row in label_rows
                if normalize_name(row.get("normalized_name") or row.get("name"))
            })
            ambiguous_voiced_name = False
            for norm in norms:
                if norm in voiced_names and any(other_id != npc_id for other_id in voiced_names[norm]):
                    ambiguous_voiced_name = True
                if len(name_to_ids[norm]) > 1:
                    reasons.append("normalized name collides across canonical NPC identities")
            if ambiguous_voiced_name:
                reasons.append("normalized name matches a voiced identity with a different or unresolved NPC ID")

            info = lines.get(canonical_id(npc_id), {})
            if not info.get("row_ids"):
                reasons.append("no unvoiced dialogue rows")
            meta = game_metadata(npc_id, game_index)
            if reasons:
                status = "review"
            else:
                status = "eligible"
            candidates.append({
                "canonical_id": canonical_id(npc_id),
                "npc_id": npc_id,
                "name": _display_name(npc_id, label_ids, names, game_index),
                "speaker_ids": label_ids,
                "aliases": sorted({names[label] for label in label_ids if names.get(label)}),
                "normalized_names": norms,
                "identity_status": status,
                "has_se_reference": npc_id in voiced_ids or any(row.get("has_scd") for row in label_rows),
                "identity_ambiguous": bool(ambiguous_links or ambiguous_voiced_name or any("collision" in r for r in reasons)),
                "dialogue_count": len(info.get("row_ids", set())),
                "unique_line_count": len(info.get("line_ids", set())),
                "source_counts": {
                    source: len(row_ids)
                    for source, row_ids in sorted(info.get("source_rows", {}).items())
                },
                "source_line_ids": {
                    source: len(ids) for source, ids in sorted(info.get("source_ids", {}).items())
                },
                "expansion_counts": {
                    bucket: len(row_ids)
                    for bucket, row_ids in sorted(info.get("expansion_rows", {}).items())
                },
                "race_id": meta.get("race_id") or next((row.get("race_id") for row in label_rows if row.get("race_id")), None),
                "race": meta.get("race"),
                "tribe_id": meta.get("tribe_id"),
                "clan": meta.get("clan"),
                "sex": meta.get("sex") if meta.get("sex") != "unknown" else (
                    "male" if any(row.get("gender") == 0 for row in label_rows)
                    else "female" if any(row.get("gender") == 1 for row in label_rows)
                    else "unknown"
                ),
                "lore_data_available": False,
                "race_clan_metadata_available": bool(meta.get("race") or meta.get("clan")),
                "review_reasons": sorted(set(reasons)),
            })

        unresolved = []
        for voice_id in sorted(active):
            speaker = speaker_by_id.get(voice_id)
            if speaker is None or speaker.get("has_scd"):
                continue
            npc_ids = links.get(voice_id, set())
            if npc_ids:
                continue
            unresolved.append({
                "canonical_id": unresolved_id(voice_id),
                "npc_id": None,
                "name": names.get(voice_id, voice_id),
                "speaker_ids": [voice_id],
                "aliases": [names.get(voice_id, voice_id)],
                "normalized_names": [normalize_name(speaker.get("normalized_name") or speaker.get("name"))],
                "identity_status": "review_unresolved",
                "has_se_reference": False,
                "identity_ambiguous": True,
                "dialogue_count": int(speaker.get("dialogue_lines") or 0),
                "unique_line_count": 0,
                "source_counts": {},
                "source_line_ids": {},
                "expansion_counts": {},
                "race_id": speaker.get("race_id") or None,
                "race": None,
                "tribe_id": None,
                "clan": None,
                "sex": "unknown",
                "lore_data_available": False,
                "race_clan_metadata_available": False,
                "review_reasons": ["speaker label has no canonical NPC ID; do not create a named character profile"],
            })

        candidates.extend(unresolved)
        status_counts = Counter(candidate["identity_status"] for candidate in candidates)
        eligible = [candidate for candidate in candidates if candidate["identity_status"] == "eligible"]
        return {
            "schema_version": "1.0",
            "language": lang,
            "database": str(Path(db).expanduser().resolve()),
            "read_only": True,
            "population_rule": (
                "Synthetic candidates require speakers.has_scd=0, dialogue/narration "
                "rows, canonical identity resolution, and no voiced alias for that identity."
            ),
            "counts": {
                "speaker_labels": len(speakers),
                "speaker_labels_without_se_reference": sum(not row.get("has_scd") for row in speakers),
                "canonical_npc_identities": len(all_candidate_ids),
                "synthetic_identity_groups": len([c for c in candidates if c["npc_id"] is not None]),
                "eligible_synthetic_npc_identities": len(eligible),
                "unresolved_review_labels": len(unresolved),
                "by_status": dict(sorted(status_counts.items())),
                "eligible_dialogue_rows": sum(c["dialogue_count"] for c in eligible),
                "eligible_unique_line_ids": sum(c["unique_line_count"] for c in eligible),
            },
            "expansion_note": (
                "dialogue.db does not store an expansion column. Cutscene sheet groups are "
                "preserved verbatim; other source families are marked unmapped rather than guessed."
            ),
            "candidates": candidates,
        }
