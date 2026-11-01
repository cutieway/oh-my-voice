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

The model sees `compact_state` (`jev.py`). In the style/identity call that is
game identity, `dialogue_measurements` (aggregates only), `dialogue_coverage`,
sampled lines with their signal tags, `lore_quotes`, `voice_evidence`,
`wiki_resolution`, `fact_conflicts`, and eight guardrails. In a mood chunk call
it is identity, `dialogue_aggregates` (the token-cheap subset of the same
numbers), `dialogue_coverage`, the chunk's dialogue as bare strings,
`dialogue_chunk` (index, count, sources), and the guardrails; `lore_quotes`,
`voice_evidence`, `wiki_resolution`, and `fact_conflicts` are withheld so no
lore sentence can stand in for dialogue. The model answers with a probability
distribution over that question's closed value list; `classifier` accepts a
choice only above the threshold and records the distribution in the
classification.

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
Mood is decided from **in-game dialogue alone**; wiki text is still fetched for
identity fields but never reaches a mood call.

A character with 1,552 lines cannot be judged in one 8k-context request, and a
24-line sample makes one dramatic scene look like a baseline. So the dossier
carries a `dialogue_pool` (signal-ranked, bucket-stratified, deduplicated) and
`classifier` asks the same two typed questions on every chunk window
(`mood.py`, `build_mood_chunks`): corpus-share stratification across short /
medium / long, signal weight first inside each bucket, and a 15% overlap
between consecutive windows. Each chunk state carries the same compact
aggregates (`dialogue_aggregates`) and identity, and no wiki text at all.

| Volume | Chunks | Note |
|---|---:|---|
| < 40 unique lines | 0 | no mood question is asked; `blocked_by: ["insufficient_volume"]` |
| 40–119 | 1–2 | one request per window |
| 120–499 | 3–4 | |
| 500–799 | 5 | |
| 800–1,199 | 6 | |
| 1,200–1,999 | 7 | |
| ≥ 2,000 | 8 | `mood_max_chunks` caps it |

Each window is one request: `1 + chunks` calls for a mood-eligible dossier
(one identity/style call plus one per chunk).

`mood_chunk_chars` is 7,000 characters because this tokenizer runs ~1.34
characters per token: that is ~5.2k tokens of dialogue, and a measured chunk
state is ~7.1k of the 8k context including aggregates, schema, and questions.
The first design used 16k characters and the server rejected it with
`exceed_context_size_error: request (9068 tokens) exceeds 8192`.

Sampling inside the pool reuses the style selector, so chunking never invents a
second convention: bucket quotas by corpus share, then signal weight (dialect,
colloquial, formal, all-caps, honorific, exclamation, question, ellipsis,
contraction), then a stable per-NPC order, with near-duplicates rejected. Pure
stage directions never reach the pool — `(-Label-)` prefixes are split off and an
empty remainder is dropped — and pure quest steps are excluded upstream by the
ingestion filter `text_kind IN ('dialogue','narration')`, which keeps the 44,191
`journal` rows out; among the 248,904 dialogue and narration lines only 205
(0.08%) still match an imperative quest template, so a runtime pattern filter
would be dead weight.

`mood.aggregate` then decides the archon:

| Gate | Threshold | Rationale |
|---|---|---|
| Winner | argmax of the mean probability vector; ties go to `none` | one answer per character, not per chunk |
| Consistency | the winner leads (or is within `mood_consistency_margin` 0.05 of the top) in ≥ `ceil(N × 0.6)` chunks | a single loud chunk must not decide |
| Selected probability | mean `mood_probability` 0.50 | the preset must beat the whole field, on average |
| Confidence | mean `mood_confidence` 0.70 | stricter than style (0.55): a wrong archon changes every line |
| Dispersion | normalised entropy of the mean vector ≤ 0.55 | a flat field means no dominant register |
| Justification | mean `mood.justified` ≥ `noul_yes` 0.75 | a default register, not one emotional scene |

