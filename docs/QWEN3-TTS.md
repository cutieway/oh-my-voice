# Qwen3-TTS backend

> **Supported method: average x-vectors.** Always prefer one precomputed
> averaged speaker embedding per voice. Single-reference 3-30 s zero-shot and
> ICL transcripts are retired for packs; use them only for isolated tests.

Status: the only active backend. Implemented as `tools/qwen/generate.py`
and the `tts-qwen` compose service. Model weights are Apache-2.0; this
repo's AGPL-3.0 covers the integration code only.

## Upstream

- Repository: <https://github.com/QwenLM/Qwen3-TTS>
- Model: `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (4.5 GB), 12 Hz tokenizer +
  discrete multi-codebook LM, not a diffusion model. The 0.6B Base was
  tested and rejected: worse output and not faster per generated second.
- The supported generation path loads one precomputed average x-vector per
  voice and uses x-vector-only prompting. Single-reference cloning and ICL
  transcripts are not used for packs.
- The package is `qwen-tts==0.1.1`; it pins `transformers==4.57.3`,
  `accelerate==1.12.0`. The image installs it `--no-deps` on the ROCm
  PyTorch base and adds the runtime deps without torch/torchaudio, so pip
  cannot replace the ROCm wheels.

## Integration

- `tools/qwen/Dockerfile`: ROCm/PyTorch 2.8 + Python 3.10 image.
- `compose.yaml`: `tts-qwen`, consumes `data/xvectors` and defaults to the
  1.7B Base model.
- `compose.xvector.yaml`: the separate average-x-vector builder.
- `./voice.sh`: the one-command speaker loop (export, generate, resume).
- `tools/qwen/generate.py`: Qwen-only generator with `--events`,
  `--xvectors`, `--qwen3-model`, `--qwen3-attn` (`sdpa` default),
  `--qwen3-non-streaming`, `--qwen3-seed`. Only `Result="miss"` rows are
  generated; hits are diagnostics.
- `tools/xvector/build_xvectors.py`: builds one averaged speaker embedding
  per game `speaker_token` from every extracted cutscene clip. It writes
  `data/xvectors/<token>.npy` and does not generate speech. Existing vectors
  are reused only when the WAV-corpus, model-weight, and audio-pipeline
  fingerprints match; otherwise that speaker rebuilds. See “Average speaker
  x-vectors” below.
- `tools/voice-map.json`: NPC key -> x-vector name. An explicit entry wins;
  otherwise a normalized NPC name is used only when exactly one vector
  matches. Ambiguous matches are skipped, as are missing vectors, and remain
  in the worklist.
- Phase 1 only covers NPCs with in-game voice (`speaker_token`). Unvoiced NPCs
  have no vector and are deliberately left silent; synthetic/generic voices
  are a later phase, not a fallback.

```bash
docker compose -f compose.xvector.yaml run --rm xvector  # build/refresh vectors
./voice.sh                  # resume every speaker with queued lines, most first
./voice.sh -plan            # show that order without starting anything
./voice.sh alphinaud        # export, generate missing lines, resume later
./voice.sh alphinaud yshtola alisaie  # same, one speaker after another
OMV_FORCE=1 ./voice.sh alphinaud  # regenerate every planned line
OMV_RECHECK=1 ./voice.sh alphinaud  # rebuild only changed generation profiles
```

With no speaker, `./voice.sh` reads `dialogue.db` and runs the speakers that
still have queued lines, largest queue first, resuming each. If a run is
already active it resumes only that speaker, and the next `./voice.sh`
continues with the rest. Every exit path - normal finish, `-stop`, Ctrl+C, or
a container crash - links the manifest back, so progress is never lost. Progress is a
database column, not a file: `tools/qwen/link_audio.py` marks a line
`audio_status='generated'` once its root audio exists and still matches the
stored text, so `v_generation_queue` only lists what is missing. A patch that
adds lines therefore needs only a re-harvest plus `./voice.sh`; a line whose
text changed keeps `audio_stale=1` and is regenerated. `OMV_FORCE=1` exports
`v_generation_candidates` (`export --all`) so a finished pack can be rebuilt,
and `xivdata migrate` re-applies the views and speaker counts without a
harvest. `python3 tools/roster.py table --out docs/workload.csv` refreshes the
informational workload table.

A model, encoder, or sanitizer change does not queue anything by itself (the
fingerprint, not the text, moved). Use `OMV_RECHECK=1 ./voice.sh <speaker>`:
it exports every planned line too, but the generator rebuilds only lines whose
`GenerationFingerprint` no longer matches, which is much cheaper than
`OMV_FORCE=1` on a whole pack.

`./voice.sh` reads the sanitizer pin from `xivdata pin` and merges into
`data/out/<voice_id>`. Use `OMV_OUT=...` to target another pack. First run
downloads the model into `data/models/qwen3-tts/<model>`; the HF cache lives
in `data/cache/hf`, not beside the model.

## Making a pack

Packs are distributed separately from this repo. This recipe generates your
own from a local game install. It needs Docker, an AMD GPU with ROCm, your
installed game for `sqpack`, and `vgmstream-cli`.

```bash
# 1. harvest the dialogue into a local database
dotnet run --project tools/xivdata -- --source all \
  --db data/dialogue.db --out data/xivdata --xvectors data/xvectors

