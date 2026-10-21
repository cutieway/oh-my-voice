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
- Cache hit/miss event log (`events.jsonl`): deduped per result + ID, with
  game-derived NPC metadata (name, race, sex, expansion, zone). Misses are
  the generation worklist; hits prove coverage. No live TTS fallback.
- One `speaker_token` keeps the same voice/race/sex across expansions;
  expansion/zone are per event.
- Real-VO suppression: ~100 ms wait before start, stop-if-started,
  data-driven blocklist.
- Playback: NAudio `WaveOut` + Concentus decode. Zero native dependencies.
- Batch generator: Qwen3-TTS Base on ROCm, in Docker, driven only by
  precomputed average speaker x-vectors.
- No fine-tuning and no LoRA. Generation consumes one averaged embedding per
  voice. Single-reference 3-30 s cloning and ICL transcripts are retired;
  use them only for isolated tests, not for packs.
- X-vector builder: averages every extracted game clip for a
  `speaker_token` into one 2,048-float Qwen speaker embedding. No training,
  no speech generation.
- Offline dialogue DB (`data/dialogue.db`): all static text, speaker/token
  attribution, `has_scd`, and generation queue. Built by `tools/xivdata`;
  SQLite/WAL, indexed, editable while testing. See
  [tools/xivdata/README.md](tools/xivdata/README.md).

## Layout

```text
OhMyVoice.slnx
OhMyVoice/            # Dalamud plugin (net10.0-windows)
  Plugin.cs           # entrypoint, service wiring
  Configuration.cs    # Enabled/Volume/VoWaitMs/StopOnVo/DebugLogging
  GameInterop.cs      # cutscene/duty state, SeString readers
  TalkCapture.cs      # AddonTalk poll + TalkSubtitle PostDraw
  VoSuppression.cs    # SoundManager hooks, real-VO detection
  Dispatcher.cs       # ID, VO-wait, dedupe, hit/miss logging, play
  Playback.cs         # Concentus decode + NAudio WaveOut
  PackStore.cs        # manifest + override resolution + sanitizer gate
  NpcMetadataService.cs # live ENpcResident/ENpcBase metadata for logs
  VoiceLog.cs         # hit/miss JSONL events, compacted on load
  Windows/            # /omv status + settings UI
OhMyVoice.Common/     # net10.0 shared: Sanitizer v1, VoiceId, PackTypes
scripts/check-refs.sh # Q6 guard: no voice audio/embeddings in repo
compose.yaml          # batch generator: tts-qwen
compose.xvector.yaml  # average x-vectors: xvector
sample.sh             # one-off prompt sample into data/samples/
tools/
  qwen/               # Qwen3-TTS Dockerfile, entrypoint, generator
  voice.sh            # one-command live dev loop
  voice-map.json      # NPC -> xvector name
  xivdata/            # offline dialogue DB + cutscene SCD extractor (Lumina)
  xvector/            # average speaker x-vector builder (no generation)
data/                 # gitignored runtime root
  models/             # Qwen3-TTS Base weights
  cache/              # HF/Torch/MIOpen caches
  game-voices/        # xivdata output: scd/ + wav/ per speaker_token
  dialogue.db         # xivdata dialogue/worklist DB (SQLite, WAL)
  xvectors/           # one .npy + .json per speaker_token, manifest.json
  out/                # generated pack
archive/tts-backends/ # XTTS/IndexTTS code, reference only, not built
docs/
  QWEN3-TTS.md        # Qwen integration, quality, measurements
  VOICES.md           # voice roster: counts, metadata, caveats
  voices.csv          # one row per speaker label, metadata only
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

One pinned ROCm/PyTorch image. Qwen3-TTS Base is driven by one precomputed
average x-vector per voice. The host needs only the amdgpu driver,
`/dev/kfd`, `/dev/dri`, and Docker — no host Python, CUDA, or Nix TTS
package.

```bash
mkdir -p data/{models,cache,xvectors,out}
# build data/xvectors first (see below), then point voice-map.json at them
export OMV_EVENTS="$HOME/.xlcore/pluginConfigs/OhMyVoice/events.jsonl"
export OMV_SANITIZER_PIN="v1:..."   # from the plugin's manifest / settings

