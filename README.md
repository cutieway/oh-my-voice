# Oh My Voice

Cloned voices for unvoiced FFXIV dialogue. Pre-generated TTS voice pack
(IndexTTS-2.5 default; XTTS-v2 and Qwen3-TTS alternatives), local playback, silent on cache
miss. Never doubles real voice-over.

AGPL-3.0. See `LICENSE.md`.

## Status: P1 plugin / P2 batch generator

- ARR MSQ `AddonTalk` + `TalkSubtitle` capture, EN only.
- `id = md5(lang:voiceId:npcId:sentence)` over sanitized text (`OhMyVoice.Common`).
- Pack: `voicelines/{id}.ogg` (Opus 48 kHz mono, 32 kbps), `voicelines_override/` wins.
- Silent miss + deduped `misses.jsonl` worklist. No live TTS fallback in P1.
- Real-VO suppression: ~100 ms wait before start, stop-if-started, data-driven blocklist.
- Playback: NAudio `WaveOut` + Concentus decode. Zero native dependencies.
- Batch generator: `docker compose` on ROCm. IndexTTS-2.5 is the default
  backend (zero-shot, reference clip); XTTS-v2 and Qwen3-TTS Base are
  alternatives. Consumes
  `misses.jsonl`, emits the Opus pack. Host needs no Python/ROCm toolchain.
- Linux dev + Wine playtest. CI on `ubuntu-latest`.

## Layout

```text
OhMyVoice.slnx
OhMyVoice/            # Dalamud plugin (net10.0-windows)
  Plugin.cs           # entrypoint, service wiring
  Configuration.cs    # Enabled/Volume/VoWaitMs/StopOnVo/DebugLogging
  GameInterop.cs      # cutscene/duty state, SeString readers
  TalkCapture.cs      # AddonTalk poll + TalkSubtitle PostDraw
  VoSuppression.cs    # SoundManager hooks, real-VO detection
  Dispatcher.cs       # ID, VO-wait, dedupe, miss logging, play
  Playback.cs         # Concentus decode + NAudio WaveOut
  PackStore.cs        # manifest + override resolution + sanitizer gate
  MissLog.cs          # deduped JSONL worklist (compacted on load)
  Windows/            # /omv status + settings UI
OhMyVoice.Common/     # net10.0 shared: Sanitizer v1, VoiceId, PackTypes
scripts/check-refs.sh # Q6 guard: no reference audio in repo
compose.yaml          # batch TTS services (ROCm IndexTTS/XTTS/Qwen3-TTS)
tools/generate.py     # misses.jsonl -> .ogg pack generator (three backends)
tools/tts/            # XTTS Dockerfile + pinned Python requirements
tools/tts-indextts/   # IndexTTS-2.5 Dockerfile + model-download entrypoint
tools/tts-qwen/       # Qwen3-TTS Base Dockerfile + model-download entrypoint
models/               # local TTS weights (gitignored; IndexTTS ~11 GB, Qwen 2.5-4.5 GB)
.cache/               # local HF/XTTS caches (gitignored)
pack/                 # local generated packs (gitignored)
```

## Build (Linux)

```bash
# dotnet 10.0.401 via nix store path (not on PATH):
DOTNET=/nix/store/j8zkmcl067q2aqlg8r3lhm7kbv8iwx6v-dotnet-sdk-wrapped-10.0.401/bin/dotnet
$DOTNET build -c Release
```

Dalamud dev DLLs resolve from `~/.xlcore/dalamud/Hooks/dev` (`DALAMUD_HOME` overrides).

## Batch generation (Docker, ROCm)

Three pinned ROCm/PyTorch images. IndexTTS-2.5 is the default; XTTS-v2 and
Qwen3-TTS Base are alternatives. All are zero-shot: 3-30 s of clean speech
per speaker, no training. They emit the same pack format, so the plugin does
not know which backend produced a line. The host needs only the amdgpu
driver, `/dev/kfd`, `/dev/dri`, and Docker - no host Python, CUDA, or Nix
TTS package.

IndexTTS-2.5 upstream assumes CUDA. Its image starts from ROCm/PyTorch
(Python 3.10, torch 2.8.0+rocm7.2.4), installs IndexTTS with `--no-deps`,
then installs upstream's dependency list minus torch/torchaudio. That keeps
pip from replacing the ROCm wheel with a CUDA/CPU one. The loader passes
`use_cuda_kernel=False` to skip the CUDA-only BigVGAN kernel, and the build
asserts `torch.version.hip` at the end.

IndexTTS-2.5 code and weights are released under Bilibili's
`LicenseRef-Bilibili-IndexTTS` / model license, not AGPL-3.0. This repository's
AGPL-3.0 covers oh-my-voice code; review the upstream model license before
redistributing weights or generated packs.

```bash
mkdir -p .cache/tts models pack
export OMV_MISSES="$HOME/.xlcore/pluginConfigs/OhMyVoice/misses.jsonl"
export OMV_REFS="$HOME/oh-my-voice-refs"
export OMV_SANITIZER_PIN="v1:..."   # from the plugin's manifest / settings

docker compose build tts-indextts
docker compose run --rm tts-indextts  # -> pack/voicelines/*.ogg + pack/manifest.json

# Alternatives:
# docker compose build tts && docker compose run --rm tts            # XTTS-v2
# docker compose build tts-qwen && docker compose run --rm tts-qwen  # Qwen3-TTS 1.7B
```

- IndexTTS first run downloads `IndexTeam/IndexTTS-2.5` (weights plus the
  w2v-BERT/BigVGAN auxiliary models; ~11 GB) into
  `models/checkpoints`. The auxiliary Hugging Face cache goes to
  `.cache/tts`. Keep both between runs.
