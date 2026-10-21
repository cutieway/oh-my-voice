# xivdata — offline FFXIV dialogue harvester

Reads the installed game's `sqpack` through Lumina and writes one SQLite build
database. No Dalamud, no hooking, no network.

The DB is the source of truth for the offline pipeline:

- every text line from dialogue-bearing sheets;
- the speaker token / NPC row / normalized voice id;
- whether the game ships an SCD for that line (`has_scd`);
- the exact sanitized sentence (`text_clean`) and runtime line id
  (`voice_line_id`);
- generation state, so the next step is a queue, not a re-scan.

The runtime plugin never reads this DB. A pack is exported from it later.

## Quick start

```bash
# Full corpus, one DB. This is the normal command.
dotnet run --project tools/xivdata -- \
  --source all \
  --db data/dialogue.db \
  --out data/xivdata \
  --xvectors data/xvectors

# Fast pilot: only cutscene + DefaultTalk
dotnet run --project tools/xivdata -- \
  --source cutscene,defaulttalk \
  --db data/dialogue.db \
  --xvectors data/xvectors

# One quest sheet while iterating
dotnet run --project tools/xivdata -- \
  --source quest --sheet KinGba401 \
  --db /tmp/kin.db

# Extract cutscene SCD/WAV as before (optional)
dotnet run --project tools/xivdata -- \
  --source cutscene --extract --by-token \
  --out data/game-voices \
  --db data/dialogue.db
```

Defaults: `--sqpack ~/Public/xlcore/ffxiv/game/sqpack`,
`--db ./data/dialogue.db`, `--out ./xivdata-out`,
`--xvectors ./data/xvectors`, `--lang en`, `--source cutscene`.

## Sources

| `--source` | Sheet(s) | Notes |
|---|---|---|
| `cutscene` | `cut_scene/*` | Parses `TEXT_VOICEMAN_*`; marks `has_scd`; stores choice/system rows too |
| `defaulttalk` | `DefaultTalk` | Direct `ENpcBase.ENpcData` links; multiple text lines per row |
| `balloon` | `Balloon` | Best-effort `ENpcBase` links |
| `contenttalk` | `ContentTalk` | Content text, not AddonTalk |
| `npcyell` | `NpcYell` | Battle barks; `NpcYell.Name` link when available |
| `instancecontent` | `InstanceContentTextData` | Duty text |
| `publiccontent` | `PublicContentTextData` | Public content text |
| `partycontent` | `PartyContentTextData` | Party content text |
| `massivepc` | `MassivePcContentTextData` | Mass PC text |
| `gimmicktalk` | `GimmickTalk` | Gimmick text |
| `goldsaucertalk` | `GoldSaucerTextData` | Gold Saucer text |
| `quest` | `quest/*` | `TEXT_<sheet>_<speaker>_...`; journal/choice/system classified separately |
| `custom`, `raid`, `content`, `warp`, `transport`, `opening`, `guild_order`, `leve` | same-named families | Scripted NPC talk |
| `scripted` | all scripted families | Expands the eight families above |
| `all` | everything | Full corpus |

`quest/*` and scripted sheets are parsed by key convention, not by Lua
bytecode. That catches the static dialogue keys without a full quest-script
VM. Unresolved speaker tokens remain in the DB with `speaker_match='unresolved'`
so they can be aliased later.

## Database

`dialogs` — one row per text line.

| Column | Meaning |
|---|---|
| `id` | Stable harvest id: `md5(lang,source,sheet,row_id,line_index)` |
| `lang`, `source`, `sheet`, `row_id`, `line_index` | Provenance |
| `text_key` | Game key (e.g. `TEXT_ALPHINAUD_...`), when present |
| `audio_base` | `vo_voiceman_...` base for cutscene SCD lookup |
| `speaker_id` | Voice id: speaker token for cutscene/quest/scripted, normalized name for DefaultTalk |
| `npc_id` | Canonical `ENpcResident` row id, when resolved |
| `speaker_name` | Display name resolved from the NPC sheet |
| `npc_key` | Runtime-cleaned speaker key used to look up `manifest.Npcs` |
| `speaker_match` | `token`, `token_ambiguous`, `direct`, `direct_ambiguous`, `unresolved`, `none` |
| `text_raw` | `ExtractText()` result |
| `text_macro` | `ToMacroString()` result; keeps player-name macros for debugging |
| `text_clean` | Sanitizer v1 output; this is the runtime `sentence` |
| `text_hash` | SHA-256 of `text_clean`; used for stale detection |
| `voice_line_id` | `md5(lang:voice_id:npc_id:text_clean)`, the pack lookup key |
| `text_kind` | `dialogue`, `narration`, `choice_prompt`, `choice_answer`, `system`, `journal`, `battle_talk`, `content`, `unknown` |
| `has_scd` | 1 when a real game SCD exists for this line |
| `scd_path`, `scd_bytes`, `scd_lang` | Game audio location/size when present |
| `audio_status` | `covered`, `pending`, `unassigned`, `generated`, `failed`, `skipped` |
| `audio_path`, `duration_ms`, `generation_profile`, `error` | Generation result |
| `audio_stale` | 1 when text/speaker changed after generation |
| `notes` | User-editable review note |

