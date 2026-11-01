#!/usr/bin/env bash
# Render blueprint references and one cloned test line per voice.
#
#   ./scripts/voicebank.sh gerolt godbert        # two blueprints
#   ./scripts/voicebank.sh                       # every character + archetype
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
                             [--seeds N] [--with-style] [--out DIR] [blueprint ...]

Blueprint ids are file names under blueprints/characters and blueprints/archetypes
(or explicit paths). The default is --moods neutral: identity prompt alone.
Pass --with-style to append the profile's classified speech_style clause to the
identity instruct. Pass --moods all to render every mood the blueprint declares.
--seeds N draws N references per archon. With --repeat-seed every draw reuses
the SAME seed, which is a repeatability test; without it each draw advances the
seed, which gives N voice options to choose from.
EOF
}

moods="neutral"
out=""
explicit_out=0
samples=3
seeds=1
repeat="0"
with_style=0
targets=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --moods) moods="${2:-}"; shift 2 ;;
    --samples) samples="${2:-3}"; shift 2 ;;
    --seeds) seeds="${2:-1}"; shift 2 ;;
    --repeat-seed) repeat=1; shift ;;
    --with-style) with_style=1; shift ;;
    --out) out="${2:-}"; explicit_out=1; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) targets+=("$1"); shift ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
[[ -f "$DESIGN/model.safetensors" ]] || { echo "missing design model: $DESIGN" >&2; exit 1; }
[[ -f "$BASE/model.safetensors" ]] || { echo "missing base model: $BASE" >&2; exit 1; }

if [[ ${#targets[@]} -eq 0 ]]; then
  mapfile -t targets < <(
    for dir in "$ROOT/blueprints/characters" "$ROOT/blueprints/archetypes"; do
      for path in "$dir"/*.json; do
        [[ -f "$path" ]] || continue
        basename "${path%.json}"
      done
    done | sort -u
  )
fi
[[ ${#targets[@]} -gt 0 ]] || { echo "no blueprints found" >&2; exit 1; }

# Compose writes content-addressed profiles, so several files can describe the
# same character. Keep only the newest file per character id, otherwise the
# plan step refuses two different plans for one profile id.
mapfile -t targets < <("$PYTHON" - "${targets[@]}" <<'PYEOF'
import json, os, sys

newest = {}
order = []
for target in sys.argv[1:]:
    try:
        with open(target, encoding="utf-8") as handle:
            profile = json.load(handle)
    except (OSError, ValueError):
        # Not a profile path (archetype name or character name); keep as given.
        if target not in newest:
            newest[target] = (0, target)
            order.append(target)
        continue
    identity = (profile.get("character") or {}).get("id") or target
    stamp = os.path.getmtime(target)
    if identity not in newest or stamp > newest[identity][0]:
        if identity not in newest:
            order.append(identity)
        newest[identity] = (stamp, target)
for key in order:
    print(newest[key][1])
PYEOF
)
[[ ${#targets[@]} -gt 0 ]] || { echo "no blueprints found" >&2; exit 1; }

if [[ "$explicit_out" == "1" ]]; then
  OUT="$out"
  [[ "$OUT" = /* ]] || OUT="$PWD/$OUT"
  [[ ! -e "$OUT" ]] || { echo "refusing to reuse existing output directory: $OUT" >&2; exit 1; }
  mkdir -p "$(dirname "$OUT")"
  mkdir "$OUT"
else
  mkdir -p "$SAMPLES"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  OUT="$SAMPLES/$stamp"
  suffix=1
  while [[ -e "$OUT" ]]; do
    OUT="$SAMPLES/$stamp.$suffix"
    suffix=$((suffix + 1))
  done
  mkdir "$OUT"
fi
mkdir -p "$CACHE"
PLANS="$(mktemp -d)"
trap 'rm -rf "$PLANS"' EXIT

for target in "${targets[@]}"; do
  "$PYTHON" "$ROOT/tools/blueprint.py" plan "$target" --out "$PLANS" \
    $( [ "$with_style" = "1" ] && echo --with-style ) >/dev/null
done
# Ship the profiles next to their plans so the audition page can show the
# decisions behind each voice (identity evidence, style probabilities).
"$PYTHON" - "$PLANS" "${targets[@]}" <<'PYEOF'
import json, pathlib, sys
plans = pathlib.Path(sys.argv[1])
for target in sys.argv[2:]:
    path = pathlib.Path(target)
    if not path.is_file():
        continue
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    profile_id = (profile.get("character") or {}).get("id")
    if not profile_id:
        continue
    destination = plans / f"{profile_id}.profile.json"
    destination.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PYEOF
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