Any failure sets `pack_archon: neutral` and records the failed gates in
`mood.blocked_by` as stable tokens: `insufficient_volume`, `insufficient_pool`,
`none_winner`, `low_consistency`, `low_probability`, `low_confidence`,
`high_dispersion`, `not_justified`. `insufficient_pool` is a pipeline-state
refusal, not a character judgement: a dossier built before the mood pool carries
only the 24-line style sample, and chunking that would present one window as the
corpus, so `classify` asks no mood question and adds a review reason telling the
operator to re-run `dossiers`. The profile evidence for `pack_archon` is then level `default`
with those reasons in the note. An accepted mood is level `creative_selection`
with the preset, mean confidence, mean probability, chunk count, and agreeing
chunk count in the note, and declares the archon in `moods` so
`validate_profile` accepts it.

`mood.chunks` records every chunk's top preset, its probability for the
winner, confidence, justified value, and full distribution; `mood.aggregate`
records the mean vector, agreeing/required counts, mean probability, mean
confidence, mean justification, and dispersion. A profile can therefore be
audited back to the individual windows that produced it.

### Gating policy in one paragraph

Mood is decided from in-game dialogue alone: below 40 unique lines no mood
question is asked and the pack is neutral with `blocked_by: ["insufficient_volume"]`;
above it the signal-ranked dialogue pool is cut into overlapping, bucket-stratified
windows (1 for 40-119 lines up to 8 at 2,000+), each window is asked the same two
typed questions with the same compact aggregates and no wiki text, and the archon
is the argmax of the mean probability vector accepted only if that preset leads
(or is within 0.05 of the top) in at least 60% of windows, the mean selected
probability is at least 0.50, mean confidence at least 0.70, the normalised
entropy of the mean vector at most 0.55, and mean `mood.justified` at least 0.75.
Every failure is recorded as a stable token in `mood.blocked_by`
(`insufficient_volume`, `insufficient_pool`, `none_winner`, `low_consistency`,
`low_probability`, `low_confidence`, `high_dispersion`, `not_justified`), the pack archon stays
`neutral`, and the profile evidence is level `default` naming those gates; an
accepted preset is level `creative_selection`, declares the archon in `moods`,
and keeps every per-window answer, distribution, and the mean vector for audit.
Acoustics never enter this path: they remain deterministic casting from game data.

The rules provider (`--provider rules`) does not chunk: it is deterministic and
answers `none`, so offline runs are unchanged and always neutral.

Measured 2026-11-02 against the local Nimble 9B, sixteen real NPCs, on dossiers
rebuilt by the current `dossiers` command:

| NPC | lines | chunks | per-chunk tops | mean top | p | conf | outcome |
|---|---:|---:|---|---|---:|---:|---|
| Gerolt | 1,552 | 7 | angry x3, neutral x4 | neutral | 0.36 | 0.38 | `low_consistency`, `low_probability`, `low_confidence`, `high_dispersion`, `not_justified` |
| Hildibrand | 1,133 | 6 | happy x5, none x1 | happy | 0.51 | 0.47 | `low_confidence`, `not_justified` |
| wandering minstrel | 1,062 | 6 | neutral x6 | neutral | 0.57 | 0.51 | `low_confidence`, `not_justified` |
| Haermaga | 466 | 4 | happy x1, neutral x3 | neutral | 0.41 | 0.38 | `low_probability`, `low_confidence`, `high_dispersion`, `not_justified` |
| Widargelt | 459 | 4 | neutral x3, none x1 | neutral | 0.54 | 0.52 | `low_confidence`, `not_justified` |
| Erik | 405 | 4 | neutral x4 | neutral | 0.61 | 0.55 | `low_confidence`, `not_justified` |
| Motte | 51 | 1 | happy | happy | 0.63 | 0.58 | `low_confidence`, `not_justified` |
| Hachisuba | 51 | 1 | neutral | neutral | 0.52 | 0.46 | `low_confidence`, `not_justified` |
| Linu Vanu | 49 | 1 | neutral | neutral | 0.60 | 0.55 | `low_confidence`, `high_dispersion`, `not_justified` |
| Knem Cheleim-In | 47 | 1 | neutral | neutral | 0.62 | 0.57 | `low_confidence`, `not_justified` |
| six sparse NPCs | 5-8 | 0 | - | - | - | - | `insufficient_volume` |