`dialog_speakers` — all NPC links for a line (a DefaultTalk row can be shared
by several NPCs), with `is_primary`.

`speakers` — per voice id: canonical NPC, `npc_key` (runtime-cleaned speaker),
race/gender, derived line counts, `has_scd` (any game audio for this voice),
and `xvector_path`/`xvector_sha256` when `data/xvectors/<voice_id>.npy` exists.

`harvest_runs` — provenance and per-run counts.

Views:

- `v_speakers` — compact speaker inventory.
- `v_generation_queue` — unvoiced `dialogue`/`narration` lines for speakers
  that have at least one SCD and a resolved voice; includes stale/failed rows.
  One row per `(lang, voice_line_id)`, and any line whose id also exists as a
  `has_scd=1` cutscene line is excluded.
- `v_pack_npcs` — `npc_key -> NpcId/VoiceId` mapping ready for a pack
  manifest.
- `v_speaker_workload` — queued line count per speaker.
- `v_speaker_workload` — queued line count per speaker (deduped).
- `v_integrity` — zero-row checks for NPC/line-id/key invariants; must show
  `failures=0` for every check.

`dialogs_fts` — FTS5 index over `text_clean` (if the SQLite build supports
FTS5). It is kept in sync by triggers, including manual edits.

### Status semantics

- `covered`: `has_scd=1`; the game already has audio, so this line is not in
  the generation queue.
- `pending`: `has_scd=0`, speaker resolved, text is dialogue/narration.
- `unassigned`: no speaker, or text kind is choice/journal/system/content.
- `generated`, `failed`, `skipped`: generation outcomes.
- `audio_stale=1`: source text or speaker changed after generation. The queue
  includes stale rows even when their old status is `generated`.
