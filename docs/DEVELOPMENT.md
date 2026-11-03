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
overrides that. Download the latest Dalamud build into that layout if needed.

The plugin assembly lands at
`OhMyVoice/bin/x64/Release/OhMyVoice.dll`; the Dalamud SDK also writes the
packaged plugin (DLLs, `OhMyVoice.json`, `latest.zip`) into
`OhMyVoice/bin/x64/Release/OhMyVoice/`.

## Playtest (Wine)

1. Build, then in game under `/xlsettings` add a dev plugin location:
   `Z:\home\<user>\Projects\oh-my-voice\OhMyVoice\bin\x64\Release\OhMyVoice.dll`
2. `/xlplugins` → enable **Oh My Voice**. `/omv` opens the status window; logs
   go to `/xllog`.
3. Set the pack root to `Z:\home\<user>\Projects\oh-my-voice\data\out`
   and confirm the status window reports the expected voice and line counts.
4. Acceptance run with debug logging on: an unvoiced `Talk` NPC is audible, a
   voiced line is suppressed, subtitle duplication does not double-play,
   and both stop behaviors (new line, real VO) work.

## Layout

```text
OhMyVoice.slnx
OhMyVoice/                 # Dalamud plugin (net10.0-windows)
  Plugin.cs                # entrypoint, service wiring, /omv
  Configuration.cs         # TermsAccepted/PackDirectory and playback/settings flags
  GameInterop.cs           # SeString/text readers, zone, player state
  TalkCapture.cs           # AddonTalk poll + TalkSubtitle PostDraw
  VoSuppression.cs         # SoundManager hooks, real-VO detection
  Dispatcher.cs            # ID, VO wait, dedupe, hit/miss logging, play
  Playback.cs              # Concentus decode + NAudio WaveOut
  PackStore.cs             # multi-manifest catalog, overrides, sanitizer gate
  NpcMetadataService.cs    # live ENpcResident/ENpcBase metadata for logs
  VoiceLog.cs              # events.jsonl, compaction, legacy migration
  Windows/                 # /omv status + settings UI
OhMyVoice.Common/          # net10.0 shared: Sanitizer v1, VoiceId, PackTypes
compose.yaml               # batch generator: tts-qwen
compose.xvector.yaml       # average x-vectors: xvector
voice.sh                   # generate/resume speakers, queue-driven (no args = all)
scripts/
  sample.sh                # one-off prompt sample into data/samples/
  check-refs.sh            # repo guard: no voice audio/embeddings in git
tools/
  qwen/                    # Qwen3-TTS generator, linker, pronoun-pack migrator
  roster.py                # per-speaker workload from dialogue.db (run order, CSV)
  progress.py              # corpus size + generation progress from dialogue.db
  voice-map.json           # NPC -> xvector name
  xivdata/                 # offline DB, SCD extractor, Lumina NPC index
  blueprint_pipeline/      # read-only Phase 2 inventory, dossiers, classifiers, review
  xvector/                 # average speaker embedding builder
data/                      # gitignored runtime root (see below)
docs/
  PLUGIN.md                # runtime pipeline and pack format
  DEVELOPMENT.md           # this file
  QWEN3-TTS.md             # Qwen integration, quality, measurements
  VOICES.md                # voice roster: counts, metadata, caveats
  progress.md              # corpus scale + generation progress snapshot
  voices.csv               # one row per speaker label, metadata only
  workload.csv             # generation progress per speaker (from dialogue.db)
  assets/                  # README logo and diagrams
vendors/                   # gitignored reference checkouts, never built
```

`data/` is the only runtime state:

```text
data/models/        Qwen3-TTS Base weights
data/cache/         HF/Torch/MIOpen caches
data/game-voices/   xivdata output: scd/ + wav/ per speaker_token
data/dialogue.db    xivdata dialogue/worklist DB (SQLite, WAL)
data/xvectors/      one .npy + .json per speaker_token, manifest.json
data/blueprints/    Phase 2 caches, reports, assignments, generated draft profiles
data/out/           generated multi-voice pack root (one folder per voice)
data/samples/       one-off prompt samples
data/backup/        retired packs, logs, and artifacts moved here, never deleted
```

Everything under `data/` is local and gitignored; back it up separately. If a
runtime artifact is no longer needed in place, move it under the matching
`data/backup/<path>` instead of deleting it.

