# Advice request: speech-style classification benchmark

Status as of 2026-11-01. The implementation and model runs are complete;
human annotation and adjudication are still pending.

## Immediate issue for review

Before asking people to spend time annotating, we need to decide whether this
benchmark and its six speech-style fields are clear and useful enough to
freeze, or whether the taxonomy or protocol should change first. The 80-item
benchmark has two blank independent annotation forms and no adjudicated gold
labels, so there are no accuracy results yet. Nimble's identical-repeat
agreement is 100%, but agreement fell to 89.7% when evidence order changed and
to 81.3% on alternate 24-line samples; these are stability signals, not
evidence of correctness or incorrectness. The most useful review is whether
to proceed with annotation as designed or first revise ambiguous fields,
low-volume evidence handling, and the proposed release gates.

## Context

Oh My Voice needs a defensible way to describe how an NPC's dialogue is
written so a later voice-design workflow can reuse broad speech-style
archetypes instead of hand-authoring a unique profile for every NPC. The
current experiment asks whether dialogue alone can support useful labels such
as register, vocabulary complexity, sentence complexity, colloquialism,
rhetorical style, and an experimental diction-style field. These labels are
about observable wording; they must not be used to infer a character's voice,
age, accent, identity, personality, or social class.

The immediate question is how to make a small, credible evaluation before
deciding whether any classifier should influence profiles. We want to compare
the existing deterministic rules, a local language model, and eventually a
human-adjudicated reference set. No classifier has been integrated into the
production pipeline.

## Benchmark prepared

- 80 canonical NPC dossiers selected from the dialogue database, split into
  20 development and 60 locked-test items.
- Balanced across four dialogue-volume strata: 20 each with 5–20, 21–50,
  51–200, and more than 200 source dialogue rows.
- Each dossier uses up to 24 frozen, anonymized text lines. The 5–20 stratum
  cannot provide 24 distinct lines for every NPC; 21 dossiers have fewer than
  24, with a minimum of five. Lines are not duplicated to pad samples.
- Two separate blind annotation forms and a guide are ready. Each annotator
  labels six fields and evidence sufficiency independently. Forms contain no
  NPC identities, demographics, split labels, or model predictions. Raw forms
  remain blank; adjudicated gold labels do not exist yet.
- The 60-item locked test is balanced at 15 NPCs per dialogue-volume stratum,
  not proportional to the full eligible population. The evaluator reports
  per-stratum results and explicitly marks its aggregate metrics as
  unweighted; population-level estimates would need an agreed target
  distribution and weights.
- The local benchmark artifacts are under
  `data/blueprints/evaluation/benchmark_v1/`. That directory is ignored by
  Git because it includes a private identity manifest, predictions, and data
  derived from the dialogue database. Only the runner and this advice summary
  are committed.

## Results so far

- The deterministic full-data and sample-only rules baselines were generated
  for all 80 dossiers. Rule scores are not treated as calibrated confidence.
- Bespoke-Nimble-9B-v3 Q4_K_M was run through llama.cpp's native
  `/v1/systemone` decision endpoint on the same sample-only dossiers. It
  completed 80/80 with zero failures. Median latency was 4.72 seconds per
  dossier and p95 was 5.56 seconds on an AMD Radeon RX 9060 XT (16 GiB).
  The output includes per-label model scores, which are not assumed to be
  calibrated probabilities.
- The server initially returned HTTP 500 for a few requests at 4K context per
  slot. Restarting with 8K context per slot resolved the failures; the
  complete rerun succeeded.
- Qwen3.5-9B-Base Q4_K_M completed constrained JSON generation for 80/80 with
  zero failures. Median latency was 3.02 seconds and p95 was 4.44 seconds.
  It returns labels only, so no confidence distribution is claimed. This is a
  third-party quantization of the official Apache-2.0 base checkpoint. The
  Nimble fine-tune has a separate CC BY-NC 4.0 license.
- Nimble was repeated, reordered, and evaluated on fixed, alternate, and
  expanded text subsets for all 60 locked-test NPCs. It made 306 requests with
  zero failures. Identical repeats matched all 360 field decisions; reordering
  matched 89.7%, below the proposed 95% target. The fixed 12-line subset
  matched 88.1%. Alternate 24-line and expanded 36-line subsets matched 81.3%
  and 86.4%, respectively, across the 33 NPCs with enough unique lines. These
  are descriptive robustness measurements; no test outputs were used to tune
  prompts or thresholds.
- Reordering agreement by field: 95.0% register, 80.0% vocabulary
  complexity, 86.7% sentence complexity, 98.3% colloquialism, 95.0% rhetorical
  style, and 83.3% diction style. On alternate 24-line samples, rhetorical
  style agreement was 54.5% and vocabulary complexity was 69.7%.
- OpenRouter/JEV is not configured in this benchmark.
- A provenance audit found that all 80 frozen `rules_full` records reused the
  sample dossier hash instead of the source dossier hash. Their full-corpus
  labels and measurements remain intact, but that field does not identify the
  evidence actually used. The 80 sample-only rule hashes match. The runner is
  fixed for future preparations, and
  `reports/baseline_input_provenance.json` records the expected hashes for
  these frozen files; no data artifacts were overwritten.
- There are 960 missing human field labels (80 items × 6 fields × 2
  annotators). Therefore accuracy, macro-F1, agreement, calibration, and any
  acceptance threshold cannot yet be reported. These model and stability
  results say nothing about classification correctness.

