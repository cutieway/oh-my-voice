# Oh My Voice

Cloned voices for unvoiced FFXIV dialogue. Pre-generated Qwen3-TTS voice
packs, local playback, silent on cache miss. Never doubles real voice-over.

AGPL-3.0. See `LICENSE.md`.

## Status

- ARR MSQ `AddonTalk` + `TalkSubtitle` capture, EN only.
- `id = md5(lang:voiceId:npcId:sentence)` over sanitized text
  (`OhMyVoice.Common`).
- Pack: `voicelines/{id}.ogg` (Opus 48 kHz mono, 32 kbps),
  `voicelines_override/` wins.
- Silent miss + deduped `misses.jsonl` worklist. No live TTS fallback.
- Real-VO suppression: ~100 ms wait before start, stop-if-started,
  data-driven blocklist.
- Playback: NAudio `WaveOut` + Concentus decode. Zero native dependencies.
- Batch generator: Qwen3-TTS Base zero-shot voice clone on ROCm, in Docker.
  It consumes `misses.jsonl`, emits the Opus pack, and installs it into the
  plugin config. Host needs no Python/ROCm toolchain.
- No fine-tuning and no LoRA. The pack generator uses reference audio at
  inference only.

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
compose.yaml          # one batch service: tts-qwen
tools/
  qwen/               # Qwen3-TTS Dockerfile, entrypoint, generator
  voice.sh            # one-command live dev loop
  voice-map.json      # NPC -> voiceId
  xivdata/            # optional cutscene SCD extractor (Lumina)
data/                 # gitignored runtime root
  models/             # Qwen3-TTS Base weights
  cache/              # HF/Torch/MIOpen caches
  refs/               # reference WAVs + optional .txt transcripts
  out/                # generated pack
archive/tts-backends/ # XTTS/IndexTTS code, reference only, not built
docs/
  QWEN3-TTS.md        # Qwen integration, quality, measurements
  archive/            # superseded IndexTTS performance report
```

Only `data/` is runtime state. It is gitignored; back it up separately from
the repo. `archive/` is source-only and is never built or composed.

## Build (Linux)

```bash
# dotnet 10.0.401 via nix store path (not on PATH):
DOTNET=/nix/store/j8zkmcl067q2aqlg8r3lhm7kbv8iwx6v-dotnet-sdk-wrapped-10.0.401/bin/dotnet
$DOTNET build -c Release
```

Dalamud dev DLLs resolve from `~/.xlcore/dalamud/Hooks/dev`
(`DALAMUD_HOME` overrides).

## Batch generation (Docker, ROCm)

One pinned ROCm/PyTorch image. Qwen3-TTS Base is zero-shot: 3-30 s of clean
speech per speaker, no training. The host needs only the amdgpu driver,
`/dev/kfd`, `/dev/dri`, and Docker — no host Python, CUDA, or Nix TTS
package.

```bash
mkdir -p data/{models,cache,refs,out}
cp /path/to/generic-male-01.wav data/refs/
export OMV_MISSES="$HOME/.xlcore/pluginConfigs/OhMyVoice/misses.jsonl"
export OMV_SANITIZER_PIN="v1:..."   # from the plugin's manifest / settings

docker compose build tts-qwen
docker compose run --rm tts-qwen    # -> data/out/voicelines/*.ogg + manifest.json
```

- First run downloads `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (~4.3 GB) into
  `data/models/qwen3-tts/`. HF/Torch caches go to `data/cache/`.
  `--qwen3-model` / `OMV_QWEN_MODEL_DIR` can point at another Base checkpoint.
  The 0.6B Base was tested and rejected (worse and not faster).
- Qwen clone mode uses a sibling `<voiceId>.txt` transcript next to
  `<voiceId>.wav`; without one it falls back to x-vector-only cloning
  (lower quality). See [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md).
