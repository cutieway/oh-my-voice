# FFXIV voice roster

Metadata-only inventory of the English cutscene voice lines extracted by
`tools/xivdata` from game version **2026.10.16**. No audio is included;
`scd_bytes` is the encoded payload size only.

## What counts as a voice

| Measure | Count |
|---|---:|
| Voiced cutscene lines | 25,598 |
| Distinct speaker labels (`speaker_token`) | 667 |
| Labels resolved to an `ENpcResident` row | 289 |
| Labels with no NPC match | 378 |
| Distinct NPC row IDs behind those labels | 288 |
| Cutscene sheets with voiced rows | 96 |
| SCD payload | 4.74 GB |

The finest identity the data carries is the speaker label, not a voice actor.
The extracted fields contain no casting information, so a label is not a
unique performer: one character can appear under multiple labels
(`nanamo` and `nanamoulnamo` both resolve to Nanamo Ul Namo; `sphene`,
`originalsphene`, and `newsphene` are separate labels), and multiple
characters can share an actor. Read the counts as labels and characters,
not performers.

## Standard metadata

Per-line columns in `dialogue.csv` (the source of this roster):

| Column | Meaning |
|---|---|
| `sheet` | `cut_scene/...` Excel sheet that supplied the row |
| `row_id` | Row number inside that sheet |
| `text_key` | Game text key, e.g. `TEXT_VOICEMAN_05400_000010_ALPHINAUD` |
| `audio_base` | SCD basename derived from `text_key` |
| `speaker_token` | Speaker token from `text_key`, lowercased |
| `npc_id` / `npc_name` | Resolved `ENpcResident` row; empty when unmatched |
| `race_id` / `gender` | From `ENpcBase`; `0/0` when that row is absent |
| `scd_path` | Game path; its expansion segment gives the patch family |
| `scd_bytes` | Encoded SCD size, not duration |
| `text` | The spoken line text (EN) |

`docs/voices.csv` aggregates one row per speaker label with the same identity
fields plus `lines`, `scenes` (distinct sheets), `expansions`, and `scd_bytes`.

## Where the lines come from

| Expansion | Lines | Speaker labels |
|---|---:|---:|
| ARR | 1,525 | 43 |
| HW | 2,462 | 99 |
| StB | 3,300 | 129 |
| ShB | 4,931 | 162 |
| EW | 7,556 | 180 |
| DT | 5,824 | 193 |
| **Total** | **25,598** | **667** |

## Race and sex (resolved labels)

78 of the 289 resolved labels have no `ENpcBase` row, so their
race and sex are recorded as unknown (e.g. Wuk Lamat). For the rest:

| Race | Male | Female | Labels |
|---|---:|---:|---:|
| Hyur | 63 | 26 | 89 |
| Elezen | 36 | 11 | 47 |
| Lalafell | 10 | 8 | 18 |
| Miqo'te | 7 | 4 | 11 |
| Roegadyn | 16 | 3 | 19 |
| Au Ra | 4 | 5 | 9 |
| Hrothgar | 9 | 1 | 10 |
| Viera | 2 | 6 | 8 |
| **Total** | **147** | **64** | **211** |

Race and sex come from `ENpcBase` and are not always canonical: Yugiri is
recorded as Miqo'te, Hydaelyn as Hyur / male, and 78 resolved labels have
no base row at all.

## Largest speakers

