# How a blueprint decides each field

Every profile field is decided by exactly one source. This page says which, so
no value is a mystery.

| Source | Meaning | Where |
|---|---|---|
| **Game data** | read from the game files (`ENpcResident`, `ENpcBase`, `Race`, `Tribe`) | `tools/xivdata`, `data/xivdata/npc-index.json` |
| **Casting rules** | deterministic acoustic choices computed from game facts | `tools/blueprint_pipeline/casting.py` |
| **Model** | the local decision model answers a typed question | `jev.build_questions`, `cli.py classify` |
| **Default** | explicit project fallback, never evidence | `vocabulary.json` `classification.defaults` |

## Why acoustics are not asked of the model

Measured 2026-11-01 on ten NPCs: when the acoustic fields were model questions,
the answers collapsed onto the middle band — `pitch=mid` for 9/10, `timbre=smooth`
for 8/10, `perceived_age=adult` for 6/10, `resonance` never accepted. Rewording
the questions as explicit casting decisions did not change that; it only lowered
confidence so more fields were rejected. The text does not contain the answer,
and a 9B Q4 model fills the gap with the safest label.

So acoustics now come from `casting.py`, which applies a handful of readable
rules to facts the game states, and the model is asked only what dialogue can
support: speech style.

## Game identity

`data/xivdata/npc-index.json` holds 30,989 NPCs exported by
`dotnet run --project tools/xivdata -- npc-index --sqpack ... --lang en`:

| Field | Example |
|---|---|
| `npc_id`, `name` | `1009242`, `Haermaga` |
| `race_name` | `Roegadyn` |
| `tribe_name` | `Sea Wolf` |
| `sex` | `male` |
| `body_type` | `1` adult, `3` aged model, `4` small model |
| `height` | scale percent; `0` means no override |

`game_data.game_metadata` joins this index into every dossier, so `identity`
carries race, clan, sex, body type and height before any model call.

## Casting rules

`casting.cast_identity(identity)` returns one decision per acoustic field:
`{"value", "rule", "facts"}`. The rule name is written into the profile evidence
as `casting:<rule>`, so the audition page shows exactly why each value exists.

| Field | Rule | Basis |
|---|---|---|
| `presentation` | `sex_presentation` | `ENpcBase.Sex` |
| `perceived_age` | `lore_age_band` / `body_type_age` / none | documented lore age wins; else `BodyType` 4 → `young_teen`, 3 → `mature` (both `creative_selection`); otherwise `adult` as evidence level **`default`**, not a game fact |
| `pitch` | `sex_race_pitch_ladder`, or `sex_race_age_pitch_ladder` when an age band is known | sex base band shifted by race (Roegadyn/Hrothgar down, Lalafell up) and, when an age band exists, by age |
| `pitch_range` | `age_race_range` | children wide, elders narrow, else race default with a stable per-NPC choice |
| `resonance` | `age_race_resonance` | race placement, overridden by age for children/elders |
| `timbre_primary` | `race_timbre_family` | each race has a three-value family (Hyur clear/warm/bright, Roegadyn rich/dark/gravelly, Elezen silvery/bright/clear, ...); the NPC picks one |
| `articulation` | `race_or_stable_articulation` | race default, else a stable per-NPC pick among relaxed/rounded/fluid |
| `speaking_pace` | `age_pace` | child brisk, elder slow, else a stable per-NPC pick among measured/moderate |
| `intonation` | `race_or_stable_intonation` | race default, else a stable per-NPC pick among natural/restrained/expressive |
| `accent` | project default | `General British`, light |

Stable per-NPC choices hash `canonical_id`, so two Hyur Midlander men differ
in timbre, range, articulation, intonation and pace while every rebuild of the
same NPC is identical. `Height` is exported but not
used: its `0` value means "no override" and its scale differs per race.

All casting evidence is `creative_selection` — it describes the voice being
created, never the original actor — except values that are pure project
fallbacks (`perceived_age: adult` without an age fact, `accent`), which are
recorded as `default`. A profile may still be overridden by hand.