# 2. extract cutscene audio, then average each speaker into one voice vector
dotnet run --project tools/xivdata -- --source cutscene --extract --by-token \
  --out data/game-voices --db data/dialogue.db
docker compose -f compose.xvector.yaml build
docker compose -f compose.xvector.yaml run --rm xvector

# 3. generate one character; rerun the same command to resume
./voice.sh alphinaud
```

The default `data/out/` directory is the multi-voice pack root: each run writes
`data/out/<voice>/manifest.json` and `data/out/<voice>/voicelines/`. Select that
root once in the plugin settings; newly generated voices are picked up after
**Reload pack**, without copying files around.

To build the same layout somewhere else, set `OMV_OUT` to a per-voice
subdirectory:

```bash
mkdir -p "$HOME/OhMyVoice/alphinaud" "$HOME/OhMyVoice/urianger"
OMV_OUT="$HOME/OhMyVoice/alphinaud" ./voice.sh alphinaud
OMV_OUT="$HOME/OhMyVoice/urianger" ./voice.sh urianger
```

Then select the parent `$HOME/OhMyVoice` as the pack root.

Start small with `OMV_LIMIT=5 ./voice.sh alphinaud`, and use
`./voice.sh -pause` / `-start` / `-stop` while a run is attached. Harvesting
details are in [../tools/xivdata/README.md](../tools/xivdata/README.md); the
vector builder is in [../tools/xvector/README.md](../tools/xvector/README.md).

## Operations

Compose and `./voice.sh` read these overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OMV_EVENTS` | required | The plugin's `events.jsonl` |
| `OMV_OUT` | `./data/out/<voice>` | Output directory for one voice pack |
| `OMV_LANG` | `en` | Pack language |
| `OMV_VOICE_MAP` | `tools/voice-map.json` | NPC -> x-vector map |
| `OMV_XVECTORS` | `./data/xvectors` | Voice embeddings |
| `OMV_MODELS` | `./data/models` | Model weights |
| `OMV_CACHE` | `./data/cache` | HF/Torch/MIOpen caches |
| `OMV_UID` / `OMV_GID` | `1000` | Ownership of files written through a bind mount; `./voice.sh` sets your host uid/gid |

- The image is ~40 GB uncompressed. Keep room for Docker layers and the model;
  `docker builder prune` reclaims build cache.
- Do not generate while the game is running: both contend for the GPU.
- The base image is pinned by tag and digest in `tools/qwen/Dockerfile`.
  `docker compose build --build-arg BASE_IMAGE=...` overrides it.
- `generate.py` is baked into the image at build time; rebuild after changing
  the generator.
- One-off sample: `./scripts/sample.sh <speaker_token> "<sentence>"` writes
  `data/samples/<speaker_token>/` with the x-vector metadata and line audio. It
  never installs into a pack or goes through `./voice.sh`.

## Generation cache

