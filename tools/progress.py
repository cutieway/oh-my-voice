#!/usr/bin/env python3
"""Corpus and generation progress from ``dialogue.db``.

Three disjoint buckets:

  * SE voiced: the line has a game SCD.
  * Has reference, unvoiced: the speaker has at least one SE clip, but this
    line does not; these are the clone targets (phase 1).
  * No reference: the speaker has no SE clip at all; these need synthetic
    voices (phase 2).

Rows are text rows. Line IDs are the pack lookup keys
(``md5(lang:voiceId:npcId:sentence)``), so repeated sentences collapse.

Usage:

  python3 tools/progress.py
  python3 tools/progress.py --markdown

``--markdown`` prints the tables used by ``docs/progress.md``.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

DIALOG_KINDS = ("dialogue", "narration")
PHASE3_LABELS = {
    "journal": "Journal / lore / quest log",
    "battle_talk": "Battle talk / NPC yells",
    "system": "System messages",
    "content": "Duty/content text",
    "choice_answer": "Choice answers",
    "unknown": "Unknown",
    "balloon": "Overhead balloon text",
    "choice_prompt": "Choice prompts",
}


@dataclass(frozen=True)
class Bucket:
    labels: int
    rows: int
    line_ids: int


@dataclass(frozen=True)
class Phase:
    done: int
    total: int
    left: int

    @property
    def percent(self) -> float:
        return 100.0 * self.done / self.total if self.total else 0.0


@dataclass(frozen=True)
class Details:
    raw_referenced_ids: int
    duplicate_voiced_ids: int
    skipped_ids: int
    raw_total_ids: int
    referenced_with_unvoiced: int
    referenced_with_remaining: int
    unknown_race_rows: int
    shared_identity_labels: int
    shared_identity_rows: int


def connect(db: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_dialogue(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in DIALOG_KINDS)
    return connection.execute(
        f"""
        SELECT d.speaker_id,
               d.voice_line_id,
               d.source,
               d.has_scd,
               d.audio_status,
               d.audio_stale,
               s.has_scd AS speaker_has_scd,
               s.npc_id,
               s.race_id,
               s.normalized_name
        FROM dialogs d
        JOIN speakers s ON s.voice_id = d.speaker_id
        WHERE d.text_kind IN ({placeholders})
        """,
        DIALOG_KINDS,
    ).fetchall()


def bucket(rows: list[sqlite3.Row]) -> Bucket:
    return Bucket(
        labels=len({row["speaker_id"] for row in rows}),
        rows=len(rows),
        line_ids=len({row["voice_line_id"] for row in rows}),
    )


def phase_one(connection: sqlite3.Connection) -> Phase:
    total = connection.execute(
        "SELECT COUNT(*) FROM v_generation_candidates"
    ).fetchone()[0]
    done = connection.execute(
        """
        SELECT COUNT(*) FROM v_generation_candidates
        WHERE audio_status = 'generated' AND audio_stale = 0
        """
    ).fetchone()[0]
    left = connection.execute(
        "SELECT COUNT(*) FROM v_generation_queue"
    ).fetchone()[0]
    return Phase(done=done, total=total, left=left)


def phase_three(connection: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (row["text_kind"], row["rows"])
        for row in connection.execute(
            """
            SELECT text_kind, COUNT(*) AS rows
            FROM dialogs
            WHERE speaker_id IS NULL
            GROUP BY text_kind
            ORDER BY rows DESC, text_kind
            """
        )
    ]


def fmt(value: int) -> str:
    return f"{value:,}"


def pct(value: float) -> str:
    return f"{value:.1f}%"


def markdown(
    se: Bucket,
    referenced: Bucket,
    synthetic: Bucket,
    total: Bucket,
    reference_labels: int,
    phase1: Phase,
    phase2: Phase,
    project: Phase,
    audible: Phase,
    synth_sources: dict[str, tuple[int, int]],
    synth_sizes: list[tuple[str, int, int, int]],
    synth_identity: tuple[int, int, int, int, int],
    details: Details,
    phase3: list[tuple[str, int]],
) -> None:
    print("### Corpus\n")
    print("| Bucket | Dialogue rows | Unique line IDs |")
    print("|---|---:|---:|")
    print(f"| Voiced by SE | {fmt(se.rows)} | {fmt(se.line_ids)} |")
    print(f"| Has SE reference, unvoiced | {fmt(referenced.rows)} | "
          f"{fmt(referenced.line_ids)} |")
    print(f"| No SE reference (synthetic) | {fmt(synthetic.rows)} | "
          f"{fmt(synthetic.line_ids)} |")
    print(f"| **Total** | **{fmt(total.rows)}** | **{fmt(total.line_ids)}** |")

    print("\n### Characters\n")
    print("| Set | Labels | All rows | SE-voiced | Unvoiced |")
    print("|---|---:|---:|---:|---:|")
    print(f"| Has an SE reference | {fmt(reference_labels)} | "
          f"{fmt(se.rows + referenced.rows)} | {fmt(se.rows)} | "
          f"{fmt(referenced.rows)} |")
    print(f"| Never appears in SE VO | {fmt(synthetic.labels)} | "
          f"{fmt(synthetic.rows)} | 0 | {fmt(synthetic.rows)} |")

    print("\n### Generation progress\n")
    print("| Measure | Done | Total | Percent | Left |")
    print("|---|---:|---:|---:|---:|")
    print(f"| Phase 1: cloned | {fmt(phase1.done)} | {fmt(phase1.total)} | "
          f"{pct(phase1.percent)} | {fmt(phase1.left)} |")
    print(f"| Phase 2: synthetic | {fmt(phase2.done)} | {fmt(phase2.total)} | "
          f"{pct(phase2.percent)} | {fmt(phase2.left)} |")
    print(f"| Project lines (1+2) | {fmt(project.done)} | {fmt(project.total)} | "
          f"{pct(project.percent)} | {fmt(project.left)} |")
    print(f"| All dialogue (incl. SE VO) | {fmt(audible.done)} | "
          f"{fmt(audible.total)} | {pct(audible.percent)} | {fmt(audible.left)} |")

    phase3_total = sum(rows for _, rows in phase3)

    print("\n### Phase 2 by source\n")
    print("| Source | Rows | Unique line IDs |")
    print("|---|---:|---:|")
    for source, (rows, line_ids) in synth_sources.items():
        print(f"| `{source}` | {fmt(rows)} | {fmt(line_ids)} |")

    print("\n### Phase 2 by label size\n")
    print("| Lines per label | Labels | Rows | Unique line IDs |")
    print("|---|---:|---:|---:|")
    for name, labels, rows, line_ids in synth_sizes:
        print(f"| {name} | {fmt(labels)} | {fmt(rows)} | {fmt(line_ids)} |")

    resolved_labels, resolved_npcs, resolved_rows, unresolved_labels, unresolved_rows = synth_identity
    print("\n### Phase 2 identity\n")
    print(f"- Resolved: {fmt(resolved_labels)} labels / {fmt(resolved_npcs)} NPCs, "
          f"{fmt(resolved_rows)} rows.")
    print(f"- Unresolved: {fmt(unresolved_labels)} labels, {fmt(unresolved_rows)} rows.")
    print(f"- No race/sex metadata: {fmt(details.unknown_race_rows)} rows.")
    print(f"- Shares an NPC id or normalized name with a voiced label: "
          f"{fmt(details.shared_identity_labels)} labels / "
          f"{fmt(details.shared_identity_rows)} rows.")

    print("\n### Phase 3: speakerless text\n")
    print("These rows have no speaker and no `voice_line_id`, so the current "
          "pack format cannot look them up. They are not in the 248,904 "
          f"dialogue total or the percentages above. Total DB rows: "
          f"{fmt(total.rows + phase3_total)} ({fmt(total.rows)} dialogue + "
          f"{fmt(phase3_total)} speakerless).\n")
    print("| Kind | Rows |")
    print("|---|---:|")
    for kind, rows in phase3:
        print(f"| {PHASE3_LABELS.get(kind, kind)} | {fmt(rows)} |")
    print(f"| **Total** | **{fmt(phase3_total)}** |")
    print("\n| Measure | Done | Total | Percent | Left |")
    print("|---|---:|---:|---:|---:|")
    print(f"| Phase 3: speakerless text (rows) | 0 | {fmt(phase3_total)} | "
          "0.0% | " + f"{fmt(phase3_total)} |")

    print("\n### Details\n")
    print(f"- Referenced unvoiced IDs: {fmt(details.raw_referenced_ids)} raw -> "
          f"{fmt(referenced.line_ids)} planned "
          f"({fmt(details.duplicate_voiced_ids)} duplicate SE sentences, "
          f"{fmt(details.skipped_ids)} skipped).")
    print(f"- Database unique IDs: {fmt(details.raw_total_ids)} raw; "
          f"{fmt(total.line_ids)} planned.")
    print(f"- Referenced labels with unvoiced lines: "
          f"{fmt(details.referenced_with_unvoiced)} of {fmt(reference_labels)}.")
    print(f"- Referenced labels with queued lines: "
          f"{fmt(details.referenced_with_remaining)}.")
    print("- SE clips outside cutscenes are not represented; labels are not actors.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default="data/dialogue.db")
    parser.add_argument("--markdown", action="store_true",
                        help="print the tables used by docs/progress.md")
    args = parser.parse_args()

    if not Path(args.db).is_file():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 1

    connection = connect(args.db)
    try:
        rows = load_dialogue(connection)
        phase1 = phase_one(connection)
        phase3 = phase_three(connection)
        remaining_labels = connection.execute(
            "SELECT COUNT(*) FROM v_speaker_workload WHERE queued > 0"
        ).fetchone()[0]
    except sqlite3.OperationalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    se_rows = [row for row in rows if row["has_scd"]]
    referenced_rows = [row for row in rows
                       if not row["has_scd"] and row["speaker_has_scd"]]
    synthetic_rows = [row for row in rows
                      if not row["has_scd"] and not row["speaker_has_scd"]]

    se = bucket(se_rows)
    referenced_raw = bucket(referenced_rows)
    synthetic = bucket(synthetic_rows)
    # Phase 1's planned set is deduped by voice_line_id and excludes lines
    # whose sentence already exists voiced for that speaker, so use the view.
    referenced = Bucket(referenced_raw.labels, referenced_raw.rows, phase1.total)

    all_labels = len({row["speaker_id"] for row in rows})
    total = Bucket(
        labels=all_labels,
        rows=len(rows),
        # The planned sets are disjoint; 3 skipped IDs exist in the raw union
        # but are deliberately not planned.
        line_ids=se.line_ids + referenced.line_ids + synthetic.line_ids,
    )
    reference_labels = len({row["speaker_id"] for row in se_rows + referenced_rows})

    synthetic_done_ids = {
        row["voice_line_id"] for row in synthetic_rows
        if row["audio_status"] == "generated" and not row["audio_stale"]
    }
    phase2 = Phase(
        done=len(synthetic_done_ids),
        total=synthetic.line_ids,
        left=synthetic.line_ids - len(synthetic_done_ids),
    )
    project_total = phase1.total + phase2.total
    project = Phase(
        done=phase1.done + phase2.done,
        total=project_total,
        left=project_total - phase1.done - phase2.done,
    )
    audible = Phase(
        done=se.line_ids + phase1.done + phase2.done,
        total=total.line_ids,
        left=total.line_ids - se.line_ids - phase1.done - phase2.done,
    )

    source_rows: Counter[str] = Counter()
    source_ids: dict[str, set[str]] = defaultdict(set)
    per_speaker_rows: Counter[str] = Counter()
    per_speaker_ids: dict[str, set[str]] = defaultdict(set)
    for row in synthetic_rows:
        source_rows[row["source"]] += 1
        source_ids[row["source"]].add(row["voice_line_id"])
        per_speaker_rows[row["speaker_id"]] += 1
        per_speaker_ids[row["speaker_id"]].add(row["voice_line_id"])
    synth_sources = {
        source: (count, len(source_ids[source]))
        for source, count in source_rows.most_common()
    }

    groups = {"100+": [], "10-99": [], "1-9": []}
    for speaker, count in per_speaker_rows.items():
        key = "100+" if count >= 100 else "10-99" if count >= 10 else "1-9"
        groups[key].append(speaker)
    synth_sizes = [
        (
            name,
            len(speakers),
            sum(per_speaker_rows[speaker] for speaker in speakers),
            len({line_id for speaker in speakers
                 for line_id in per_speaker_ids[speaker]}),
        )
        for name, speakers in groups.items()
    ]

    speakers = {}
    for row in synthetic_rows:
        speakers[row["speaker_id"]] = (
            row["npc_id"],
            row["race_id"],
            row["normalized_name"],
        )
    resolved = [speaker for speaker, (npc_id, _, _) in speakers.items()
                if npc_id is not None]
    unresolved = [speaker for speaker, (npc_id, _, _) in speakers.items()
                  if npc_id is None]
    synth_identity = (
        len(resolved),
        len({speakers[speaker][0] for speaker in resolved}),
        sum(per_speaker_rows[speaker] for speaker in resolved),
        len(unresolved),
        sum(per_speaker_rows[speaker] for speaker in unresolved),
    )

    voiced_npcs = {
        row["npc_id"] for row in rows
        if row["speaker_has_scd"] and row["npc_id"] is not None
    }
    voiced_names = {
        row["normalized_name"] for row in rows
        if row["speaker_has_scd"] and row["normalized_name"]
    }
    shared_identity = {
        speaker for speaker, (npc_id, _, normalized_name) in speakers.items()
        if npc_id in voiced_npcs or normalized_name in voiced_names
    }
    se_ids = {row["voice_line_id"] for row in se_rows}
    referenced_ids = {row["voice_line_id"] for row in referenced_rows}
    duplicate_voiced_ids = len(referenced_ids & se_ids)
    details = Details(
        raw_referenced_ids=referenced_raw.line_ids,
        duplicate_voiced_ids=duplicate_voiced_ids,
        skipped_ids=referenced_raw.line_ids - phase1.total - duplicate_voiced_ids,
        raw_total_ids=len({row["voice_line_id"] for row in rows}),
        referenced_with_unvoiced=referenced_raw.labels,
        referenced_with_remaining=remaining_labels,
        unknown_race_rows=sum(
            per_speaker_rows[speaker] for speaker, (_, race_id, _) in speakers.items()
            if race_id == 0
        ),
        shared_identity_labels=len(shared_identity),
        shared_identity_rows=sum(per_speaker_rows[speaker]
                                 for speaker in shared_identity),
    )

    if args.markdown:
        markdown(se, referenced, synthetic, total, reference_labels, phase1,
                 phase2, project, audible, synth_sources, synth_sizes,
                 synth_identity, details, phase3)
        return 0

    print(f"Corpus: {args.db} (EN dialogue/narration)")
    print(f"{'Bucket':<28} {'Rows':>9} {'Line IDs':>10}")
    print(f"{'SE voiced':<28} {se.rows:9,} {se.line_ids:10,}")
    print(f"{'Has reference, unvoiced':<28} {referenced.rows:9,} "
          f"{referenced.line_ids:10,}")
    print(f"{'No reference (synthetic)':<28} {synthetic.rows:9,} "
          f"{synthetic.line_ids:10,}")
    print(f"{'Total':<28} {total.rows:9,} {total.line_ids:10,}")
    phase3_total = sum(rows for _, rows in phase3)
    print()
    for name, phase in (
        ("Phase 1 (cloned)", phase1),
        ("Phase 2 (synthetic)", phase2),
        ("Project lines (1+2)", project),
        ("All dialogue", audible),
    ):
        print(f"{name:<22} {phase.done:7,} / {phase.total:7,} "
              f"{pct(phase.percent):>6}  {phase.left:7,} left")
    print(f"{'Phase 3 (rows)':<22} {0:7,} / {phase3_total:7,} "
          f"{pct(0):>6}  {phase3_total:7,} left  (unplanned)")
    for kind, rows in phase3:
        print(f"  {kind:<20} {rows:7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
