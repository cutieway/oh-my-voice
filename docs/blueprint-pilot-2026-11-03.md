# Style pilot, 2026-11-03: what ran and what did not

Scope: prove that the Style evidence chain, the matched audition renders and the
human artifact agree, and hand a reviewer an auditable set. This is a plumbing
and evidence-integrity pilot, **not** a validation of the Style taxonomy. No
blind human judgment, acoustic analysis, or JEV stability run exists.

## Classification (local JEV, one run per NPC)

Model: `Bespoke-Nimble-9B-v3-Q4_K_M.gguf` served by `llama-server` on
`127.0.0.1:8081`, `--ctx-size 8192`. Evidence policy
`style-evidence-3.1`; evidence pass `jev-evidence-pass-1.0`.

| NPC | Race / sex | Unique lines | Supported fields | Status |
|---|---|---:|---|---|
| npc_1005512 Hildibrand | unknown / unknown | 1133 | colloquialism, lexical_complexity, register, sentence_complexity | ready_for_audition |
| npc_1013274 Stephanivien | Elezen / male | 454 | colloquialism, lexical_complexity, register, sentence_complexity | ready_for_audition |
| npc_1010139 Oboro | Hyur / male | 521 | colloquialism, lexical_complexity, register, sentence_complexity | ready_for_audition |
| npc_1030775 Ronitt | Lalafell / male | 390 | colloquialism, lexical_complexity, register, sentence_complexity | ready_for_audition |
| npc_1025498 Mikoto | Au Ra / female | 452 | colloquialism, lexical_complexity, register, sentence_complexity | needs_review |
| npc_1009943 Jacke | unknown / unknown | 515 | colloquialism, lexical_complexity, register | needs_review |
| npc_1026852 Martyn | Hyur / male | 636 | colloquialism, lexical_complexity, register | needs_review |
| npc_1005501 Nashu Mhakaracca | Miqo'te / female | 660 | colloquialism, register | needs_review |
| npc_1003075 Gerolt | Hyur / male | 1552 | colloquialism, register | needs_review |
| npc_1005513 Godbert | Hyur / male | 708 | lexical_complexity, sentence_complexity | needs_review |
| npc_1009242 Haermaga | Roegadyn / male | 466 | lexical_complexity | needs_review |
| npc_1013969 Sidurgu | Au Ra / male | 458 | colloquialism, lexical_complexity, sentence_complexity | needs_review |
| npc_1020995 X'rhun Tia | Miqo'te / male | 528 | lexical_complexity, sentence_complexity | needs_review |
| npc_1007900 Jihli Aliapoh | Miqo'te / female | 511 | colloquialism, lexical_complexity, register, sentence_complexity | needs_review |
| npc_1004093 Serendipity | Hyur / female | 451 | colloquialism | needs_review |

`needs_review` is the gate working, not a bug: every rejected field failed a
documented threshold (fewer than three ranked support lines, or a `mixed` value
whose pattern questions returned fewer than three lines each). No threshold was
loosened to reach `ready_for_audition`. `discourse_style` is `provisional` for
npc_1005512, npc_1013274, npc_1010139 and npc_1030775: stored, not rendered,
because its field tier is below supported.

Recorded `input_tokens` per classification is 60k–78k for the two passes
combined; the endpoint reports `output_tokens: 0`, so that figure is not a
response size.

## Audition (matched identity vs identity+style)

Only profiles with real game identity facts produce a meaningful identity
prompt. npc_1005512 has no race or sex in its dossier, so its identity prompt
renders as the single sentence `A voice.`; its renders prove the plumbing but
cannot support a style claim. npc_1013274, npc_1010139 and npc_1030775 carry
real race/sex facts and are the usable subjects.

| Profile | Conditions | Seeds | Reference seconds |
|---|---|---:|---|
| npc_1013274 Stephanivien | identity_only, identity_style | 20881189, 20882189, 20883189 | 12.96 / 11.84 / 11.28 (identity only); 12.32 / 12.56 / 9.92 (identity+style) |
| npc_1005512 Hildibrand | identity_only, identity_style | 20740153, 20741153, 20742153 | 11.20 / 10.72 / 10.80; 10.96 / 12.80 / 12.32 |
| npc_1010139 Oboro | identity_only, identity_style | 20551045, 20552045, 20553045 | 11.20 / 9.28 / 10.48; 11.76 / 10.80 / 10.32 |
| npc_1030775 Ronitt | identity_only, identity_style | 20352976, 20353976, 20354976 | 11.04 / 10.96 / 11.68; 10.96 / 10.32 / 10.08 |

