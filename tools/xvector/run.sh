#!/usr/bin/env bash
# Container entrypoint for tools/xvector/build_xvectors.py.
# Defaults come from OMV_*; explicit flags override them:
#   --audio-root DIR  --out DIR  --model DIR
# Everything else is passed through:
#   docker compose -f compose.xvector.yaml run --rm xvector --speaker sphene --force
set -euo pipefail

# Match compose.xvector.yaml when this script is run directly. MIOpen's
# heuristic tuning made the same 255-clip pass 185 s instead of 37 s.
export MIOPEN_FIND_MODE="${MIOPEN_FIND_MODE:-FAST}"

MODEL_DIR="${OMV_QWEN_MODEL_DIR:-/models/qwen3-tts/Qwen3-TTS-12Hz-1.7B-Base}"
AUDIO_ROOT="${OMV_AUDIO_ROOT:-/data/game-voices/wav}"
OUT="${OMV_XVECTOR_OUT:-/data/xvectors}"

extra=()
while (($#)); do
  case "$1" in
    --audio-root) AUDIO_ROOT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --model) MODEL_DIR="$2"; shift 2 ;;
    *) extra+=("$1"); shift ;;
  esac
done

if [ ! -d "$AUDIO_ROOT" ]; then
  echo "audio root not found: $AUDIO_ROOT" >&2
  echo "run: dotnet run --project tools/xivdata -- --out data/game-voices --extract --by-token" >&2
  exit 1
fi

if [ ! -f "$MODEL_DIR/model.safetensors" ]; then
  REPO="${OMV_QWEN_REPO:-Qwen/$(basename "$MODEL_DIR")}"
  echo "downloading $REPO to $MODEL_DIR"
  mkdir -p "$MODEL_DIR"
  python - "$REPO" "$MODEL_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(sys.argv[1], local_dir=sys.argv[2])
PY
fi

exec python -u /tools/xvector/build_xvectors.py \
  --audio-root "$AUDIO_ROOT" \
  --out "$OUT" \
  --model "$MODEL_DIR" \
  "${extra[@]}"
