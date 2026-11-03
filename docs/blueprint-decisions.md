# How a blueprint decides each field

Every profile field is decided by exactly one source. This page says which, so
no value is a mystery.

| Source | Meaning | Where |
|---|---|---|
| **Game data** | read from the game files (`ENpcResident`, `ENpcBase`, `Race`, `Tribe`) | `tools/xivdata`, `data/xivdata/npc-index.json` |
| **Casting rules** | deterministic acoustic choices computed from game facts | `tools/blueprint_pipeline/casting.py` |
| **JEV model** | the local model answers five typed Style questions and must cite supplied line IDs | `jev.build_questions`, `cli.py classify` |
| **Rules baseline** | an explicitly selected offline baseline, never a fallback | `classifier.RulesProvider` |
| **Abstention** | the classifier or evidence floor refuses a label; the field is null | `classifier.classify_dossier` |

No source inserts a hidden default. A field with insufficient or rejected
evidence is stored as `value: null` with an explicit status, and the renderer
omits it.

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

`casting.cast_identity(identity)` returns one decision per identity field:
`{"value", "rule", "evidence_type", "rule_version", "facts", ...}`. The rule
name and provenance are written into the profile, so the audition page shows
exactly why each value exists.

| Field | Rule | Basis |
|---|---|---|
| `model_gender` | `enpcbase_gender_as_recorded` | `ENpcBase.Gender` (0 male, 1 female), only for a recognized playable-race model; unknown, non-playable, and zero-default rows are null |
| `presentation` | `sex_to_voice_presentation` | deterministic male → masculine / female → feminine convention |
| `race`, `clan`, `non_playable_race` | `*_as_recorded` | `ENpcBase.Race` / `Tribe`; unknown values warn and normalize to null while the raw value stays in `facts` |
| `perceived_age` | `body_type_*` | BodyType 3 → elderly (game fact), 4 → youthful (game fact), 1 → adult only as a labelled project convention, otherwise null |
| `pitch` | `gender_base_pitch` / `body_type_child_pitch` | sex baseline (`low_mid` male, `mid_high` female) and the BodyType 4 child override; race, height and muscle never shift it |
| `pitch_range` | none | null unless measured from audio |
| `perceived_vocal_size`, `vocal_weight` | `race_morphology_prior` | weak documented race/clan/gender morphology priors, recorded as `prior_score` / `casting_strength` |
| `timbre_primary`, `timbre_secondary`, `timbre_preferences` | `size_weight_timbre_preference` | optional preferences derived only from the size/weight axes |
| `resonance`, `articulation`, `speaking_pace`, `intonation` | none | null unless measured from verified audio; customization sliders are not vocal anatomy |
| `distinctive_traits` | none | null unless explicit approved evidence exists |

Morphology priors are creative casting scores, not probabilities, confidence, or
measured acoustics. They use the values in `casting.py` (`RACE_MORPHOLOGY_PRIOR`,
`CLAN_MORPHOLOGY_PRIOR`, `RACE_GENDER_MORPHOLOGY_PRIOR`) and remain
configurable/versioned. No race forces chest/head/forward/throaty resonance,
timbre, pitch range, pace or accent. Hrothgar Helion/Lost are preserved as
social-status labels with no biological or acoustic effect.

`has_recorded_voice` is separate from casting. An SCD/container reference or a
`has_se_voice_reference` flag is only a raw game fact; acoustic routing changes
only when verified speaker-to-audio evidence exists. Verified game facts are
never removed from the record.

## What the model is asked

Five questions, from `blueprints/vocabulary.json` `classification.questions`:

| Question | Type | Feeds |
|---|---|---|
| `speech_style.register` | choice | `speech_style.register` |
| `speech_style.lexical_complexity` | choice | `speech_style.lexical_complexity` |
| `speech_style.sentence_complexity` | choice | `speech_style.sentence_complexity` |
| `speech_style.colloquialism` | choice | `speech_style.colloquialism` |
| `speech_style.discourse_style` | choice | `speech_style.discourse_style` |

Identity, voice evidence, and individual-design decisions are deterministic
policy, not model questions. The model is text-only: it must not measure pitch,
timbre, resonance, speech rate, breathiness, or accent.

