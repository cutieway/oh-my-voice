# Speech-style classification: current state

Goal: a local classifier reads an NPC's dialogue and fills the blueprint fields.
Blueprint -> reference audio -> generated voices is already proven. This document
records how far the classifier part is, what is verified, and what still blocks
trusting its output.

Code: `tools/blueprint_pipeline/`. Artifacts: `data/blueprints/evaluation/`.

## 1. The chain we are building

```
dialogue lines -> classifier -> blueprint -> reference audio -> generated voice
                  ^^^^^^^^^
                  this document
```

The classifier only writes `speech_style` fields. Acoustic identity (pitch,
timbre, pace) comes from game data, x-vectors, and voice evidence, and is not
part of this question.

## 2. Evidence sources, in priority order

1. **In-game data only.** `dialogue.db` lines per speaker, `ENpcResident` /
   `ENpcBase` metadata, and the SCD voice files. This is measured, offline, and
   what dossiers are built from. The MediaWiki source was removed on 2026-11-03.
2. **Anything else only if it adds signal.** SCD audio through x-vectors,
   existing hand-authored archetypes as a sanity check. Each addition needs a
   reason; volume is not a reason.

## 3. Verified working (2026-11-01)

The runtime already exists; nothing needs installing.

| Piece | Status | Evidence |
|---|---|---|
| `llama-server` with `/v1/systemone` | works, no install | pin `llama-cpp-12032` ships the route in `libllama-server-impl.so`; the model rejects text generation with `use /v1/systemone` |
| `Bespoke-Nimble-9B-v3-Q4_K_M.gguf` | loads and answers | ROCm, 9B Q4_K_M, ~1.9 s per NPC, ~10k input tokens per request |
| Pipeline call path | works end to end | `evaluation.py nimble --condition text_only --limit 2` returned 5 fields with probability distributions, zero failures |
| Output plausibility | good on spot check (taxonomy v2, historical) | `"Ye got to make 'im pay!"` -> `colloquialism: high` (0.80), `register: conversational` (0.81), `vocabulary_complexity: plain` (0.50) |

Compose draft profiles (archetype acoustics + classified style) and audition
them:

```bash
python3 -m tools.blueprint_pipeline.cli compose --only npc_1003075
./scripts/voicebank.sh --samples 1 data/blueprints/profiles/<profile>.json
```

Ten-NPC audition from 2026-11-01: `data/samples/voicebank/jev10/` (`montage.wav`
plays all ten cloned lines in order; `index.html` has references, lines, and the
exact instruct per voice).

Reproduce:

```bash
llama-server --model data/models/qwen3.5-9b/Bespoke-Nimble-9B-v3-Q4_K_M.gguf \
  --port 18081 --n-gpu-layers 999 --ctx-size 8192 --jinja
python3 -m tools.blueprint_pipeline.evaluation nimble \
  --endpoint http://127.0.0.1:18081/v1/systemone --condition text_only --limit 2
python3 -m tools.blueprint_pipeline.evaluation jev \
  --endpoint http://127.0.0.1:8081/v1/systemone --model jev-1.14.0 --limit 2
```

Request shape the server accepts: `state` is chat messages, `choice` criteria
must be an object (`{"label": "description"}`), `score` criteria a list. The
pipeline's `jev.build_questions()` already emits this.

## 3a. Taxonomy v3 status (2026-11-03)

The five production fields are now `register`, `lexical_complexity`,
`sentence_complexity`, `colloquialism`, and `discourse_style`, with construct
definitions in `blueprints/vocabulary.json` and the rename/meaning changes in
`blueprints/migrations/style-taxonomy-v3.md`. Benchmark v2 is frozen and stays
immutable; v3 labels and definitions differ, so v2 scores do not transfer.

