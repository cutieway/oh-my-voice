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
| `perceived_age` | `body_type_age` / none | `BodyType` 4 → `young_teen`, 3 → `mature` (both `creative_selection`); otherwise `adult` as evidence level **`default`**, not a game fact |
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

`BodyType` only bounds age at the extremes. Measured 2026-11-01 against 12 NPCs
with documented ages, `BodyType` alone matched 2/11 and `bt1 → adult` was wrong for
Merlwyb, Momodi, Hildibrand, Godbert and Kan-E-Senna — so `bt1` produces the
`default` band rather than an age claim.

## What the model is asked

Seven questions, from `blueprints/vocabulary.json` `classification.questions`:

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

(`identity_resolved` and the design score are gates; the five style choices are
the only free classifications.)

The model sees `compact_state` (`jev.py`): game identity, `dialogue_measurements`
(aggregates only), `dialogue_coverage`, sampled lines with their signal tags,
`voice_evidence`, and six guardrails. No wiki text, lore quotes, or fact-conflict
records exist any more. The model answers with a probability distribution over
that question's closed value list; `classifier` accepts a choice only above the
threshold and records the distribution in the classification.

`dialogue_measurements` deliberately excludes the rules-derived interpretation
(`register: formal`, …). Showing the model its own baseline's labels turned the
answer into an echo of the rules; the measurements stay, the conclusion does not.

`dialogue_coverage` reports how much of the corpus the samples represent
(`sampled_lines / unique_lines`) and sets a `warning` below 25%. Gerolt is 24 of
1,552 lines (1.5%), and the state says so, so a confident answer cannot pretend
to have read the whole character.

## Volume gates

| Gate | Value | Effect |
|---|---|---|
| `min_dialogue_lines_for_style` | 40 unique lines | below it every style field is recorded with `value: null`, `method: insufficient_dialogue_volume`, and the deterministic estimate kept only for audit; the profile carries `speech_style: null` and renders from identity alone |

Sparse characters stay simple by construction, not by luck: six of the sixteen
test NPCs (5–8 lines) produced zero style fields.

Removed from the question set on 2026-11-01:

- `speech_style.diction_style` and `speech_style.contraction_frequency` — no
  validation exists; they failed the benchmark and never reach a profile.
- every `identity.*` question — replaced by casting rules.

## Speech-style fields

| Field | Asked? | Validated by the benchmark | In profile | In instruct |
|---|---|---|---|---|
| `register` | yes | yes | yes | yes (default clause) |
| `vocabulary_complexity` | yes | yes | yes | yes (default clause) |
| `sentence_complexity` | yes | yes | yes | yes (default clause) |
| `colloquialism` | yes | yes | yes | yes (default clause) |
| `rhetorical_style` | yes | yes | yes | yes (default clause) |
| `diction_style` | no | dropped from benchmark v2 | never | no |
| `contraction_frequency` | no | never validated | never | no |
| `speech_habits` | no | no | no | no |

`generator.VALIDATED_STYLE_FIELDS` is the allow-list a profile is built from, so
an unvalidated field cannot reach a profile even if a dossier analysis produces
one.

When all five validated fields are present, `render_style()` appends one clause
after the identity sentence. That is the default render, not a flag: listening on
2026-11-03 preferred styled takes over identity-only takes for every sampled
voice. The identity text stays byte-identical, so the clause cannot rewrite an
acoustic field.

