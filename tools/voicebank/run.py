#!/usr/bin/env python3
"""Render VoiceDesign references and clone sample lines for each take.

Runs inside the tts-qwen image (ROCm torch + qwen-tts). Input is a directory of
plan files produced by `tools/blueprint.py plan`; output is, per voice:

    refs/take.wav                the designed reference (one take per seed)
    lines/take.<n>.wav           one clone of each sample line
    report.json / index.html     prompt, seeds, durations, pitch, audition page

`--seeds N` draws N independent references from the same instruct, so one
blueprint can be auditioned as N different voice options.

Nothing here writes a pack. The instruct is the profile's identity sentence plus
its speech-style clause.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import pathlib
import sys

SAMPLE_TEXTS = [
    "The satchel is on the table by the door. I left it there this morning, before the bells rang.",
    "Have you spoken with the guildmaster? She asked after you twice already.",
    "We should leave before dusk. The eastern road floods when the rains come.",
]


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)


def median_f0(samples, sr, low=60.0, high=400.0):
    """Crude autocorrelation pitch estimate: enough to spot a drifted voice."""
    import numpy as np

    data = np.asarray(samples, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if data.max() > 1.5:
        data = data / 32768.0
    window = int(0.04 * sr)
    hop = int(0.02 * sr)
    values = []
    for start in range(0, len(data) - window, hop):
        frame = data[start:start + window]
        if float(np.sqrt(np.mean(frame ** 2))) < 0.01:
            continue
        frame = frame - frame.mean()
        ac = np.correlate(frame, frame, "full")[window - 1:]
        ac = ac / (ac[0] + 1e-9)
        lo, hi = int(sr / high), int(sr / low)
        segment = ac[lo:hi]
        if not len(segment):
            continue
        lag = lo + int(np.argmax(segment))
        if ac[lag] < 0.3:
            continue
        values.append(sr / lag)
    return round(float(np.median(values)), 1) if values else None


def summarize_profile(profile: dict) -> dict:
    """Flatten a blueprint into what a listener needs to judge the voice."""
    identity = profile.get("identity") or {}
    evidence = profile.get("evidence") or {}
    analysis = (profile.get("speech_style_analysis") or {}).get("fields") or {}
    identity_rows = []
    for field, value in identity.items():
        if value in (None, [], {}):
            continue
        record = evidence.get(field) or {}
        identity_rows.append({
            "field": field,
            "value": value if not isinstance(value, dict) else json.dumps(value, sort_keys=True),
            "evidence": record.get("level"),
            "source": record.get("source"),
        })
    style_rows = []
    for field, value in (profile.get("speech_style") or {}).items():
        row = analysis.get(field) or {}
        probabilities = row.get("probabilities") or {}
        ranked = sorted(probabilities.items(), key=lambda item: -item[1])[:3]
        style_rows.append({
            "field": field,
            "value": value,
            "confidence": row.get("confidence"),
            "method": row.get("method"),
            "top": ranked,
        })
    character = profile.get("character") or {}
    return {
        "character": {key: character.get(key) for key in
                      ("id", "name", "npc_id", "race", "clan", "sex")},
        "identity": identity_rows,
        "style": style_rows,
        "classification": profile.get("classification") or {},
        "style_source": (profile.get("speech_style_analysis") or {}).get("model_id"),
    }


def render_decisions(profile_summary: dict) -> str:
    if not profile_summary:
        return ""
    character = profile_summary["character"]
    facts = " · ".join(
        f"{key}={html.escape(str(value))}" for key, value in character.items()
        if value not in (None, "")
    )
    identity_rows = "".join(
        f'<tr><td>{html.escape(row["field"])}</td><td>{html.escape(str(row["value"]))}</td>'
        f'<td>{html.escape(str(row["evidence"] or ""))}</td>'
        f'<td>{html.escape(str(row["source"] or ""))}</td></tr>'
        for row in profile_summary["identity"]
    )
    style_rows = "".join(
        f'<tr><td>{html.escape(row["field"])}</td><td><b>{html.escape(str(row["value"]))}</b></td>'
        f'<td>{"—" if row["confidence"] is None else format(row["confidence"], ".2f")}</td>'
        f'<td>{html.escape(", ".join(f"{label} {prob:.2f}" for label, prob in row["top"]))}</td></tr>'
        for row in profile_summary["style"]
    )
    classification = profile_summary["classification"]
    return (
        '<details open><summary>decisions</summary>'
        f'<p class="facts">{facts}</p>'
        '<table><caption>acoustic identity</caption>'
        '<tr><th>field</th><th>value</th><th>evidence</th><th>source</th></tr>'
        f'{identity_rows}</table>'
        '<table><caption>speech style (model probabilities)</caption>'
        '<tr><th>field</th><th>value</th><th>confidence</th><th>top labels</th></tr>'
        f'{style_rows}</table>'
        f'<p class="facts">model={html.escape(str(profile_summary["style_source"]))} '
        f'status={html.escape(str(classification.get("status")))} '
        f'review={html.escape(", ".join(classification.get("review_reasons") or []))}</p>'
        '</details>'
    )


def write_audition(out: pathlib.Path, report: dict) -> None:
    blocks = []
    for voice in report["voices"]:
        takes = []
        for take in voice["takes"]:
            clips = "".join(
                f'<audio controls preload="none" src="{html.escape(line["file"])}"></audio>'
                for line in take["lines"]
            )
            f0s = ", ".join(
                f'{line["f0"]:.0f}' for line in take["lines"] if line["f0"]
            )
            takes.append(
                '<div class="take">'
                f'<div class="meta"><b>take {take["take"]}</b>'
                f'<span>seed {take["seed"]}</span>'
                f'<span>ref {take["ref_seconds"]:.1f}s · {take["ref_f0"]} Hz</span>'
                f'<span>line pitch {f0s} Hz</span>'
                f'<span class="sha">sha {html.escape(take["ref_sha256"][:12])}</span></div>'
                f'<div class="clips">{clips}</div></div>'
            )
        blocks.append(
            '<section class="voice">'
            f'<div class="head"><b>{html.escape(voice["profile_id"])}</b>'
            f'<span>{len(voice["takes"])} take(s) · '
            f'{voice["distinct_references"]} distinct reference hash(es)</span></div>'
            + "".join(takes)
            + (f'<div class="verdict">same seed, {len(voice["takes"])} draws: '
               + ("byte-identical" if voice["identical"]
                  else f'{voice["distinct_references"]} different outputs')
               + '</div>' if voice["repeat_seed"] else '')
            + '<details><summary>instruct</summary>'
            f'<p>{html.escape(voice["instruct"])}</p></details>'
            + render_decisions(voice.get("profile"))
            + '</section>'
        )
    (out / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>voicebank audition</title>'
        '<style>body{font:14px system-ui,sans-serif;margin:24px;max-width:1100px}'
        "section{border-top:1px solid rgba(128,128,128,.35);padding:14px 0}"
        ".head{display:flex;gap:10px;align-items:baseline;margin-bottom:8px}"
        ".head span{opacity:.65;font-size:12px}"
        ".take{display:grid;grid-template-columns:180px 1fr;gap:8px;align-items:start;"
        "padding:6px 0}"
        ".meta{display:flex;flex-direction:column;gap:1px}"
        ".meta span{opacity:.7;font-size:12px}"
        ".meta .sha{opacity:.45;font-family:ui-monospace,monospace}"
        "section .verdict{margin:6px 0 0;font-size:12px;opacity:.8}"
        ".clips{display:flex;flex-direction:column;gap:4px}audio{width:100%;height:30px}"
        "details{opacity:.75;margin-top:4px}"
        "table{border-collapse:collapse;margin:6px 0 10px;font-size:12px}"
        "caption{text-align:left;opacity:.6;padding-bottom:2px}"
        "th,td{border-bottom:1px solid rgba(128,128,128,.25);padding:2px 10px 2px 0;"
        "text-align:left;vertical-align:top}"
        ".facts{font-size:12px;opacity:.75;margin:4px 0}"
        "summary{cursor:pointer;font-size:12px}details p{margin:4px 0;font-size:12px}"
        "</style><h1>voicebank audition</h1>"
        f"<p>Reference script: <q>{html.escape(report['reference_text'])}</q><br>"
        f"Sample lines: {' '.join(f'<q>{html.escape(t)}</q>' for t in report['texts'])}</p>"
        + "\n".join(blocks)
        + "\n",
        encoding="utf-8",
    )


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--plans", type=pathlib.Path, required=True)
    p.add_argument("--out", type=pathlib.Path, required=True)
    p.add_argument("--design-model", required=True)
    p.add_argument("--base-model", required=True)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--samples", type=int, default=len(SAMPLE_TEXTS))
    p.add_argument("--texts-file", type=pathlib.Path,
                   help="one sample line per line; defaults to the built-in neutral set")
    p.add_argument("--seeds", type=int, default=1,
                   help="draw N independent references from the same instruct")
    p.add_argument("--seed-step", type=int, default=1000,
                   help="seed offset between draws (ignored with --repeat-seed)")
    p.add_argument("--repeat-seed", action="store_true",
                   help="draw every take with the SAME seed: a determinism/repeatability test")
    p.add_argument("--ref-seconds", default="8,18",
                   help="accept range for a reference clip")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    texts = SAMPLE_TEXTS
    if args.texts_file:
        texts = [
            line.strip()
            for line in args.texts_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not texts:
        print("no sample texts", file=sys.stderr)
        return 2
    texts = texts[: max(1, args.samples)]

    plans = sorted(args.plans.glob("*.plan.json"))
    if not plans:
        print(f"no plans in {args.plans}", file=sys.stderr)
        return 2
    low, high = (float(x) for x in args.ref_seconds.split(","))
    draws = max(1, args.seeds)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    design = Qwen3TTSModel.from_pretrained(
        args.design_model, device_map=args.device, dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    if getattr(design.model, "tts_model_type", None) != "voice_design":
        print(f"not a VoiceDesign model: {getattr(design.model, 'tts_model_type', None)}",
              file=sys.stderr)
        return 2

    report = {
        "design_model": args.design_model,
        "base_model": args.base_model,
        "texts": texts,
        "reference_text": None,
        "voices": [],
    }
    for plan in plans:
        data = json.loads(plan.read_text(encoding="utf-8"))
        report["reference_text"] = data["reference"]["text"]
        voice_dir = out / "voices" / data["profile_id"]
        (voice_dir / "refs").mkdir(parents=True, exist_ok=True)
        (voice_dir / "lines").mkdir(parents=True, exist_ok=True)
        profile_path = plan.with_name(plan.name.replace(".plan.json", ".profile.json"))
        profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.is_file() else None
        entry = {
            "profile_id": data["profile_id"],
            "profile_version": data["profile_version"],
            "profile": summarize_profile(profile) if profile else None,
            "instruct": data["instruct"],
            "seed": data["seed"],
            "takes": [],
        }
        takes = entry["takes"]
        for draw in range(draws):
            seed = data["seed"] if args.repeat_seed else data["seed"] + draw * args.seed_step
            torch.manual_seed(seed)
            wavs, sr = design.generate_voice_design(
                text=data["reference"]["text"],
                language=data["reference"]["language"],
                instruct=data["instruct"],
            )
            name = "take" if draws == 1 else f"take.{draw}"
            path = voice_dir / "refs" / f"{name}.wav"
            sf.write(path, wavs[0], sr)
            seconds = len(wavs[0]) / sr
            flag = "" if low <= seconds <= high else " OUT-OF-RANGE"
            print(f"[design] {data['profile_id']}/{name} {seconds:.2f}s{flag}",
                  flush=True)
            takes.append({
                "take": draw,
                "seed": seed,
                "ref": str(path.relative_to(out)),
                "ref_sha256": file_sha256(path),
                "ref_seconds": round(seconds, 3),
                "ref_in_range": low <= seconds <= high,
                "ref_f0": median_f0(wavs[0], sr),
                "lines": [],
            })
        distinct_refs = len({t["ref_sha256"] for t in takes})
        entry["repeat_seed"] = bool(args.repeat_seed)
        entry["distinct_references"] = distinct_refs
        entry["identical"] = distinct_refs == 1 and len(takes) > 1
        report["voices"].append(entry)
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n",
                                         encoding="utf-8")

    del design
    torch.cuda.empty_cache()

    base = Qwen3TTSModel.from_pretrained(
        args.base_model, device_map=args.device, dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    if getattr(base.model, "tts_model_type", None) != "base":
        print(f"not a Base model: {getattr(base.model, 'tts_model_type', None)}",
              file=sys.stderr)
        return 2

    for voice in report["voices"]:
        voice_dir = out / "voices" / voice["profile_id"]
        many = len(voice["takes"]) > 1
        for take in voice["takes"]:
            prompt = base.create_voice_clone_prompt(
                ref_audio=str(out / take["ref"]),
                ref_text=report["reference_text"],
                x_vector_only_mode=False,
            )
            stem = f"take.{take['take']}" if many else "take"
            for index, text in enumerate(texts):
                torch.manual_seed(take["seed"] + 101 + index)
                wavs, sr = base.generate_voice_clone(
                    text=text, language="English", voice_clone_prompt=prompt,
                    non_streaming_mode=True, max_new_tokens=1024,
                )
                path = voice_dir / "lines" / f"{stem}.{index}.wav"
                sf.write(path, wavs[0], sr)
                take["lines"].append({
                    "text": text,
                    "file": str(path.relative_to(out)),
                    "sha256": file_sha256(path),
                    "seconds": round(len(wavs[0]) / sr, 3),
                    "f0": median_f0(wavs[0], sr),
                })
                print(f"[clone] {voice['profile_id']}/{stem}#{index} "
                      f"{take['lines'][-1]['seconds']:.2f}s", flush=True)
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n",
                                             encoding="utf-8")

    write_audition(out, report)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
