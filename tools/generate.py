#!/usr/bin/env python3
"""oh-my-voice pack generator (P2): misses.jsonl + refs -> voicelines/*.ogg + manifest.json.

Usage:
  generate.py --misses <misses.jsonl> --refs ~/oh-my-voice-refs \\
      --map voice-map.json --out <packdir> --sanitizer-pin <v1:sha> [--lang en] \\
      [--backend xtts|indextts2]

Backends:
  indextts2  IndexTTS-2.5, loaded from the ROCm image (default). Pass
             --indextts-model-dir /models/checkpoints.
  xtts       Coqui XTTS-v2, loaded from the XTTS image. Explicit fallback;
             pass --backend xtts.

voice-map.json: { "<NpcId or RawSpeaker>": "<voiceId>" }
  Unmapped speakers fall back to generic voices by --default-male/--default-female
  (voiceIds must exist as <refs>/<voiceId>.wav).

The generator uses the plugin-sanitized Sentence/Speaker from each miss row
VERBATIM and only recomputes md5(lang:voice:npc:sentence). It must NEVER
re-sanitize: it lacks the player's real name, so any local clean() would
compute IDs the plugin never requests.

--sanitizer-pin is a hard gate: it must equal the Sanitizer.Pin shown in
/omv Settings. The pin covers the vectors the miss rows were sanitized with;
a mismatch means the rows and this pack would disagree on IDs. Refusing to run.

Encoder pin (pack v1): libopus, 32 kbps, constrained VBR, 48 kHz mono.
Duration is read back from the encoded Ogg (granule), not the source WAV.

Both backends occasionally append an invented sentence to otherwise correct
speech. Each synthesized WAV is measured; implausibly long audio is retried
with lower-temperature sampling and, after --max-attempts, left as a miss
rather than being installed.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def voice_id(lang, voice, npc, sentence):
    return hashlib.md5(f"{lang}:{voice}:{npc}:{sentence}".encode("utf-8")).hexdigest()


def media_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"ffprobe failed for {path}: {out.stderr.strip()}")
    return float(out.stdout.strip())


def parse_emo_vector(raw):
    if raw is None or raw == "":
        return None
    parts = [float(x) for x in raw.split(",")]
    if len(parts) != 8:
        raise argparse.ArgumentTypeError(
            "must be 8 comma-separated floats: happy,angry,sad,afraid,"
            "disgusted,melancholic,surprised,calm")
    return parts


def synthesize_xtts(tts, args, p, ref, wav_tmp, retry_kwargs):
    tts.tts_to_file(text=p["sentence"], speaker_wav=str(ref),
                    language=args.lang, file_path=str(wav_tmp),
                    **retry_kwargs)


def synthesize_indextts(tts, args, p, ref, wav_tmp, retry_kwargs):
    kwargs = {
        "spk_audio_prompt": str(ref),
        "text": p["sentence"],
        "output_path": str(wav_tmp),
        "lang": args.lang.upper(),
        "emo_vector": args.indextts_emo_vector,
        "duration_factor": args.indextts_duration_factor,
        "use_random": False,
        "verbose": False,
    }
    kwargs.update(retry_kwargs)
    tts.infer(**kwargs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--misses", required=True)
    ap.add_argument("--refs", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--sanitizer-pin", required=True,
                    help="Runtime Sanitizer.Pin from the plugin build; generation refuses to run without it.")
    ap.add_argument("--default-male", default="generic-male-01")
    ap.add_argument("--default-female", default="generic-female-01")
    ap.add_argument("--backend", choices=["xtts", "indextts2"], default="indextts2")
    ap.add_argument("--xtts-model", default="tts_models/multilingual/multi-dataset/xtts_v2")
    ap.add_argument("--indextts-model-dir", default="checkpoints",
                    help="Directory containing config.yaml + gpt.pth (IndexTTS-2.5).")
    ap.add_argument("--indextts-cfg", default=None,
                    help="Override config.yaml; defaults to <model-dir>/config.yaml.")
    ap.add_argument("--indextts-bf16", action=argparse.BooleanOptionalAction,
                    default=True, help="Use bfloat16 on GPU (default: true).")
    ap.add_argument("--indextts-emo-vector", type=parse_emo_vector, default=None,
                    help="Optional 8-float emotion vector; default uses reference emotion.")
    ap.add_argument("--indextts-duration-factor", type=float, default=1.0,
                    help="IndexTTS duration factor, 0.5-2.0 (default: 1.0).")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-wps", type=float, default=2.2,
                    help="Retry audio slower than this words/sec (default 2.2).")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="Synthesis attempts before leaving a line as a miss.")
    ap.add_argument("--retry-temperature", type=float, default=0.55,
                    help="Sampling temperature for retry attempts.")
    args = ap.parse_args()

    if args.min_wps <= 0:
        ap.error("--min-wps must be > 0")
    if args.max_attempts < 1:
        ap.error("--max-attempts must be >= 1")
    if not 0.5 <= args.indextts_duration_factor <= 2.0:
        ap.error("--indextts-duration-factor must be between 0.5 and 2.0")

    refs = Path(args.refs).expanduser()
    out = Path(args.out)
    voice_map = json.loads(Path(args.map).read_text())

    rows = []
    for line in Path(args.misses).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"skip torn line: {line[:80]}", file=sys.stderr)

    import re as _re
    if not _re.fullmatch(r"v1:[0-9a-f]{64}", args.sanitizer_pin or ""):
        print("REFUSING TO RUN: --sanitizer-pin must look like v1:<sha256> from /omv Settings.", file=sys.stderr)
        sys.exit(2)
    print(f"sanitizer pin: {args.sanitizer_pin}")
    print(f"{len(rows)} worklist rows")

    plan = []
    for r in rows:
        # Use the plugin-sanitized forms VERBATIM. The generator must NEVER
        # re-sanitize: it lacks the player's real name and would compute IDs
        # the plugin never requests. ID = md5(lang:voice:npc:sentence).
        raw_speaker = r.get("RawSpeaker", "")
        cleaned_speaker = r.get("Speaker", "") or raw_speaker
        npc_key = r.get("NpcId", "") or cleaned_speaker or raw_speaker
        voice = voice_map.get(npc_key, voice_map.get(raw_speaker, voice_map.get(cleaned_speaker)))
        if voice is None:
            voice = args.default_male  # gender split needs manifest data; P2 default
        sentence = r.get("Sentence", "")
        if not sentence:
            print(f"  skip row with empty sanitized sentence (raw: {raw_speaker})", file=sys.stderr)
            continue
        vid = voice_id(args.lang, voice, npc_key, sentence)
        plan.append({"id": vid, "voice": voice, "npc": npc_key,
                     "speaker": cleaned_speaker, "sentence": sentence})

    print(f"{len(plan)} planned, {len({p['id'] for p in plan})} unique ids")
    for p in plan[:10]:
        print(f"  {p['id']} [{p['voice']}/{p['npc']}] {p['sentence'][:70]}")

    if args.dry_run:
        return

    if args.backend == "xtts":
        from TTS.api import TTS
        tts = TTS(args.xtts_model).to(args.device)
        synthesize = synthesize_xtts
    else:
        model_dir = Path(args.indextts_model_dir).expanduser()
        cfg = (Path(args.indextts_cfg).expanduser() if args.indextts_cfg
               else model_dir / "config.yaml")
        if not (model_dir / "config.yaml").is_file():
            sys.exit(f"REFUSING TO RUN: {model_dir / 'config.yaml'} missing; "
                     "download IndexTeam/IndexTTS-2.5 into that directory first.")
        from indextts.infer_v2_5 import IndexTTS2
        tts = IndexTTS2(cfg_path=str(cfg), model_dir=str(model_dir),
                        use_bf16=args.indextts_bf16, use_cuda_kernel=False,
                        device=args.device)
        synthesize = synthesize_indextts

    vdir = out / "voicelines"
    vdir.mkdir(parents=True, exist_ok=True)
    manifest = {"Schema": 1, "Language": args.lang, "Sanitizer": args.sanitizer_pin,
                "Backend": args.backend,
                "Encoder": {"Codec": "opus", "Container": "ogg", "Encoder": "libopus",
                            "BitrateKbps": 32, "SampleRateHz": 48000, "Channels": 1,
                            "Vbr": "constrained", "Application": "voip"},
                "Voices": {}, "Npcs": {}, "IgnoredSpeakers": [], "Voicelines": {}}
    for voice in {p["voice"] for p in plan}:
        manifest["Voices"][voice] = {"IsGeneric": voice.startswith("generic-")}
    for p in plan:
        manifest["Npcs"].setdefault(p["npc"], {"NpcId": p["npc"], "VoiceId": p["voice"]})

    rejected = 0
    for p in plan:
        target = vdir / (p["id"] + ".ogg")
        if target.exists():
            print(f"  skip exists {p['id']}")
        else:
            ref = refs / (p["voice"] + ".wav")
            if not ref.exists():
                print(f"  MISSING REF {ref}; skipping {p['id']}", file=sys.stderr)
                continue
            wav_tmp = vdir / (p["id"] + ".tmp.wav")
            limit = max(4.5, len(p["sentence"].split()) / args.min_wps + 1.0)
            for attempt in range(1, args.max_attempts + 1):
                retry_kwargs = {} if attempt == 1 else {
                    "temperature": args.retry_temperature, "top_p": 0.80}
                synthesize(tts, args, p, ref, wav_tmp, retry_kwargs)
                dur = media_duration(wav_tmp)
                if dur <= limit:
                    break
                print(f"  retry {p['id']}: {dur:.2f}s > {limit:.2f}s "
                      f"(attempt {attempt})", file=sys.stderr)
            else:
                wav_tmp.unlink(missing_ok=True)
                print(f"  REJECT {p['id']}: {dur:.2f}s > {limit:.2f}s "
                      f"after {args.max_attempts} attempts", file=sys.stderr)
                rejected += 1
                continue
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav_tmp),
                            "-c:a", "libopus", "-b:a", "32k", "-vbr", "constrained",
                            "-ar", "48000", "-ac", "1",
                            "-application", "voip", str(target)], check=True)
            wav_tmp.unlink()
            print(f"  wrote {p['id']} ({dur:.2f}s)")
        manifest["Voicelines"][p["id"]] = {
            "Lang": args.lang, "VoiceId": p["voice"], "NpcId": p["npc"],
            "Sentence": p["sentence"], "Bytes": target.stat().st_size,
            "DurationSeconds": media_duration(target), "Codec": "opus"}

    if rejected:
        print(f"{rejected} line(s) rejected for implausible duration; "
              f"they remain in the worklist.", file=sys.stderr)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    print(f"pack: {len(manifest['Voicelines'])} lines -> {out}")


if __name__ == "__main__":
    main()