All 24 takes are inside the 8–18 s acceptance range and every sha256 in
`report.json` matches the file on disk. Artifacts:

```text
data/blueprints/auditions/npc_1013274/{plans,voices,report.json,style-audit.html}
data/blueprints/auditions/npc_1005512/{plans,voices,report.json,style-audit.html}
data/blueprints/auditions/npc_1010139/{plans,voices,report.json,style-audit.html}
data/blueprints/auditions/npc_1030775/{plans,voices,report.json,style-audit.html}
```

Re-rendering `identity_style.seed-20551045` from its plan in a clean output
directory reproduced the same file: sha256
`60b126bc6df696532b429d9f432ebf44069421190c8d76b9c0760ce49af03b9f` both times.

Each `style-audit.html` groups the six clips into three matched seed pairs and
shows per clip the condition, seed, duration, sha256, and the exact instruct.
It also renders the recorded post-trim request sample: 24 opaque IDs with text,
plus 11–14 cited lines per NPC that resolve to those anchors.

Sample capture had two defects, both fixed in `classifier.py`:

1. `representative_sample` was keyed on `sample["id"]`, which real dossier rows
   do not carry, so every recorded request sample was empty and the audit page
   could only say "not recorded".
2. The sample was re-derived from the trimmed dossier rows rather than copied
   from the state that was actually sent, so its order could differ from the
   request.

It is now an exact ordered copy of `sent_state["dialogue_samples"]`, with the
opaque ID falling back to the same `sample_id()` the request uses. The four
pilot NPCs were reclassified after the fix (input tokens 60,492–63,090 each);
each recorded sample equals the request state byte for byte and every citation
resolves to an anchor on the page.

## Not run

| Item | State |
|---|---|
| Human listening, any clip | not_run |
| Any `reference_pin` on a production profile | not_run — pinning an unlistened WAV would fabricate a review record |
| Blind A/B ranking and character-fit judgments | not_run |
| Acoustic diagnostics (F0, RMS, embeddings per cell) | not_run |
| JEV repeat / order / sample stability | not_run — one classification per NPC |
| Base ICL same-speaker check on these profiles | not_run |

The six pin snapshots produced while smoke-testing `review audition` were
unlistened agent output, so they were moved to
`data/backup/auditions/npc_1013274/pinned-unlistened-agent-smoke/` rather than
left where they could be mistaken for human review.

## Commands

```bash
llama-server --model data/models/qwen3.5-9b/Bespoke-Nimble-9B-v3-Q4_K_M.gguf \
  --port 8081 --n-gpu-layers 999 --ctx-size 8192 --jinja

python3 tools/blueprint_pipeline/cli.py classify --provider jev \
  --only npc_1013274 npc_1010139 npc_1030775 npc_1005512 --limit 4 --force

python3 tools/blueprint_pipeline/cli.py audition \
  data/blueprints/profiles/npc_1013274.b7fb01c63ebf721e.json --seeds 3

./scripts/audition.sh data/blueprints/auditions/npc_1013274/plans \
  data/blueprints/auditions/npc_1013274

python3 tools/blueprint_pipeline/cli.py audition \
  data/blueprints/profiles/npc_1013274.b7fb01c63ebf721e.json --seeds 3 \
  --report data/blueprints/auditions/npc_1013274/report.json

python3 tools/blueprint_pipeline/cli.py inspect npc_1013274 \
  --audit-html /tmp/npc_1013274-style-audit.html
```

Pin a real listening decision only after a human has heard the clip:

```bash
python3 tools/blueprint_pipeline/cli.py review audition <profile> --by <reviewer> \
  --reference <wav> --condition identity_style --seed <seed>
```

## What a reviewer must decide

1. For each matched pair, does the Style clause improve character fit without
   caricature or damaging naturalness?
2. Does one identity-only take already read as the character (i.e. is the clause
   doing anything)?
3. Is the Style instruction honest about what the dialogue supports? Open
   `style-audit.html` and compare a field's cited lines with its label.

Answers belong in `review audition` / `review approve`, not in this document.