docker compose build tts-qwen
docker compose run --rm tts-qwen    # -> data/out/voicelines/*.ogg + manifest.json
```

- First run downloads `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (~4.3 GB) into
  `data/models/qwen3-tts/`. HF/Torch caches go to `data/cache/`.
  `--qwen3-model` / `OMV_QWEN_MODEL_DIR` can point at another Base checkpoint.
  The 0.6B Base was tested and rejected (worse and not faster).
- The generator reads `events.jsonl` and selects only `Result="miss"` rows;
  hits are diagnostics. Each event carries NPC metadata resolved at playback
  time (`Npc.Name`, `Npc.NpcId`, `Npc.Race`, `Npc.Sex`, `Npc.Expansion`,
  `Npc.Zone`). Race/sex are stable per `speaker_token`; expansion varies.
- The generator reads `<voice>.npy` from `--xvectors` (mounted at
  `/xvectors`). `tools/voice-map.json` maps NPC keys to x-vector names; an
  explicit entry wins, otherwise a normalized NPC name matching exactly one
  existing vector is used (Alphinaud -> `alphinaud`). Ambiguous normalized
  matches are skipped. Missing vectors are skipped and stay in the worklist.
  Build the vectors with `compose.xvector.yaml` (below).
  See [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md).
- The image is ~40 GB uncompressed. Keep free space for Docker layers and
  the model. `docker builder prune` reclaims build cache.
- Do not run batch generation while the game is running (VRAM contention).
- `OMV_EVENTS` (required; plugin `events.jsonl`), `OMV_OUT` (default
  `./data/out`), `OMV_LANG` (default `en`),
  `OMV_VOICE_MAP` (default `tools/voice-map.json`), `OMV_XVECTORS`
  (default `./data/xvectors`), `OMV_UID`/`OMV_GID`, `OMV_MODELS` (default
  `./data/models`), and `OMV_CACHE` (default `./data/cache`) are
  overridable.
- The base image is pinned by tag and digest in `tools/qwen/Dockerfile`;
  `docker compose build --build-arg BASE_IMAGE=...` overrides it.
- Torch/torchaudio come from the ROCm base image. Never add a CUDA torch
  wheel. `qwen-tts` is installed with `--no-deps` before its runtime deps so
  pip cannot replace the ROCm pair.
- Qwen3-TTS controls: `--qwen3-model`, `--qwen3-attn` (`sdpa`/`eager`/
  `flash_attention_2`), `--qwen3-non-streaming`, and `--qwen3-seed`. The
  image bakes `generate.py` at build time: rebuild after changing the
  generator.
