# Progress

Snapshot of `data/dialogue.db` (English, harvest 2026-10-21) after linking the
packs in `data/out/`, on 2026-10-31. Refresh the tables before committing when
the database, generation output, or bucket definitions change:

```bash
python3 tools/progress.py --markdown
```

The roadmap has three phases:

- **Phase 1 — cloned voices.** Speakers with an SE reference; generate their
  unvoiced lines.
- **Phase 2 — synthetic voices.** Speakers with no SE reference; assign a
  designed voice.
- **Phase 3 — speakerless text.** Journal, battle talk, system, content,
  choice, balloon, and unknown rows with no speaker. They have no line ID yet
  and are outside the phase 1/2 totals.

The 248,904 attributed dialogue rows split into three disjoint buckets:

- **Voiced by SE** — the line ships with cutscene SCD audio.
- **Has SE reference, unvoiced** — the speaker label has at least one SE clip,
  but this line does not. These are the clone targets (phase 1).
- **No SE reference** — the speaker label never appears in SE cutscene VO.
  These need a synthetic voice (phase 2).

A **row** is one text line in the dialogue DB. A **line ID** is the pack lookup
key, `md5(lang:voiceId:npcId:sentence)`; repeated sentences with the same
speaker collapse to one ID.

## Corpus

| Bucket | Dialogue rows | Unique line IDs |
|---|---:|---:|
| Voiced by SE | 25,450 | 25,105 |
| Has SE reference, unvoiced | 49,781 | 48,200 |
| No SE reference (synthetic) | 173,673 | 166,559 |
| **Total** | **248,904** | **239,864** |

The totals are the sum of the three planned sets. The database contains
239,867 unique IDs in total; the extra 3 are skipped lines that are not planned.
The phase-1 set is 48,256 raw IDs reduced to 48,200: 53 lines already have a
voiced sentence for the same speaker, and 3 are skipped.

## Characters

| Set | Labels | All rows | SE-voiced | Unvoiced |
|---|---:|---:|---:|---:|
| Has an SE reference | 667 | 75,231 | 25,450 | 49,781 |
| Never appears in SE VO | 9,208 | 173,673 | 0 | 173,673 |

Labels are speaker tokens, not actors. 322 of the 667 referenced labels still
have unvoiced lines; the other 345 are fully voiced by SE.

## Generation progress

| Measure | Done | Total | Percent | Left |
|---|---:|---:|---:|---:|
| Phase 1: cloned | 29,394 | 48,200 | 61.0% | 18,806 |
| Phase 2: synthetic | 0 | 166,559 | 0.0% | 166,559 |
| Project lines (1+2) | 29,394 | 214,759 | 13.7% | 185,365 |
| All dialogue (incl. SE VO) | 54,499 | 239,864 | 22.7% | 185,365 |

- **Phase 1** is the current cloned-voice work. 29,394 unique lines are
  generated (`v_generation_candidates`), 18,806 are queued, and every speaker
  with remaining work has an x-vector.
- **Phase 2** has not started. Synthetic speakers are absent from
  `v_generation_candidates` by design because it requires `has_scd = 1`.
- **Project lines** excludes the 25,105 lines already voiced by SE. It is the
  real remaining workload for the project.
- **All dialogue** treats the SE lines as already audible. It is useful for
  “how much of all spoken dialogue is available”, not for project completion.

Phase 3 is tracked separately below in rows; those rows have no line IDs and
are not part of these percentages.

## Phase 2 scale

| Source | Rows | Unique line IDs |
|---|---:|---:|
| `quest` | 151,710 | 146,852 |
| `custom` | 18,212 | 16,630 |
| `defaulttalk` | 3,155 | 3,121 |
| `content` | 140 | 128 |
| `warp` | 132 | 109 |
| `balloon` | 121 | 118 |
| `transport` | 60 | 49 |
| `npcyell` | 57 | 57 |
| `opening` | 39 | 31 |
| `raid` | 18 | 15 |
| `cutscene` | 15 | 15 |
| `guild_order` | 10 | 8 |
| `leve` | 4 | 4 |

Unique IDs overlap across sources, so only the overall total is additive.

| Lines per label | Labels | Rows | Unique line IDs |
|---|---:|---:|---:|
| 100+ | 335 | 78,112 | 74,487 |
| 10–99 | 2,881 | 73,055 | 70,093 |
| 1–9 | 5,992 | 22,506 | 21,979 |

- Resolved: 4,576 labels / 4,183 distinct NPCs, 131,924 rows.
- Unresolved: 4,632 labels, 41,749 rows.
- 65,011 rows (37.4%) have no race/sex metadata, so a race/gender voice bank
  cannot classify them automatically.
- Only 69 labels / 380 rows share an NPC id or normalized name with a voiced
  label; the rest need a genuinely new voice assignment.

## Phase 3: speakerless text

128,332 rows have no speaker and no `voice_line_id`, so the current pack format
cannot look them up. They are not in the 248,904 dialogue total or the phase
1/2 percentages above. Total DB rows: 377,236 (248,904 dialogue + 128,332
speakerless).

| Kind | Rows |
|---|---:|
| Journal / lore / quest log | 44,191 |
| Battle talk / NPC yells | 18,568 |
| System messages | 17,307 |
| Duty/content text | 11,928 |
| Choice answers | 11,020 |
| Unknown | 10,421 |
| Overhead balloon text | 10,242 |
| Choice prompts | 4,655 |
| **Total** | **128,332** |

| Measure | Done | Total | Percent | Left |
|---|---:|---:|---:|---:|
| Phase 3: speakerless text (rows) | 0 | 128,332 | 0.0% | 128,332 |

Phase 3 is unplanned: no rows are currently addressable by a speaker or line
ID, so this records scope rather than work in progress.

## Details

- Referenced unvoiced IDs: 48,256 raw → 48,200 planned (53 duplicate SE
  sentences, 3 skipped).
- Database unique IDs: 239,867 raw; 239,864 planned.
- Referenced labels with unvoiced lines: 322 of 667.
- Referenced labels with queued lines: 302.

## Caveats

- “Voiced by SE” covers cutscene `VoiceMan` SCDs only. 25,598 clips were
  extracted; 148 are non-verbal, empty, or padding (`<moan>`, `<sigh>`, empty
  text) and are filtered, leaving 25,450 rows. If the game has voiced audio
  outside those sheets, this DB does not see it.
- 128,332 rows with no speaker are outside the dialogue buckets: journal,
  battle talk, system, content, choices, balloon, and unknown. Some are spoken
  text, but the DB has no speaker attribution for them.
- Labels are not unique performers: one character can have several labels, and
  multiple characters can share an actor.
- Generation rates and storage depend on line length. See
  [QWEN3-TTS.md](QWEN3-TTS.md) for the current model and benchmarks.
