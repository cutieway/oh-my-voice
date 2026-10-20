# IndexTTS-2.5 on ROCm: performance report

Date: 2026-10-20. Scope: the oh-my-voice batch generator, IndexTTS-2.5
backend. The plugin/playback path is out of scope.

## Host and stack

| Item | Value |
|---|---|
| GPU | AMD Radeon RX 9060 XT, `gfx1200` |
| Driver | amdgpu; `/dev/kfd` + `/dev/dri` passed to Docker |
| ROCm / PyTorch | ROCm 7.2.x / `torch 2.8.0+rocm7.2.4` |
| IndexTTS | IndexTTS-2.5, Python 3.10 |
| Model | IndexTTS weights + w2v-BERT + BigVGAN, ~11 GB |
| Container | `oh-my-voice/tts-indextts:0.1.0`, ~42 GB |
| Reference | `/home/lexi/oh-my-voice-refs/generic-male-01.wav` mounted at `/refs` |
| Test line | "An OIC officer of arms arrives to the scene and assesses the situation. He shakes his head." |

All times are wall-clock inference times after model load unless noted.
The full-line measurements are `n=2` (first and warm), not averaged. The
original pack-level baseline was a 3-line worklist run; it is not the same
workload as the single-line benchmarks.

## Executive summary

1. The original per-line bottleneck was **MIOpen's default exhaustive
   per-shape algorithm search**, not steady-state GPU compute. BigVGAN with
   a fixed input shape is ~3.7 s after the first call, but a new audio length
   paid tens of seconds again.
2. `MIOPEN_FIND_MODE=FAST` is the only shipped change. With the real
   reference it cuts a warm ~7 s line from **64-71 s to 27.9 s** (RTF
   ~13 -> 4.0), and the cold first line from ~91 s to ~58 s. It is a
   config-only change, same kernels, no quality change by construction.
3. The earlier claim of 15.8 s / 4.1x was measured with a synthetic 220 Hz
   reference and is **invalid for the pack**. The real reference changes
   sequence lengths; s2mel is 12.4 s, not 5.4 s. The documented numbers now
   use the real reference.
4. The custom HIP BigVGAN kernel and the fixed-length vocoder bucket were
   both rejected: ~10% and ~11% respectively, below the complexity/risk
   bar. Neither is in the tree.
5. The warm budget is now roughly **GPT 6.9 s + s2mel 12.4 s + BigVGAN
   8.5 s**. s2mel is the largest stage and the next place to look.
6. Follow-up audit (2026-10-20, same line/reference): **FP16 autocast on
   s2mel/BigVGAN** cuts the warm line from **29.9 s to 19.1 s (1.56x)**;
   FP16 + `torch.compile` reaches **14.3-15.1 s (≈2x)** after a 5-7 minute
   first-line compile; fp32 compile alone reaches 26.0 s because BigVGAN
   per-shape work dominates; fewer CFM steps / CFG 0 mainly cut s2mel and
   stack with FP16. All are opt-in in `tools/generate.py`; none changes the
   defaults, and none has had a listening/ASR quality gate.

Qwen3-TTS is a separate backend, not an IndexTTS optimization. Its
integration and measured warm performance (RTF ~3.3 on the same host) are
in [QWEN3-TTS.md](QWEN3-TTS.md); it does not use the IndexTTS steps/CFG/fp16
knobs.

## Baseline

| Measurement | Audio / line | Total | RTF | Notes |
|---|---:|---:|---:|---|
| Original 3-line worklist | 23.17 s | 316 s | 13.6 | Pre-FAST |
| README 3-line run | 23.17 s | 372 s / 6.2 min | ~16 | Includes ~55 s model load |
| README 5.5 s line | 5.5 s | ~71 s | ~13 | Pre-FAST |
| README 10 s line | 10 s | ~152 s | ~15 | Pre-FAST |
| Single benchmark line, no FAST | 4.99 s | 70.89 s | 12.9 | BigVGAN 51.93 s |
| Single benchmark line, no FAST, custom HIP kernel | 4.99 s | 64.44 s | 12.9 | BigVGAN 46.69 s; ~9% |

The no-FAST 70.89 s and the custom-kernel 64.44 s are separate runs. The
output length is stochastic, so the 4.99 s and 7.0 s FAST runs are not an
exact apples-to-apples pair. The stage breakdowns and RTFs are the useful
comparison.

## Tested