- Both compose services set `MIOPEN_FIND_MODE=FAST`. Keep it: without it,
  MIOpen's exhaustive per-shape find drops decode from RTF ~3 to ~12; a
  6.65 s sample went from 41.9 s to 106.6 s wall. Details and regression
  history: [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md#miopen-fast-find-is-required).
- One-off sample: `./sample.sh <speaker_token> "<sentence>"` writes
  `data/samples/<speaker_token>/` with the x-vector metadata and line audio.
  It never uses `tools/voice.sh` or installs into the plugin pack.
- Existing `.ogg` files are reused only when their recorded
  `GenerationFingerprint` matches the current model revision, generation
  settings, encoder settings, and that voice's x-vector hash. Manifests from
  before fingerprints cause a one-time rebuild. Use
  `./tools/voice.sh --force` (or `--force` directly) to regenerate every
  planned line. `tools/voice.sh` stages the installed manifest so profiles
  survive the dev loop. Ogg writes are crash-safe: each line is encoded to a
  sibling `.tmp.ogg`, ffprobe-validated, then atomically renamed; orphaned
  temp files are removed before any line is encoded.
- Prefer a local `--qwen3-model` directory for the cache key: its weight
  files are hashed, so the recorded revision is exact. A repo id resolves to
  the Hugging Face cache commit after download; if it remains unresolved the
  manifest warns and fingerprints use the repo id.
- **Do not set `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` for Qwen3-TTS.**
  It measured ~13-14% faster, but produced a different decode with an
  audibly shifted pitch/timbre versus the source clips; the baseline `sdpa`
  reruns were byte-identical. Keep the variable unset. Details:
  [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md#aotriton-do-not-enable).
- Qwen3-TTS 1.7B measured warm RTF **3.24-3.42** with FAST on the RX 9060 XT
  (load ~8 s). Batch only; do not put it in the live dialogue path. Listening
  samples and caveats: [docs/QWEN3-TTS.md](docs/QWEN3-TTS.md).

### Average speaker x-vectors (game audio)

**Supported method.** Do not use 3-30 s single-reference zero-shot or ICL
transcripts for packs; use x-vectors unless explicitly testing another
method. This path does not generate speech. It builds one average Qwen
speaker embedding per `speaker_token` from **every** extracted cutscene clip,
which drives the pack generator. It is not training and not LoRA.

```bash
# 1. extract all cutscene SCDs + WAVs, grouped by speaker_token
DOTNET=/nix/store/j8zkmcl067q2aqlg8r3lhm7kbv8iwx6v-dotnet-sdk-wrapped-10.0.401/bin/dotnet
$DOTNET run --project tools/xivdata -- \
  --out data/game-voices --extract --by-token

# 2. average all clips into data/xvectors/<speaker_token>.npy
docker compose -f compose.xvector.yaml build
docker compose -f compose.xvector.yaml run --rm xvector
# one speaker while iterating:
docker compose -f compose.xvector.yaml run --rm xvector --speaker sphene --force
```

- Step 1 needs the installed game's `sqpack` and `vgmstream-cli`; it writes
  `data/game-voices/scd/<token>/` and `data/game-voices/wav/<token>/`.
  Use `--speaker TOKEN` / `--limit N` to pilot.
- Step 2 reads `data/game-voices/wav/<token>/*.wav`, resamples to 24 kHz
  mono, extracts one 2,048-d embedding per clip, and means them. Output is
  `data/xvectors/<token>.npy` (~8 KB), a sibling `.json`, and
  `manifest.json`.
- No clip sampling and no curated-subset filter: all extracted lines count.
  Corpus size scales only the one-time embedding pass: 255 clips /
  1,519.84 s of audio measured 37.18 s of extraction (≈41× realtime, 62.28 s
  including model load). Alphinaud is 6.4× that clip count, so ≈4 minutes
  one-time [estimate]; per-line generation cost is unchanged because the
  prompt stays one fixed vector.
- Existing `.npy` + `.json` pairs are reused only when the WAV corpus, model
  weights, and audio-pipeline version still match the recorded fingerprints;
  otherwise that speaker rebuilds automatically. Pass `--force` to rebuild
  regardless. Broken clips are logged, counted in the sidecar, and do not
  abort the speaker. A speaker with zero usable clips produces no vector.
- WAV, not Ogg: lossy encoding would perturb the embeddings. The extraction
  WAVs are 44.1 kHz; the builder resamples.
- Phase 1 covers only characters with in-game voice audio and therefore a
  `speaker_token`. Unvoiced NPCs (Storm Mage, OIC officers, generic citizens)
  stay in the worklist and play nothing; synthetic/generic voices are a later
  phase, not a hidden fallback.
- Full measured run: 667 speakers, 25,597 clips, 152,667 s of audio,
  832.0 s total, 0 clip errors, 11 MB of vectors. Extraction produced 18 GB
  of SCD+WAV and took 731 s; one `vgmstream` clip failed.

### Live dev loop

`tools/voice.sh` is the one-command loop: it stages the installed pack,
generates every missed line, validates the manifest, and installs it into
`~/.xlcore/pluginConfigs/OhMyVoice`. It reads the sanitizer pin from the
installed manifest, so no env exports are needed. Then use `/omv` →
**Reload pack**.

```bash
./tools/voice.sh            # reuse lines whose generation profile matches
./tools/voice.sh --force    # regenerate every planned line
```

The old manifest is kept as `manifest.json.bak`. Existing `.ogg` files are
reused only when their generation profile matches; new ids are synthesized.
The first run builds the image, downloads the model, and creates the `data/`
tree.

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

## Voice-audio policy (Q6)

Raw game audio (`data/game-voices/`) and derived x-vectors
(`data/xvectors/`) stay local and gitignored. No single-reference/ICL
transcript path is used for packs. NEVER commit raw audio or embeddings; CI
rejects `references/` paths, audio blobs, and files over 2 MiB. Distributed
packs use game-derived or consented voices only. `voiceId` in the ID makes
clean packs a distinct namespace by construction.
