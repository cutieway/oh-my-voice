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
  MissLog.cs          # atomic JSONL worklist
  Windows/            # /omv status + settings UI
OhMyVoice.Common/     # net10.0 shared: Sanitizer v1, VoiceId, PackTypes
scripts/check-refs.sh # Q6 guard: no reference audio in repo
Data/                 # pack fixtures only (small .ogg allowed)
```

## Build (Linux)

```bash
# dotnet 10.0.401 via nix store path (not on PATH):
DOTNET=/nix/store/j8zkmcl067q2aqlg8r3lhm7kbv8iwx6v-dotnet-sdk-wrapped-10.0.401/bin/dotnet
$DOTNET build -c Release
```

Dalamud dev DLLs resolve from `~/.xlcore/dalamud/Hooks/dev` (`DALAMUD_HOME` overrides).

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
