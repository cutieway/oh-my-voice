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

Ten questions, from `blueprints/vocabulary.json` `classification.questions`:

| Question | Type | Feeds |
|---|---|---|
| `identity_resolved` | yes/no | eligibility gate |
| `voice_evidence_present` | yes/no | individual-design score |
| `individual_design` | score 0–4 | whether an individual profile is justified |
| `mood.preset` | choice, `none` + 7 presets | proposed default archon |
| `mood.justified` | yes/no | whether a default register exists at all |
| `speech_style.register` | choice | `speech_style.register` |
| `speech_style.vocabulary_complexity` | choice | `speech_style.vocabulary_complexity` |
| `speech_style.sentence_complexity` | choice | `speech_style.sentence_complexity` |
| `speech_style.colloquialism` | choice | `speech_style.colloquialism` |
| `speech_style.rhetorical_style` | choice | `speech_style.rhetorical_style` |

`mood.preset` options are generated from `moods.allowed_presets` plus `none`, so
the list cannot drift and no free-form mood name can be answered. Each preset's
`classification_description` describes a **default** register, not a scene.

The model sees `compact_state` (`jev.py`): game identity, `dialogue_measurements`
(aggregates only), `dialogue_coverage`, sampled lines with their signal tags,
`lore_quotes`, `voice_evidence`, `wiki_resolution`, `fact_conflicts`, and six
guardrails. It answers with a probability distribution over that question's
closed value list; `classifier` accepts a choice only above the threshold and
records the distribution in the classification.

`dialogue_measurements` deliberately excludes the rules-derived interpretation
(`register: formal`, …). Showing the model its own baseline's labels turned the
answer into an echo of the rules; the measurements stay, the conclusion does not.

`dialogue_coverage` reports how much of the corpus the samples represent
(`sampled_lines / unique_lines`) and sets a `warning` below 25%. Gerolt is 24 of
1,552 lines (1.5%), and the state says so, so a confident answer cannot pretend
to have read the whole character.

## Mood is an archon decision

`moods` in `vocabulary.json` already forbids per-line mood: one archon per
character, chosen once, recorded in `pack_archon`, defaulting to `neutral`.
The model only *proposes* it. `classifier._mood_decision` requires all four:

| Gate | Threshold | Rationale |
|---|---|---|
| Choice confidence | `mood_confidence` 0.70 | stricter than style (0.55): a wrong archon changes every line |
| Selected probability | `mood_probability` 0.50 | the preset must beat the whole field |
| Justification | `mood.justified` = yes | a default register, not one emotional scene |
| Volume | `min_dialogue_lines_for_mood` 120 unique lines | a baseline register needs a corpus |

Any failure sets `pack_archon: neutral` and records every failed gate in
`mood.blocked_by`; the profile evidence for `pack_archon` is then level
`default` with those reasons in the note. An accepted mood is level
`creative_selection` with the preset, confidence, probability and line count in
the note, and declares the archon in `moods` so `validate_profile` accepts it.

Measured 2026-11-02 against the local Nimble 9B:

| Case | Answer | Gate outcome |
|---|---|---|
| Gerolt, Hildibrand, Haermaga (real samples) | `none` at 0.96, justified 0.004 | neutral |
| Knem Cheleim-In (47 lines) | `none` at 0.95 | neutral |
| Probe: 400 explicitly angry lines | `angry` at 0.99, justified 0.99 | accepted → `pack_archon: angry` |
| Probe: 400 calm lines | `calm` at 0.94, justified 0.94 | accepted → `pack_archon: calm` |

So the question discriminates, and the model is conservative on real dialogue:
no real NPC among the sixteen tested earned a mood archon.

## Volume gates

| Gate | Value | Effect |
|---|---|---|
| `min_dialogue_lines_for_style` | 40 unique lines | below it every style field is recorded with `value: null`, `method: insufficient_dialogue_volume`, and the deterministic estimate kept only for audit; the profile carries `speech_style: null` and renders from identity alone |
| `min_dialogue_lines_for_mood` | 120 unique lines | below it no mood archon is possible |

Sparse characters therefore stay simple by construction, not by luck: six of the
sixteen test NPCs (5–8 lines) produced zero style fields.

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
lore quotes, and stores `wiki_resolution`, `lore_evidence` and `lore_quotes` on
the dossier. `lore_quotes` holds up to eight sentences that describe a
disposition (`calm`, `stern`, `brusque`, `temperament`, …), which is exactly the
kind of source that can justify a mood archon; `compact_state` forwards them as
`lore_quotes`. It is off by default: without `--allow-network` a dossier has no
wiki data.

Three properties of `ffxiv.consolegameswiki.com` shaped this code, each measured
2026-11-02:

1. **No TextExtracts extension.** `prop=extracts` returns an empty string for
   every page, so the quote and infobox paths used to see nothing at all.
   `wiki.plain_text` renders the wikitext instead: tables, templates, refs,
   files, categories and `__MAGIC__` words are dropped, link labels and prose
   survive.
2. **Infoboxes are `{{NPC infobox …}}`,** not `{{Infobox …}}`; the old matcher
   required the bare template name and parsed zero fields from every real page.
   Race, clan, sex, occupation, faction and location now arrive as
   `lore_evidence`, and disagreements with game data are recorded in
   `fact_conflicts` for review (measured: `Elezen` vs `Elf`, `Sea Wolf` vs
   `Sea Wolves`, and one real `male` vs `Female` page mismatch).