| Item | Status | Evidence |
|---|---|---|
| Taxonomy v3 defined and versioned | implemented | `vocabulary.json` `taxonomy_version: 3.0`; questions and definitions are non-overlapping |
| Application-derived status and mixed gates | implemented | Production validation requires three valid cited lines per field, two recurring patterns with three unique cited lines each for `mixed`, and two independent contexts |
| Local default, remote opt-in | implemented | Loopback endpoint is the default; non-loopback requires `--allow-remote` |
| Anonymized request (opaque IDs, text, aggregate counts) | implemented | No NPC identity, lore, source names, or sheet names |
| Request-visible evidence tiers | implemented | 40-line representative capacity reaches `strong` (40 lines / 600 words / 3 contexts) |
| Benchmark v3 dev/test split | **done** | 80 items, 20 development / 60 locked test, seeded NPC split; forms are blank |
| Two-annotator blind pilot (12 NPCs) | **not_run** | Blank v3 forms exist; no completed labels |
| Human gold labels | **not_run** | `adjudicated/gold.json` is `pending_independent_annotations_and_adjudication` |
| Local JEV predictions | **not_run** | No live JEV endpoint was available |
| JEV repeat / order / sample-size stability | **not_run** | Blocked on predictions |
| Rules baseline predictions | runs | Explicitly tagged `rules_baseline`; not a validation result |
| VoiceDesign A/B of Style fields | **pending** | Protocol in `docs/style-ab-2026-11-02.md` is v2 history; v3 matrix not run |

No accuracy claim is made. Every Style field is experimental until the v3 pilot
and gold labels exist. `discourse_style` remains the least established field
because its taxonomy is new. Full protocol:
`docs/speech-style-benchmark-v3.md`; reference A/B protocol:
`docs/style-ab-v3-protocol.md`.

## 3b. Where style is allowed to act

Style is an input to **reference generation only** — the VoiceDesign step that
produces the one clip a pack is built from. Dialogue lines are then cloned from
that reference; the Base model never reads a style instruction, so a per-line
style would be a second, undocumented control path.

Current code path: `blueprint.py render_style()` appends the clause to the
identity instruct, and every plan carries the resulting instruct. There is no
opt-in flag. Style was opt-in from 2026-11-02 until 2026-11-03, when listening
preferred it for every sampled voice; the mood layer that confounded the earlier
A/B was removed at the same time. See `style-ab-2026-11-02.md` for that history.

Measured effect, one profile, one seed, otherwise identical inputs:

| Reference | Duration | Median F0 |
|---|---:|---:|
| identity only | 9.52 s | 96.0 Hz |
| identity + style clause | 10.32 s | 114.3 Hz |

Re-rendering the identity-only plan twice produced byte-identical WAVs
(`sha256 739ed00d…`), so the shift is caused by the clause, not sampling noise.
The clause therefore moves the reference voice, including pitch; that is the
sanctioned effect and the reason the identity text is kept byte-identical, so
the move is always attributable to the style clause alone.

## 4. What the benchmark harness adds

`evaluation.py` freezes 80 NPCs into blind items and can score model output
against human labels. It exists so a bad classifier cannot silently write
blueprints. It is infrastructure, not a prerequisite for trying the classifier.

| Piece | State |
|---|---|
| Frozen benchmark v2 (80 items, 20 development / 60 test, 5 fields) | done, immutable; split is by NPC, not line; taxonomy v2 only |
| Benchmark v3 (80 items, 20 development / 60 locked test, v3 labels) | manifest and blind forms frozen; labels blank, gold pending |
| Blind forms + share packets for annotators | done; both annotation files are present but blank |
| Rules baseline (`RulesProvider`) | runs |
| Qwen3.5-9B-Base baseline (text only / assisted) | runs |
| Human gold labels | **missing**; `adjudicated/gold.json` has empty labels |
| Metrics: accuracy, macro-F1, Cohen's kappa (weighted for ordinal fields), confusion, mixed/insufficient detection, abstention coverage, selective accuracy, precision-at-coverage, per-stratum, leakage audit, citation validity, repeat/order/sample stability | implemented; reported only where predictions and labels exist. Sample-size/order/repeat runner currently covers the local Nimble baseline; JEV stability stays staged until a JEV endpoint and predictions exist. |
| Evidence-citation validation | implemented in production and the JEV benchmark runner; a cited line absent from the request rejects the field. Per-field citation rate and validity rate are reported from stored predictions even before gold labels. |
| Production status | staged: benchmark has not cleared, so classifier output is not treated as calibrated |

