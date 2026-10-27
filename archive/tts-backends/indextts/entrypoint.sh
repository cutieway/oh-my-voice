#!/bin/sh
# Download the IndexTTS-2.5 checkpoint once, then run the pack generator.
# The upstream infer_v2_5.py hardcodes HF_HUB_CACHE=./checkpoints/hf_cache at
# import time, so run from the model directory's parent.
set -eu

MODEL_DIR="${OMV_INDEXTTS_MODEL_DIR:-/models/checkpoints}"
export HOME="${HOME:-/cache/home}"
export HF_HOME="${HF_HOME:-/cache/hf}"
export TORCH_HOME="${TORCH_HOME:-/cache/torch}"
mkdir -p "$MODEL_DIR" "$HOME" "$HF_HOME" "$TORCH_HOME"

if [ ! -f "$MODEL_DIR/config.yaml" ]; then
  echo "Downloading IndexTeam/IndexTTS-2.5 to $MODEL_DIR ..."
  python - "$MODEL_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download("IndexTeam/IndexTTS-2.5", local_dir=sys.argv[1])
PY
fi

cd "$(dirname "$MODEL_DIR")"
exec python /opt/omv/generate.py "$@"
