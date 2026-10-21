#!/usr/bin/env bash
# Generate one off-the-cuff voice sample for a speaker token.
#
#   ./sample.sh wuklamat "ahoy there"
#
# Output lives in data/samples/<speaker_token>/:
#   <speaker_token>.json   x-vector metadata copied from data/xvectors/
#   <speaker_token>.npy    x-vector embedding
#   <line-id>.ogg          generated audio (stable md5(lang:voice:npc:sentence))
#   <line-id>.json         manifest entry for that line
#
# This never uses tools/voice.sh and never installs into the live plugin pack.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: ./sample.sh <speaker_token> "<sentence>"

Example:
  ./sample.sh wuklamat "ahoy there"
EOF
}

[[ $# -ge 2 ]] || { usage; exit 2; }
speaker=$1
shift
sentence=$*
[[ -n "$sentence" ]] || { usage; exit 2; }
[[ "$speaker" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
  echo "invalid speaker token: $speaker" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XVECTORS="${OMV_XVECTORS:-$ROOT/data/xvectors}"
MODELS="${OMV_MODELS:-$ROOT/data/models}"
CACHE="${OMV_CACHE:-$ROOT/data/cache}"
SAMPLES="${OMV_SAMPLES:-$ROOT/data/samples}"
PIN="${OMV_SANITIZER_PIN:-v1:$(printf '0%.0s' {1..64})}"

VECTOR="$XVECTORS/$speaker.npy"
METADATA="$XVECTORS/$speaker.json"
[[ -f "$VECTOR" ]] || { echo "missing x-vector: $VECTOR" >&2; exit 1; }
[[ -f "$METADATA" ]] || { echo "missing x-vector metadata: $METADATA" >&2; exit 1; }
[[ "$PIN" =~ ^v1:[0-9a-f]{64}$ ]] || {
  echo "OMV_SANITIZER_PIN must look like v1:<64 hex> (only the format is checked)" >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 not available" >&2; exit 1; }
[[ -d "$MODELS" ]] || { echo "missing models directory: $MODELS" >&2; exit 1; }
# Create bind sources ourselves. If Docker creates a missing bind source it is
# root-owned, and the uid-1000 entrypoint cannot create /cache/home.
mkdir -p "$MODELS" "$CACHE"

OUT="$SAMPLES/$speaker"
mkdir -p "$OUT"

# Pick the canonical NPC id/name from the voice table when possible. The id
# only affects the reproducible line id; the audio itself uses the x-vector.
row="$(python3 - "$ROOT/docs/voices.csv" "$speaker" <<'PY'
import csv
import sys

path, token = sys.argv[1], sys.argv[2]
fallback = (token, token)
try:
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("speaker_token") == token:
                print(row.get("npc_id") or token, row.get("npc_name") or token,
                      sep="\t")
                break
        else:
            print(*fallback, sep="\t")
except OSError:
    print(*fallback, sep="\t")
PY
)" || exit 1
IFS=$'\t' read -r npc_id npc_name <<< "$row"
[[ -n "$npc_id" ]] || npc_id=$speaker
[[ -n "$npc_name" ]] || npc_name=$speaker

TMP="$(mktemp -d "${TMPDIR:-/tmp}/omv-sample.XXXXXX")"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/out"

EVENTS="$TMP/request.jsonl"
VOICE_MAP="$TMP/voice-map.json"
python3 - "$EVENTS" "$VOICE_MAP" "$speaker" "$npc_id" "$npc_name" "$sentence" <<'PY'
import json
import sys

events, voice_map, speaker, npc_id, npc_name, sentence = sys.argv[1:]
with open(events, "w", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "Result": "miss",
        "RawSpeaker": npc_name,
        "Speaker": npc_name,
        "NpcId": npc_id,
        "Sentence": sentence,
        "VoiceId": speaker,
    }, ensure_ascii=False) + "\n")
with open(voice_map, "w", encoding="utf-8") as handle:
    json.dump({npc_id: speaker, speaker: speaker}, handle, ensure_ascii=False)
    handle.write("\n")
PY

export OMV_EVENTS="$EVENTS"
export OMV_VOICE_MAP="$VOICE_MAP"
export OMV_XVECTORS="$XVECTORS"
export OMV_MODELS="$MODELS"
export OMV_CACHE="$CACHE"
export OMV_OUT="$TMP/out"
export OMV_LANG="${OMV_LANG:-en}"
export OMV_SANITIZER_PIN="$PIN"
export OMV_UID="$(id -u)"
export OMV_GID="$(id -g)"

cd "$ROOT"
echo "speaker: $speaker ($npc_name, npc $npc_id)"
echo "output:  $OUT"
echo "Do not use tools/voice.sh for samples; this is isolated."
docker compose build --quiet tts-qwen
docker compose run --rm tts-qwen

audio_id="$(python3 - "$TMP/out/manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
lines = manifest.get("Voicelines") or {}
if len(lines) != 1:
    raise SystemExit(f"expected exactly one generated line, got {len(lines)}")
print(next(iter(lines)))
PY
)" || exit 1

audio_src="$TMP/out/voicelines/$audio_id.ogg"
[[ -f "$audio_src" ]] || { echo "missing generated audio: $audio_src" >&2; exit 1; }

# Re-create in case the output tree was cleaned while generation was running.
mkdir -p "$OUT"
cp -f "$METADATA" "$OUT/$speaker.json"
cp -f "$VECTOR" "$OUT/$speaker.npy"
cp -f "$audio_src" "$OUT/$audio_id.ogg"
python3 - "$TMP/out/manifest.json" "$OUT/$audio_id.json" "$audio_id" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
entry = manifest["Voicelines"][sys.argv[3]]
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(entry, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
PY

printf '\naudio:    %s\n' "$OUT/$audio_id.ogg"
printf 'metadata: %s\n' "$OUT/$speaker.json"
printf 'line:     %s\n' "$OUT/$audio_id.json"
