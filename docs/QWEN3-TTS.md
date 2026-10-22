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
- `tools/voice.sh`: the one-command Qwen pack loop.
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
export OMV_EVENTS=... OMV_SANITIZER_PIN=v1:...
docker compose -f compose.xvector.yaml run --rm xvector  # build/refresh vectors
./tools/voice.sh            # reuse matching generation profiles
./tools/voice.sh --force    # regenerate every planned line
# or:
docker compose build tts-qwen
docker compose run --rm tts-qwen
```

First run downloads the model into `data/models/qwen3-tts/<model>`; the HF
cache lives in `data/cache/hf`, not beside the model.

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
duration budget used for the retry check (Qwen3-TTS is a 12 Hz codec). Passing
a positive value overrides it. This matters for unattended batches: the
upstream default is 2048 tokens, and a single looping line can otherwise spend
many minutes decoding. A 20-line Sphene pilot hit one short line that took
655 s on an uncapped retry; the capped run completed 20/20 in 9m12s with no
rejections. The full 165-line Sphene queue also finished 165/165; two lines
landed within 5% of the duration estimate and were accepted after a retry
rather than dropped. The output pack directory must exist and be writable by
`OMV_UID` before Docker starts; a bind mount created by root leaves
`/out/voicelines` unwritable.

## Merging speaker-scoped runs

A worklist exported for one speaker writes a manifest containing only that
speaker's planned lines. To add it to a pack that already holds other
speakers, pass `--merge`; `tools/voice.sh` always does. Merge:

- carries previous `Voicelines`, `Voices`, `Npcs`, `IgnoredSpeakers`, and
  `VoiceEmbeddings`;
- only carries a line whose `<id>.ogg` still exists, and refreshes its byte
  size/duration if the file changed;
- requires matching `Language`, `Sanitizer`, `Backend`, `Model`, `Generation`,
  and `Encoder` settings, because one manifest cannot describe mixed profiles;
- refuses to write a manifest with fewer `Voicelines` than the previous one.

A bare run without `--merge` is still the clean-room mode for tests. A merge
against a corrupt manifest is fatal rather than silently replacing it.

Prefer a local `--qwen3-model` directory: `model_revision` hashes every weight
file (ignoring `README.md`/`.gitattributes`), so the revision is exact. A repo
id resolves through the HF cache after `from_pretrained`; if it stays
unresolved the run warns and fingerprints use `unresolved:<repo-id>`.

## Average speaker x-vectors

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

For historical comparison, IndexTTS fp32 was RTF ~3.35 warm; IndexTTS FP16
~2.2. IndexTTS is archived; see
[the report](archive/indextts-performance.md). Qwen3-TTS has no diffusion
steps/CFG knobs; its cost is autoregressive token generation.

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

End-to-end `./sample.sh` for a 6.65 s line: **106.6 s without FAST**,
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