| Label | NPC | Race / sex | Lines | Scenes |
|---|---|---|---:|---:|
| `alphinaud` | Alphinaud (1003053) | Elezen / male | 1,631 | 61 |
| `yshtola` | Y'shtola (1003782) | Miqo'te / female | 1,244 | 44 |
| `alisaie` | Alisaie (1003054) | Elezen / female | 1,159 | 47 |
| `wuklamat` | Wuk Lamat (1045157) | unknown / unknown | 1,071 | 11 |
| `krile` | Krile (1012813) | Lalafell / female | 776 | 37 |
| `thancred` | Thancred (1003781) | Hyur / male | 642 | 40 |
| `urianger` | Urianger (1003784) | Elezen / male | 641 | 38 |
| `mysteryvoice` | — | — | 602 | 12 |
| `estinien` | Estinien (1007115) | Elezen / male | 548 | 32 |
| `emetselch` | Emet-Selch (1028112) | Elezen / male | 534 | 15 |
| `grahatia` | G'raha Tia (1005517) | Miqo'te / male | 525 | 21 |
| `mysteriousperson` | mysterious person (1030939) | Hyur / male | 462 | 6 |
| `erenville` | Erenville (1036997) | Viera / male | 375 | 17 |
| `lyse` | Lyse (1018327) | Hyur / female | 348 | 15 |
| `raubahn` | Raubahn (1003786) | Hyur / male | 333 | 18 |
| `hien` | Hien (1019544) | Hyur / male | 327 | 9 |
| `aymeric` | Aymeric (1008194) | Elezen / male | 321 | 19 |
| `warriorofdarkness` | Warrior of Darkness (1013810) | Hyur / male | 288 | 9 |
| `minfilia` | Minfilia (1003783) | Hyur / female | 260 | 9 |
| `sphene` | Sphene (1047396) | Hyur / female | 255 | 4 |
| `hythlodaeus` | Hythlodaeus (1029257) | Elezen / male | 247 | 6 |
| `zenos` | Zenos yae Galvus (1018053) | unknown / unknown | 245 | 17 |
| `venas` | — | — | 241 | 3 |
| `ryne` | Ryne (1027137) | Hyur / female | 239 | 13 |
| `originalsphene` | — | — | 219 | 3 |
| `hermes` | Hermes (1038060) | Elezen / male | 216 | 2 |
| `bakooljaja` | Bakool Ja Ja (1047305) | unknown / unknown | 209 | 6 |
| `meteion` | Meteion (1038058) | unknown / unknown | 202 | 2 |
| `yugiri` | Yugiri (1007097) | Miqo'te / female | 198 | 14 |
| `elidibus` | Elidibus (1017303) | Hyur / male | 195 | 7 |
| `lucia` | Lucia (1008193) | Elezen / female | 193 | 10 |
| `koana` | Koana (1046617) | Miqo'te / male | 190 | 8 |
| `gosetsu` | Gosetsu (1018057) | Roegadyn / male | 179 | 9 |
| `jullus` | Jullus (1038047) | Hyur / male | 179 | 6 |
| `nanamoulnamo` | Nanamo Ul Namo (1002440) | Lalafell / female | 176 | 11 |
| `tataru` | Tataru (1003785) | Lalafell / female | 175 | 23 |
| `calyx` | Calyx (1052412) | Elezen / male | 171 | 5 |
| `fourchenault` | Fourchenault (1034125) | Elezen / male | 171 | 6 |
| `gulooljaja` | Gulool Ja Ja (1049591) | unknown / unknown | 163 | 4 |
| `blackknight` | — | — | 160 | 5 |
| `vritra` | — | — | 159 | 7 |
| `varshahn` | Varshahn (1038053) | Au Ra / male | 150 | 6 |
| `varshahnbig` | — | — | 148 | 4 |
| `amon` | Amon (1040679) | unknown / unknown | 144 | 4 |
| `livingway` | Livingway (1036857) | unknown / unknown | 142 | 5 |
| `merlwyb` | Merlwyb (1002441) | Roegadyn / female | 139 | 9 |
| `zoraalja` | Zoraal Ja (1046906) | unknown / unknown | 139 | 7 |
| `hydaelyn` | Hydaelyn (1003070) | Hyur / male | 137 | 10 |
| `yotsuyu` | Yotsuyu (1019099) | Hyur / female | 127 | 4 |
| `asahi` | Asahi (1024637) | Hyur / male | 123 | 3 |
| `cahciua` | Cahciua (1047310) | unknown / unknown | 123 | 4 |
| `nidhana` | Nidhana (1036523) | unknown / unknown | 123 | 8 |
| `edmont` | — | — | 120 | 10 |
| `iceheart` | — | — | 119 | 6 |
| `shale` | Shale (1048161) | Elezen / female | 118 | 7 |
| `fordola` | Fordola (1019098) | Hyur / female | 112 | 7 |
| `lyna` | Lyna (1028109) | Viera / female | 103 | 8 |
| `kanesenna` | Kan-E-Senna (1003027) | Hyur / female | 102 | 9 |

## Runtime event log

The plugin writes `events.jsonl` beside its config (`~/.xlcore/pluginConfigs/OhMyVoice/`).
Every cache lookup is one JSON object, deduped by `(Result, Id)` across sessions:

```json
{"Result":"miss","Id":"<md5>","Lang":"en","VoiceId":"alphinaud","NpcId":"Alphinaud",
 "Speaker":"Alphinaud","Sentence":"...","RawSpeaker":"Alphinaud","RawSentence":"...",
 "Source":"Talk","Zone":"Gridania","GameVersion":"...",
 "Npc":{"Name":"Alphinaud","NpcId":1003053,"Match":"name","Candidates":1,
        "Ambiguous":false,"RaceId":2,"Race":"Elezen","Gender":0,"Sex":"male",
        "ExpansionId":0,"Expansion":"A Realm Reborn"},
 "Utc":"2026-10-21T00:00:00+00:00"}
```

- `Result` is `hit` or `miss`. Only misses feed the generator.
- `Speaker`/`Sentence` are already sanitized v1; the generator never re-sanitizes.
- `Npc.Name`/`Npc.NpcId`/`Npc.Race`/`Npc.Sex` are stable for a `speaker_token`;
  `Npc.Expansion`/`Npc.Zone` describe the event's current zone, so the same
  token can appear under several expansions.
- `Npc.Match` is `id`, `name`, or `none`; `Candidates` is the
  number of same-name resident rows considered. Generic labels can map to
  many rows; the first valid row wins and `Ambiguous` is true.
- The pre-log `misses.jsonl` is migrated into `events.jsonl` on first load.

## Full list

All 667 labels, with line counts, sheet breadth, expansion coverage,
and payload size: [`voices.csv`](voices.csv). Resolved characters come first,
then unresolved labels; each block is sorted by line count. `resolved=no`
means the token had no exact `ENpcResident` match; those include story
aliases (`thordan`, `iceheart`) and generic labels (`mysteryvoice`, 602 lines).

Regenerate the source scan with `tools/xivdata` (see its README); this roster
is derived from `dialogue.csv` and goes stale when the game updates.