- XTTS first run downloads XTTS v2 (~1.9 GB) into `.cache/tts`. The XTTS
  image is ~40 GB uncompressed; the IndexTTS image is ~42 GB. Keep ~50 GB
  free during the first build; `docker builder prune` reclaims build cache
  if needed.
- Qwen3-TTS first run downloads `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (~4.5 GB)
  into `models/qwen3-tts`; `--qwen3-model` / `OMV_QWEN_MODEL_DIR` can point at
  another Base checkpoint. The 0.6B Base was tested and rejected (worse and
  not faster). The image shares the IndexTTS ROCm base. Qwen clone mode uses a sibling `<voiceId>.txt` transcript;
  without one it falls back to x-vector-only cloning (lower quality).
  See [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md).
- Do not run batch generation while the game is running (VRAM contention).
- `OMV_OUT` (default `./pack`), `OMV_LANG` (default `en`), `OMV_VOICE_MAP`
  (default `tools/voice-map.json`), `OMV_UID`/`OMV_GID`, and
  `OMV_MODELS` (default `./models`) are overridable.
- `models/` is gitignored and mounted at `/models`; keep weights out of git.
  `models/checkpoints` holds the IndexTTS main and auxiliary weights;
  `models/qwen3-tts` holds Qwen3-TTS.
- The base images are pinned by tag and digest in `tools/tts/Dockerfile`,
  `tools/tts-indextts/Dockerfile`, and `tools/tts-qwen/Dockerfile`;
  `docker compose build --build-arg BASE_IMAGE=...` overrides them.
- Torch/torchaudio come from the ROCm base image. Never add a CUDA torch
  wheel. `tools/tts/requirements.txt` deliberately pins `transformers` to
  the 4.57 line for coqui-tts; IndexTTS uses its upstream `pyproject.toml`
  with torch/torchaudio filtered out.
- Optional IndexTTS controls exist on `tools/generate.py`:
  `--indextts-emo-vector` (8 floats), `--indextts-duration-factor`,
  `--no-indextts-bf16` (GPT stage only), and the experimental speed flags
  `--indextts-fp16` (s2mel/BigVGAN fp16 autocast), `--indextts-steps`,
  `--indextts-cfg-rate`, and `--indextts-torch-compile`. Defaults are fp32
  s2mel/BigVGAN, 25 steps, CFG 0.7, no compile, bf16 GPT. The speed flags
  are not quality-gated yet; see the report. The image bakes `generate.py`
  at build time: rebuild (`docker compose build tts-indextts tts-qwen`) after
  changing the generator, or a mounted script is required.
- Qwen3-TTS controls: `--qwen3-model`, `--qwen3-attn` (`sdpa`/`eager`/
  `flash_attention_2`), `--qwen3-x-vector-only`, and
  `--qwen3-non-streaming`. The clone transcript is `<voiceId>.txt` next to
  the reference WAV; `docs/QWEN3-TTS.md` covers the measured numbers.
- Measured on the RX 9060 XT dev host with the real `generic-male-01`
  reference and `MIOPEN_FIND_MODE=FAST` (compose default): warm RTF ~3.8
  (7-8 s line ~30 s; first line ~58 s). `--indextts-fp16` alone measured
  **19.1 s warm (RTF 2.58, 1.56x)**; FP16 + 12 steps + CFG 0 measured 44.7 s
  for a cold line. Quality of those paths is unaudited. Without FAST,
  MIOpen's per-shape find made the same class of line ~64-71 s (RTF ~13).
  Batch only; do not put it in the live dialogue path. Full benchmark
  matrix, rejected candidates, and untested options:
  [docs/PERFORMANCE.md](docs/PERFORMANCE.md).
- Qwen3-TTS 1.7B measured warm RTF **3.24-3.42** on the same host (load
  ~8 s, first reference prompt ~29 s, later prompts ~0.5 s). Listening
  samples and caveats: [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md).

### Live dev loop

`tools/voice.sh` is the one-command loop: it stages the installed pack,
generates every missed line, validates the manifest, and installs it into
`~/.xlcore/pluginConfigs/OhMyVoice`. It reads the sanitizer pin from the
installed manifest, so no env exports are needed. Then use `/omv` →
**Reload pack**.

```bash
./tools/voice.sh                      # IndexTTS-2.5 (default)
OMV_BACKEND=xtts ./tools/voice.sh     # XTTS-v2 fallback
OMV_BACKEND=qwen3tts ./tools/voice.sh # Qwen3-TTS 1.7B Base
```

The old manifest is kept as `manifest.json.bak`. Existing `.ogg` files are
preserved and skipped; only new ids are synthesized. The first IndexTTS run
builds the image and downloads the checkpoint.

All backends occasionally append an invented sentence to otherwise correct
speech. The generator measures every new WAV and retries audio longer than
`max(4.5s, words / 2.2 + 1s)` with lower-temperature sampling; after three
attempts it rejects the line, leaves it in the worklist, and installs no bad
audio.

## Playtest (Wine)

1. Build, then in-game `/xlsettings` add dev plugin location:
   `Z:\home\lexi\Projects\oh-my-voice\OhMyVoice\bin\x64\Release\OhMyVoice\OhMyVoice.dll`
2. `/xlplugins` → enable. `/omv` status window. Logs in `/xllog`.
3. P1 proof: audible unvoiced Talk NPC + voiced ARR suppression +
   subtitle dedupe + stop behaviors, with debug flag on for the run.

## Reference-audio policy (Q6)

Real-actor reference wavs stay local-personal: `~/oh-my-voice-refs/` or a
gitignored `references/` directory. NEVER committed, NEVER in the worklist.
CI rejects `references/` paths, audio blobs, and files over 2 MiB.
Distributed packs use generic-archetype or consented voices only.
`voiceId` in the ID makes clean packs a distinct namespace by construction.
