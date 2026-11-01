#!/usr/bin/env bash
# Render blueprint references and one cloned test line per voice.
#
#   ./scripts/voicebank.sh gerolt godbert        # two blueprints
#   ./scripts/voicebank.sh                       # every blueprint
#   ./scripts/voicebank.sh --moods neutral nashu_mhakaracca
#
# Each run writes data/samples/voicebank/<utc timestamp>/, which holds the
# VoiceDesign reference WAVs, the cloned test lines, report.json, and an
# index.html audition page. Nothing is installed into a pack and no existing
# data is overwritten: every run gets a new directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
MODELS="${OMV_MODELS:-$ROOT/data/models}"
CACHE="${OMV_CACHE:-$ROOT/data/cache}"
SAMPLES="${OMV_VOICEBANK_OUT:-$ROOT/data/samples/voicebank}"
DESIGN="$MODELS/qwen3-tts/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
BASE="$MODELS/qwen3-tts/Qwen3-TTS-12Hz-1.7B-Base"

usage() {
  cat >&2 <<'EOF'
usage: ./scripts/voicebank.sh [--moods neutral|all|neutral,happy] [--samples N]
                             [--seeds N] [--out DIR] [blueprint ...]

Blueprint ids are file names under blueprints/characters (or explicit paths).
The default is --moods neutral: the identity prompt alone, no emotion.
Pass --moods all to render every mood the blueprint declares.
--seeds N draws N references per archon. With --repeat-seed every draw reuses
the SAME seed, which is a repeatability test; without it each draw advances the
seed, which gives N voice options to choose from.
EOF
}

moods="neutral"
out=""
samples=3
seeds=1
repeat="0"
targets=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --moods) moods="${2:-}"; shift 2 ;;
    --samples) samples="${2:-3}"; shift 2 ;;
    --seeds) seeds="${2:-1}"; shift 2 ;;
    --repeat-seed) repeat=1; shift ;;
    --out) out="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) targets+=("$1"); shift ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
[[ -f "$DESIGN/model.safetensors" ]] || { echo "missing design model: $DESIGN" >&2; exit 1; }
[[ -f "$BASE/model.safetensors" ]] || { echo "missing base model: $BASE" >&2; exit 1; }

if [[ ${#targets[@]} -eq 0 ]]; then
  mapfile -t targets < <(find "$ROOT/blueprints/characters" -maxdepth 1 -name '*.json' -printf '%f\n' | sed 's/\.json$//' | sort)
fi
[[ ${#targets[@]} -gt 0 ]] || { echo "no blueprints found" >&2; exit 1; }

OUT="${out:-$SAMPLES/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT" "$CACHE"
PLANS="$(mktemp -d)"
trap 'rm -rf "$PLANS"' EXIT

for target in "${targets[@]}"; do
  "$PYTHON" "$ROOT/tools/blueprint.py" plan "$target" --out "$PLANS" >/dev/null
done
if [[ "$moods" == "all" ]]; then moods=""; fi
echo "plans: $(find "$PLANS" -name '*.plan.json' | wc -l), moods: ${moods:-all}, samples: $samples, seeds: $seeds"
echo "out:   $OUT"

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
  -v "$PLANS:/plans:ro" \
  -v "$OUT:/out" \
  -v "$ROOT/tools/voicebank:/vb:ro" \
  oh-my-voice/tts-qwen:0.1.0 \
  /vb/run.py \
  --plans /plans \
  --out /out \
  --design-model "/models/qwen3-tts/$(basename "$DESIGN")" \
  --base-model "/models/qwen3-tts/$(basename "$BASE")" \
  ${moods:+--moods "$moods"} \
  --samples "$samples" \
  --seeds "$seeds" \
  $( [ "$repeat" = "1" ] && echo --repeat-seed )

echo "audition: $OUT/index.html"
