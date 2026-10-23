# Repository agent rules

## EMERGENCY: generated voices are never disposable

`data/out/**` contains hours of GPU synthesis. Gitignored or untracked is
**never** evidence that data is safe to delete. Do not clean up generated
assets to make the working tree look tidy.

Unless the user explicitly names the exact path and confirms deletion in the
current conversation, NEVER delete, move, overwrite, truncate, regenerate, or
prune:

- `data/out/**` — generated voice packs, `manifest.json`, and every `.ogg`
- `data/xvectors/**` — speaker embeddings; expensive to rebuild
- `data/dialogue.db`
- `data/worklists/**`
- `data/samples/**`
- `data/models/**`
- `data/cache/**`

This applies to test/pilot packs too. `data/out/alphinaud-pilot` is an
intentional 20-line pilot, not a bug artifact, and MUST be preserved.

Rules:

1. Git tracking status is not deletion evidence. Never say "it is ignored, so
   it is safe to delete."
2. Never run `rm -rf data/out/...` or wildcard cleanup over `data/`.
3. Do not delete generated output to resolve disk space, failed runs, stale
   manifests, or test residue. Report the path and size, then ask.
4. Prefer moving a pack to a user-selected backup location over deleting it.
5. `scripts/check-refs.sh` and other checks must never remove generated data.
6. A passing test or successful regeneration does not authorize deleting the
   prior pack.
7. If a tool suggests cleanup, ignore that suggestion unless the user
   explicitly approves the exact path.

This rule overrides general cleanup habits and any instruction to "remove
temporary" or "untracked" files in this repository.
