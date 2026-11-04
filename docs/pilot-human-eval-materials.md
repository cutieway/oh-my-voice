# Pilot human evaluation materials (2026-11-04)

Two independent human tasks are ready to hand out. Neither reveals JEV's
predictions, NPC identities, races, or generated audio inside the annotation
task.

## 1. Blind speech-style annotation (12 NPCs, 2 annotators)

Packets (immutable, hashed in `pilot_manifest.json`):

```text
data/blueprints/evaluation/benchmark_v3/annotations/share_packets/
├── pilot_annotator_a.zip
├── pilot_annotator_b.zip
└── pilot_manifest.json
```

Each ZIP holds the same four files:

| File | Use |
|---|---|
| `pilot_annotator_a_form.html` / `pilot_annotator_b_form.html` | Offline form. Double-click; fill in; press **Download completed form**. |
| `README.md` | Instructions for both paths. |
| `annotation_guide.md` | Rubric, decision order, boundary examples, gates. |
| `annotation_form.json` | Blank JSON for a text-editor workflow instead of the page. |

Rules that matter to the annotators:

- You are not told who the speaker is, and you do not need to know. Every field
  is a property of the text: "formal" means the wording is formal, not that the
  speaker is noble. Each field prints the exact question it asks.
- Give one packet to each person. They must not discuss labels before both are
  finished, and must not look up characters.
- Everything is local: the HTML page has no network access and no dependencies.
- The page counts missing requirements live and refuses to export while any
  field is incomplete or inconsistent (for example `mixed` with one cited line,
  `insufficient_evidence` with a cited line, or more than three cited lines).
- Two of the twelve items (`B734FDB9B`, `B00671355`) have 5 usable lines, below
  the 8-line evidence floor, so most fields there should end up
  `insufficient_evidence`. That is the protocol working, not a mistake.

### Importing the results

```bash
python3 -m tools.blueprint_pipeline.evaluation import-annotations \
  --scope pilot \
  --annotator-a <a_completed.json or .zip> \
  --annotator-b <b_completed.json or .zip>
```

`import-annotations` accepts either the downloaded JSON or the ZIP with the form
replaced. Save the returned `annotation_run`.

```bash
python3 -m tools.blueprint_pipeline.evaluation evaluate \
  --scope pilot --annotation-run <ANNOTATION_RUN>
```

The pilot report is written to `reports/pilot_evaluation.json` and contains
per-field exact agreement, Cohen's kappa (linear-weighted for the three ordinal
fields), evidence-sufficiency agreement, the per-field pilot gate, and the
pilot adjudication status. It never scores the locked test set.

### Adjudicating

```bash
python3 -m tools.blueprint_pipeline.evaluation prepare-adjudication \
  --scope pilot --annotation-run <ANNOTATION_RUN>
```

Fill `adjudicated_labels` and `adjudication_reasons` in the generated
`adjudicated/forms/<run>/gold.json` without touching `dialogue_samples` or
either annotator's raw labels and evidence, then:

```bash
python3 -m tools.blueprint_pipeline.evaluation import-adjudication \
  --scope pilot --annotation-run <ANNOTATION_RUN> --source <completed-gold.json>
python3 -m tools.blueprint_pipeline.evaluation evaluate \
  --scope pilot --annotation-run <ANNOTATION_RUN>
```

Scope is pinned in every manifest: a pilot run cannot be adjudicated or scored
as `--scope full`, and the reverse is rejected too.

## 2. Blind Identity vs Identity+Style listening

The existing `style-audit.html` pages label the condition and always list
identity-only first, so they are exploratory only. The blind kit is generated
from the same recorded takes:

```bash
python3 -m tools.blueprint_pipeline.evaluation blind-listening
```

```text
data/blueprints/auditions/blind_ab/
├── dist/                 <-- SEND ONE ZIP PER PERSON
│   ├── A.zip  B.zip  C.zip  D.zip  E.zip
│   └── F.zip  G.zip  H.zip  I.zip  J.zip
├── judge_A_blind_ab.html … judge_J_blind_ab.html
├── judge_A_key.json      (organiser only, never sent)
│   … judge_J_key.json
├── clips/                48 WAVs, opaque hash names
└── manifest.json
```

Each `dist/<letter>.zip` is self-contained: `listening.html`, `clips/` (48
WAVs), and a short `README.txt`. Open the HTML, work through it, press
**Download answers**, send back `<letter>_answers.json`. Keys are never
packaged, so a judge cannot unblind their own kit — and the page never mentions
a key, because none is sent.