The model sees an identity-blind `compact_state` (`jev.py`): stable sample IDs,
the sampled text, aggregate numeric coverage and evidence-sufficiency counts,
version tags, and guardrails. Source and sheet names stay in the application's
provenance record, never in the request. It never sees NPC names, race, clan,
sex, appearance, voice evidence, wiki text, lore quotes, or rules-derived
interpretation. Optional assisted measurements are excluded from the production
Style request.

Each answer carries `choice`, `supporting_line_ids`,
`counterexample_line_ids`, optional `patterns`, and optional `rationale`.
`choice` is the model's proposed classification value. The model does not
assign status; the application derives `supported`, `provisional`,
`insufficient_evidence`, `unresolved`, or `rejected` from the exact
request-visible sample. `mixed` is a value, never a status. A supported field
requires at least three cited lines that exist in the request. A `mixed` value
requires two recurring patterns, each with at least three cited lines, unique
across patterns, and spanning at least two independent contexts. One
counterexample is not enough. Model self-reported confidence and probability
distributions are retained as diagnostics only; they never become assignment
confidence or calibrated quality.

`dialogue_coverage` reports how much of the corpus the samples represent
(`sampled_lines / unique_lines`) and sets a `warning` below 25%. A 40-line
sample of a 1,552-line character is 2.6%, and the state says so, so a confident
answer cannot pretend to have read the whole character. If the request exceeds
the byte limit, `compact_state` removes samples in a deterministic balanced
order, preferring strata that can lose a sample without emptying a length
bucket or a source/sheet context. It recomputes coverage and
`sample_manifest_hash` after every removal and never drops the evidence
metadata first. `budget_reduction`
reports retained/lost length buckets, source and context counts, and explicit
lost-count fields when the limit makes full coverage impossible.

### Style provenance

Each field decision carries `method`, `provider`, `model_id`, `evidence_tier`,
`evidence`, `status`, `value`, `supporting_line_ids`,
`counterexample_line_ids`, `support_ratio`, `rationale`,
`model_self_reported_confidence`, and `probabilities`. The enclosing
`speech_style_analysis` carries `taxonomy_version`, `classifier_version`,
`prompt_version`, `sampler_version`, `corpus_hash`, `sample_manifest_hash`,
raw/unique/sampled line and word counts, source/context coverage, and per-field
tiers. `profile.classification` copies the version tags and source audit hash.

The canonical reference hash (`blueprint.prompt_hashes`) includes the rendered
Identity, rendered Style, reference script, design model, renderer version, and
casting-rules version. The Style hash covers `speech_style` and the per-field
analysis, not the classification timestamp, so re-running the same classifier
does not churn the reference key. `identity_prompt_hash` never includes Style.

## Evidence tiers

`dialogue.evidence_sufficiency` measures the lines the classifier actually sees,
not the whole corpus. Floors live in `vocabulary.json`
`speech_style.sufficiency`:

| Tier | Lines | Words | Contexts | Effect |
|---|---:|---:|---:|---|
| `strong` | 40 | 600 | 3 | eligible for supported fields |
| `supported` | 20 | 300 | 2 | eligible for supported fields |
| `provisional` | 8 | 100 | 1 | label stored, marked `provisional`, not rendered |
| `insufficient` | below minimum | below minimum | below minimum | `value: null`, `status: insufficient_evidence` |

The representative request capacity is 40 lines / 12,000 characters, so the
`strong` tier is reachable from the classifier's actual sample rather than
being a full-corpus target. An independent context is a canonical
`source::sheet` pair, case-folded. If only one side is known it is used alone;
`unknown`/`n/a`/empty placeholders never create a context, and repeated exports
of the same pair collapse to one key. Source count remains in the audit record
but does not substitute for context diversity.

The frozen benchmark v2 dossiers keep their 24-line snapshots so the staged
artifacts stay reproducible; those benchmark items cannot reach `strong`.
Benchmark v3 uses the same frozen 24-line item snapshots for comparability.
Production uses the 40-line capacity above.

`discourse_style` carries a 1.25 multiplier because its pattern needs more
evidence. A field below the floor gets no first-option, neutral, or majority
default. `mixed` is a real classification value and is preserved with
`status: supported` or `provisional`; unresolved evidence yields `null` with
`status: unresolved`.

