# Oh My Voice

Cloned voices for unvoiced FFXIV dialogue. Pre-generated XTTS voice pack,
local playback, silent on cache miss. Never doubles real voice-over.

AGPL-3.0. See `LICENSE.md`.

## Status: P1 skeleton

- ARR MSQ `AddonTalk` + `TalkSubtitle` capture, EN only.
- `id = md5(lang:voiceId:npcId:sentence)` over sanitized text (`OhMyVoice.Common`).
- Pack: `voicelines/{id}.ogg` (Opus 48 kHz mono, 32 kbps), `voicelines_override/` wins.
- Silent miss + deduped `misses.jsonl` worklist. No live TTS fallback in P1.
- Real-VO suppression: ~100 ms wait before start, stop-if-started, data-driven blocklist.
- Playback: NAudio `WaveOut` + Concentus decode. Zero native dependencies.
- Batch generator: `docker compose` (ROCm XTTS) consumes `misses.jsonl`,
  emits the Opus pack. Host needs no Python/ROCm toolchain.
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
Data/                 # pack fixtures only (small .ogg allowed)
compose.yaml          # batch TTS service (ROCm image)
tools/generate.py     # misses.jsonl -> .ogg pack generator
tools/tts/            # Dockerfile + pinned Python requirements
```

## Build (Linux)

```bash
# dotnet 10.0.401 via nix store path (not on PATH):
DOTNET=/nix/store/j8zkmcl067q2aqlg8r3lhm7kbv8iwx6v-dotnet-sdk-wrapped-10.0.401/bin/dotnet
$DOTNET build -c Release
```

Dalamud dev DLLs resolve from `~/.xlcore/dalamud/Hooks/dev` (`DALAMUD_HOME` overrides).

## Batch generation (Docker, ROCm)

The generator runs in a pinned ROCm/PyTorch image; the host needs only the
amdgpu driver, `/dev/kfd`, `/dev/dri`, and Docker. No host Python, CUDA, or
Nix TTS package.

```bash
mkdir -p .cache/tts pack
export OMV_MISSES="$HOME/.xlcore/pluginConfigs/OhMyVoice/misses.jsonl"
export OMV_REFS="$HOME/oh-my-voice-refs"
export OMV_SANITIZER_PIN="v1:..."   # from the plugin's manifest / settings

docker compose build tts
docker compose run --rm tts          # -> pack/voicelines/*.ogg + pack/manifest.json
```

- First run downloads XTTS v2 (~1.9 GB) into `.cache/tts`. Keep that directory
  between runs; delete it to force a re-download.
- The pinned ROCm image is ~40 GB uncompressed (the first build pulls ~10 GB).
  Keep ~50 GB free; `docker builder prune` reclaims build cache if needed.
- Do not run batch generation while the game is running (VRAM contention).
- `OMV_OUT` (default `./pack`), `OMV_LANG` (default `en`), `OMV_VOICE_MAP`
  (default `tools/voice-map.json`), and `OMV_UID`/`OMV_GID` are overridable.
- The base image is pinned by tag and digest in `tools/tts/Dockerfile`;
  `docker compose build --build-arg BASE_IMAGE=...` overrides it.
- Torch/torchaudio come from the base image. `tools/tts/requirements.txt`
  deliberately pins `transformers` to the 4.57 line, because `coqui-tts`
  accepts an unbounded `>=4.57` and 5.x is untested.

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
preserved and skipped; only new ids are synthesized.

XTTS occasionally appends an invented sentence to otherwise correct speech.
The generator measures every new WAV and retries audio longer than
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
