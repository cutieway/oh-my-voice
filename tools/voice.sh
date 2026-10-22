#!/usr/bin/env bash
# One-command live dev loop: stage the installed pack, generate every missed
# line with Qwen3-TTS, validate it, then install it into the plugin config.
# The running game is untouched until you press /omv -> Reload pack.
#
#   ./tools/voice.sh           # reuse lines whose generation profile matches
#   ./tools/voice.sh --force   # regenerate every planned line
set -euo pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
  shift
fi
[[ $# -eq 0 ]] || { echo "usage: $0 [--force]" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_CFG="${OMV_PLUGIN_CONFIG:-$HOME/.xlcore/pluginConfigs/OhMyVoice}"
EVENTS="${OMV_EVENTS:-$PLUGIN_CFG/events.jsonl}"
[[ -f "$EVENTS" || ! -f "$PLUGIN_CFG/misses.jsonl" ]] || EVENTS="$PLUGIN_CFG/misses.jsonl"
XVECTORS="${OMV_XVECTORS:-$ROOT/data/xvectors}"
MODELS="${OMV_MODELS:-$ROOT/data/models}"
CACHE="${OMV_CACHE:-$ROOT/data/cache}"
PIN="${OMV_SANITIZER_PIN:-}"

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 not available" >&2; exit 1; }
[[ -f "$EVENTS" ]] || { echo "missing event log: $EVENTS" >&2; exit 1; }
[[ -d "$XVECTORS" ]] || {
  echo "missing xvector dir: $XVECTORS" >&2
  echo "build it: docker compose -f compose.xvector.yaml run --rm xvector" >&2
  exit 1
}

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

ROWS="$(python3 - "$EVENTS" <<'PY'
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue
    if str(row.get("Result", "miss")).lower() == "miss":
        n += 1
print(n)
PY
)"
[[ "$ROWS" -gt 0 ]] || { echo "no misses in event log: $EVENTS" >&2; exit 1; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/omv-pack.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# Preserve already-generated lines and the manifest that records their
# generation profiles. The generator reuses a line only when its profile
# still matches; old manifests without profiles cause a one-time rebuild.
mkdir -p "$TMP/voicelines"
if [[ -d "$PLUGIN_CFG/voicelines" ]]; then
  cp -a "$PLUGIN_CFG/voicelines/." "$TMP/voicelines/"
fi
if [[ -f "$PLUGIN_CFG/manifest.json" ]]; then
  cp -f "$PLUGIN_CFG/manifest.json" "$TMP/manifest.json"
fi

cd "$ROOT"
export OMV_EVENTS="$EVENTS" OMV_XVECTORS="$XVECTORS" OMV_SANITIZER_PIN="$PIN"
export OMV_MODELS="$MODELS" OMV_CACHE="$CACHE"
export OMV_OUT="$TMP" OMV_UID="$(id -u)" OMV_GID="$(id -g)"

echo "events:   $ROWS misses"
echo "backend:  Qwen3-TTS Base (average x-vectors)"
echo "staging:  $TMP"
echo "Do not click new dialogue until generation finishes."
# Rebuild so the image always contains the current tools/qwen/generate.py; the
# layer cache makes an unchanged run a no-op.
docker compose build tts-qwen
# Always merge: the staged manifest may cover speakers outside this worklist.
if (( FORCE )); then
  docker compose run --rm -e OMV_FORCE=1 -e OMV_MERGE=1 tts-qwen
else
  docker compose run --rm -e OMV_MERGE=1 tts-qwen
fi

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
