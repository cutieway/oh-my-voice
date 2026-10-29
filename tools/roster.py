#!/usr/bin/env python3
"""Per-speaker generation workload, read live from ``dialogue.db``.

There is no separate progress file: ``v_generation_queue`` derives what is left
from ``dialogs.audio_status``, which ``tools/qwen/link_audio.py`` marks per
generated line. New lines from a later patch arrive as ``pending`` and show up
here automatically; lines whose text changed stay ``audio_stale`` and are
requeued.

Commands:

  next    run order for ``./voice.sh`` with no arguments: speakers that still
          have queued lines and a usable x-vector, most lines first
  table   the same data as CSV, for ``docs/workload.csv`` (informational only;
          nothing reads the CSV back)
"""
from __future__ import annotations

import argparse
import csv
import signal
import sqlite3
import sys
from pathlib import Path

WORKLOAD_SQL = """
SELECT s.voice_id,
       s.npc_id,
       s.name,
       s.voiced_lines,
       s.unvoiced_lines,
       s.dialogue_lines,
       s.generated_lines,
       COALESCE(w.queued, 0) AS remaining,
       s.xvector_path
FROM speakers s
LEFT JOIN v_speaker_workload w ON w.voice_id = s.voice_id
WHERE s.has_scd = 1
ORDER BY remaining DESC, s.voiced_lines DESC, s.name COLLATE NOCASE
"""


def connect(db: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def xvector_path(row: sqlite3.Row, xvector_dir: str | None) -> Path | None:
    stored = row["xvector_path"]
    candidates = [Path(stored)] if stored else []
    if xvector_dir:
        candidates.append(Path(xvector_dir) / f"{row['voice_id']}.npy")
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0] if candidates else None


def has_xvector(row: sqlite3.Row, xvector_dir: str | None) -> bool:
    path = xvector_path(row, xvector_dir)
    return bool(path and path.is_file())


def load_rows(db: str) -> list[sqlite3.Row]:
    connection = connect(db)
    try:
        return connection.execute(WORKLOAD_SQL).fetchall()
    finally:
        connection.close()


def command_next(args) -> int:
    rows = [row for row in load_rows(args.db)
            if row["remaining"] > 0 and has_xvector(row, args.xvector_dir)]
    for row in rows:
        print(f"{row['voice_id']}\t{row['name']}\t{row['remaining']}")
    return 0


def command_table(args) -> int:
    rows = load_rows(args.db)
    out = sys.stdout if args.out in (None, "-") else open(args.out, "w", newline="")
    try:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["voice_id", "npc_id", "name", "voiced", "unvoiced",
                         "total", "generated", "remaining", "xvector"])
        for row in rows:
            writer.writerow([
                row["voice_id"],
                row["npc_id"] if row["npc_id"] is not None else "",
                row["name"],
                row["voiced_lines"],
                row["unvoiced_lines"],
                row["dialogue_lines"],
                row["generated_lines"],
                row["remaining"],
                "yes" if has_xvector(row, args.xvector_dir) else "no",
            ])
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/dialogue.db")
    parser.add_argument("--xvector-dir", default="data/xvectors",
                        help="fall back to <dir>/<voice_id>.npy when the DB has no path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("next", help="run order: queued lines first, most first")

    table = sub.add_parser("table", help="write the informational CSV")
    table.add_argument("--out", default="-", help="output path (default: stdout)")

    args = parser.parse_args()
    if args.command == "next":
        return command_next(args)
    return command_table(args)


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main())