```bash
python3 -m tools.blueprint_pipeline.evaluation blind-listening \
  --judges A B C D E F G H I J --pack data/blueprints/auditions/blind_ab/dist
```

Each judge gets an independent random order and side assignment (verified: 10
distinct assignments out of 10, 54 of 120 pairs putting Style on the left).

`data/blueprints/auditions/<npc>/` holds the render source — the pinned
references and the cloned sentences under `<npc>/same_voice/`. Those WAVs are
byte-identical to files already inside `blind_ab/clips/` (verified: all 24 lines
and all 8 references). They are kept beside each NPC for provenance, not for
distribution, and they still carry condition names in their paths.

**Answering "does it fit the character" without naming the character.** Each
pair carries a *speaker brief*: up to three of that speaker's other lines, with
proper nouns, placeholders and stage directions stripped. The judge is told
"these lines are the same speaker as both clips" and rates fit against the
manner of that text. The brief is identical for both sides of a pair, so it
cannot hint which side has the Style clause, and it never names the character.

**Only questions one clip can answer are asked:**

| Question | Anchors |
|---|---|
| Fit to the speaker's other lines | 1 = different kind of speaker · 5 = plainly the same manner |
| Naturalness | 1 = obviously synthetic · 5 = could pass as a recording |
| Freedom from overacting | 1 = caricature · 5 = restrained and believable |

**Part 2 measures same-voice consistency on one pinned reference.** A
different seed is a different VoiceDesign speaker — earlier auditions in this
repository showed that even when both land in the same profile — so three
seeds would test voice *variance*, not consistency. Each block instead takes
**one** pinned reference WAV and clones three different sentences from it:

| Index | Punctuation | Text |
|---|---|---|
| 0 | `.` | The satchel is on the table by the door. |
| 1 | `!` | We must leave before the bells ring, because the eastern road floods when the rains come! |
| 2 | `?` | Have you spoken with the guildmaster since the last council meeting? |

The reference is the shared standard script ("Good evening. The road ahead is
quiet tonight…"), byte-identical to the pinned WAV in the pair section, so a
judge answers: same pin, different content — does it still sound like one
person? Blocks are unlabelled; eight blocks cover 4 voices × 2 conditions.

Rendered with the clone-only path, which never re-runs VoiceDesign:

```bash
docker run ... /vb/run.py --plans /plans --out /out \
  --design-model .../Qwen3-TTS-12Hz-1.7B-VoiceDesign \
  --base-model .../Qwen3-TTS-12Hz-1.7B-Base \
  --samples 3 --seeds 1 --clone-only --refs-from /pinned --texts-file /plans/texts.txt
```

`--clone-only` copies the pinned reference into the output dir, checks its
SHA-256 against the recorded hash, and clones every line from that one
reference. It refuses to run if a reference is missing or its hash changed, so
a block can never silently become a different speaker. Output lives beside the
NPC it came from: `data/blueprints/auditions/<npc>/same_voice/`, which
`blind-listening` discovers automatically.

- 12 matched pairs (4 NPCs × 3 seeds). Both clips in a pair share text, model
  and seed; only the Style clause differs.
- Clip filenames and side assignment are randomized per judge, and no condition
  label, NPC id, or character name appears in either HTML page.
- Keys stay with the organiser; judges must not open them.

Collect the downloaded answer files and unblind:

```bash
python3 -m tools.blueprint_pipeline.evaluation blind-listening \
  --judges A B C D E F G H I J \
  --answers A_answers.json B_answers.json … J_answers.json
```

Every returned file must carry a distinct judge letter. Two files with the same
label are rejected rather than merged, because the per-judge totals would
silently keep only one of them.

The command prints per-question means for `identity_only` and `identity_style`,
the per-condition same-speaker means, and preference counts resolved through the
key.

## What these materials do not prove

- Twelve pilot items test whether the five Style fields are understandable by
  humans. They do not establish model accuracy.
- Ten of the twelve pilot items are at or above the evidence floor; agreement
  on the two short items is mostly a test of the `insufficient_evidence` rule.
- The listening kit covers four NPCs and one reference text. It answers "does
  Style change the voice meaningfully and for the better", not "does Style
  improve every character". The speaker brief is three lines, so fit judgments
  are coarse: a judge can tell whether a delivery matches that manner, not
  whether it matches the whole character.
- Part 2 establishes whether one pin holds identity across different sentences.
  It does not measure how much separate pins differ; that is the seed-variance
  question the earlier auditions already answered.
- Neither kit randomizes judges' environments or measures repeatability of the
  same judge, so no reliability statistic can be claimed from a single judge
  per side.
