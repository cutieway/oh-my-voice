# Archived TTS backends

These are the XTTS-v2 and IndexTTS-2.5 implementations, kept for reference
only. The active pipeline is Qwen3-TTS Base in `tools/qwen/`.

Nothing here is wired into `compose.yaml`, `./voice.sh`, or CI. The files
retain their original paths and build contexts, so they will not build from
this location without adjusting `COPY` paths and the compose build context.

- `xtts/` — Coqui XTTS-v2 Dockerfile, entrypoint, and Python requirements.
- `indextts/` — IndexTTS-2.5 Dockerfile and model-download entrypoint.
- `generate-multibackend.py` — the old three-backend generator, including
  IndexTTS CFM/FP16/compile overrides and XTTS loading.
- `compose.yaml` — the old three-service compose file.

Model weights, caches, and images for these backends were removed from the
working tree and Docker. Recreate them from these definitions only if a
future comparison requires it.