`vendors/` holds reference checkouts and is not part of any build.

## Local checks

CodeTrunk does not currently provide hosted runners, so this repository has no
automatic CI workflow. Run the checks locally:

```bash
bash scripts/check-refs.sh
python3 -m unittest discover -s tools
python3 tools/blueprint.py check
python3 -m unittest discover -s tools/qwen
python3 -m unittest tools/test_roster.py   # queue views; skipped without dotnet
python3 tools/progress.py                 # corpus/generation snapshot
dotnet build --configuration Release
```

`check-refs.sh` rejects tracked reference paths, audio/embedding blobs, and any
tracked file over 2 MiB. There is no .NET test project; `test_roster.py` runs
`xivdata migrate` against a throwaway DB so the resume views are tested from
the real SQL.

Verify plugin changes with the playtest above; verify script and compose
changes with the affected service or test module.

## Voice-audio policy (Q6)

Raw game audio (`data/game-voices/`) and derived x-vectors (`data/xvectors/`)
stay local and gitignored. No single-reference/ICL transcript path is used for
packs. `scripts/check-refs.sh` rejects `references/` paths, audio blobs, and
files over 2 MiB.

## Synthetic blueprint workflow (Phase 2)

Phase 2 is separate from `voice.sh`, its speaker selection, x-vector
construction, and the Qwen Base generation path. `tools/blueprint_pipeline`
opens `dialogue.db` read-only, collapses `dialog_speakers` aliases by canonical
NPC ID, excludes any identity with an SE-voiced alias, and routes ambiguous or
unresolved identities to review. `xivdata npc-index` exports optional race,
tribe/clan, and game sex metadata from Lumina without changing the DB.

```bash
dotnet run --project tools/xivdata -- npc-index --lang en > /tmp/game-npc-index.json
python3 tools/blueprint_pipeline/cli.py inventory --game-index /tmp/game-npc-index.json
python3 tools/blueprint_pipeline/cli.py dossiers --game-index /tmp/game-npc-index.json --limit 25
python3 tools/blueprint_pipeline/cli.py classify --provider rules --limit 25
python3 tools/blueprint_pipeline/cli.py classify --provider jev --limit 25
python3 tools/blueprint_pipeline/cli.py inspect npc_1003075 --review-report
python3 tools/blueprint_pipeline/cli.py validate
python3 tools/blueprint_pipeline/cli.py report
```

Rules classification is offline. Jev defaults to a loopback endpoint and needs
no network opt-in or API key there. Non-loopback endpoints require
`--allow-remote` plus `TYPESAFE_API_KEY` in the environment. Jev defaults to 25
dossiers per run. Larger batches require `--allow-bulk` and
print a request, token, and approximate cost estimate first. Cached outputs are
immutable under gitignored `data/blueprints/`, so unchanged dossiers and
classifier settings resume without duplicate API work. Jev cannot process
audio or provide citations.
Race, clan, and sex describe identity and are not acoustic evidence. Synthetic
profiles remain drafts until a human auditions a VoiceDesign clip, pins its
hash, and approves it. This task does not wire those references into the
runtime or alter Phase 1.
Distributed packs use game-derived or consented voices only. `voiceId` in the
line ID makes clean packs a distinct namespace by construction.

## Publishing a Dalamud custom repository

`repo.json` at the repository root is the custom repository index. The URL users
add in Dalamud is the raw file:

```text
https://repos.codetrunk.eu/cutieway/oh-my-voice/raw/branch/main/repo.json
```

For each release:

1. Bump `<Version>` in `OhMyVoice/OhMyVoice.csproj`.
2. Run `dotnet build --configuration Release`.
3. Create a CodeTrunk release tagged `v<version>`.
4. Upload `OhMyVoice/bin/x64/Release/OhMyVoice/latest.zip` as
   `OhMyVoice.zip`. Keep the ZIP flat: `OhMyVoice.dll`, `OhMyVoice.json`, and
   dependencies must be at its root.
5. Update `AssemblyVersion`, `LastUpdate`, and the download links in
   `repo.json`, then commit and push it.

`DownloadLinkInstall` and `DownloadLinkUpdate` may use the same versioned
release asset URL. `InternalName` must remain `OhMyVoice`. Do not commit release
ZIPs; keep them as CodeTrunk release assets.
