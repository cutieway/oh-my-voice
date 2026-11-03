#!/usr/bin/env bash
# Render matched Identity-only and Identity+Style reference WAVs.
#
#   python3 tools/blueprint_pipeline/cli.py audition npc_1003075 \
#       --out data/blueprints --seeds 3
#   ./scripts/audition.sh \
#       data/blueprints/auditions/npc_1003075/plans \
#       data/blueprints/auditions/npc_1003075
#
# The plans come from `cli.py audition`; both conditions share the production
# plan's reference text, design model and seed schedule. This script renders
# VoiceDesign references only; it does not clone dialogue lines or write a pack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
MODELS="${OMV_MODELS:-$ROOT/data/models}"
CACHE="${OMV_CACHE:-$ROOT/data/cache}"
DESIGN="$MODELS/qwen3-tts/Qwen3-TTS-12Hz-1.7B-VoiceDesign"

usage() {
  cat >&2 <<'EOF'
usage: ./scripts/audition.sh [--seeds N] [--out DIR] PLAN_DIR [OUT_DIR]

PLAN_DIR holds the paired *.plan.json files from `cli.py audition`.
OUT_DIR defaults to PLAN_DIR/..
EOF
}

plans=""
out=""
seeds=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --seeds) seeds="${2:-1}"; shift 2 ;;
    --out) out="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ -z "$plans" ]]; then plans="$1"; elif [[ -z "$out" ]]; then out="$1"; else
        echo "unexpected argument: $1" >&2; usage; exit 2
      fi
      shift ;;
  esac
done
[[ -n "$plans" ]] || { usage; exit 2; }
[[ -d "$plans" ]] || { echo "missing plan directory: $plans" >&2; exit 1; }
plans="$(cd "$plans" && pwd)"
if [[ -z "$out" ]]; then out="$(dirname "$plans")"; fi
mkdir -p "$out"
out="$(cd "$out" && pwd)"
[[ -f "$DESIGN/model.safetensors" ]] || { echo "missing design model: $DESIGN" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
mkdir -p "$CACHE"

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --device /dev/kfd --device /dev/dri \
  --entrypoint python \
  -e HOME=/cache/home \
  -e HF_HOME=/cache/hf \
  -e TORCH_HOME=/cache/torch \
  -e PYTHONUNBUFFERED=1 \
  -e HIP_VISIBLE_DEVICES=0 \
  -e MIOPEN_FIND_MODE=FAST \
  -v "$MODELS:/models:ro" \
  -v "$CACHE:/cache" \
  -v "$plans:/plans:ro" \
  -v "$out:/out" \
  -v "$ROOT/tools/voicebank:/vb:ro" \
  oh-my-voice/tts-qwen:0.1.0 \
  /vb/run.py \
  --plans /plans \
  --out /out \
  --design-model "/models/qwen3-tts/$(basename "$DESIGN")" \
  --base-model "/models/qwen3-tts/Qwen3-TTS-12Hz-1.7B-Base" \
  --samples 1 \
  --seeds "$seeds" \
  --references-only

echo "references: $out/voices"
echo "report:     $out/report.json"
