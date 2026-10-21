# Qwen3-TTS backend

Status: implemented as `--backend qwen3tts` and the `tts-qwen` compose
service. Model weights are Apache-2.0; this repo's AGPL-3.0 covers the
integration code only.

## Upstream

- Repository: <https://github.com/QwenLM/Qwen3-TTS>
- Model: `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (4.5 GB), 12 Hz tokenizer +
  discrete multi-codebook LM, not a diffusion model. The 0.6B Base was
  tested and rejected: worse output and not faster per generated second.
- Clone mode needs reference audio plus its transcript. With no transcript
  Qwen can use `x_vector_only_mode`, but upstream warns quality is reduced.
- The package is `qwen-tts==0.1.1`; it pins `transformers==4.57.3`,
  `accelerate==1.12.0`. The image installs it `--no-deps` on the ROCm
  PyTorch base and adds the runtime deps without torch/torchaudio, so pip
  cannot replace the ROCm wheels.

## Integration

- `tools/tts-qwen/Dockerfile`: ROCm/PyTorch 2.8 + Python 3.10 image.
- `compose.yaml`: `tts-qwen`, defaults to the 1.7B Base model.
- `tools/voice.sh`: `OMV_BACKEND=qwen3tts`.
- `tools/generate.py`: `--backend qwen3tts`, `--qwen3-model`,
  `--qwen3-attn` (`sdpa` default), `--qwen3-x-vector-only`,
  `--qwen3-non-streaming`, `--qwen3-seed`.
- Reference transcript: a sibling `<voiceId>.txt` next to
  `<voiceId>.wav`. Without it, the backend falls back to x-vector-only and
  prints a warning. The Piper references in the listening set have `.txt`
  files; the real `generic-male-01.wav` does not.

```bash
export OMV_MISSES=... OMV_REFS=... OMV_SANITIZER_PIN=v1:...
OMV_BACKEND=qwen3tts tools/voice.sh
# or:
docker compose build tts-qwen
docker compose run --rm tts-qwen
```

First run downloads the model into `models/qwen3-tts/<model>`; the HF
cache lives in the compose cache volume (`/cache/hf`), not beside the model.

## Measured on RX 9060 XT (16 GB), Piper reference clips, EN

The three lines are the listening-set lines (4-13 s). `sdpa` attention,
bf16, no flash-attn (not installed; ROCm warns that flash/mem-efficient SDPA
is experimental).

| Model | Load | First ref prompt | Later ref prompts | Warm line 1 | Warm line 2 | Warm line 3 | Warm RTF |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1.7B Base | 7.9 s | 29.3 s | 0.45 s | 15.1 s / 4.4 s | 20.6 s / 6.2 s | 40.7 s / 12.6 s | 3.24-3.42 |
| 1.7B, non-streaming | 6.7 s | 29.0 s | 0.47-0.49 s | 17.9 s / 4.0 s | 19.8 s / 6.3 s | 43.8 s / 13.7 s | 3.12-3.20 |

- End-to-end generator run (3 lines, model load included): **143 s** for
  1.7B. The 0.6B model was rejected.
- The first `create_voice_clone_prompt` costs ~30 s (tokenizer/first-call
  work); subsequent voices ~0.5 s. Reuse is per process, per voice.
- For comparison, IndexTTS fp32 was RTF ~3.35 warm; IndexTTS FP16 ~2.2.
  Qwen3-TTS has no diffusion steps/CFG knobs; its cost is autoregressive
  token generation.

## Quality

`non_streaming_mode` is **not** a different model and does not disable
cloning. It changes how text is fed to the same Base model: full text at
once vs simulated streaming chunks. Measured with the model's speaker
encoder, cosine similarity to the reference clip was 0.986-0.992 for all
two modes (1.7B streamed and non-streaming), so speaker identity is
preserved. The first 15/17 comparison was unseeded and confounded by sampling
variance; groups 18/19 in the listening set are the controlled A/B (same
process, same prompt, seed 1234): default 4.16/6.16/12.32 s vs non-streaming
4.24/6.24/11.68 s. The difference is small; use those two groups to judge
`non_streaming_mode` by ear.
Qwen's non-cloning models are `CustomVoice` and `VoiceDesign`; the generator
refuses anything that is not a `*-Base` model.

Local listening set: `/home/lexi/omv-quality-test/clips/15-qwen3-1.7b`,
`17-qwen3-1.7b-nonstream`, `18-qwen3-1.7b-seeded`, and
`19-qwen3-1.7b-nonstream-seeded`, with the same three synthetic reference
voices and lines as the IndexTTS matrix. `ratings.csv` includes all
three groups. No subjective quality verdict is recorded here; listen before
choosing.

## Caveats

- `flash-attn` is not installed. The image uses `sdpa`; ROCm prints an
  experimental-attention warning. `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1`
  may enable faster kernels but is not enabled by default.
- Qwen cloning uses the reference transcript for ICL mode. Missing or wrong
  transcripts degrade clone quality; sidecars are the supported path.
- The upstream license is Apache-2.0, but generated voice packs still need
  the same donor-consent/licensing review as any cloned voice. Do not
  distribute clones of real actors without permission.
