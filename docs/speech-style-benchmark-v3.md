# Speech-style benchmark v3 protocol

Status: **not run**. No annotator data, JEV predictions, or stability runs exist
for v3. Do not describe any Style field as validated.

Code: `tools/blueprint_pipeline/evaluation.py` (`BENCHMARK_VERSION =
"speech-style-benchmark-v3"`). Artifacts: `data/blueprints/evaluation/benchmark_v3/`.
Benchmark v2 at `benchmark_v2/` is frozen under taxonomy v2 and is never rebuilt
with these labels.

## Items and split

- Reuse the 80 v2 NPC items without changing their dialogue snapshots (24
  lines each) so versions stay comparable.
- Freeze an NPC-level development/test split with the manifest seed
  `oh-my-voice-benchmark-v3-2026-11-03`. No NPC appears in both sides, and no
  split information reaches annotators or the model.
- Fields: `register`, `lexical_complexity`, `sentence_complexity`,
  `colloquialism`, `discourse_style`. Each has `mixed` and
  `insufficient_evidence` as annotation options.
- Blind forms show only anonymized dialogue and the field definitions. They
  never show Identity, lore, game metadata, voice data, model output, or split.

## Annotation

1. Two annotators label an independent 12-NPC pilot blind.
2. Report per-field agreement (Cohen's kappa for nominal, weighted kappa for
   ordinal). A field with poor agreement is revised and re-piloted; a field
   that still fails does not enter the production Style prompt.
3. Adjudicate disagreements to produce gold labels.
4. Annotators complete all 80 items only after the pilot field set is stable.
5. Store blank forms and adjudication under the benchmark root; the "not run"
   state means no completed labels exist.

## Prediction and tuning

1. Run the local JEV endpoint (`http://127.0.0.1:8081/v1/systemone` default) and
   the `RulesProvider` baseline, each explicitly tagged.
2. Tune prompts, taxonomy wording, and thresholds only on the development side.
3. Evaluate once on the locked test side.
4. JEV stability: repeat each item, permute line order, and vary sample size.
   Report value and status agreement per field; instability makes a field
   ineligible for production rendering.

## Metrics

Macro-F1; per-class precision and recall; confusion matrices; Cohen's kappa
(weighted where ordinal); mixed/abstention accuracy; selective accuracy at
accepted coverage; accepted coverage; citation validity; and repeat/order/
sample-size stability. Report by split and by volume stratum.

## Gates

A Style field may enter the production prompt only when:

- pilot and full gold agreement clear the protocol threshold;
- test macro-F1 and per-class behaviour are reported, not assumed;
- citation validity is high and no invalid ID is accepted;
- JEV stability clears the repeat/order/sample thresholds;
- a controlled VoiceDesign A/B shows the field improves character fit without
  harming stable identity or naturalness.

`discourse_style` additionally needs the pilot to support the new taxonomy;
otherwise it stays audit-only.

## Current evidence

| Requirement | State |
|---|---|
| v2 benchmark immutable | frozen |
| v3 manifest and NPC split | generated: 80 items, 20 development / 60 locked test |
| 12-NPC two-annotator pilot | not_run; blank forms exist |
| Full 80-item gold | not_run; forms blank, gold pending |
| Local JEV predictions | not_run |
| JEV repeat/order/sample stability | not_run |
| Rules baseline | runs; explicitly non-validated |
| VoiceDesign A/B matrix | pending |

Every Style field remains experimental until these rows change.