No real NPC passed, and the binding gate is `mood_confidence`: the model's
self-reported confidence on real dialogue sits at 0.38-0.58, well under the
0.70 bar. The two informative cases are Gerolt and Hildibrand. Gerolt splits
3 angry / 4 neutral across seven windows, so the mean lands on `neutral` and
`low_consistency` joins the refusal: the corpus really does mix forge-grumbling
with ordinary instruction. Hildibrand is 5/6 `happy` with mean p 0.51, so the
register is consistent and the model still will not claim it confidently;
refusing that is the conservative reading, not a bug in the aggregation. The
accept path itself is proven with the same model on uniform evidence: a 400-line
angry probe produced 4/4 chunks at mean p 0.994, confidence 0.993, dispersion
0.019, and `pack_archon: angry` with `method: jev_chunks`.

## Volume gates

| Gate | Value | Effect |
|---|---|---|
| `min_dialogue_lines_for_style` | 40 unique lines | below it every style field is recorded with `value: null`, `method: insufficient_dialogue_volume`, and the deterministic estimate kept only for audit; the profile carries `speech_style: null` and renders from identity alone |
| `min_dialogue_lines_for_mood` | 40 unique lines | below it no mood question is asked; `insufficient_volume` |

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
transcripts, and mood no longer reads them at all: lore supports identity fields
and `voice_evidence`, never the archon.

## Sample selection

`dialogue.select_representative_samples` picks 24 lines (≤6,000 chars) per
dossier, and the same selector with a larger budget builds the mood pool, so the
style sample and the chunk windows are drawn by one convention:

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

| NPC | unique lines | style fields | mood chunks | pack_archon | blocked_by |
|---|---:|---:|---:|---|---|
| Pimoh Nelhah | 5 | 0 | 0 | neutral | insufficient_volume |
| Colina | 5 | 0 | 0 | neutral | insufficient_volume |
| Diggin | 6 | 0 | 0 | neutral | insufficient_volume |
| Colden | 7 | 0 | 0 | neutral | insufficient_volume |
| Waldew | 8 | 0 | 0 | neutral | insufficient_volume |
| Hathenbet | 8 | 0 | 0 | neutral | insufficient_volume |
| Knem Cheleim-In | 47 | 5 | 1 | neutral | low_confidence, not_justified |
| Linu Vanu | 49 | 5 | 1 | neutral | low_confidence, high_dispersion, not_justified |
| Motte | 51 | 5 | 1 | neutral | low_probability, low_confidence, high_dispersion, not_justified |
| Hachisuba | 51 | 5 | 1 | neutral | low_confidence, not_justified |
| Erik | 405 | 5 | 4 | neutral | low_confidence, not_justified |
| Widargelt | 459 | 5 | 4 | neutral | low_confidence, not_justified |
| Haermaga | 466 | 5 | 4 | neutral | low_consistency, low_probability, low_confidence, not_justified |
| wandering minstrel | 1062 | 5 | 6 | neutral | low_confidence, not_justified |
| Hildibrand | 1133 | 5 | 6 | neutral | low_confidence, not_justified |
| Gerolt | 1552 | 5 | 7 | neutral | low_probability, low_confidence, high_dispersion, not_justified |

### Mood, offline (no model server)

`RulesProvider` never chunks and always answers `none`, so an offline run proves
the gates and the profile shape but not the model path:

```bash
python3 -m unittest discover -s tools -p "test_*.py"
```