Each manifest line records a `GenerationFingerprint` over the schema version,
exact model revision, attention/seed/non-streaming/device, retry settings,
language, encoder settings, and the voice's x-vector hash. The top-level
manifest also records `Model` (`Id`, `Revision`), `Generation`, and
`VoiceEmbeddings`. A line is reused only when its fingerprint and byte size
still match; a changed x-vector, model weight file, or setting rebuilds that
line. Manifests written before fingerprints cause one full rebuild.

`--force` skips the fingerprint check. A line whose x-vector is missing is
kept if its audio already exists (it cannot be validated or rebuilt), and is
skipped if it has no audio.

Each Ogg is encoded to `<id>.tmp.ogg` in the pack directory, validated with
ffprobe, then atomically renamed to `<id>.ogg`; only after that rename is the
line recorded in the manifest. A crash during encoding leaves only a temp file,
which the next run removes before encoding any line.

`--max-new-tokens` defaults to 0, which derives a per-line cap from the same
duration budget used for the retry check (Qwen3-TTS is a 12 Hz codec). The
retry limit is `1.47 * max(4.5s, words / 2.2 + 1s)`; 1.47 is calibrated to
the slowest of the 29 pilot clips (7.45 s against a 5.09 s budget, 1.463x). A
valid line that exceeds it should raise the factor deliberately, not bypass
the check. Passing a positive `--max-new-tokens` overrides the cap. This
matters for unattended batches: the upstream default is 2048 tokens, and a
single looping line can otherwise spend many minutes decoding. A 20-line
Sphene pilot hit one short line that took 655 s on an uncapped retry; the
capped run completed 20/20 in 9m12s with no rejections. The full 165-line
Sphene queue also finished 165/165; two lines landed within 5% of the
duration estimate and were accepted after a retry rather than dropped. The
output pack directory must exist and be writable by
`OMV_UID` before Docker starts; a bind mount created by root leaves
`/out/voicelines` unwritable.

## Player-name speech variants

The canonical manifest `Sentence` keeps `_FIRSTNAME_` so IDs never change.
Only the text sent to TTS is rewritten:

- vocative-only lines (`Greetings, _FIRSTNAME_.`) drop the name and write one
 `<id>.ogg`;
- lines consisting only of the player's name cannot be spoken meaningfully and
 are skipped during planning; they produce no OGG. The generator does not
 mutate the dialogue DB, so the worklist/DB row is left as it was.
- third-person references (`_FIRSTNAME_ and I...`, `_FIRSTNAME_'s unit`,
  `_FIRSTNAME_ and band`) write three files: `<id>.ogg` (they/them),
  `<id>.she.ogg`, and `<id>.he.ogg`, with entries under
  `Voicelines[<id>].Variants` for she/he.

Singular *they* also adjusts nearby agreement (`they are`, `they have`, `they
make`). A name coordinated with another person takes the case of the whole
noun phrase, not the case of its slot: `brought _FIRSTNAME_ and Raha with me`
uses `them and Raha`, while `you and _FIRSTNAME_ can delve` uses `you and
they`. The source text's own `and I` does not force a subject reading.
The plugin resolves the manual **Player pronouns** setting first; Auto
uses the character's sex. Missing she/he audio falls back to the neutral root,
never to the other gender. A manual `voicelines_override/<id>.ogg` still wins
for neutral lines, and `<id>.she.ogg` / `<id>.he.ogg` overrides win for their
matching variant.

Packs are schema 2 only. The plugin disables manifests with an older schema,
and the linker skips them rather than guessing which file is neutral.

## Merging speaker-scoped runs

A worklist exported for one speaker writes a manifest containing only that
speaker's planned lines. To add it to a pack that already holds other
speakers, pass `--merge`; `./voice.sh` always does. Merge:

- carries previous `Voicelines`, `Voices`, `Npcs`, `IgnoredSpeakers`, and
  `VoiceEmbeddings`;
- only carries a line whose `<id>.ogg` still exists, and refreshes its byte
  size/duration if the file changed;
- drops entries whose audio is already missing, with a warning, so one broken
  entry cannot deadlock every later merge;
