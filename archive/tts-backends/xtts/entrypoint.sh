#!/bin/sh
# The /cache bind mount hides the image's /cache contents, so create the
# XTTS/HF/Torch cache roots before any library tries to write to them.
set -eu
mkdir -p "$HOME" "$TTS_HOME" "$HF_HOME" "$TORCH_HOME"
exec python /opt/omv/generate.py "$@"
