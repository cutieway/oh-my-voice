#!/usr/bin/env bash
# Q6 enforcement: real-actor reference audio must NEVER be committed.
# Refs live in ~/oh-my-voice-refs/ or a gitignored references/ directory.
set -euo pipefail

fail=0

# 1. No references/ directory may be tracked.
if git ls-files | grep -E '(^|/)references/' ; then
  echo "ERROR: references/ paths are tracked. Real-actor refs must stay out of the repo (Q6)." >&2
  fail=1
fi

# 2. No audio blobs tracked, except pack fixtures under Data/ (ogg only, small).
while IFS= read -r f; do
  case "$f" in
    *.wav|*.flac|*.mp3)
      echo "ERROR: tracked audio blob: $f" >&2
      fail=1
      ;;
    *.ogg)
      case "$f" in
        Data/*) ;;
        *)
          echo "ERROR: tracked .ogg outside Data/: $f" >&2
          fail=1
          ;;
      esac
      ;;
  esac
done < <(git ls-files)

# 3. Size cap: any tracked file over 2 MiB needs justification.
while IFS= read -r f; do
  size=$(wc -c < "$f")
  if [ "$size" -gt 2097152 ]; then
    echo "ERROR: tracked file over 2 MiB: $f (${size} bytes)" >&2
    fail=1
  fi
done < <(git ls-files)

if [ "$fail" -ne 0 ]; then
  echo "check-refs FAILED" >&2
  exit 1
fi
echo "check-refs OK"
