#!/usr/bin/env python3
"""Link generated OGG files into dialogue.db and record what is done.

The generator writes the truth (manifest + files); this host-side script only
mirrors it into SQLite. It never deletes rows. Re-run it after a batch;
existing (voice_line_id, pronoun) rows are replaced with the current manifest
values.

Two tables are updated:

- ``audio_variants``: one row per rendered file.
- ``dialogs``: a line whose root file exists *and* whose stored text still
  matches the manifest sentence becomes ``audio_status='generated'`` with
  ``audio_stale=0``. That is what drains ``v_generation_queue``, so a later run
  (including after a patch adds lines) only exports what is still missing.
  A line whose text changed keeps its old status; the harvest sets
  ``audio_stale=1``, which keeps it queued until it is rendered again.

The root (they/them) rendering is stored under ``they`` and the third-person
variants under ``she`` and ``he``. Manifests with any other schema are skipped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_variants (
  voice_line_id TEXT NOT NULL,
  pronoun TEXT NOT NULL CHECK (pronoun IN ('none','they','she','he')),
  audio_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL,
  generation_profile TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (voice_line_id, pronoun)
);
CREATE INDEX IF NOT EXISTS idx_audio_variants_path
  ON audio_variants(audio_path);
"""


def plain_file(name: str) -> bool:
    path = Path(name)
    return bool(name) and name not in {".", ".."} and path.name == name


def relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def manifest_paths(args) -> list[Path]:
    if args.manifest:
        return [Path(path) for path in args.manifest]
    out_root = Path(args.out_root)
    paths = sorted(out_root.glob("*/manifest.json"))
    if args.voice:
        paths = [path for path in paths if path.parent.name == args.voice]
    return paths


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _backup_table_name(connection: sqlite3.Connection) -> str:
    index = 1
    while True:
        name = ("audio_variants_schema1" if index == 1
                else f"audio_variants_schema1_{index}")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
        if not exists:
            return name
        index += 1


def migrate_audio_variants(connection: sqlite3.Connection) -> bool:
    """Rename the old gender-keyed table and copy its rows to pronouns.

    The old table is retained as ``audio_variants_schema1`` so a migration is
    reversible without deleting history. Returns True when a migration ran.
    """
    columns = _table_columns(connection, "audio_variants")
    if not columns or "pronoun" in columns:
        return False
    if "gender" not in columns:
        raise ValueError("audio_variants has neither pronoun nor gender")

    backup = _backup_table_name(connection)
    connection.execute(f'ALTER TABLE audio_variants RENAME TO "{backup}"')
    connection.execute("DROP INDEX IF EXISTS idx_audio_variants_path")
    connection.executescript(SCHEMA)
    connection.execute(
        f"""
        INSERT INTO audio_variants
          (voice_line_id, pronoun, audio_path, bytes, duration_ms,
           generation_profile, updated_at)
        SELECT voice_line_id,
               CASE gender WHEN 'male' THEN 'he'
                           WHEN 'female' THEN 'she'
                           ELSE 'none' END,
               audio_path, bytes, duration_ms, generation_profile, updated_at
        FROM "{backup}"
        """)
    connection.commit()
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    return True


def mark_generated(connection: sqlite3.Connection, roots: dict, now: str):
    """Record root renderings as generated lines in ``dialogs``.

    ``roots`` maps voice_line_id -> (lang, sentence, audio_path, duration_ms,
    generation_profile). A row flips to ``generated`` only when its stored text
    equals the manifest sentence, so a line whose game text changed keeps its
    old status and stays queued through ``audio_stale``. Returns
    (marked, changed, excluded, unknown): markable rows whose text differs,
    rows the view would never mark (covered, skipped, non-dialogue), and ids
    the DB does not have at all.
    """
    if not _table_columns(connection, "dialogs"):
        return (0, 0, 0, 0)
    update = """
        UPDATE dialogs SET
          audio_status = 'generated',
          audio_stale = 0,
          audio_path = ?,
          duration_ms = ?,
          generation_profile = ?,
          updated_at = ?
        WHERE lang = ?
          AND voice_line_id = ?
          AND has_scd = 0
          AND speaker_id IS NOT NULL
          AND text_kind IN ('dialogue','narration')
          AND text_clean = ?
    """
    marked = changed = excluded = unknown = 0
    for line_id, (lang, sentence, path, duration, profile) in roots.items():
        cursor = connection.execute(
            update, (path, duration, profile, now, lang, line_id, sentence))
        if cursor.rowcount:
            marked += 1
            continue
        row = connection.execute(
            "SELECT text_clean FROM dialogs WHERE lang = ? AND voice_line_id = ? "
            "AND has_scd = 0 AND speaker_id IS NOT NULL "
            "AND text_kind IN ('dialogue','narration') LIMIT 1",
            (lang, line_id)).fetchone()
        if row is not None:
            changed += 1
            continue
        exists = connection.execute(
            "SELECT 1 FROM dialogs WHERE lang = ? AND voice_line_id = ? LIMIT 1",
            (lang, line_id)).fetchone()
        if exists is not None:
            excluded += 1
        else:
            unknown += 1
    return (marked, changed, excluded, unknown)


