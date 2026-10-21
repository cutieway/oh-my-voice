#!/usr/bin/env python3
"""Build one average Qwen3-TTS speaker embedding (x-vector) per speaker_token.

This is not training.  Every WAV under ``<audio-root>/<speaker_token>/`` is
embedded by the frozen Qwen3-TTS Base model; the embeddings are averaged into a
single 2048-float vector.  No speech is generated.

Existing vectors are reused only when the WAV corpus, model weights, and audio
processing version recorded beside them still match.  Otherwise the speaker is
rebuilt automatically.

Layout::

    <audio-root>/<speaker_token>/<clip>.wav
    <out>/<speaker_token>.npy
    <out>/<speaker_token>.json
    <out>/manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import time

# Heavy runtime dependencies are imported by the functions that need them.
# This keeps fingerprint/cache helpers testable without the GPU stack.

SAMPLE_RATE = 24_000
SCHEMA_VERSION = 2
# Bump when load_audio/resampling or the embedding input changes.  This is part
# of the cache key because identical WAVs can produce different vectors after a
# processing-pipeline change.
PIPELINE_VERSION = 1
HASH_CHUNK = 1024 * 1024
MODEL_IGNORED_FILES = {"README.md", ".gitattributes"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-root", type=pathlib.Path, required=True,
                        help="directory containing <speaker_token>/*.wav")
    parser.add_argument("--out", type=pathlib.Path, required=True,
                        help="output directory for .npy, .json, and manifest.json")
    parser.add_argument("--model", required=True,
                        help="Qwen3-TTS Base model directory")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--speaker", action="append", default=[],
                        help="only this speaker_token (repeatable; default: all)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild vectors that already have .npy + .json")
    return parser.parse_args()


def safe_name(token: str) -> str:
    return SAFE_NAME.sub("_", token).strip("._") or "speaker"


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clip_fingerprint(path: pathlib.Path) -> str:
    # A bad clip should not prevent the rest of a speaker from being cached.
    # If it becomes readable later, this value changes and forces a rebuild.
    try:
        return "sha256:" + file_sha256(path)
    except OSError as exc:
        return f"unreadable:{exc.errno}"


def corpus_fingerprint(files: list[pathlib.Path],
                       speaker_dir: pathlib.Path) -> str:
    digest = hashlib.sha256()
    digest.update(f"pipeline:{PIPELINE_VERSION}\n".encode("utf-8"))
    for path in sorted(files):
        relative = path.relative_to(speaker_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(clip_fingerprint(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def model_fingerprint(model: str) -> str:
    path = pathlib.Path(model).expanduser()
    if not path.is_dir():
        raise SystemExit(f"model directory not found: {path}")
    digest = hashlib.sha256()
    for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
        relative = file_path.relative_to(path)
        if (relative.name in MODEL_IGNORED_FILES
                or any(part.startswith(".") for part in relative.parts)):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(file_path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_speaker_stats(path: pathlib.Path) -> dict | None:
    try:
        stats = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return stats if isinstance(stats, dict) else None


def reuse_status(stats_path: pathlib.Path, vector_path: pathlib.Path,
                 corpus_fp: str, model_fp: str) -> tuple[bool, str]:
    if not vector_path.is_file():
        return False, "vector missing"
    stats = load_speaker_stats(stats_path)
    if stats is None:
        return False, "metadata missing or invalid"
    if stats.get("Schema") != SCHEMA_VERSION:
        return False, "metadata schema changed"
    if stats.get("PipelineVersion") != PIPELINE_VERSION:
        return False, "audio pipeline changed"
    if stats.get("CorpusFingerprint") != corpus_fp:
        if not stats.get("CorpusFingerprint"):
            return False, "corpus fingerprint missing"
        return False, "clips changed"
    if stats.get("ModelFingerprint") != model_fp:
        if not stats.get("ModelFingerprint"):
            return False, "model fingerprint missing"
        return False, "model changed"
    return True, "fingerprints match"


def load_audio(path: pathlib.Path) -> tuple[np.ndarray, float]:
    import librosa
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration = len(audio) / sr
    if sr != SAMPLE_RATE:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    return np.ascontiguousarray(audio, dtype=np.float32), duration


def speaker_stats(path: pathlib.Path, token: str, model: str,
                  model_fp: str, corpus_fp: str, clip_files: int,
                  clips: int, errors: int, seconds: float,
                  dim: int, norm: float) -> dict:
    return {
        "Schema": SCHEMA_VERSION,
        "Token": token,
        "File": path.name,
        "ClipFiles": clip_files,
        "Clips": clips,
        "Errors": errors,
        "Seconds": round(seconds, 3),
        "Dim": dim,
        "Norm": round(norm, 6),
        "SampleRate": SAMPLE_RATE,
        "Model": model,
        "ModelFingerprint": model_fp,
        "CorpusFingerprint": corpus_fp,
        "PipelineVersion": PIPELINE_VERSION,
    }


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def manifest_model_fields(speakers: dict) -> dict:
    models = {}
    for stats in speakers.values():
        name = str(stats.get("Model") or "unknown")
        fingerprint = str(stats.get("ModelFingerprint") or "unknown")
        key = (name, fingerprint)
        models[key] = models.get(key, 0) + 1
    if len(models) == 1:
        (name, fingerprint), _ = next(iter(models.items()))
        return {"Model": name, "ModelFingerprint": fingerprint}
    return {
        "Model": "mixed",
        "Models": [
            {"Name": name, "Fingerprint": fingerprint, "Speakers": count}
            for (name, fingerprint), count in sorted(models.items())
        ],
    }


def read_manifest(out_dir: pathlib.Path) -> dict:
    speakers = {}
    for path in sorted(out_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        stats = load_speaker_stats(path)
        if stats is None or "Token" not in stats:
            continue
        if not (out_dir / str(stats.get("File", ""))).is_file():
            continue
        speakers[stats["Token"]] = stats
    manifest = {
        "Schema": SCHEMA_VERSION,
        "Dim": 2048,
        "SampleRate": SAMPLE_RATE,
        "Speakers": dict(sorted(speakers.items())),
    }
    manifest.update(manifest_model_fields(speakers))
    return manifest


def main() -> int:
    args = parse_args()
    audio_root = args.audio_root.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    if not audio_root.is_dir():
        raise SystemExit(f"audio root not found: {audio_root}")
    out_dir.mkdir(parents=True, exist_ok=True)

    wanted = {s.lower() for s in args.speaker}
    jobs = []
    skipped = 0
    model_fp: str | None = None
    for speaker_dir in sorted(p for p in audio_root.iterdir() if p.is_dir()):
        token = speaker_dir.name
        if wanted and token.lower() not in wanted:
            continue
        files = sorted(speaker_dir.rglob("*.wav"))
        if not files:
            print(f"skip {token}: no .wav files", file=sys.stderr)
            continue
        stem = safe_name(token)
        vector_path = out_dir / f"{stem}.npy"
        stats_path = out_dir / f"{stem}.json"
        corpus_fp = corpus_fingerprint(files, speaker_dir)
        if model_fp is None:
            model_fp = model_fingerprint(args.model)
        if not args.force:
            reusable, reason = reuse_status(
                stats_path, vector_path, corpus_fp, model_fp,
            )
            if reusable:
                skipped += 1
                print(f"skip {token}: {reason}")
                continue
            print(f"rebuild {token}: {reason}", file=sys.stderr)
        jobs.append((token, stem, files, corpus_fp))

    if jobs:
        assert model_fp is not None
        import numpy as np
        import torch
        from qwen_tts import Qwen3TTSModel

        print(f"loading {args.model}")
        tts = Qwen3TTSModel.from_pretrained(
            args.model,
            device_map=args.device,
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
        if getattr(tts.model, "tts_model_type", "base") != "base":
            raise SystemExit("this builder requires a Qwen3-TTS Base model")

        started = time.monotonic()
        for index, (token, stem, files, corpus_fp) in enumerate(jobs, 1):
            embeddings = []
            errors = 0
            seconds = 0.0
            for path in files:
                try:
                    audio, duration = load_audio(path)
                    if audio.size == 0:
                        raise ValueError("empty audio")
                    embedding = tts.model.extract_speaker_embedding(
                        audio=audio, sr=SAMPLE_RATE,
                    )
                    embeddings.append(embedding.detach().float().cpu())
                    seconds += duration
                except Exception as exc:  # keep one bad clip from losing the speaker
                    errors += 1
                    print(f"  error {path.name}: {exc}", file=sys.stderr)

            if not embeddings:
                print(f"skip {token}: no usable clips ({errors} errors)", file=sys.stderr)
                continue

            average = torch.stack(embeddings).mean(dim=0).float().numpy()
            vector_path = out_dir / f"{stem}.npy"
            np.save(vector_path, average, allow_pickle=False)
            stats = speaker_stats(
                vector_path, token, args.model, model_fp, corpus_fp,
                len(files), len(embeddings), errors, seconds,
                int(average.size), float(np.linalg.norm(average)),
            )
            write_json(out_dir / f"{stem}.json", stats)
            print(
                f"[{index}/{len(jobs)}] {token}: {len(embeddings)} clips, "
                f"{seconds / 60:.1f} min audio, dim={average.size}, "
                f"errors={errors}, {time.monotonic() - started:.1f}s total"
            )
    else:
        print("nothing to build")

    manifest = read_manifest(out_dir)
    write_json(out_dir / "manifest.json", manifest)
    print(f"wrote {out_dir / 'manifest.json'} with {len(manifest['Speakers'])} speakers "
          f"({skipped} skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