`BodyType` only bounds age at the extremes. Measured 2026-11-01 against 12 wiki
ground-truth NPCs, `BodyType` alone matched 2/11 and `bt1 → adult` was wrong for
Merlwyb, Momodi, Hildibrand, Godbert and Kan-E-Senna — so `bt1` produces the
`default` band rather than an age claim.

## What the model is asked

Eight questions, from `blueprints/vocabulary.json` `classification.questions`:

| Question | Type | Feeds |
|---|---|---|
| `identity_resolved` | yes/no | eligibility gate |
| `voice_evidence_present` | yes/no | individual-design score |
| `individual_design` | score 0–4 | whether an individual profile is justified |
| `speech_style.register` | choice | `speech_style.register` |
| `speech_style.vocabulary_complexity` | choice | `speech_style.vocabulary_complexity` |
| `speech_style.sentence_complexity` | choice | `speech_style.sentence_complexity` |
| `speech_style.colloquialism` | choice | `speech_style.colloquialism` |
| `speech_style.rhetorical_style` | choice | `speech_style.rhetorical_style` |

The model sees `compact_state` (`jev.py`): game identity (race, clan, sex,
occupation, faction, location), dialogue statistics, dialogue analysis, sampled
lines, `lore_evidence`, `voice_evidence`, `wiki_resolution`, `fact_conflicts`,
and four guardrails. It answers with a probability distribution over that
question's closed value list; `classifier` accepts a choice only above the
confidence threshold and records the distribution in the classification.

Removed from the question set on 2026-11-01:

- `speech_style.diction_style` and `speech_style.contraction_frequency` — no
  validation exists; they failed the benchmark and never reach a profile.
- every `identity.*` question — replaced by casting rules.

## Speech-style fields

| Field | Asked? | Validated by the benchmark | In profile | In instruct |
|---|---|---|---|---|
| `register` | yes | yes | yes | only with `--with-style` |
| `vocabulary_complexity` | yes | yes | yes | only with `--with-style` |
| `sentence_complexity` | yes | yes | yes | only with `--with-style` |
| `colloquialism` | yes | yes | yes | only with `--with-style` |
| `rhetorical_style` | yes | yes | yes | only with `--with-style` |
| `diction_style` | no | dropped from benchmark v2 | never | no |
| `contraction_frequency` | no | never validated | never | no |
| `speech_habits` | no | no | no | no |

`generator.VALIDATED_STYLE_FIELDS` is the allow-list a profile is built from, so
an unvalidated field cannot reach a profile even if a dossier analysis produces
one.

## Wiki

`cli.py enrich` resolves a canonical NPC on the wiki, caches infobox data and
lore quotes, and stores `wiki_resolution` plus `lore_evidence` on the dossier.
`compact_state` already forwards both to the model. It is off by default:
without `--allow-network` a dossier has no wiki data.

## Pipeline order

```bash
# 1. game identity index (once per game patch)
dotnet run --project tools/xivdata -- npc-index \
  --sqpack "$HOME/Public/xlcore/ffxiv/game/sqpack" --lang en \
  > data/xivdata/npc-index.json

# 2. dossiers with game facts
python3 -m tools.blueprint_pipeline.cli dossiers \
  --game-index data/xivdata/npc-index.json --only npc_1003075 ...

# 3. speech style only
python3 -m tools.blueprint_pipeline.cli classify --provider jev \
  --endpoint http://127.0.0.1:8081/v1/systemone \
  --model data/models/qwen3.5-9b/Bespoke-Nimble-9B-v3-Q4_K_M.gguf \
  --allow-network --only npc_1003075 ...

# 4. compose: casting + style
python3 -m tools.blueprint_pipeline.cli compose \
  --game-index data/xivdata/npc-index.json --only npc_1003075 ...
```

## Known limits

1. `pitch` returned `mid` for 9/10 NPCs when it was a model question; the casting
   ladder replaces it. Bands are coarse by design: seven steps.
2. `speech_style.sentence_complexity` returned `mixed` for 74/80 benchmark items
   and `rhetorical_style` for 68/80 — those two fields carry little signal as
   currently worded.
3. `vocabulary_complexity` confidence is low (median 0.31 across 80 items).
4. Casting rules are taste encoded once: they are auditable, not measured. A
   human override is still required before a profile becomes canon.