Profile allocation is deterministic: verified recorded voice routes to the
official audio path; fewer than 50 unique dialogue lines routes to a shared
archetype; otherwise at least two supported Style fields are required for an
individual design. Allocation does not compare Style distance against
archetypes: the current six archetypes carry no Style labels, so such a
comparison would be invented rather than measured. A provisional field is
retained in the audit record but is not written into
`profile.speech_style`.

Sparse characters stay simple by construction, not by luck. Earlier runs used a
single 40-line gate; the tiered floors now separate "no label" from "provisional
label" instead of collapsing both to null.

Removed from the question set on 2026-11-01:

- `speech_style.diction_style` and `speech_style.contraction_frequency` — no
  validation exists; they failed the benchmark and never reach a profile.
- every `identity.*` question — replaced by casting rules.
- `identity_resolved`, `voice_evidence_present`, `individual_design` — replaced
  by deterministic gates and `profile_allocation`.

## Speech-style fields

| Field | Asked? | Benchmark | In profile | In instruct |
|---|---|---|---|---|
| `register` | yes | staged; gold pending | status `supported` only | yes |
| `lexical_complexity` | yes | taxonomy v3 pilot pending | status `supported` only | yes |
| `sentence_complexity` | yes | taxonomy v3 pilot pending | status `supported` only | yes |
| `colloquialism` | yes | taxonomy v3 pilot pending | status `supported` only | yes |
| `discourse_style` | yes | taxonomy v3 pilot pending; experimental | status `supported` only | yes |
| `diction_style` | no | dropped from benchmark v2 | never | no |
| `contraction_frequency` | no | never validated | never | no |
| `speech_habits` | no | no | no | no |

`mixed` is a real value in every production dimension, paired with status
`supported` (or `provisional`). `insufficient_evidence` is not a label: it maps
to `value: null` plus `status: insufficient_evidence`. Taxonomy v3 renamed
`vocabulary_complexity` to `lexical_complexity` (`educated` -> `advanced`,
`specialized`/`poetic` -> audit-only features) and `rhetorical_style` to
`discourse_style`. Migration details are in
`blueprints/migrations/style-taxonomy-v3.md`; old cached classifications are
not silently reinterpreted.

`generator.VALIDATED_STYLE_FIELDS` is the allow-list a profile is built from, and
`generator.build_individual_profile` additionally requires `status` to be
`supported`. A provisional, unresolved, rejected, or insufficient field cannot
reach `profile.speech_style`, even though it remains in
`profile.speech_style_analysis` for audit.

When at least one validated field is supported, `render_style()` appends one
clause after the identity sentence and omits missing dimensions. That is the
default render, not a flag: listening on 2026-11-03 preferred styled takes over
identity-only takes for every sampled voice. The identity text stays
byte-identical, so the clause cannot rewrite an acoustic field.

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
`pinned_on`), and approval must quote the same hash. When available it also
stores the exact combined `instruct`, `taxonomy_version`, `classifier_version`,
`prompt_version`, `sampler_version`, `corpus_hash`, and
`sample_manifest_hash`, so the pinned WAV remains traceable to the request and
style evidence that produced it.

### Migration note: cached classifications and profiles

Old cached classifications and generated profiles used a different
`speech_style_analysis` shape:

| Old | New |
|---|---|
| `fields.<field>.confidence` | `fields.<field>.model_self_reported_confidence` (diagnostic only) |
| `fields.<field>.value` with no status | `fields.<field>.value` + `status` in `supported`, `provisional`, `insufficient_evidence`, `unresolved`, `rejected`; legacy `mixed` status maps to `supported` only when the value is `mixed`, otherwise `unresolved` |
| `method` only | `method` (`jev` or `rules_baseline`) + `provider` + `model_id` |
| no citations | `supporting_line_ids`, `counterexample_line_ids`, `support_ratio` |
| no versions | `taxonomy_version`, `classifier_version`, `prompt_version`, `sampler_version` |
| `classification` object without versions | versions copied into `profile.classification` |
| no sample manifest or budget metadata | `sample_manifest_hash`, `budget_reduction` |
| `profile_allocation.style_distinctiveness` | ignored; no replacement until archetypes carry Style labels |