- requires matching `Language`, `Sanitizer`, `Backend`, `Model`, `Generation`,
  and `Encoder` settings, because one manifest cannot describe mixed profiles;
- refuses to write a manifest that loses a line which had audio.

A bare run without `--merge` is still the clean-room mode for tests. A merge
against a corrupt manifest is fatal rather than silently replacing it.

## Pause and resume

The generator writes `manifest.json` atomically after every completed line,
so a long run can be stopped without losing finished audio. `./voice.sh`
reruns the same speaker command, skips completed lines by generation
fingerprint, and prints progress as `[done/remaining]` where the denominator
counts only lines still needing generation.

```bash
./voice.sh alphinaud          # start or resume
./voice.sh alphinaud yshtola  # finish alphinaud, then yshtola
./voice.sh -pause alphinaud   # freeze the running container
./voice.sh -start alphinaud   # unpause, or start/resume after a stop
./voice.sh -stop alphinaud    # same as Ctrl+C
```

A batch runs speakers in the order given and stops if one fails. Control
commands still address one speaker at a time.

The attached console also accepts `-pause`, `-start`, and `-stop`. A pause
keeps the model and GPU state; a stop releases them, and the next start
reloads the model. A manifest written mid-run is a valid partial pack.

Prefer a local `--qwen3-model` directory: `model_revision` hashes every weight
file (ignoring `README.md`/`.gitattributes`), so the revision is exact. A repo
id resolves through the HF cache after `from_pretrained`; if it stays
unresolved the run warns and fingerprints use `unresolved:<repo-id>`.

## Average speaker x-vectors

> **Same speaker only.** Average clips from one `speaker_token`, never from
> several voice actors. A 2026-11-01 listening test mixed per-speaker x-vectors
> into 27 race/clan archetypes; the results were uniformly robotic and unusable.
> The mean of different actors is not a synthetic voice. For unvoiced NPCs use
> VoiceDesign/ICL, not a cross-speaker average. Same-speaker averaging remains
> the supported pack method.

### Synthetic voices

Unvoiced NPCs have no source speaker, so there is nothing valid to average.
Phase 2 now implements the source-to-draft pipeline and its audition handoff:
read-only SQLite inventory, canonical identity gates, dialogue dossiers,
offline rules or opt-in Jev classification, shared synthetic archetypes,
generated drafts, and human review lifecycle.
It stops before the runtime plugin or pack-generation path; no Phase 1 code was
changed.

The evaluation path remains VoiceDesign -> Base ICL: render one standard
reference clip for an approved blueprint, then clone with
`create_voice_clone_prompt(..., x_vector_only_mode=False)`. The renderer is
`tools/blueprint.py`; the classification code is under
`tools/blueprint_pipeline/`.

```bash
# Optional canonical race/clan metadata from installed Lumina sheets.
dotnet run --project tools/xivdata -- npc-index --lang en > /tmp/game-npc-index.json
python3 tools/blueprint_pipeline/cli.py inventory --game-index /tmp/game-npc-index.json
python3 tools/blueprint_pipeline/cli.py dossiers --game-index /tmp/game-npc-index.json --limit 25
python3 tools/blueprint_pipeline/cli.py classify --provider rules --limit 25

# Network use is explicit. Set TYPESAFE_API_KEY in the shell for Jev.
python3 tools/blueprint_pipeline/cli.py classify --provider jev --allow-network --limit 25

python3 tools/blueprint.py render archetype_neutral_clear_mid
./scripts/voicebank.sh archetype_neutral_clear_mid
```

Jev requires `--allow-network`; rules need no credentials.
The Jev preflight prints a workload and approximate token-cost estimate, and
requests are capped at 25 dossiers unless `--allow-bulk` is supplied. Jev
accepts text/structured state only: it cannot hear WAV files and does not
provide citations. Jev's probabilities and confidence are useful
routing signals, not proof; low-confidence choices go to review or a shared
archetype.

