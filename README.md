# Oh My Voice

Cloned voices for unvoiced FFXIV dialogue. Pre-generated TTS voice pack
(IndexTTS-2.5 default, XTTS-v2 fallback), local playback, silent on cache
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
  backend (zero-shot, reference clip); XTTS-v2 remains the fallback. Consumes
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
compose.yaml          # batch TTS services (ROCm IndexTTS + ROCm XTTS)
tools/generate.py     # misses.jsonl -> .ogg pack generator (both backends)
tools/tts/            # XTTS Dockerfile + pinned Python requirements
tools/tts-indextts/   # IndexTTS-2.5 Dockerfile + model-download entrypoint
models/               # local IndexTTS weights (gitignored, ~11 GB)
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

Two pinned ROCm/PyTorch images. IndexTTS-2.5 is the default; XTTS-v2 remains
for A/B comparison and fallback. Both are zero-shot: 10-30 s of clean speech
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

# XTTS-v2 fallback:
# docker compose build tts
# docker compose run --rm tts
```

- IndexTTS first run downloads `IndexTeam/IndexTTS-2.5` (weights plus the
  w2v-BERT/BigVGAN auxiliary models; ~11 GB) into
  `models/checkpoints`. The auxiliary Hugging Face cache goes to
  `.cache/tts`. Keep both between runs.
- XTTS first run downloads XTTS v2 (~1.9 GB) into `.cache/tts`. The XTTS
  image is ~40 GB uncompressed; the IndexTTS image is ~42 GB. Keep ~50 GB
  free during the first build; `docker builder prune` reclaims build cache
  if needed.
- Do not run batch generation while the game is running (VRAM contention).
- `OMV_OUT` (default `./pack`), `OMV_LANG` (default `en`), `OMV_VOICE_MAP`
  (default `tools/voice-map.json`), `OMV_UID`/`OMV_GID`, and
  `OMV_MODELS` (default `./models`) are overridable.
- `models/` is gitignored and mounted at `/models`; keep weights out of git.
  `models/checkpoints` holds the IndexTTS main and auxiliary weights.
- The base images are pinned by tag and digest in `tools/tts/Dockerfile` and
  `tools/tts-indextts/Dockerfile`; `docker compose build --build-arg
  BASE_IMAGE=...` overrides them.
- Torch/torchaudio come from the ROCm base image. Never add a CUDA torch
  wheel. `tools/tts/requirements.txt` deliberately pins `transformers` to
  the 4.57 line for coqui-tts; IndexTTS uses its upstream `pyproject.toml`
  with torch/torchaudio filtered out.
- Optional IndexTTS controls exist on `tools/generate.py`:
  `--indextts-emo-vector` (8 floats), `--indextts-duration-factor`, and
  `--no-indextts-bf16`. Defaults use the reference clip's emotion, bf16 on.
- Measured on the RX 9060 XT dev host: IndexTTS-2.5 RTF 12-15.
  A 5.5 s line takes ~71 s; a 10 s line ~152 s. The 3-line test pack took
  6.2 min including ~55 s model load. That is ~30 GPU-hours per 1,000
  8-second lines. Batch only; do not put it in the live dialogue path.

### Live dev loop

`tools/voice.sh` is the one-command loop: it stages the installed pack,
generates every missed line, validates the manifest, and installs it into
`~/.xlcore/pluginConfigs/OhMyVoice`. It reads the sanitizer pin from the
installed manifest, so no env exports are needed. Then use `/omv` →
**Reload pack**.

```bash
./tools/voice.sh                    # IndexTTS-2.5 (default)
OMV_BACKEND=xtts ./tools/voice.sh   # XTTS-v2 fallback
```

The old manifest is kept as `manifest.json.bak`. Existing `.ogg` files are
preserved and skipped; only new ids are synthesized. The first IndexTTS run
builds the image and downloads the checkpoint.

Both backends occasionally append an invented sentence to otherwise correct
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
