# Repository agent rules

## NEVER DELETE FROM `data/`

Agents MUST NEVER delete, truncate, overwrite, or prune ANY content under
`data/`, tracked or untracked. This includes generated voices, xvectors, the
dialogue DB, models, cache, worklists, samples, logs, and backups.

If content is no longer needed in its active location — test output, failed
run, stale pack, bug, or disk pressure — MOVE it to the matching backup path.
Never delete it.

```text
data/<path>  ->  data/backup/<path>
```

Examples:

```text
data/out/foo              -> data/backup/out/foo
data/worklists/foo.jsonl  -> data/backup/worklists/foo.jsonl
data/dialogue.db          -> data/backup/dialogue.db
```

Rules:

1. Create parent directories, then use `mv`; preserve the relative path.
2. Never overwrite an existing backup. If the destination exists, use a unique
   suffix such as `.1` and report it.
3. Never use `rm`, `rmdir`, `find -delete`, truncating redirects, or any other
   deletion against `data/`.
4. Never "clean up" or delete to save space. Move to backup and report sizes.
5. If actual deletion is truly required, the user must do it; agents only move.
6. Report the old and new paths after every move.
