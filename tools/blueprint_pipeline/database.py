from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import LANGUAGE

DIALOGUE_KINDS = ("dialogue", "narration")


def connect_readonly(path: pathlib.Path | str) -> sqlite3.Connection:
    resolved = pathlib.Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"dialogue database not found: {resolved}")
    connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(connection, table):
        return set()
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def require_dialogue_schema(connection: sqlite3.Connection) -> None:
    missing = []
    for table, required in {
        "speakers": {"voice_id", "npc_id", "name", "normalized_name", "has_scd"},
        "dialogs": {
            "id", "lang", "speaker_id", "npc_id", "source", "sheet", "row_id",
            "text_clean", "text_kind", "has_scd", "voice_line_id",
        },
    }.items():
        absent = required - columns(connection, table)
        if absent:
            missing.append(f"{table}: {', '.join(sorted(absent))}")
    if missing:
        raise RuntimeError("unsupported dialogue.db schema; missing " + "; ".join(missing))


@contextmanager
def read_db(path: pathlib.Path | str) -> Iterator[sqlite3.Connection]:
    connection = connect_readonly(path)
    try:
        require_dialogue_schema(connection)
        yield connection
    finally:
        connection.close()


def speaker_rows(connection: sqlite3.Connection) -> list[dict]:
    fields = columns(connection, "speakers")
    wanted = [
        "voice_id", "npc_id", "name", "npc_key", "normalized_name", "race_id",
        "gender", "speaker_match", "has_scd", "voiced_lines", "unvoiced_lines",
        "dialogue_lines",
    ]
    selected = [field for field in wanted if field in fields]
    return [dict(row) for row in connection.execute(
        f"SELECT {', '.join(selected)} FROM speakers ORDER BY voice_id"
    )]


def association_rows(connection: sqlite3.Connection, lang: str = LANGUAGE) -> list[dict]:
    if not table_exists(connection, "dialog_speakers"):
        return []
    fields = columns(connection, "dialog_speakers")
    if not {"speaker_id", "npc_id", "dialog_id"} <= fields:
        return []
    match_source = "ds.match_source" if "match_source" in fields else "NULL AS match_source"
    is_primary = "ds.is_primary" if "is_primary" in fields else "0 AS is_primary"
    return [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT ds.speaker_id, ds.npc_id, {match_source}, {is_primary}
            FROM dialog_speakers ds
            JOIN dialogs d ON d.id = ds.dialog_id
            WHERE d.lang = ?
              AND d.text_kind IN ('dialogue','narration')
            ORDER BY ds.speaker_id, ds.npc_id, ds.dialog_id
            """,
            (lang,),
        )
    ]


def iter_dialogue_rows(
    connection: sqlite3.Connection,
    lang: str = LANGUAGE,
    *,
    speaker_ids: set[str] | None = None,
) -> Iterator[dict]:
    dialog_fields = columns(connection, "dialogs")
    association_fields = columns(connection, "dialog_speakers")
    has_associations = {"dialog_id", "speaker_id"} <= association_fields
    association_join = (
        "LEFT JOIN dialog_speakers ds ON ds.dialog_id = d.id"
        if has_associations else ""
    )
    def dialog_field(name: str) -> str:
        return f"d.{name}" if name in dialog_fields else f"NULL AS {name}"

    if not has_associations:
        association_fields = set()
    def association_field(name: str, fallback: str = "NULL") -> str:
        return f"ds.{name} AS associated_{name}" if name in association_fields else f"{fallback} AS associated_{name}"

    args: list[object] = [lang]
    sql = f"""
        SELECT d.id, d.lang, {dialog_field('source')}, {dialog_field('sheet')},
               {dialog_field('row_id')}, {dialog_field('speaker_id')},
               {dialog_field('npc_id')}, {dialog_field('speaker_name')},
               {dialog_field('npc_key')}, {dialog_field('speaker_match')},
               {dialog_field('text_clean')}, {dialog_field('text_kind')},
               {dialog_field('has_scd')}, {dialog_field('voice_line_id')},
               {association_field('speaker_id')}, {association_field('npc_id')},
               {association_field('match_source')}, {association_field('is_primary', '0')}
        FROM dialogs d
        {association_join}
        WHERE d.lang = ?
          AND d.text_kind IN ('dialogue','narration')
    """
    if speaker_ids is not None:
        if not speaker_ids:
            return
        placeholders = ",".join("?" for _ in speaker_ids)
        ids = sorted(speaker_ids)
        if has_associations:
            sql += f" AND (d.speaker_id IN ({placeholders}) OR ds.speaker_id IN ({placeholders}))"
        else:
            sql += f" AND d.speaker_id IN ({placeholders})"
        args.extend(ids)
        if has_associations:
            args.extend(ids)
    order = " ORDER BY d.id"
    if has_associations and "is_primary" in association_fields:
        order += ", ds.is_primary DESC"
    if has_associations and "speaker_id" in association_fields:
        order += ", ds.speaker_id"
    sql += order
    for row in connection.execute(sql, args):
        yield dict(row)


def actual_dialogue_counts(connection: sqlite3.Connection, lang: str = LANGUAGE) -> dict:
    return {
        "dialogue_rows": connection.execute(
            "SELECT COUNT(*) FROM dialogs WHERE lang=? AND text_kind IN ('dialogue','narration')",
            (lang,),
        ).fetchone()[0],
        "voice_line_ids": connection.execute(
            """
            SELECT COUNT(DISTINCT voice_line_id) FROM dialogs
            WHERE lang=? AND text_kind IN ('dialogue','narration')
              AND voice_line_id IS NOT NULL
            """,
            (lang,),
        ).fetchone()[0],
        "speaker_labels": connection.execute(
            "SELECT COUNT(*) FROM speakers WHERE has_scd=0"
        ).fetchone()[0],
    }