The six initial archetypes under `blueprints/archetypes/` are deliberately
hand-curated drafts, not approved voices. Generated character profiles,
dossiers, classification results, and assignment registries
live under gitignored `data/blueprints/`. Use `cli.py inspect <id>
--review-report` to see the dossier, evidence, distributions, assignment, and
rendered neutral instruction. Run `./scripts/voicebank.sh <profile-id>` to
audition a draft. `cli.py review audition` hashes a valid reference WAV and
`cli.py review approve` records the human approval. Assignments are not usable
for future generation until the resolved profile is human-approved.

The instruct is the identity sentence plus the profile's rendered speech-style
clause when it has one. Listening on 2026-11-03 preferred styled takes over
identity-only takes for every sampled voice, so the clause is the default. The
mood/archon layer was removed on the same date.

The prior reference and ICL pipeline remains the tested mechanical path. The
new classification pipeline has been exercised on a three-character local
rules pilot, which assigned candidates to shared draft archetypes. A single
archetype audition rendered `archetype_neutral_clear_mid`: its reference is
11.28 seconds and one ICL sample is 5.84 seconds, under
`data/samples/voicebank/20261101T131313Z/`. This verifies the rendering path;
the draft archetype still needs a human listen and approval. Synthetic
blueprints are not yet consumed by `generate.py` or installed in plugin packs;
that remains a later integration step requiring human-approved references.

`tools/xvector/build_xvectors.py` is the recovered average-speaker method from
the Sphene experiment. It is not training and not generation:

1. `tools/xivdata --extract --by-token` writes every game clip under
   `data/game-voices/wav/<speaker_token>/`.
2. The builder reads all WAVs for a token, resamples to 24 kHz mono, calls
   `extract_speaker_embedding()` per clip, and means the embeddings
   (`torch.stack(...).mean(0)`).
3. It saves one 2,048-float vector plus stats per token and a manifest.

All clips count; there is no curated subset or sampling. The original command
was `average_voice.py` in the Sphene workspace, using
`Qwen/Qwen3-TTS-12Hz-1.7B-Base` and an `x_vector_only_mode=True` prompt item.
The image was tagged `tts-qwen-lora`, but the script loaded Base weights and
never applied a LoRA adapter.

Measured cost on the RX 9060 XT, 255 Sphene clips / 1,519.84 s of audio:

| Step | Time |
| --- | ---: |
| model load | 16.10 s |
| 255 embeddings | 37.18 s (≈41× realtime) |
| averaging | 0.004 s |
| prompt build incl. load | 62.28 s |

The five-line generation in that experiment was a separate run (131.12 s
total). Corpus size scales only the one-time embedding pass: Alphinaud is
6.4× Sphene's clip count, so ≈4 minutes one-time [estimate]; per-line
zero-shot cost is unchanged because the prompt is one fixed vector.

The `xvector` compose service sets `MIOPEN_FIND_MODE=FAST`, matching the
experiment. Without it, MIOpen heuristic tuning made the same 255-clip pass
take 185 s instead of 37 s; the resulting vector still matched the reference
at cosine 1.0000001, norm 17.70244 vs 17.70219.

Full measured run on the same GPU: 667 speakers, 25,597 clips, 152,667 s of
audio, 832.0 s total, 0 clip errors, 11 MB of output. Extraction was 731 s
for 25,598 SCDs/25,597 WAVs (18 GB); one `vgmstream` clip failed.

Legacy prompt comparison (encoder similarity only; ICL is retired):

| Prompt | Similarity |
| --- | ---: |
| single 12.25 s reference, x-vector | 0.6748 |
| average of 255 clips, x-vector | 0.6959 |
| average of 255 clips + one ICL ref code | 0.6844 |
| base ICL | 0.7094 |

Averaging beat one short x-vector reference. ICL scored higher on this
per-clip encoder metric, but listening preferred the average vector's
consistency across generated lines; the metric is not a pack-quality verdict.
The supported choice is the average x-vector. A plain mean is outlier-sensitive:
1,631 Alphinaud clips span six expansions and many emotional states, so the
centroid can drift. The untested comparison is all clips vs a curated/trimmed
subset.

## Decode performance

