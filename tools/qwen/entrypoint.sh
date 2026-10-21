#!/bin/sh
# Download the Qwen3-TTS Base checkpoint once, then run the pack generator.
set -eu

MODEL_DIR="${OMV_QWEN_MODEL_DIR:-/models/qwen3-tts/Qwen3-TTS-12Hz-1.7B-Base}"
export HOME="${HOME:-/cache/home}"
export HF_HOME="${HF_HOME:-/cache/hf}"
export TORCH_HOME="${TORCH_HOME:-/cache/torch}"
mkdir -p "$MODEL_DIR" "$HOME" "$HF_HOME" "$TORCH_HOME"

if [ ! -f "$MODEL_DIR/model.safetensors" ]; then
  REPO="${OMV_QWEN_REPO:-Qwen/$(basename "$MODEL_DIR")}"
  echo "Downloading $REPO to $MODEL_DIR ..."
  python - "$REPO" "$MODEL_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(sys.argv[1], local_dir=sys.argv[2])
PY
fi

# Compose supplies the model via OMV_QWEN_MODEL_DIR. Pass it through unless
# the caller already gave an explicit --qwen3-model.
has_model=0
for arg in "$@"; do
  [ "$arg" = "--qwen3-model" ] && has_model=1
done
if [ "$has_model" -eq 0 ]; then
  set -- --qwen3-model "$MODEL_DIR" "$@"
fi

if [ "${OMV_FORCE:-0}" = "1" ]; then
  set -- --force "$@"
fi

exec python /opt/omv/generate.py "$@"