def refresh_generated_counts(connection: sqlite3.Connection, now: str) -> int:
    """Recompute ``speakers.generated_lines`` from the dialogs statuses."""
    if not _table_columns(connection, "speakers"):
        return 0
    cursor = connection.execute(
        """
        UPDATE speakers SET
          generated_lines = (
            SELECT COUNT(*) FROM dialogs d
            WHERE d.speaker_id = speakers.voice_id
              AND d.audio_status = 'generated'
          ),
          updated_at = ?
        WHERE generated_lines <> (
            SELECT COUNT(*) FROM dialogs d
            WHERE d.speaker_id = speakers.voice_id
              AND d.audio_status = 'generated'
          )
        """,
        (now,),
    )
    return cursor.rowcount


def line_rows(manifest_path: Path, repo_root: Path):
    pack_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    lines = manifest.get("Voicelines")
    if not isinstance(lines, dict):
        raise ValueError(f"{manifest_path}: missing Voicelines object")
    schema = manifest.get("Schema")
    if schema != 2:
        raise ValueError(f"unsupported manifest schema {schema!r} (expected 2)")

    lang = manifest.get("Language") or "en"

    for line_id, entry in lines.items():
        if not isinstance(entry, dict):
            continue
        variants = entry.get("Variants")
        if not isinstance(variants, dict) or not variants:
            audio_path = pack_dir / "voicelines" / f"{line_id}.ogg"
            yield (line_id, "none", audio_path, entry, lang,
                   entry.get("GenerationFingerprint"))
            continue

        yield (line_id, "they",
               pack_dir / "voicelines" / f"{line_id}.ogg",
               entry, lang, entry.get("GenerationFingerprint"))
        for key, pronoun in (("she", "she"), ("he", "he")):
            variant = variants.get(key)
            if not isinstance(variant, dict):
                continue
            file_name = str(variant.get("File", ""))
            if not plain_file(file_name):
                print(f"skip {line_id} {pronoun}: unsafe file {file_name!r}",
                      file=sys.stderr)
                continue
            audio_path = pack_dir / "voicelines" / file_name
            yield (line_id, pronoun, audio_path, variant, lang,
                   variant.get("GenerationFingerprint",
                               entry.get("GenerationFingerprint")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-root", default="data/out",
                        help="directory containing <voice>/manifest.json")
    parser.add_argument("--manifest", action="append",
                        help="specific manifest.json (repeatable); overrides --out-root")
    parser.add_argument("--voice", help="only this pack directory name")
    parser.add_argument("--repo-root", default=".",
                        help="paths are stored relative to this directory when possible")
    parser.add_argument("--no-status", action="store_true",
                        help="only fill audio_variants; leave dialogs.audio_status alone")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    paths = manifest_paths(args)
    if not paths:
        print("no manifests found", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    roots = {}
    missing = 0
    for manifest_path in paths:
        try:
            for line_id, pronoun, audio_path, meta, lang, profile in line_rows(
                    manifest_path, repo_root):
                if not audio_path.is_file():
                    print(f"missing audio: {audio_path}", file=sys.stderr)
                    missing += 1
                    continue
                size = audio_path.stat().st_size
                declared = int(meta.get("Bytes") or 0)
                if declared and declared != size:
                    print(f"size mismatch: {audio_path} manifest={declared} disk={size}",
                          file=sys.stderr)
                duration = float(meta.get("DurationSeconds") or 0)
                path = relative(audio_path, repo_root)
                rows.append((
                    line_id, pronoun, path, size,
                    int(round(duration * 1000)), profile, now,
                ))
                sentence = meta.get("Sentence")
                if pronoun in ("none", "they") and isinstance(sentence, str) and sentence:
                    roots[line_id] = (lang, sentence, path,
                                      int(round(duration * 1000)), profile)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"skip {manifest_path}: {exc}", file=sys.stderr)

    print(f"{len(rows)} row(s) from {len(paths)} manifest(s); {missing} missing file(s)")
    if args.dry_run:
        for row in rows[:5]:
            print("  ", row)
        return 0

    connection = sqlite3.connect(args.db)
    try:
        if migrate_audio_variants(connection):
            print("migrated audio_variants from gender to pronoun", file=sys.stderr)
        connection.executescript(SCHEMA)
        connection.executemany(
            """
            INSERT INTO audio_variants
              (voice_line_id, pronoun, audio_path, bytes, duration_ms,
               generation_profile, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(voice_line_id, pronoun) DO UPDATE SET
              audio_path = excluded.audio_path,
              bytes = excluded.bytes,
              duration_ms = excluded.duration_ms,
              generation_profile = excluded.generation_profile,
              updated_at = excluded.updated_at
            """,
            rows,
        )
        marked = changed = excluded = unknown = refreshed = 0
        if not args.no_status:
            marked, changed, excluded, unknown = mark_generated(
                connection, roots, now)
            refreshed = refresh_generated_counts(connection, now)
        connection.commit()
    finally:
        connection.close()
    print(f"linked {len(rows)} row(s) into {args.db}")
    if not args.no_status:
        print(f"marked {marked} line(s) generated, {changed} kept pending "
              f"(text changed), {excluded} not markable (covered/skipped/"
              f"non-dialogue), {unknown} not in the DB; "
              f"refreshed {refreshed} speaker count(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
