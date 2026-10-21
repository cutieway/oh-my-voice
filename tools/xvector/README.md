# xvector — average Qwen speaker embeddings per `speaker_token`

One job: turn every extracted game clip for a token into a single averaged
Qwen3-TTS speaker embedding. No training, no LoRA, no speech generation.

## Pipeline

```bash
# 1. game audio -> data/game-voices/{scd,wav}/<speaker_token>/
DOTNET=/nix/store/j8zkmcl067q2aqlg8r3lhm7kbv8iwx6v-dotnet-sdk-wrapped-10.0.401/bin/dotnet
$DOTNET run --project tools/xivdata -- \
  --out data/game-voices --extract --by-token

# 2. every WAV -> data/xvectors/<speaker_token>.npy
docker compose -f compose.xvector.yaml build
docker compose -f compose.xvector.yaml run --rm xvector

# pilot one speaker, rebuild an existing vector
docker compose -f compose.xvector.yaml run --rm xvector --speaker sphene --force
```

## Method

For each `data/game-voices/wav/<token>/`:

1. Read every `*.wav`, mix to mono.
2. Resample to 24 kHz with `librosa` when needed (game WAVs are 44,099 Hz).
3. `Qwen3TTSModel.model.extract_speaker_embedding()` once per clip.
4. `torch.stack(embeddings).mean(0)` -> one 2,048-float x-vector.

All clips are used; there is no sampling or curated-subset filter. The vector
is ~8 KB; the input corpus can be hundreds of MB.

## Output

```
data/xvectors/<speaker_token>.npy    # float32, shape (2048,)
data/xvectors/<speaker_token>.json   # clips, errors, seconds, dim, norm, model
data/xvectors/manifest.json          # all built speakers
```

Existing `.npy` + `.json` pairs are skipped; `--force` rebuilds. A clip that
fails to decode is counted in `Errors` and does not abort the speaker. A
speaker with zero usable clips writes no vector and is absent from the
manifest.

## Options

`run.sh` appends extra arguments to the builder defaults:

| Flag | Meaning |
| --- | --- |
| `--speaker TOKEN` | only this token; repeatable |
| `--force` | rebuild vectors that already exist |
| `--audio-root DIR` | override `OMV_AUDIO_ROOT` (default `/data/game-voices/wav`) |
| `--out DIR` | override `OMV_XVECTOR_OUT` (default `/data/xvectors`) |
| `--model DIR` | override `OMV_QWEN_MODEL_DIR` |
| `--device DEV` | default `cuda:0` |

## Cost

Measured on the RX 9060 XT for 255 Sphene clips, 1,519.84 s of audio:

- embedding extraction: 37.18 s (≈41× realtime)
- model load: 16.10 s
- prompt build total: 62.28 s including load

Corpus size scales only this one-time per-speaker step; the resulting prompt
is one fixed vector, so later per-line zero-shot speed is unchanged. WAV, not
Ogg, because lossy encoding would perturb the embedding.

Full measured run: 667 speakers, 25,597 clips, 152,667 s of audio, 832.0 s
total, 0 clip errors, 11 MB of vectors. The 44.1 kHz extraction tree was
18 GB. Only speakers with in-game voice have a token; unvoiced NPCs are out
of scope for this phase and stay silent.

`run.sh`/compose set `MIOPEN_FIND_MODE=FAST` to match those numbers. Without
it MIOpen tuning made the 255-clip pass 185 s; the vector still matched the
reference experiment at cosine 1.0000001.