- The image is ~40 GB uncompressed. Keep free space for Docker layers and
  the model. `docker builder prune` reclaims build cache.
- Do not run batch generation while the game is running (VRAM contention).
- `OMV_OUT` (default `./data/out`), `OMV_LANG` (default `en`),
  `OMV_VOICE_MAP` (default `tools/voice-map.json`), `OMV_UID`/`OMV_GID`,
  `OMV_MODELS` (default `./data/models`), `OMV_CACHE` (default
  `./data/cache`), and `OMV_REFS` (default `./data/refs`) are overridable.
- The base image is pinned by tag and digest in `tools/qwen/Dockerfile`;
  `docker compose build --build-arg BASE_IMAGE=...` overrides it.
- Torch/torchaudio come from the ROCm base image. Never add a CUDA torch
  wheel. `qwen-tts` is installed with `--no-deps` before its runtime deps so
  pip cannot replace the ROCm pair.
- Qwen3-TTS controls: `--qwen3-model`, `--qwen3-attn` (`sdpa`/`eager`/
  `flash_attention_2`), `--qwen3-x-vector-only`, `--qwen3-non-streaming`, and
  `--qwen3-seed`. The image bakes `generate.py` at build time: rebuild after
  changing the generator.
- **Do not set `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` for Qwen3-TTS.**
  It measured ~13-14% faster, but produced a different decode with an
  audibly shifted pitch/timbre versus the source clips; the baseline `sdpa`
  reruns were byte-identical. Keep the variable unset. Details:
  [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md#aotriton-do-not-enable).
- Qwen3-TTS 1.7B measured warm RTF **3.24-3.42** on the RX 9060 XT (load
  ~8 s, first reference prompt ~29 s, later prompts ~0.5 s). Batch only; do
  not put it in the live dialogue path. Listening samples and caveats:
  [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md).

### Live dev loop

`tools/voice.sh` is the one-command loop: it stages the installed pack,
generates every missed line, validates the manifest, and installs it into
`~/.xlcore/pluginConfigs/OhMyVoice`. It reads the sanitizer pin from the
installed manifest, so no env exports are needed. Then use `/omv` →
**Reload pack**.

```bash
./tools/voice.sh
```

The old manifest is kept as `manifest.json.bak`. Existing `.ogg` files are
preserved and skipped; only new ids are synthesized. The first run builds
the image, downloads the model, and creates the `data/` tree.

The model occasionally appends an invented sentence to otherwise correct
speech. The generator measures every new WAV and retries audio longer than
`max(4.5s, words / 2.2 + 1s)` with lower-temperature sampling; after three
attempts it rejects the line, leaves it in the worklist, and installs no bad
audio.

### Removed backends

XTTS-v2 and IndexTTS-2.5 are archived under
[`archive/tts-backends/`](archive/tts-backends/README.md) for reference.
Their Docker images, model weights, and caches were removed. They are not
services, not CI targets, and not part of the active generator.

The IndexTTS performance report is preserved at
[`docs/archive/indextts-performance.md`](docs/archive/indextts-performance.md).

## Playtest (Wine)

1. Build, then in-game `/xlsettings` add dev plugin location:
   `Z:\home\lexi\Projects\oh-my-voice\OhMyVoice\bin\x64\Release\OhMyVoice\OhMyVoice.dll`
2. `/xlplugins` → enable. `/omv` status window. Logs in `/xllog`.
3. P1 proof: audible unvoiced Talk NPC + voiced ARR suppression +
   subtitle dedupe + stop behaviors, with debug flag on for the run.

## Reference-audio policy (Q6)

Real-actor reference wavs stay local-personal: `data/refs/` (gitignored) or
another path supplied via `OMV_REFS`. NEVER committed, NEVER in the worklist.
CI rejects `references/` paths, audio blobs, and files over 2 MiB.
Distributed packs use generic-archetype or consented voices only.
`voiceId` in the ID makes clean packs a distinct namespace by construction.