Operational observations on the RX 9060 XT: Nimble process RSS was about
0.92 GiB and Qwen Base process RSS about 0.95 GiB. System-wide VRAM use while
loaded was 13.45 GB for Nimble and 12.83 GB for Qwen Base, out of 17.10 GB
total. `rocm-smi` reports system-wide VRAM, not process-attributed memory;
desktop graphics may contribute. Detailed run, input-size, token, memory, and
per-field stability reports are local under
`data/blueprints/evaluation/benchmark_v1/reports/` and are not tracked by Git.
Both models had a median frozen speech-style state size of 4,204 UTF-8 bytes
(p95 5,699 bytes). Serial projections from the 80-item median latency are
about 7.9 minutes / 78.7 minutes for 100 / 1,000 Nimble dossiers and 5.0
minutes / 50.4 minutes for Qwen Base; these exclude startup and other overhead.
Qwen Base reported 144,896 prompt tokens and 3,938 completion tokens over the
80 requests. The Qwen checkpoint is the pretrained base model, not an
instruction-tuned model, so this comparison describes that specific
constrained-generation baseline rather than a tuned chat model.

## Current implementation

`tools/blueprint_pipeline/evaluation.py` prepares the frozen sample-only
dossiers, blank annotation forms, deterministic baselines, local-model
predictions, and evaluation reports. Model inputs are rebuilt from dialogue
text and measurements computed from those exact samples; the model does not
receive NPC identity, demographics, lore, or deterministic classifications.
The evaluation code is standalone and does not alter production profile
generation.

Returned annotation forms now have a strict import path. From the repository
root, submit the two independently completed ZIP packets with:

```bash
python3 -m tools.blueprint_pipeline.evaluation import-annotations \
  --annotator-a /path/to/annotator-a.zip \
  --annotator-b /path/to/annotator-b.zip
```

The command validates all 80 IDs, frozen dialogue samples, five labels per
item, and evidence fields, then preserves the original ZIPs and parsed forms
under the ignored benchmark data directory in a new immutable import run. Use
the returned run ID to create an anonymized adjudication form containing the
dialogue and both label sets plus their evidence notes:

```bash
python3 -m tools.blueprint_pipeline.evaluation prepare-adjudication \
  --annotation-run RUN_ID
```

The 12-item pilot uses the same commands with `--scope pilot` on import,
adjudication preparation, adjudication import, and evaluation. A run's scope is
recorded in the adjudication form and import manifests, and every adjudication
step resolves the run with the requested scope, so a pilot run cannot be
adjudicated or scored as a full run (or the reverse). Pilot evaluation reports
inter-annotator agreement, the pilot gate, and pilot adjudication status only;
it never scores locked-test items.

After completing `gold.json`, import it and generate the evaluation report:

```bash
python3 -m tools.blueprint_pipeline.evaluation import-adjudication \
  --annotation-run RUN_ID --source /path/to/completed-gold.json
python3 -m tools.blueprint_pipeline.evaluation evaluate \
  --annotation-run RUN_ID
```

Imports preserve each submission as a new revision and reject changes to the
frozen dialogue or either annotator's original responses. No human annotations
have been imported yet; this workflow is ready for the returned packets.

Once annotations are returned, the evaluator reports per-field accuracy,
macro-F1, balanced accuracy, class precision/recall, confusion matrices,
linear weighted kappa and mean ordinal distance where appropriate, paired
bootstrap comparison against the best rules baseline, calibration and
top-choice margin buckets, abstention and auto-accept coverage. Bootstrap
intervals are reported for accuracy, macro-F1, per-class metrics, ordinal
metrics, accepted precision, and coverage. It also breaks locked-test
classification metrics down by dialogue-volume stratum. Thresholds are
selected per field on development annotations to maximize coverage while
meeting the proposed 0.90 precision and 0.50 coverage targets, then evaluated
on locked test; a fixed global CLI override is available. Existing stability
reports cover repeats, ordering, and sample sizes.

Macro-F1 averages categories observed in either gold or predictions, so
predicted-only classes are penalized. Per-class precision or recall is reported
as undefined when its denominator is zero rather than as a fabricated zero.

## Questions for review

1. Is the six-field label set useful and sufficiently observable from dialogue
   alone? Which labels overlap, are underspecified, or should be removed?
2. Is an 80-item benchmark with a 20/60 development/test split and balanced
   dialogue-volume strata a reasonable first validation, given that the
   lowest-volume group often has only 5–20 unique lines?
3. Are the annotation instructions and proposed adjudication process strong
   enough to produce useful gold labels? What agreement statistic and minimum
   agreement threshold would be appropriate for nominal versus ordinal fields?
4. Which evaluation metrics and release gates should determine whether a
   classifier is useful for generating archetype suggestions? How should
   `mixed` and `insufficient_evidence` be scored, and should abstention be
   measured separately?
5. Is it methodologically sound to give the model sample-derived measurements
   alongside the text? Could that bias the comparison against the rules
   baseline, and should there also be a text-only condition?
6. Should the experimental `diction_style` field be retained, or does it add
   subjective overlap without enough practical value?
7. What additional controls are needed to prevent NPC identity, genre
   conventions, source type, or sample size from acting as shortcuts?
8. If validated, should these outputs become reusable archetype suggestions
   with human approval, and what constraints would keep suggestions separate
   from approved per-NPC profiles?
9. Given the balanced dialogue-volume strata, should the benchmark also report
   population-weighted estimates? If so, what eligible population and stratum
   distribution should define those weights?

## Requested advice format

Please critique the benchmark design and recommend concrete changes before
human annotation begins. Separate methodological blockers from useful
improvements. Do not infer model accuracy from the current predictions or
runtime measurements. The local predictions are not included in this summary
so the advice can focus on the evaluation design without exposing test-set
outputs.
