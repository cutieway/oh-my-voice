#!/usr/bin/env bash
# One-command live dev loop: stage the installed pack, generate every missed
# line with Qwen3-TTS, validate it, then install it into the plugin config.
# The running game is untouched until you press /omv -> Reload pack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_CFG="${OMV_PLUGIN_CONFIG:-$HOME/.xlcore/pluginConfigs/OhMyVoice}"
MISSES="${OMV_MISSES:-$PLUGIN_CFG/misses.jsonl}"
REFS="${OMV_REFS:-$ROOT/data/refs}"
MODELS="${OMV_MODELS:-$ROOT/data/models}"
CACHE="${OMV_CACHE:-$ROOT/data/cache}"
PIN="${OMV_SANITIZER_PIN:-}"

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 not available" >&2; exit 1; }
[[ -f "$MISSES" ]] || { echo "missing worklist: $MISSES" >&2; exit 1; }
[[ -d "$REFS" ]] || { echo "missing reference dir: $REFS" >&2; exit 1; }

mkdir -p "$MODELS" "$CACHE"

# The pack manifest carries the sanitizer pin; fall back to an explicit env.
if [[ -z "$PIN" && -f "$PLUGIN_CFG/manifest.json" ]]; then
  PIN="$(python3 - "$PLUGIN_CFG/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["Sanitizer"])
PY
)"
fi
[[ -n "$PIN" ]] || {
  echo "Set OMV_SANITIZER_PIN (shown in /omv Settings) or install a manifest first." >&2
  exit 1
}

ROWS="$(python3 - "$MISSES" <<'PY'
import sys
print(sum(1 for line in open(sys.argv[1], encoding="utf-8") if line.strip()))
PY
)"
[[ "$ROWS" -gt 0 ]] || { echo "worklist is empty: $MISSES" >&2; exit 1; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/omv-pack.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# Preserve already-generated lines: the generator skips existing ids and
# records their on-disk size/duration in the new manifest.
mkdir -p "$TMP/voicelines"
if [[ -d "$PLUGIN_CFG/voicelines" ]]; then
  cp -a "$PLUGIN_CFG/voicelines/." "$TMP/voicelines/"
fi

cd "$ROOT"
export OMV_MISSES="$MISSES" OMV_REFS="$REFS" OMV_SANITIZER_PIN="$PIN"
export OMV_MODELS="$MODELS" OMV_CACHE="$CACHE"
export OMV_OUT="$TMP" OMV_UID="$(id -u)" OMV_GID="$(id -g)"

echo "worklist: $ROWS rows"
echo "backend:  Qwen3-TTS Base"
echo "staging:  $TMP"
echo "Do not click new dialogue until generation finishes."
# Rebuild so the image always contains the current tools/qwen/generate.py; the
# layer cache makes an unchanged run a no-op.
docker compose build tts-qwen
docker compose run --rm tts-qwen

[[ -s "$TMP/manifest.json" ]] || { echo "generator produced no manifest" >&2; exit 1; }

# Refuse to replace a working pack with an empty manifest (e.g. every row
# referenced a missing voice).
python3 - "$TMP/manifest.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
n = len(d.get("Voicelines", {}))
if n == 0:
    sys.exit("refusing to install an empty manifest")
print(f"staged: {n} lines, {len(d.get('Npcs', {}))} NPCs")
PY

# Install files first, manifest last, so the plugin never sees a manifest
# pointing at missing audio. The old manifest is kept as .bak.
mkdir -p "$PLUGIN_CFG/voicelines"
if [[ -f "$PLUGIN_CFG/manifest.json" ]]; then
  cp -f "$PLUGIN_CFG/manifest.json" "$PLUGIN_CFG/manifest.json.bak"
fi
cp -a "$TMP/voicelines/." "$PLUGIN_CFG/voicelines/"
cp -f "$TMP/manifest.json" "$PLUGIN_CFG/manifest.json"

echo "installed: $PLUGIN_CFG"
echo "in game:   /omv -> Reload pack"