`ChunkedMoodTests` covers the design without a server: below the floor no mood
question is asked and `blocked_by` is `["insufficient_volume"]`; a dossier with
no mood pool is refused with `insufficient_pool` and zero chunk calls; consistent
chunks accept (`stub_chunks`); one loud chunk among disagreeing ones is blocked
by `low_consistency`; a flat field is blocked by `high_dispersion` and
`low_probability`; every rejection names at least one gate; `plan_chunk_count`
follows the volume bands; a Gerolt-scale pool yields seven windows that all
contain short, medium, and long lines and overlap their neighbour; a chunk
state carries the global aggregates and no lore; the aggregate records per-chunk
evidence; a composed profile names the agreeing-chunk count in its evidence.

### Mood, real multi-chunk path

With the local server up:

```bash
llama-server --model data/models/qwen3.5-9b/Bespoke-Nimble-9B-v3-Q4_K_M.gguf \
  --port 18081 --n-gpu-layers 999 --ctx-size 8192 --jinja
python3 -m tools.blueprint_pipeline.cli classify --provider jev --allow-network \
  --endpoint http://127.0.0.1:18081/v1/systemone --model nimble-9b --only $NPC --force
python3 -m tools.blueprint_pipeline.cli compose --game-index data/xivdata/npc-index.json --only $NPC
```

`classify --dry-run` prints the request count and bytes first; a mood-eligible
dossier costs one request per chunk plus one for identity and style.

Expected and observed: every sparse character renders from identity alone, every
character at or above 40 lines carries all five style fields, and sparse
characters ask no mood question at all. Acceptance is exercised by
`ChunkedMoodTests` with a scripted provider (uniform angry/calm chunks across
all windows); real characters land on neutral with the failed gates named.

The suite also covers the style floor, state contents, label stripping,
whole-word term matching, signal-weighted sampling, bucket quotas across many
sheets, wikitext rendering without extracts, `{{NPC infobox}}` parsing, template
disambiguation, and the mood profile evidence levels.

## Known limits

1. `pitch` returned `mid` for 9/10 NPCs when it was a model question; the casting
   ladder replaces it. Bands are coarse by design: seven steps.
2. `speech_style.sentence_complexity` returned `mixed` for 74/80 benchmark items
   and `rhetorical_style` for 68/80 — those two fields carry little signal as
   currently worded.
3. `vocabulary_complexity` confidence is low (median 0.31 across 80 items).
4. Casting rules are taste encoded once: they are auditable, not measured. A
   human override is still required before a profile becomes canon.
5. None of the sixteen real NPCs earned a mood archon. Chunking changed the
   shape of the refusal: the model no longer answers `none` at 0.96 from 24
   lines, it now splits a 1,552-line character across seven windows and lands
   on `neutral` with 3 of 7 windows reading `angry`. The binding gate is
   `mood_confidence` (0.32-0.57 against 0.70), so a consistent but hedged
   register like Hildibrand's 6/6 `happy` is still refused. That is the safe
   failure direction; human auditions, not the model, remain the way a mood
   archon gets chosen in practice. If real auditions show the model is simply
   under-confident, `mood_confidence` is the one knob to calibrate first - not
   the consistency gate, which is what protects against one loud scene.
6. Wiki lore is no longer mood evidence. It is still fetched for identity
   fields, but a mood chunk state withholds `lore_quotes`, `voice_evidence`,
   `wiki_resolution`, and `fact_conflicts`, so no disposition sentence can
   stand in for dialogue. Most FFXIV NPC pages are stubs or dialogue
   transcripts; enrich mainly buys identity fields and `fact_conflicts`.
7. `mood.justified` is a yes/no gate rather than a score: a score would need
   calibration against human labels before it could set a threshold, and the
   gate is what the archon decision actually needs. No `insufficient_evidence`
   label was added to the style dimensions either; the volume gate covers that
   case without widening the closed label vocabulary.
