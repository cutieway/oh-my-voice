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
| Output plausibility | good on spot check | `"Ye got to make 'im pay!"` -> `colloquialism: high` (0.80), `register: conversational` (0.81), `vocabulary_complexity: plain` (0.50) |

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
```

Request shape the server accepts: `state` is chat messages, `choice` criteria
must be an object (`{"label": "description"}`), `score` criteria a list. The
pipeline's `jev.build_questions()` already emits this.

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
| Frozen benchmark v2 (80 items, 20 development / 60 test, 5 fields) | done, immutable |
| Blind forms + share packets for annotators | done |
| Rules baseline (`RulesProvider`) | runs |
| Qwen3.5-9B-Base baseline (text only / assisted) | runs |
| Human gold labels | **missing** |
| Metrics: accuracy, macro-F1, weighted kappa, mixed/insufficient as classes, precision-at-coverage, per-stratum, leakage audit | implemented |

## 5. Roadblocks

1. **No human gold yet.** Every accuracy claim is unmeasured. Without gold we
   know only that outputs look sane.
2. ~~**Field set mismatch.**~~ **Resolved 2026-11-01**: the question set now asks
   exactly the five validated style fields; `diction_style` and
   `contraction_frequency` were removed, and `generator.VALIDATED_STYLE_FIELDS`
   keeps them out of profiles even if a dossier analysis produces them.
3. **`mixed` and `insufficient_evidence` have no blueprint values.** The model
   returns them, the vocabulary cannot store them. They need a mapping rule
   (e.g. pick the majority label, or fall back to an archetype).
4. **Probabilities are raw scores, not calibrated.** Confidence thresholds are
   usable only after a calibration check on labelled data.

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

- **8+ lines: own blueprint.** The classifier has real text, the fields are
  measurable, and the reference audio follows the NPC's own lines.
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

1. Align the pipeline questions with the validated field set (drop or keep
   `diction_style` and `contraction_frequency` deliberately).
2. Decide the `mixed` / `insufficient_evidence` -> blueprint mapping.
3. Run the classifier over all 8+ line NPCs, write `speech_style` into blueprints,
   and keep the confidence per field.
4. Label ~30 items by hand, measure per-field agreement, then decide whether the
   full protocol is needed.
5. Only then let the pipeline write profiles without review.
