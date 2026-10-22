#!/usr/bin/env bash
# Generate every missing line for one speaker, and resume safely.
#
#   ./voice.sh alphinaud          # start or resume all queued lines
#   ./voice.sh -pause alphinaud   # pause the running GPU process
#   ./voice.sh -start alphinaud   # unpause, or start/resume after a stop
#   ./voice.sh -stop alphinaud    # stop (same as Ctrl+C)
#
# While ./voice.sh <speaker> is attached, you can also type -pause, -start,
# and -stop in its console.
#
# Output defaults to data/out/<voice_id>. Set OMV_OUT to merge into another
# pack, OMV_SANITIZER_PIN to skip the pin lookup, OMV_LIMIT=N to export only
# the first N queued lines, or OMV_FORCE=1 to rebuild every planned line.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
DB="${OMV_DB:-$ROOT/data/dialogue.db}"
LANG="${OMV_LANG:-en}"
XVECTORS="${OMV_XVECTORS:-$ROOT/data/xvectors}"
MODELS="${OMV_MODELS:-$ROOT/data/models}"
CACHE="${OMV_CACHE:-$ROOT/data/cache}"
WORKLIST_DIR="$ROOT/data/worklists"

usage() {
  cat >&2 <<'EOF'
usage:
  ./voice.sh <speaker>          start or resume all missing lines
  ./voice.sh -pause <speaker>   pause the running container
  ./voice.sh -start <speaker>   unpause, or resume after a stop
  ./voice.sh -stop <speaker>    stop (same as Ctrl+C)

<speaker> is a voice id, NPC key, or numeric NPC id:
  ./voice.sh alphinaud
  ./voice.sh Alphinaud
  ./voice.sh 1003053
EOF
}

die() { echo "error: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
[[ -f "$DB" ]] || die "dialogue DB not found: $DB (run tools/xivdata first)"

CMD=run
POS=()
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    -pause|pause) CMD=pause ;;
    -start|start) CMD=start ;;
    -stop|stop) CMD=stop ;;
    -*) die "unknown option: $arg" ;;
    *) POS+=("$arg") ;;
  esac