3. **Disambiguation is a template, not a pageprops flag.** `{{disambig}}` pages
   (Gerolt has ARR/HW/SB/Eureka/ShB/EW/DT instances) used to resolve as if they
   were the character. `is_disambiguation_page` reads the template, and
   `_resolve_disambiguation_links` follows the page's links, scoring each by
   canonical-name title prefix (5) plus one point per race/clan/occupation/
   faction/location term found in the linked page's own prose (infobox values are
   stripped). A unique top score ≥ 6 resolves; otherwise the dossier is marked
   `ambiguous` with `wiki_review_required`, never silently wrong.

The infobox `description` is the editors' one-line characterization, so it joins
the quote pool for both `voice_evidence` and `lore_quotes`.

Measured on the 16-NPC set (`enrich --allow-network --force`): 13 resolved with
1–15 infobox fields, Gerolt `ambiguous` (no location context to pick an
instance), the minstrel `missing` (page title differs), 4 `fact_conflicts`, and
1 NPC (Hachisuba) with disposition quotes. Most NPC pages are stubs or dialogue
transcripts, so wiki lore rarely decides a mood on its own; it is evidence, not
a substitute for the dialogue corpus.

## Sample selection

`dialogue.select_representative_samples` picks 24 lines (≤6,000 chars) per
dossier, deterministically:

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

Run against the 83 cached dossiers; this set spans the strata the gates care
about.

```bash
NPC="npc_1000179 npc_1020869 npc_1029978 npc_1000315 npc_1011915 npc_1027602 \
     npc_1027318 npc_1016088 npc_1011802 npc_1021282 \
     npc_1006749 npc_1007125 npc_1009242 npc_1000533 npc_1005512 npc_1003075"

python3 -m tools.blueprint_pipeline.cli dossiers --game-index data/xivdata/npc-index.json --only $NPC
# --force re-runs the wiki lookups; without it a dossier that already has a
# wiki_resolution is skipped, so a change to the wiki code would not take effect.
python3 -m tools.blueprint_pipeline.cli enrich --allow-network --force --limit 16 --only $NPC
python3 -m tools.blueprint_pipeline.cli classify --provider rules --only $NPC
python3 -m tools.blueprint_pipeline.cli compose --game-index data/xivdata/npc-index.json --only $NPC
python3 tools/blueprint.py check
```

| NPC | unique lines | style fields | pack_archon | pack_archon evidence |
|---|---:|---:|---|---|
| Pimoh Nelhah | 5 | 0 | neutral | default |
| Colina | 5 | 0 | neutral | default |
| Diggin | 6 | 0 | neutral | default |
| Colden | 7 | 0 | neutral | default |
| Waldew | 8 | 0 | neutral | default |
| Hathenbet | 8 | 0 | neutral | default |
| Knem Cheleim-In | 47 | 5 | neutral | default |
| Linu Vanu | 49 | 5 | neutral | default |
| Motte | 51 | 5 | neutral | default |
| Hachisuba | 51 | 5 | neutral | default |
| Erik | 405 | 5 | neutral | default |
| Widargelt | 459 | 5 | neutral | default |
| Haermaga | 466 | 5 | neutral | default |
| wandering minstrel | 1062 | 5 | neutral | default |
| Hildibrand | 1133 | 5 | neutral | default |
| Gerolt | 1552 | 5 | neutral | default |

Expected and observed: every sparse character renders from identity alone, every
character at or above 40 lines carries all five style fields, and no real
character earns a mood archon. To exercise the acceptance path, run the model
questions against a synthetic dossier whose samples are uniformly angry or calm
(400 unique lines); both are accepted, and the resulting profile validates with
`pack_archon: angry` / `calm` declared in `moods`.

`python3 -m unittest discover -s tools -p "test_*.py"` covers the gates:
mood option list, each blocking gate in isolation, the style floor, state
contents, label stripping, whole-word term matching, signal-weighted sampling,
bucket quotas across many sheets, wikitext rendering without extracts, `{{NPC
infobox}}` parsing, template disambiguation, and the mood profile evidence
levels.

## Known limits

1. `pitch` returned `mid` for 9/10 NPCs when it was a model question; the casting
   ladder replaces it. Bands are coarse by design: seven steps.
2. `speech_style.sentence_complexity` returned `mixed` for 74/80 benchmark items
   and `rhetorical_style` for 68/80 — those two fields carry little signal as
   currently worded.
3. `vocabulary_complexity` confidence is low (median 0.31 across 80 items).
4. Casting rules are taste encoded once: they are auditable, not measured. A
   human override is still required before a profile becomes canon.
5. None of the seven real NPCs run through the model earned a mood archon: it
   answers `none` at ≥0.86 on every real sample set, including 1,552-line
   Gerolt. That is the safe failure direction, but it means mood currently
   fires only when a corpus is unusually uniform. Human auditions, not the
   model, remain the way a mood archon gets chosen in practice.
6. Wiki lore is evidence, not a corpus. On the 16-NPC set only Hachisuba's page
   carried disposition sentences, and it has 51 unique lines, so the mood floor
   blocked it anyway. Most FFXIV NPC pages are stubs or dialogue transcripts;
   enrich mainly buys identity fields and `fact_conflicts`.
7. `mood.justified` is a yes/no gate rather than a score: a score would need
   calibration against human labels before it could set a threshold, and the
   gate is what the archon decision actually needs. No `insufficient_evidence`
   label was added to the style dimensions either; the volume gate covers that
   case without widening the closed label vocabulary.