Speech-style schema 2.1 separates `mixed` the classification value from
`mixed` the old decision status. New records use status `supported` (or
`provisional`) for a mixed value; `status: mixed` is rejected. On load, a
legacy `mixed` status is read as `supported` only when its value is `mixed`;
any other legacy pair is read as `unresolved` rather than promoted.

Old cached classifications may still contain identity questions
(`identity.*`, `presentation`, `pitch`, and related acoustic fields) and the
dropped `diction_style` / `contraction_frequency` dimensions; they are ignored.
They are not migrated into taxonomy v3 labels. Rendered Identity no longer appends
an unconditional neutral delivery tail; re-render it rather than preserving a
stale prompt. Old profiles remain valid; missing statuses are read as
`supported` only when a value exists, so existing `speech_style` labels still
render. They are not retroactively treated as benchmark-validated. Re-run
`classify --force` to write the evidence-backed shape. Cached classifications
are reused only when the request key matches; `--force` discards reuse and
writes a new content-addressed snapshot.

`profile.reference_pin.combined_instruct_hash` changes when Style changes, while
`identity_prompt_hash` stays fixed. A pin made before this refactor may not have
`style_profile_hash` or `combined_instruct_hash`; re-audition after regenerating
the reference rather than inventing them.

## Sample selection

`dialogue.build_dialogue_sample_pools` produces two independent pools:

- **Representative** — 40 lines (≤12,000 chars) for the production Style
  request. This pool carries no regex signal tags, so the classifier cannot be
  nudged by the rules baseline's own vocabulary.
- **Targeted** — up to 24 lines (≤4,000 chars) for rare-habit verification.
  Its signal counts are recorded in `sample_manifest.targeted_signal_counts` and
  it is never merged into the primary distribution.

Representative selection:

1. Deduplicate by `voice_line_id`, then drop near-duplicates: trigram Jaccard
   ≥ 0.82 **or** word-set containment ≥ 0.80 with length ratio ≥ 0.5. SE reuses
   quest templates and swaps one noun ("…the contours jus' right" / "…the texture
   jus' right"), which scores 0.70 Jaccard and is now caught.
2. Split the 24 slots across the short/medium/long buckets **by corpus
   share**, giving every present bucket at least one slot, then round-robin over
   `(source, sheet)` inside each bucket, then a stable
   `sha256(canonical_id:line_id)` order. Leftover slots come from the remaining
   stable order.

   Bucket balance used to be an accident of iteration order: taking one line per
   `(source, sheet)` group inside a bucket filled the whole quota from the first
   bucket whenever an NPC had many sheets. Gerolt has 200+ sheets and 181 short /
   953 medium / 947 long unique lines; the old loop produced 24 short lines (148
   words), the quota loop produces 2/12/10 (617 words), so `sentence_complexity`
   is judged on sentences rather than quips.

Every pool has a stable `sample_manifest_hash` over the IDs, texts, and corpus
hash. `analyze_dialogue` records exact duplicate rows and groups separately, so a
deduplicated sample cannot hide a repeated template in the corpus.

`dialect_terms` (aye, ye, yer, fer, nae, 'tis, thou, mayhap, …) weight the
targeted pool and are counted in `signal_line_rates.dialect_lines`, but they are
deliberately **not** part of the formal/colloquial rate: "ye" and "pray" are
archaic, not informal, and folding them in would corrupt the register
measurement.

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
  --only npc_1003075 ...

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

| NPC | unique lines |
|---|---:|
| Pimoh Nelhah | 5 |
| Colina | 5 |
| Diggin | 6 |
| Colden | 7 |
| Hathenbet | 8 |
| Waldew | 8 |
| Knem Cheleim-In | 47 |
| Linu Vanu | 49 |
| Motte | 51 |
| Hachisuba | 51 |
| Erik | 405 |
| Widargelt | 459 |
| Haermaga | 466 |
| wandering minstrel | 1,062 |
| Hildibrand | 1,133 |
| Gerolt | 1,552 |

Each field's status is recorded per dossier from the evidence tiers above; the
count alone does not decide whether a field is supported, provisional, or null.
The rules provider is offline and deterministic; Jev is the opt-in model path.

```bash
python3 -m unittest discover -s tools -p "test_*.py"
```
