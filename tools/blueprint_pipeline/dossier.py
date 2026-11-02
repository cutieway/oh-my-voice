from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import blueprint as blueprint_renderer
except ModuleNotFoundError:
    from tools import blueprint as blueprint_renderer

from .cache import object_hash, read_json, safe_component, write_content_addressed
from .config import (
    DEFAULT_CACHE,
    DEFAULT_DB,
    MAX_DIALOGUE_SAMPLE_CHARS,
    MAX_DIALOGUE_SAMPLE_COUNT,
)
from .database import iter_dialogue_rows, read_db, speaker_rows
from .dialogue import analyze_dialogue, select_representative_samples, split_speaker_label
from .game_data import game_metadata
from .identity import unresolved_id
from .inventory import build_inventory


def _candidate_line_key(row: dict, speaker_by_id: dict, candidates_by_label: dict) -> list[str]:
    keys = []
    associations = [
        (row.get("associated_speaker_id"), row.get("associated_npc_id")),
    ]
    if row.get("speaker_id"):
        associations.append((row.get("speaker_id"), row.get("npc_id")))
    for voice_id, npc_id in associations:
        if not voice_id:
            continue
        voice_id = str(voice_id)
        speaker = speaker_by_id.get(voice_id, {})
        if speaker.get("has_scd") or row.get("has_scd"):
            continue
        possible = candidates_by_label.get(voice_id, [])
        for candidate in possible:
            candidate_npc = candidate.get("npc_id")
            resolved_npc = npc_id
            if resolved_npc is None:
                resolved_npc = speaker.get("npc_id")
            if candidate_npc is None:
                if resolved_npc is None and candidate["canonical_id"] == unresolved_id(voice_id):
                    keys.append(candidate["canonical_id"])
            elif resolved_npc is not None and int(resolved_npc) == int(candidate_npc):
                keys.append(candidate["canonical_id"])
            elif candidate_npc is not None and not candidates_by_label.get(voice_id, []):
                continue
    return sorted(set(keys))


def _merge_line(target: dict[str, dict], row: dict, voice_id: str) -> None:
    dialog_id = str(row["id"])
    existing = target.get(dialog_id)
    provenance = {
        "dialog_id": dialog_id,
        "voice_line_id": row.get("voice_line_id"),
        "source": row["source"],
        "sheet": row["sheet"],
        "row_id": row["row_id"],
        "speaker_id": voice_id,
        "npc_id": row.get("associated_npc_id") or row.get("npc_id"),
    }
    if existing is None:
        text, speaker_label = split_speaker_label(row.get("text_clean") or "")
        line = {
            "dialog_id": dialog_id,
            "voice_line_id": row.get("voice_line_id"),
            "source": row["source"],
            "sheet": row["sheet"],
            "row_id": row["row_id"],
            "text": text,
            "text_kind": row["text_kind"],
            "speaker_ids": [voice_id],
            "provenance": [provenance],
        }
        if speaker_label:
            line["speaker_label"] = speaker_label
        target[dialog_id] = line
        return
    if voice_id not in existing["speaker_ids"]:
        existing["speaker_ids"].append(voice_id)
    if provenance not in existing["provenance"]:
        existing["provenance"].append(provenance)


