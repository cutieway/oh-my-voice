#!/bin/sh
# Download the Qwen3-TTS Base checkpoint once, then run the pack generator.
set -eu

MODEL_DIR="${OMV_QWEN_MODEL_DIR:-/models/qwen3-tts/Qwen3-TTS-12Hz-1.7B-Base}"
export HOME="${HOME:-/cache/home}"
export HF_HOME="${HF_HOME:-/models/hf}"
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

exec python /opt/omv/generate.py "$@"