| # | Experiment | Condition | Result | Verdict |
|---|---|---|---|---|
| T1 | Original 3-line pack | Pre-FAST | 316 s for 23.17 s; RTF 13.6 | Baseline |
| T2 | Single line, default MIOpen | Pre-FAST | 70.89 s; GPT 5.15 + s2mel 12.58 + BigVGAN 51.93; RTF 12.9 | Baseline |
| T3 | BigVGAN microbench | `80 x 500`, 128,000 samples | First 59.79 s, warm 3.74 s | Discovery: find/search, not math |
| T4 | Custom HIP BigVGAN kernel | Patched temporary source + `.so`; no FAST | 64.44 s; BigVGAN 46.69 s; microbench 3.43 s warm | ~8-10%; rejected |
| T5 | FAST, synthetic reference | 5 s 220 Hz sine at `/bench/ref.wav` | 40.21 s first / 15.79 s warm; GPT 5.91 + s2mel 5.42 + BigVGAN 4.41; RTF 2.69 | Invalid for docs |
| T6 | FAST, real reference | `/refs/generic-male-01.wav` | **57.66 s first / 27.86 s warm**; GPT 6.86 + s2mel 12.43 + BigVGAN 8.54; RTF 3.97 | Shipped |
| T7 | FAST + 1024-frame vocoder bucket | Synthetic reference; hardcoded 256 trim | 44.49 s first / 14.01 s warm; ~11% over T5 | Rejected; not tested real |
| T8 | Compose env validation | `docker compose config` | Both `tts` and `tts-indextts` show `MIOPEN_FIND_MODE: FAST` | Shipped |
| T9 | FAST + FP16 autocast s2mel/BigVGAN | Real reference, 2 runs | Warm **19.13 s**, audio 7.42 s, RTF 2.58; GPT 7.87 + s2mel 6.92 + BigVGAN 4.31 | Opt-in; quality unaudited |
| T10 | FAST + `torch.compile` s2mel DiT | Real reference, 2 runs | Warm **26.03 s**, RTF 3.58; s2mel 1.40 but BigVGAN 17.02; first line 192 s (compile) | Opt-in; limited net win |
| T11 | FAST + 16 CFM steps | Real reference, first line | 48.58 s; s2mel **6.97** (vs 13.83 cold), BigVGAN 11.26, audio 7.34 | Opt-in |
| T12 | FAST + 12 CFM steps | Real reference, first line | 46.61 s; s2mel **3.97**, BigVGAN 11.69, audio 7.66 | Opt-in; quality risk |
| T13 | FAST + CFG rate 0 | Real reference, first line | 46.61 s; s2mel **7.31**, BigVGAN 8.17, audio 7.34 | Opt-in; quality risk |
| T14 | FP16 + 12 steps + CFG 0 | Real reference, first line | **44.70 s**; GPT 15.15 (cold) + s2mel 6.46 + BigVGAN 6.73, audio 7.38 | Fastest measured; quality unaudited |
| T15 | FP16 + `torch.compile` | Real reference, 3 runs | First line 372.7 s (compile 333 s); warm **14.34 s** (RTF 2.01; BigVGAN 0.63 cached shape) and **15.11 s** (RTF 2.13; BigVGAN 6.75 new shape) | Best warm total; quality unaudited |

T14 with `torch.compile` also enabled was smoke-tested end to end: the first
line took **425 s** (s2mel compile alone 386 s). A second run measured 333 s
first line (compile 333 s), so expect roughly 5-7 minutes of one-time compile.
Compile only pays in a long-lived process. Without compile, T14 is the
recommended opt-in preset, pending a quality gate; the shipped defaults stay
fp32/25 steps/CFG 0.7.

T15 also explains the T10 BigVGAN anomaly: warm BigVGAN was 0.63 s on the
same output shape and 6.75 s on a new shape, so the 17.02 s in T10 is
per-shape MIOpen work plus run variance, not a persistent `torch.compile`
side effect. Under compile+FP16, warm total is ~14-15 s (≈2x the fp32
baseline).

### MIOpen find/search details

`MIOPEN_FIND_MODE` defaults to searching for each new convolution shape. On
`gfx1200` the BigVGAN path also produced repeated warnings:

```text
MIOpen(HIP): Warning [IsEnoughWorkspace] [GetSolutionsFallback WTI]
Solver <GemmFwdRest>, workspace required: ..., provided ptr: 0 size: 0
```

The fixed-shape microbench isolates the effect from model load and text
generation:

| BigVGAN microbench, 80 x 500 mel | First call | Warm call |
|---|---:|---:|
| Default find mode | 59.79 s | 3.74 s |
| Custom HIP kernel, default find | 57.37 s | 3.43 s |

The 16x first/warm gap is the per-shape search/autotune. A new line has a new
length, so it paid that search again. `FAST` selects immediately and skips
the exhaustive search. The full real-reference line still pays some one-time
work: 57.66 s cold, 27.86 s warm.