The supported x-vector prompt is a fixed 2,048-float `.npy`; prompt
construction is a load plus `VoiceClonePromptItem`, not the old
`create_voice_clone_prompt` path. Decode cost is unchanged. Legacy
single-reference measurements on the RX 9060 XT (16 GB) put warm RTF at
**3.24-3.42** for 1.7B and **3.12-3.20** for non-streaming, with ~7-8 s
model load. Use those only as decode bounds; do not revive the retired
prompt setup.

Qwen3-TTS has no diffusion steps or CFG knobs; its cost is autoregressive
token generation.

### MIOpen FAST find is required

Both `compose.yaml` and `compose.xvector.yaml` set
`MIOPEN_FIND_MODE=FAST`. Keep it. Without it, MIOpen's default exhaustive
per-shape find dominates decode on gfx1200. Repeated runs with the same
1.7B model, x-vector, and sentence measured:

| Config | Decode | Audio | RTF |
|---|---:|---:|---:|
| Default MIOpen | 70.4 s | 5.92 s | 11.9× |
| Default MIOpen | 69.3 s | 6.16 s | 11.3× |
| `FAST` | 20.3 s | 4.96 s | 4.1× |
| `FAST` | 15.4 s | 5.12 s | 3.0× |

End-to-end `./scripts/sample.sh` for a 6.65 s line: **106.6 s without FAST**,
**41.9 s with FAST**. The latter includes ~3.5 s imports, ~5.5 s model load,
~3.2 s hashing the 4.23 GiB local checkpoint, and container startup. A 25 s
dialogue at the old ~12× decode is about 5 minutes; at ~3× it is about
75 seconds plus that fixed overhead.

This regression has already happened once: `6e436c3` added FAST, then the
Qwen consolidation `a6fc133` dropped it from `compose.yaml` while leaving the
documentation in place. Verify with `docker compose config` after any compose
or service refactor.

## Quality

`non_streaming_mode` is **not** a different model. It changes how text is
fed to the same Base model: full text at once vs simulated streaming chunks.
Use the x-vector prompt for both; single-reference A/B results are retired.

Qwen's non-cloning models are `CustomVoice` and `VoiceDesign`; the generator
refuses anything that is not a `*-Base` model.

## AOTriton: do not enable

**Do not set `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` for Qwen3-TTS pack
generation.** It is measurably faster, but it changes the generated voice.

Controlled benchmark: same averaged Sphene speaker prompt, same five
sentences, fixed seed 1234, one untimed warm-up, only the environment variable
changed.

| Run | Load | Warm-up | 5-line generation | Peak GPU |
|---|---:|---:|---:|---:|
| Baseline | 7.85 s | 15.94 s | **71.74 s** | 4408 MB |
| Baseline rerun | 7.67 s | 15.41 s | **72.69 s** | 4408 MB |
| AOTriton | 8.02 s | 14.61 s | **62.22 s** | 4404 MB |
| AOTriton rerun | 7.84 s | 14.54 s | **62.60 s** | 4404 MB |

- Speed: about **13-14% faster** generation, no meaningful memory change.
- Baseline reruns were byte-identical; AOTriton reruns were byte-identical.
- Baseline and AOTriton outputs were **not** identical: audio durations
  changed, ECAPA speaker similarity was 0.61-0.82 per line, and listening
  showed a clearly different pitch/timbre versus the source Sphene clips.
- Therefore AOTriton is not an exact-backend speedup. It is a different
  decode, and the pitch shift is unacceptable for this voice pack.

Keep the variable unset. The existing `sdpa` path is the reference decode.

## Caveats

- `flash-attn` is not installed. The image uses `sdpa`; ROCm prints an
  experimental-attention warning. Do **not** enable
  `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1`: it is ~13-14% faster but
  changes the output pitch/timbre. See “AOTriton: do not enable” above.
- The generator requires `<xvectors>/<name>.npy`. Unmapped speakers or
  missing vectors are skipped and stay in the worklist. Build vectors with
  `compose.xvector.yaml`; do not substitute single-reference prompts.
- The upstream license is Apache-2.0, but generated voice packs still need
  the same donor-consent/licensing review as any cloned voice. Do not
  distribute clones of real actors without permission.