done
[[ ${#POS[@]} -eq 1 ]] || { usage; exit 2; }
SPEAKER="${POS[0]}"

# Resolve an alias to the canonical voice id used by x-vectors and file names.
row="$(python3 - "$DB" "$LANG" "$SPEAKER" <<'PY'
import sqlite3
import sys

db, lang, value = sys.argv[1:]
connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = connection.execute(
    """
    SELECT q.speaker_id, s.name, COUNT(*)
    FROM v_generation_queue q
    JOIN speakers s ON s.voice_id = q.speaker_id
    WHERE q.lang = ?
      AND (
        q.speaker_id = ? COLLATE NOCASE
        OR q.npc_key = ? COLLATE NOCASE
        OR CAST(q.npc_id AS TEXT) = ?
      )
    GROUP BY q.speaker_id
    ORDER BY q.speaker_id
    """,
    (lang, value, value, value),
).fetchall()
if not rows:
    print(f"no queued lines for '{value}' (lang={lang})", file=sys.stderr)
    suggestions = connection.execute(
        """
        SELECT voice_id, name, queued
        FROM v_speaker_workload
        WHERE queued > 0
        ORDER BY queued DESC
        LIMIT 10
        """
    )
    print("  speakers with queued lines:", file=sys.stderr)
    for voice_id, name, queued in suggestions:
        print(f"    {voice_id:<24} {name:<24} {queued}", file=sys.stderr)
    raise SystemExit(1)
if len(rows) > 1:
    print(f"'{value}' matches more than one voice:", file=sys.stderr)
    for voice_id, name, queued in rows:
        print(f"    {voice_id:<24} {name:<24} {queued}", file=sys.stderr)
    raise SystemExit(1)
voice_id, name, queued = rows[0]
print(f"{voice_id}\t{name}\t{queued}")
PY
)" || exit 1
IFS=$'\t' read -r VOICE VOICE_NAME QUEUED <<< "$row"
[[ "$VOICE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] ||
  die "unsafe voice id in DB: $VOICE"
NAME="omv-$VOICE"
STOP_MARKER="$ROOT/data/.omv-${VOICE}.stopping"

state() {
  docker inspect -f '{{.State.Running}} {{.State.Paused}}' "$NAME" 2>/dev/null || true
}

stop_container() {
  local state
  state="$(state)"
  [[ -n "$state" ]] || return 0
  if [[ "$state" == "true true" ]]; then
    docker unpause "$NAME" >/dev/null 2>&1 || true
  fi
  docker stop -t 10 "$NAME" >/dev/null 2>&1 || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}

if [[ "$CMD" == "pause" ]]; then
  case "$(state)" in
    "true true") echo "already paused: $NAME" ;;
    "true false")
      docker pause "$NAME" >/dev/null
      echo "paused: $NAME"
      ;;
    *) die "not running: $NAME" ;;
  esac
  exit 0
fi

if [[ "$CMD" == "stop" ]]; then
  if [[ -n "$(state)" ]]; then
    : > "$STOP_MARKER"
    stop_container
    echo "stopped: $NAME"
  else
    echo "not running: $NAME"
  fi
  echo "resume with: ./voice.sh $VOICE"
  exit 0
fi

WORKLIST="$WORKLIST_DIR/$VOICE.jsonl"
OUT="${OMV_OUT:-$ROOT/data/out/$VOICE}"
[[ "$OUT" = /* ]] || OUT="$ROOT/$OUT"
[[ "$XVECTORS" = /* ]] || XVECTORS="$ROOT/$XVECTORS"
[[ "$MODELS" = /* ]] || MODELS="$ROOT/$MODELS"
[[ "$CACHE" = /* ]] || CACHE="$ROOT/$CACHE"

LOG_PID=""
on_signal() {
  trap - INT TERM
  echo
  rm -f "$STOP_MARKER"
  if [[ -n "${LOG_PID:-}" ]]; then
    kill "$LOG_PID" 2>/dev/null || true
  fi
  stop_container
  echo "stopped: $NAME"
  echo "resume with: ./voice.sh $VOICE"
  exit 130
}

# A paused or already-running container is attached, not re-exported.
current_state="$(state)"
if [[ "$current_state" == "true true" ]]; then
  docker unpause "$NAME" >/dev/null
  echo "resumed: $NAME"
elif [[ "$current_state" == "true false" ]]; then
  echo "already running: $NAME"
elif [[ -n "$current_state" ]]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
fi

if [[ "$current_state" != "true true" && "$current_state" != "true false" ]]; then
  rm -f "$STOP_MARKER"
  command -v dotnet >/dev/null 2>&1 || die "dotnet not found (needed for xivdata)"
  docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
  [[ -f "$XVECTORS/$VOICE.npy" ]] || {
    echo "missing x-vector: $XVECTORS/$VOICE.npy" >&2
    echo "build it: docker compose -f compose.xvector.yaml run --rm xvector" >&2
    exit 1
  }

  mkdir -p "$WORKLIST_DIR" "$OUT" "$MODELS" "$CACHE"
  export_args=(export --db "$DB" --speaker "$VOICE" --lang "$LANG" --out "$WORKLIST")
  if [[ -n "${OMV_LIMIT:-}" ]]; then
    export_args+=(--limit "$OMV_LIMIT")
  fi
  dotnet run --project "$ROOT/tools/xivdata" -- "${export_args[@]}"
  QUEUED="$(wc -l < "$WORKLIST")"
  QUEUED="${QUEUED//[[:space:]]/}"

  PIN="${OMV_SANITIZER_PIN:-}"
  if [[ -z "$PIN" ]]; then
    PIN="$(dotnet run --project "$ROOT/tools/xivdata" -- pin 2>/dev/null | tail -n 1)"
  fi
  [[ "$PIN" =~ ^v1:[0-9a-f]{64}$ ]] || {
    echo "could not determine Sanitizer.Pin; set OMV_SANITIZER_PIN=v1:<64 hex>" >&2
    exit 1
  }

  export OMV_EVENTS="$WORKLIST"
  export OMV_OUT="$OUT"
  export OMV_XVECTORS="$XVECTORS"
  export OMV_MODELS="$MODELS"
  export OMV_CACHE="$CACHE"
  export OMV_LANG="$LANG"
  export OMV_SANITIZER_PIN="$PIN"
  export OMV_MERGE=1
  export OMV_UID="$(id -u)"
  export OMV_GID="$(id -g)"

  echo "voice:    $VOICE ($VOICE_NAME)"
  echo "worklist: $QUEUED lines"
  echo "out:      $OUT"
  echo "pin:      $PIN"

  docker compose build --quiet tts-qwen
  docker compose run -d -T --name "$NAME" tts-qwen >/dev/null
  echo "started: $NAME"
fi

trap on_signal INT TERM
docker logs -f "$NAME" 2>&1 &
LOG_PID=$!
echo "commands: -pause  -start  -stop   (Ctrl+C stops after the current line)"

while :; do
  s="$(state)"
  [[ "$s" == "true true" || "$s" == "true false" ]] || break
  if [[ -t 0 ]]; then
    if IFS= read -r -t 1 cmd; then
      cmd="${cmd//$'\r'/}"
      case "$cmd" in
        -pause|pause)
          s="$(state)"
          if [[ "$s" == "true true" ]]; then
            echo "already paused"
          elif [[ "$s" == "true false" ]]; then
            docker pause "$NAME" >/dev/null
            echo "paused; type -start to continue"
          fi
          ;;
        -start|start)
          s="$(state)"
          if [[ "$s" == "true true" ]]; then
            docker unpause "$NAME" >/dev/null
            echo "resumed"
          elif [[ "$s" == "true false" ]]; then
            echo "already running"
          else
            echo "container stopped; rerun: ./voice.sh $VOICE"
          fi
          ;;
        -stop|stop) on_signal ;;
        "") ;;
        *) echo "commands: -pause  -start  -stop" ;;
      esac
    fi
  else
    sleep 1
  fi
done

wait "$LOG_PID" 2>/dev/null || true
LOG_PID=""
exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$NAME" 2>/dev/null | tr -d '[:space:]')"
docker rm -f "$NAME" >/dev/null 2>&1 || true
if [[ -f "$STOP_MARKER" ]]; then
  rm -f "$STOP_MARKER"
  echo "container stopped"
  exit 0
fi
if [[ -z "$exit_code" ]]; then
  echo "container stopped"
  exit 0
fi
echo "container exited (code $exit_code)"
if [[ "$exit_code" =~ ^[0-9]+$ && "$exit_code" != "0" ]]; then
  exit "$exit_code"
fi
echo "pack: $OUT"