The earlier mood layer is gone with schema 1.2. Its block sat between identity and
style, and the measured composition was not additive (style +1.7 Hz alone,
−41.5 Hz with Hildibrand's mood block), so two delivery layers made attribution
impossible. One style layer, rendered once with the reference, stays.

## Schema 1.2

Schema 1.2 removes `moods`, `pack_archon`, `archon_pin`, `lore_age`, and the
wiki-derived dossier fields. `blueprint.py migrate` chains 1.0 → 1.1 → 1.2 and
refuses to guess; archived 1.0 profiles still validate against
`migrations/vocabulary-1.0.json`, and 1.1 profiles against
`migrations/vocabulary-1.1.json`.

Auditioning now pins one take: `mark_auditioned` writes
`profile.reference_pin` (`design_seed`, `reference_sha256`, `ref_seconds`,
`pinned_on`), and approval must quote the same hash.

## Sample selection

`dialogue.select_representative_samples` picks 24 lines (≤6,000 chars) per
dossier:

1. Deduplicate by `voice_line_id`, then drop near-duplicates: trigram Jaccard
   ≥ 0.82 **or** word-set containment ≥ 0.80 with length ratio ≥ 0.5. SE reuses
   quest templates and swaps one noun ("…the contours jus' right" / "…the texture
   jus' right"), which scores 0.70 Jaccard and is now caught.
2. Score every line by closed-vocabulary signals: `colloquial:`/`formal:` term
   3, `dialect:` term 3, ALL-CAPS 2, honorific 2, question/exclamation/ellipsis/
   contraction 1. Each sample carries its `signals`, so an answer can be traced
   to the line that supported it.
3. Split the 24 slots across the short/medium/long buckets **by corpus
   share**, giving every present bucket at least one slot, then round-robin over
   `(source, sheet)` inside each bucket, strongest signal first, then a stable
   `sha256(canonical_id:line_id)` order. Leftover slots come from the strongest
   lines overall.

   Bucket balance used to be an accident of iteration order: taking one line per
   `(source, sheet)` group inside a bucket filled the whole quota from the first
   bucket whenever an NPC had many sheets. Gerolt has 200+ sheets and 181 short /
   953 medium / 947 long unique lines; the old loop produced 24 short lines (148
   words), the quota loop produces 2/12/10 (617 words), so `sentence_complexity`
   is judged on sentences rather than quips.

`dialect_terms` (aye, ye, yer, fer, nae, 'tis, thou, mayhap, …) weight sampling
and are counted in `signal_line_rates.dialect_lines`, but they are deliberately
**not** part of the formal/colloquial rate: "ye" and "pray" are archaic, not
informal, and folding them in would corrupt the register measurement.

`(-Label-)` prefixes are stage directions, not dialogue. 8,149 English lines
carry one (`(-Zombibrand-)Urrrgggh…`, `(-???-)…`). `split_speaker_label` removes
the prefix before analysis and sampling and keeps the label on the line as
`speaker_label`.

Term matching is whole-word: substring matching counted `ye` inside "yes" and
`ere` inside "there", which inflated dialect and colloquial rates.

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

## Test plan (16 NPCs, three volume strata)

Run against the cached dossiers; this set spans the strata the style gate cares
about.

```bash
NPC="npc_1000179 npc_1020869 npc_1029978 npc_1000315 npc_1011915 npc_1027602 \
     npc_1027318 npc_1016088 npc_1011802 npc_1021282 \
     npc_1006749 npc_1007125 npc_1009242 npc_1000533 npc_1005512 npc_1003075"

python3 -m tools.blueprint_pipeline.cli dossiers --game-index data/xivdata/npc-index.json --only $NPC
python3 -m tools.blueprint_pipeline.cli classify --provider rules --only $NPC
python3 -m tools.blueprint_pipeline.cli compose --game-index data/xivdata/npc-index.json --only $NPC
python3 tools/blueprint.py check
```

| NPC | unique lines | style fields |
|---|---:|---:|
| Pimoh Nelhah | 5 | 0 |
| Colina | 5 | 0 |
| Diggin | 6 | 0 |
| Colden | 7 | 0 |
| Hathenbet | 8 | 0 |
| Waldew | 8 | 0 |
| Knem Cheleim-In | 47 | 5 |
| Linu Vanu | 49 | 5 |
| Motte | 51 | 5 |
| Hachisuba | 51 | 5 |
| Erik | 405 | 5 |
| Widargelt | 459 | 5 |
| Haermaga | 466 | 5 |
| wandering minstrel | 1,062 | 5 |
| Hildibrand | 1,133 | 5 |
| Gerolt | 1,552 | 5 |

The rules provider is offline and deterministic; it answers the style questions
from the measured signal rates. Jev is the opt-in model path.

```bash
python3 -m unittest discover -s tools -p "test_*.py"
```