## 5. Roadblocks

1. **No human gold yet.** Every accuracy claim is unmeasured. Without gold we
   know only that outputs look sane.
2. ~~**Field set mismatch.**~~ **Resolved 2026-11-01**: the question set now asks
   exactly the five validated style fields; `diction_style` and
   `contraction_frequency` were removed, and `generator.VALIDATED_STYLE_FIELDS`
   keeps them out of profiles even if a dossier analysis produces them.
3. **`mixed` is a value, not a status.** The production dimensions accept
   `mixed` as a real classification value paired with `status: supported` (or
   `provisional`). An insufficient evidence result is stored as `value: null`
   with `status: insufficient_evidence`, and the renderer omits it. A `mixed`
   value requires two recurring patterns, each citing at least three unique request
   lines across at least two independent contexts; one counterexample is not
   evidence of mixture. Any answer using `status: mixed` is rejected, even if its
   value is valid.
4. **Probabilities are raw scores, not calibrated.** Model self-reported
   confidence and probability distributions are diagnostic only. Acceptance
   depends on valid labels, cited line IDs, the evidence floor, stability, and
   held-out benchmark metrics.

## 6. Decisions

### Archetypes or one blueprint per NPC?

Population in `data/dialogue.db`:

| Lines per speaker | Speakers | NPCs with an id |
|---|---:|---:|
| any | 9,504 | 4,463 |
| >= 8 | 4,170 | 2,736 |
| >= 24 | 1,648 | 1,326 |
| >= 50 | - | 760 |

At ~1.9 s per NPC, per-NPC blueprints for all 2,736 speakers with 8+ lines cost
about 90 minutes of one local model run. Cost is not the constraint.

Recommendation:

- **8+ lines: eligible for classification.** The classifier has real text and
  the fields are measurable, but the current allocation policy still requires
  at least 50 unique lines and two supported Style fields for an individual
  design; otherwise the NPC uses a shared archetype.
- **< 8 lines: shared blueprint.** There is not enough text to classify. Group
  these by speaker role in the game data and name the group after a
  representative NPC (e.g. `gridanian_sentry`), not after an abstract
  temperament (`masculine_warm_mid`).
- **Archetypes keep one job:** a fallback and a sanity check for the low-evidence
  tail. They are not the primary design.

Reason: an abstract archetype is a guess about a character; a named exemplar is a
measurement of one. Clustering should follow measured style, and the cluster name
should point at the NPC whose lines defined it.

### How much human annotation is worth it?

| Option | Cost | What it buys |
|---|---|---|
| Full protocol: 2 blind annotators, 12-item pilot gate, then 80 items | highest | real accuracy numbers, publishable |
| **1 annotator, ~30 items, spot check** | a few hours | catches fields the model cannot do at all |
| No annotation: ship with confidence thresholds and review only low-confidence items | lowest | no accuracy number; errors reach audio |

Recommendation: the middle option first. Run the classifier over everything,
have one person label ~30 items, and look at per-field agreement. The full
two-annotator protocol only pays off once the field set has stopped moving.

## 7. Order of work

1. ~~Align the pipeline questions with the validated field set~~ done: five
   Style fields only; `diction_style` and `contraction_frequency` stay out.
2. ~~Decide the `mixed` / `insufficient_evidence` mapping~~ done: `mixed` is a
   classification value with status `supported` or `provisional`; insufficient
   evidence is `value: null` plus status.
3. Run the classifier over all 8+ line NPCs, write `speech_style` into blueprints,
   and keep per-field status and citations.
4. Complete the two blind annotation forms and adjudication; measure per-field
   agreement and citation validity before calling any field validated.
5. Only then let the pipeline write profiles without review. Until then,
   benchmark status is **staged**, not validated.
