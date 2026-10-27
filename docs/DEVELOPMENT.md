# Development

Build, run, and test the plugin. For runtime behavior see
[PLUGIN.md](PLUGIN.md); for voice generation see
[QWEN3-TTS.md](QWEN3-TTS.md).

## Build (Linux or Windows)

The solution targets .NET 10 and builds a Dalamud plugin for Windows. On Linux,
enable Windows targeting (already set in the plugin project).

```bash
dotnet build -c Release
```

Use your SDK's absolute path if `dotnet` is not on `PATH`.

Dalamud dev DLLs resolve from `~/.xlcore/dalamud/Hooks/dev`; `DALAMUD_HOME`
overrides that. CI downloads the latest Dalamud build into the same layout.

The plugin assembly lands at
`OhMyVoice/bin/x64/Release/OhMyVoice.dll`; the Dalamud SDK also writes the
packaged plugin (DLLs, `OhMyVoice.json`, `latest.zip`) into
`OhMyVoice/bin/x64/Release/OhMyVoice/`.

## Playtest (Wine)

1. Build, then in game under `/xlsettings` add a dev plugin location:
   `Z:\home\<user>\Projects\oh-my-voice\OhMyVoice\bin\x64\Release\OhMyVoice.dll`
2. `/xlplugins` → enable **Oh My Voice**. `/omv` opens the status window; logs
   go to `/xllog`.
3. Acceptance run with debug logging on: an unvoiced `Talk` NPC is audible, a
   voiced ARR line is suppressed, subtitle duplication does not double-play,
   and both stop behaviors (new line, real VO) work.

## Layout

```text
OhMyVoice.slnx
OhMyVoice/                 # Dalamud plugin (net10.0-windows)
  Plugin.cs                # entrypoint, service wiring, /omv
  Configuration.cs         # Enabled/Volume/VoWaitMs/StopOnVo/DebugLogging/PlayerPronouns
  GameInterop.cs           # SeString/text readers, zone, player state
  TalkCapture.cs           # AddonTalk poll + TalkSubtitle PostDraw
  VoSuppression.cs         # SoundManager hooks, real-VO detection
  Dispatcher.cs            # ID, VO wait, dedupe, hit/miss logging, play
  Playback.cs              # Concentus decode + NAudio WaveOut
  PackStore.cs             # manifest, override resolution, sanitizer gate
  NpcMetadataService.cs    # live ENpcResident/ENpcBase metadata for logs
  VoiceLog.cs              # events.jsonl, compaction, legacy migration
  Windows/                 # /omv status + settings UI
OhMyVoice.Common/          # net10.0 shared: Sanitizer v1, VoiceId, PackTypes
compose.yaml               # batch generator: tts-qwen
compose.xvector.yaml       # average x-vectors: xvector
voice.sh                   # generate/resume one or more speakers' queued lines
scripts/
  sample.sh                # one-off prompt sample into data/samples/
  check-refs.sh            # CI guard: no voice audio/embeddings in git
tools/
  qwen/                    # Qwen3-TTS generator, linker, pronoun-pack migrator
  voice-map.json           # NPC -> xvector name
  xivdata/                 # offline dialogue DB + cutscene SCD extractor
  xvector/                 # average speaker embedding builder
data/                      # gitignored runtime root (see below)
archive/tts-backends/      # XTTS/IndexTTS code, reference only, never built
docs/
  PLUGIN.md                # runtime pipeline and pack format
  DEVELOPMENT.md           # this file
  QWEN3-TTS.md             # Qwen integration, quality, measurements
  VOICES.md                # voice roster: counts, metadata, caveats
  voices.csv               # one row per speaker label, metadata only
  assets/                  # README logo and diagrams
  archive/                 # superseded reports
vendors/                   # gitignored reference checkouts, never built
```

`data/` is the only runtime state:

```text
data/models/        Qwen3-TTS Base weights
data/cache/         HF/Torch/MIOpen caches
data/game-voices/   xivdata output: scd/ + wav/ per speaker_token
data/dialogue.db    xivdata dialogue/worklist DB (SQLite, WAL)
data/xvectors/      one .npy + .json per speaker_token, manifest.json
data/out/           generated packs
data/samples/       one-off prompt samples
data/backup/        retired packs, logs, and artifacts moved here, never deleted
```

Everything under `data/` is local and gitignored; back it up separately. If a
runtime artifact is no longer needed in place, move it under the matching
`data/backup/<path>` instead of deleting it.

`archive/` is source-only and is never built or composed. `vendors/` holds
reference checkouts and is not part of any build.

## CI

`.github/workflows/build.yml` runs on every push/PR to `main`:

1. `scripts/check-refs.sh` — rejects tracked reference paths, audio/embedding
   blobs, and any tracked file over 2 MiB.
2. `dotnet build --configuration Release`, with Dalamud downloaded into the
   dev-hooks layout.
3. Uploads `OhMyVoice` as a 7-day build artifact.

There is no .NET test project. The generator scripts have Python unit tests:

```bash
python3 -m unittest discover tools/qwen
```

Verify plugin changes with the playtest above; verify script and compose
changes with the affected service or test module.

## Voice-audio policy (Q6)

Raw game audio (`data/game-voices/`) and derived x-vectors (`data/xvectors/`)
stay local and gitignored. No single-reference/ICL transcript path is used for
packs. CI rejects `references/` paths, audio blobs, and files over 2 MiB.
Distributed packs use game-derived or consented voices only. `voiceId` in the
line ID makes clean packs a distinct namespace by construction.

## Removed backends

XTTS-v2 and IndexTTS-2.5 are archived under
[`../archive/tts-backends/`](../archive/tts-backends/README.md) for reference.
Their Docker images, model weights, and caches were removed. They are not
services, not CI targets, and not part of the active generator.

The IndexTTS performance report is preserved at
[`archive/indextts-performance.md`](archive/indextts-performance.md).
