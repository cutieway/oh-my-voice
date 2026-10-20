#!/usr/bin/env python3
"""oh-my-voice pack generator (P2): misses.jsonl + refs -> voicelines/*.ogg + manifest.json.

Usage:
  generate.py --misses <misses.jsonl> --refs ~/oh-my-voice-refs \\
      --map voice-map.json --out <packdir> --sanitizer-pin <v1:sha> [--lang en]

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
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
def voice_id(lang, voice, npc, sentence):
    return hashlib.md5(f"{lang}:{voice}:{npc}:{sentence}".encode("utf-8")).hexdigest()


def ogg_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    return float(out.stdout.strip())


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
    ap.add_argument("--xtts-model", default="xtts_v2")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

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

    from TTS.api import TTS
    tts = TTS(args.xtts_model).to(args.device)

    vdir = out / "voicelines"
    vdir.mkdir(parents=True, exist_ok=True)
    manifest = {"Schema": 1, "Language": args.lang, "Sanitizer": args.sanitizer_pin,
                "Encoder": {"Codec": "opus", "Container": "ogg", "Encoder": "libopus",
                            "BitrateKbps": 32, "SampleRateHz": 48000, "Channels": 1,
                            "Vbr": "constrained", "Application": "voip"},
                "Voices": {}, "Npcs": {}, "IgnoredSpeakers": [], "Voicelines": {}}
    for voice in {p["voice"] for p in plan}:
        manifest["Voices"][voice] = {"IsGeneric": voice.startswith("generic-")}
    for p in plan:
        manifest["Npcs"].setdefault(p["npc"], {"NpcId": p["npc"], "VoiceId": p["voice"]})

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
            tts.tts_to_file(text=p["sentence"], speaker_wav=str(ref),
                            language=args.lang, file_path=str(wav_tmp))
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav_tmp),
                            "-c:a", "libopus", "-b:a", "32k", "-vbr", "constrained",
                            "-ar", "48000", "-ac", "1",
                            "-application", "voip", str(target)], check=True)
            wav_tmp.unlink()
            print(f"  wrote {p['id']}")
        manifest["Voicelines"][p["id"]] = {
            "Lang": args.lang, "VoiceId": p["voice"], "NpcId": p["npc"],
            "Sentence": p["sentence"], "Bytes": target.stat().st_size,
            "DurationSeconds": ogg_duration(target), "Codec": "opus"}

    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    print(f"pack: {len(manifest['Voicelines'])} lines -> {out}")


if __name__ == "__main__":
    main()