def build_dossiers(
    db: Path | str = DEFAULT_DB,
    *,
    out: Path | str = DEFAULT_CACHE,
    lang: str = "en",
    game_index: dict[int, dict] | None = None,
    only: set[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
    sample_chars: int = MAX_DIALOGUE_SAMPLE_CHARS,
    sample_count: int = MAX_DIALOGUE_SAMPLE_COUNT,
) -> list[dict]:
    vocabulary = blueprint_renderer.load_vocabulary()
    inventory = build_inventory(db, lang=lang, game_index=game_index)
    candidates = [
        candidate for candidate in inventory["candidates"]
        if candidate["identity_status"] != "excluded"
    ]
    candidates.sort(key=lambda candidate: candidate["canonical_id"])
    if only:
        candidates = [
            candidate for candidate in candidates
            if candidate["canonical_id"] in only
            or candidate["name"] in only
            or any(alias in only for alias in candidate["speaker_ids"])
        ]
    candidates = candidates[offset: offset + limit if limit is not None else None]
    candidates_by_label = defaultdict(list)
    for candidate in candidates:
        for voice_id in candidate["speaker_ids"]:
            candidates_by_label[voice_id].append(candidate)

    line_rows: dict[str, dict[str, dict]] = defaultdict(dict)
    with read_db(db) as connection:
        speakers = speaker_rows(connection)
        speaker_by_id = {str(row["voice_id"]): row for row in speakers}
        query_ids = set(candidates_by_label)
        for row in iter_dialogue_rows(connection, lang, speaker_ids=query_ids):
            if row.get("has_scd"):
                continue
            keys = _candidate_line_key(row, speaker_by_id, candidates_by_label)
            if not keys:
                continue
            voice_id = str(row.get("associated_speaker_id") or row.get("speaker_id") or "")
            for key in keys:
                _merge_line(line_rows[key], row, voice_id)

    output = []
    out_dir = Path(out) / "dossiers"
    for candidate in candidates:
        key = candidate["canonical_id"]
        lines = list(line_rows.get(key, {}).values())
        lines.sort(key=lambda line: (
            str(line.get("source") or ""),
            str(line.get("sheet") or ""),
            int(line.get("row_id") or 0),
            str(line.get("voice_line_id") or ""),
        ))
        unique_line_ids = {
            str(line["voice_line_id"])
            for line in lines if line.get("voice_line_id")
        }
        quest_rows = {
            (line.get("source"), line.get("sheet"))
            for line in lines if line.get("source") in {"quest", "custom", "raid"}
        }
        source_counts = defaultdict(lambda: {"rows": 0, "unique_line_ids": set()})
        for line in lines:
            source = str(line.get("source") or "unknown")
            source_counts[source]["rows"] += 1
            if line.get("voice_line_id"):
                source_counts[source]["unique_line_ids"].add(line["voice_line_id"])

        samples = select_representative_samples(
            key, lines, max_count=sample_count, max_chars=sample_chars,
            vocabulary=vocabulary,
        )
        for sample in samples:
            sample.pop("_rank", None)
            sample.pop("_bucket", None)
        dialogue_analysis = analyze_dialogue(lines, vocabulary)
        if candidate.get("npc_id") is None:
            metadata = {
                "race": None, "clan": None, "sex": "unknown",
                "race_id": None, "tribe_id": None, "body_type": None, "height": None,
            }
        else:
            metadata = game_metadata(candidate["npc_id"], game_index or {})
        identity = {
            "canonical_id": key,
            "npc_id": candidate.get("npc_id"),
            "name": candidate["name"],
            "aliases": candidate.get("aliases", []),
            "speaker_ids": candidate.get("speaker_ids", []),
            "normalized_names": candidate.get("normalized_names", []),
            "race_id": candidate.get("race_id") or metadata.get("race_id"),
            "race": candidate.get("race") or metadata.get("race"),
            "tribe_id": candidate.get("tribe_id") or metadata.get("tribe_id"),
            "clan": candidate.get("clan") or metadata.get("clan"),
            "sex": candidate.get("sex") or metadata.get("sex", "unknown"),
            "body_type": candidate.get("body_type") or metadata.get("body_type"),
            "height": candidate.get("height") or metadata.get("height"),
            "resolution_status": candidate["identity_status"],
            "has_se_voice_reference": candidate["has_se_reference"],
            "identity_ambiguous": candidate["identity_ambiguous"],
            "review_reasons": candidate.get("review_reasons", []),
        }
        statistics = {
            "dialogue_count": len(lines),
            "unique_line_count": len(unique_line_ids),
            "quest_count": len(quest_rows),
            "source_distribution": {
                source: {
                    "rows": counts["rows"],
                    "unique_line_ids": len(counts["unique_line_ids"]),
                }
                for source, counts in sorted(source_counts.items())
            },
            "expansion_distribution": candidate.get("expansion_counts", {}),
            "expansion_note": inventory["expansion_note"],
            "race_clan_metadata_available": bool(identity["race"] or identity["clan"]),
        }
        game_source = []
        if candidate.get("npc_id") is not None:
            game_source.append({
                "source_type": "game_data",
                "source": f"ENpcResident/{candidate['npc_id']} and ENpcBase/{candidate['npc_id']}",
                "revision_id": None,
                "fields": ["name", "race", "clan", "sex"],
            })
        dossier = {
            "schema_version": "1.0",
            "identity": identity,
            "statistics": statistics,
            "dialogue_analysis": dialogue_analysis,
            "dialogue_samples": samples,
            "voice_evidence": [],
            "sources": game_source + [
                {
                    "source_type": "dialogue_database",
                    "source": f"dialogue.db:{line.get('source')}/{line.get('sheet')}/{line.get('row_id')}",
                    "revision_id": None,
                    "dialog_id": line.get("dialog_id"),
                    "voice_line_id": line.get("voice_line_id"),
                }
                for line in samples
            ],
            "inventory": {
                "identity_status": candidate["identity_status"],
                "review_reasons": candidate.get("review_reasons", []),
            },
        }
        dossier["input_hash"] = object_hash(dossier)
        path = write_content_addressed(out_dir, safe_component(key), dossier)
        output.append({
            "canonical_id": key,
            "npc_id": candidate.get("npc_id"),
            "name": candidate["name"],
            "identity_status": candidate["identity_status"],
            "dialogue_count": len(lines),
            "unique_line_count": len(unique_line_ids),
            "dossier_hash": dossier["input_hash"],
            "path": str(path),
        })
    return output


def latest_dossiers(cache_dir: Path | str) -> dict[str, tuple[dict, Path]]:
    directory = Path(cache_dir) / "dossiers"
    latest: dict[str, tuple[dict, Path]] = {}
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime_ns):
        try:
            dossier = read_json(path)
        except (OSError, ValueError):
            continue
        canonical_id = (dossier.get("identity") or {}).get("canonical_id")
        if canonical_id:
            latest[str(canonical_id)] = dossier, path
    return latest


def dossier_input_hash(dossier: dict) -> str:
    material = dict(dossier)
    material.pop("input_hash", None)
    return object_hash(material)