## What worked

### `MIOPEN_FIND_MODE=FAST`

- Applied to both compose services:
  - `tts` (XTTS-v2 fallback; not separately benchmarked)
  - `tts-indextts` (IndexTTS-2.5)
- Verified with `docker compose config`; the variable is present on both.
- Real-reference warm line: **27.86 s** (RTF 3.97), down from **64-71 s**
  (RTF ~13). Cold first line: **57.66 s**, after **33.95 s** model load.
- Same kernels and output semantics; only algorithm selection changes.
- Commit: `6e436c3` (config + docs); corrected numbers: `00e4f70`.

### Correct reference benchmarking

- Synthetic/sine references are not valid for performance work here.
  Reference length and content change the feature/sequence shapes.
- The durable reference is
  `/home/lexi/oh-my-voice-refs/generic-male-01.wav`, mounted at `/refs`.
- The benchmark harness used `/bench/ref.wav` only for the invalid T5/T7
  exploratory runs.

### Existing safe defaults

- Loader passes `use_cuda_kernel=False`, avoiding the CUDA-only BigVGAN
  kernel path on ROCm.
- `--indextts-bf16` (default on) applies bf16 to the **GPT stage only**.
  The live dtypes in the container are GPT bf16; semantic model, semantic
  codec, s2mel, BigVGAN, and campplus fp32. The earlier "full model bf16"
  statement was incorrect; T9 is the opt-in path that moves s2mel/BigVGAN
  to fp16 autocast.

## What did not work / was rejected

| Candidate | Measurement | Why rejected |
|---|---|---|
| Custom HIP BigVGAN kernel (ported CUDA activation kernel) | BigVGAN 51.93 -> 46.69 s; total 70.89 -> 64.44 s; microbench 3.74 -> 3.43 s | ~8-10%; temporary source patch, build complexity, no 13x issue result on ROCm |
| Fixed 1024-frame BigVGAN bucket | Synthetic FAST 15.79 -> 14.01 s (~11%) | Below complexity bar; hardcoded 256x trim; no real-reference validation |
| Synthetic-reference benchmark | FAST 15.79 s | Invalid; real reference is 27.86 s; s2mel shape cost hidden |
| `MIOPEN_LOG_LEVEL=3` as a speed fix | Verbose warnings only | Diagnostics, not performance |
| `use_accel` / flash-attn | Not installed | No ROCm flash-attn path in this image; adding it is a dependency project |
| DeepSpeed | Not installed | Same; no measured ROCm benefit |
| `torch.compile` on s2mel DiT | Warm 26.03 vs 29.92 s; s2mel 13.15 -> 1.40 s; BigVGAN 9.12 -> 17.02 s on a new output shape (0.63 s cached, 6.75 s new under FP16); first line 333-386 s | fp32 compile alone only ~13%; per-shape BigVGAN work dominates. Compile+FP16 warm 14-15 s, opt-in |
| bf16/fp16 for the whole model | Not attempted | GPT bf16 is the only stage that benefits from bf16; s2mel/BigVGAN use fp16 autocast opt-in (T9) |
| Reducing CFM steps / CFG | 12 steps 46.61 s cold; CFG 0 46.61 s cold; combined with FP16 44.70 s | Quality tradeoff untested; opt-in flags only |
| ONNX Runtime / TensorRT | Not tried | TensorRT is NVIDIA-only; ORT ROCm path not established here |

The earlier 4.1x figure is explicitly withdrawn. The defensible summary is
~2.3x per line / ~3.2x RTF for the measured line, and ~2.8x for a three-line
estimate.

## Open questions / not tried yet

Ordered by expected value:

1. **Quality gate for the opt-in speed paths.** FP16 s2mel/BigVGAN, 12-16
   CFM steps, and CFG 0 all need listening/ASR/speaker-similarity checks
   against the 25-step fp32 output before any becomes a default.
2. **Real multi-line post-FAST run.** The current evidence is two runs of
   one line. Generate the original 3-line worklist with the real reference
   and compare against 316 s / 23.17 s; also measure a varied-length pack
   (5 s, 10 s, 20 s lines).
3. **Long lines (>1024 mel frames).** Bucket or pad to a fixed shape and
   measure; the synthetic bucket result may not transfer. If kept, derive
   the upsample factor from the BigVGAN config instead of a literal 256.
4. **flash-attn / `use_accel` on ROCm.** Build feasibility, sequence lengths,
   and whether the DiT actually uses it. No evidence yet.
5. **DeepSpeed.** Only if flash/compile options fail; dependency and
   compatibility risk is high.