- Text with no letters (for example `0`, `...`, `\`) is treated as padding
  and is not stored as dialogue. Square Enix development placeholders such
  as `（★未使用／削除予定★）` are filtered by the `★` + `未使用` marker.

`has_scd=1` means “use the game audio / clone reference”, not “generate a
replacement”. The per-line audio decision is explicit in `audio_status`.

### Queries

```sql
-- Pick a voiced character with an x-vector.
SELECT voice_id, name, voiced_lines, unvoiced_lines, dialogue_lines, xvector_path
FROM v_speakers
WHERE has_scd = 1
ORDER BY voiced_lines DESC;

-- Everything that still needs generation for one character.
SELECT source, sheet, row_id, sentence, audio_status, audio_stale
FROM v_generation_queue
WHERE speaker_id = 'alphinaud'
ORDER BY source, sheet, row_id;

-- Integrity: every row must be zero.
SELECT * FROM v_integrity;

-- Who has the largest backlog?
SELECT voice_id, name, queued, xvector_path
FROM v_speaker_workload
WHERE queued > 0
ORDER BY queued DESC;

-- Manifest NPC map for a voice.
SELECT speaker, npc_id, voice_id
FROM v_pack_npcs
WHERE voice_id = 'alphinaud';

-- Coverage by source.
SELECT source, text_kind, COUNT(*) AS lines,
       SUM(has_scd) AS with_scd
FROM dialogs
GROUP BY source, text_kind
ORDER BY source, text_kind;

-- Full-text search.
SELECT d.source, d.sheet, d.row_id, d.speaker_id, d.text_clean
FROM dialogs_fts f
JOIN dialogs d ON d.rowid = f.rowid
WHERE f.dialogs_fts MATCH 'Steelhog'
LIMIT 20;
```

Mark a generated line:

```sql
UPDATE dialogs
SET audio_status = 'generated',
    audio_path = ?,
    duration_ms = ?,
    generation_profile = ?,
    audio_stale = 0,
    error = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE id = ?;
```

Mark a review skip:

```sql
UPDATE dialogs
SET audio_status = 'skipped',
    notes = ?,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE id = ?;
```

### Editing while the game is running

The DB is opened in WAL mode with `busy_timeout=5000` and
`synchronous=NORMAL`, so DB Browser/SQLite CLI can read and edit while a
harvest or the game is running. Keep writes short. Re-running a harvest
preserves `audio_status`, `audio_path`, `generation_profile`, `notes`, and
`error`; changed text/speaker sets `audio_stale=1` instead of silently
overwriting a finished line.

## Cutscene SCD / audio extraction

`--extract` still writes:

```
dialogue.csv                              # cutscene rows with SCD only
scd/<NPC name>/<audio base>.scd
wav/<NPC name>/<audio base>.wav
scd/<speaker_token>/...  with --by-token
wav/<speaker_token>/...  with --by-token
```

`vgmstream-cli` is required for WAV. It resamples nothing; the x-vector
builder handles 44.1 kHz -> 24 kHz.

## How text and IDs are produced

- `ToMacroString()` is normalized: `<string(gstr1)>` and
  `<split(<string(gstr1)>, ,1)>` become `Forename`, `<string(gstr2)>` becomes
  `Surname`, other macros are stripped. `Sanitizer.Clean(..., "Forename",
  "Surname")` then produces `_FIRSTNAME_`/`_LASTNAME_`. This matches the
  plugin's runtime sanitizer for player-name lines.
- `OhMyVoice.Common.Sanitizer` is referenced directly; the DB cannot drift
  from the plugin's v1 behavior.
- `voice_line_id` uses the same `VoiceId.ComputeId` function as the plugin.
  The id uses the canonical numeric `npc_id`; `npc_key` is the separate
  runtime-cleaned speaker key used to look up `manifest.Npcs`.
- A character has many `ENpcResident` instances (Alphinaud has 798). The DB
  canonicalizes each voice to the lowest row with usable race/gender, the
  same rule as `NpcMetadataService.Choose`, and uses that one `npc_id` for
  every line. Per-spawn ids would make identical Y'shtola text hash to
  different line ids. `v_integrity` checks this invariant.
- `tools/qwen/generate.py` keys `manifest.Npcs` by the cleaned speaker name
  (`Speaker`), with `NpcId` set to the numeric id used in the hash. Keying by
  the numeric id alone made `PackStore.ResolveNpc(cleanedSpeaker)` miss every
  line.

## Build

The project references the Lumina assemblies from Dalamud:

```bash
DalamudDir=~/.xlcore/dalamud/Hooks/dev dotnet build tools/xivdata
```

Requires .NET 10. On this machine both are in the NixOS home packages
(`dotnet-sdk_10`, `vgmstream`).

## Measured on game version 2026.10.16

Full `--source all` run (EN), 3m58s on this machine:

| Measure | Value |
|---|---:|
| rows in `dialogs` | 377,236 |
| `text_kind='dialogue'` | 248,902 |
| `has_scd=1` | 25,450 |
| `has_scd=0` | 351,786 |
| resolved speakers | 201,276 |
| unresolved speakers | 47,628 |
| speakers | 9,955 |
| voiced speakers (`has_scd=1`) | 667 |
| voiced + x-vector linked | 667 |
| `v_generation_queue` (deduped) | 48,203 |
| DB size (with FTS) | 505 MB |

Main sources: `quest` 264,971; `custom` 28,812; `cutscene` 26,707;
`npcyell` 18,625; `defaulttalk` 14,471; `balloon` 10,365;
`instancecontent` 10,169; `contenttalk` 346; `raid` 389; `content` 300;
`warp` 255; `transport` 247; `gimmicktalk` 67; `opening` 39;
`guild_order` 38; `leve` 89.

`_NONE_VOICE` is mostly player answers/prompts/system text, not hidden NPC
dialogue: the cutscene pass measures 776 choice answers, 340 choice prompts,
79 system lines, and 2 narration lines in addition to the normal dialogue.
Do not chase a `.cutb` parser for those.

## Limitations

- Static speaker attribution is key/ref based, not a Lua VM. Lines whose
  speaker only exists in quest bytecode may remain `unresolved`.
- `DefaultTalk` uses direct `ENpcData` links; shop/warp/FateShop multi-hop
  chains are not ported yet.
- `Balloon`, `ContentTalk`, and `NpcYell` are best-effort; they are not the
  P1 AddonTalk path.
- Unresolved tokens remain queryable and can be aliased in DB or a later
  alias table.
- `--source all` is a full corpus harvest, not a per-patch incremental merge;
  it upserts by stable row identity and preserves generation state.
