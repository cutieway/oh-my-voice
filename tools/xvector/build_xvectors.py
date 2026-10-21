#!/usr/bin/env python3
"""Build one average Qwen3-TTS speaker embedding (x-vector) per speaker_token.

This is not training.  Every WAV under ``<audio-root>/<speaker_token>/`` is
embedded by the frozen Qwen3-TTS Base model; the embeddings are averaged into a
single 2048-float vector.  No speech is generated.

Layout::

    <audio-root>/<speaker_token>/<clip>.wav
    <out>/<speaker_token>.npy
    <out>/<speaker_token>.json
    <out>/manifest.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

import librosa
import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

SAMPLE_RATE = 24_000
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


def load_audio(path: pathlib.Path) -> tuple[np.ndarray, float]:
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration = len(audio) / sr
    if sr != SAMPLE_RATE:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    return np.ascontiguousarray(audio, dtype=np.float32), duration


def speaker_stats(path: pathlib.Path, token: str, model: str,
                  clips: int, errors: int, seconds: float,
                  dim: int, norm: float) -> dict:
    return {
        "Token": token,
        "File": path.name,
        "Clips": clips,
        "Errors": errors,
        "Seconds": round(seconds, 3),
        "Dim": dim,
        "Norm": round(norm, 6),
        "SampleRate": SAMPLE_RATE,
        "Model": model,
    }


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def read_manifest(out_dir: pathlib.Path, model: str) -> dict:
    speakers = {}
    for path in sorted(out_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            stats = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(stats, dict) or "Token" not in stats:
            continue
        if not (out_dir / str(stats.get("File", ""))).is_file():
            continue
        speakers[stats["Token"]] = stats
    return {
        "Schema": 1,
        "Model": model,
        "Dim": 2048,
        "SampleRate": SAMPLE_RATE,
        "Speakers": dict(sorted(speakers.items())),
    }


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
        if not args.force and vector_path.is_file() and stats_path.is_file():
            skipped += 1
            print(f"skip {token}: already built")
            continue
        jobs.append((token, stem, files))

    if jobs:
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
        for index, (token, stem, files) in enumerate(jobs, 1):
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
                vector_path, token, args.model, len(embeddings), errors,
                seconds, int(average.size), float(np.linalg.norm(average)),
            )
            write_json(out_dir / f"{stem}.json", stats)
            print(
                f"[{index}/{len(jobs)}] {token}: {len(embeddings)} clips, "
                f"{seconds / 60:.1f} min audio, dim={average.size}, "
                f"errors={errors}, {time.monotonic() - started:.1f}s total"
            )
    else:
        print("nothing to build")

    manifest = read_manifest(out_dir, args.model)
    write_json(out_dir / "manifest.json", manifest)
    print(f"wrote {out_dir / 'manifest.json'} with {len(manifest['Speakers'])} speakers "
          f"({skipped} skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