6. **Separate FP16 for s2mel and BigVGAN.** T9 enabled both together; the
   BigVGAN regression under `torch.compile` suggests per-stage gating may
   matter. Measure s2mel-only and BigVGAN-only warm.
7. **MIOpen variants and persistence.** `MIOPEN_FIND_MODE=HYBRID`,
   `MIOPEN_FIND_ENFORCE`, and an explicit `MIOPEN_USER_DB_PATH` were not
   compared. `HOME=/cache/home` is on the cache volume, but cross-container
   persistence of the MIOpen find DB was not validated.
8. **Batching / multi-line GPT or vocoder.** Upstream is single-line in this
   path; no batching was attempted. Serial generation remains the model.
9. **Quality evaluation.** No listening test, ASR/WER, or speaker-similarity
   comparison was run for FAST or for T9-T14. FAST should not change output
   semantics; the fp16/steps/CFG outputs were not compared sample-by-sample.
10. **XTTS-v2 with FAST.** The env is applied, but XTTS was not benchmarked
    in this round.
11. **ONNX Runtime ROCm / other runtimes.** No attempt.
12. **Build-time tuning.** `PYTORCH_ROCM_ARCH=gfx1200` (instead of the
    default multi-arch build), `--use_fast_math` removal, and the CUDA
    extension flags are build-time concerns for the rejected kernel; not
    shipped.

## Reproduce

### Compose configuration

```bash
OMV_MISSES=/tmp/misses.jsonl OMV_REFS=/tmp/refs OMV_SANITIZER_PIN=v1:test \
  docker compose -f compose.yaml config | grep -E 'MIOPEN_FIND_MODE|MIOPEN_LOG_LEVEL'
```

Both services must show `MIOPEN_FIND_MODE: FAST`.

### Full-line benchmark

The measurements used a temporary harness mounted at `/bench/bench.py`
(not committed). The essential container setup:

```bash
docker run --rm --device=/dev/kfd --device=/dev/dri \
  -e PYTHONUNBUFFERED=1 -e PYTHONPATH=/opt/index-tts \
  -e MIOPEN_FIND_MODE=FAST \
  -e HF_HOME=/models/checkpoints/hf_cache -e HF_HUB_OFFLINE=1 \
  -v /tmp/omv-bench:/bench:ro \
  -v /home/lexi/oh-my-voice-refs:/refs:ro \
  -v /home/lexi/Projects/oh-my-voice/models:/models:ro \
  --entrypoint python -w /models oh-my-voice/tts-indextts:0.1.0 \
  /bench/bench.py --mode baseline --out /tmp \
  --text "An OIC officer of arms arrives to the scene and assesses the situation. He shakes his head." \
  --ref /refs/generic-male-01.wav --runs 2
```

Remove `MIOPEN_FIND_MODE=FAST` for the no-FAST baseline. The harness's
`--mode bucket` and the custom-kernel mount were exploratory only.

### BigVGAN-only microbench

The 80-channel input matters; 100 channels fails with
`weight [1536, 80, 7]`:

```python
import torch
from indextts.s2mel.modules.bigvgan import bigvgan as bv

m = bv.BigVGAN.from_pretrained("/models/checkpoints/hf_cache/bigvgan").cuda().eval()
m.remove_weight_norm()
x = torch.randn(1, 80, 500, device="cuda")
for i in range(2):
    y = m(x)
    print(i, y.shape)
```

The first call vs second call is the find/search signal. Run once with
`MIOPEN_FIND_MODE` unset and once with `FAST`.

## Decision log

- Keep `MIOPEN_FIND_MODE=FAST` as the compose default.
- Keep fp32 s2mel/BigVGAN, 25 CFM steps, and CFG 0.7 as defaults. FP16,
  `torch.compile`, steps, and CFG overrides are opt-in generator flags until
  a quality gate passes.
- Do not ship the custom HIP BigVGAN kernel.
- Do not ship `BucketedVocoder` without a real-reference/varied-length win.
- Do not optimize against synthetic references.
- Keep `MIOPEN_LOG_LEVEL=3` only for diagnostics; it is not a speed setting.
- Next performance work is a quality gate + real multi-line measurements,
  plus per-stage FP16 gating.
- Batch only; no live dialogue path until warm latency and quality are both
  acceptable.

## Evidence files

- `compose.yaml`: `MIOPEN_FIND_MODE: "FAST"` on `tts` and `tts-indextts`.
- `README.md`: real-reference numbers and the ~9 GPU-hours / 1,000 8 s line
  estimate.
- `docs/PERFORMANCE.md`: this report.
- Commits: `6e436c3` (speedup/config), `00e4f70` (corrected benchmark/docs).
