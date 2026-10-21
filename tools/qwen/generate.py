#!/usr/bin/env python3
"""oh-my-voice Qwen3-TTS pack generator: events.jsonl + x-vectors -> ogg + manifest.

Usage:
  generate.py --events <events.jsonl> --xvectors <xvectors> --map voice-map.json \
      --out <packdir> --sanitizer-pin <v1:sha> [--lang en] \
      [--qwen3-model <id-or-dir>]

The event log contains both cache hits and misses. Only rows with
Result="miss" (or no Result, as in the legacy misses.jsonl) are generated.
Hits are diagnostics, not work.

Qwen3-TTS Base generates from one precomputed average speaker embedding
(x-vector) per voice. This is the supported method. Single-reference
zero-shot cloning and ICL transcripts are not used: they are lower quality and
their code path has been removed.

voice-map.json: { "<NpcId or RawSpeaker>": "<xvector name>" }
  An explicit map entry wins. Without one, a normalized NpcId that matches
  exactly one existing <xvectors>/<token>.npy is used (Alphinaud ->
  alphinaud). If a name or key maps to more than one vector, the row is
  skipped rather than guessing. Rows with no matching vector are skipped and
  stay in the worklist; there is no single-reference or generic default.

The generator uses the plugin-sanitized Sentence/Speaker from each miss row
VERBATIM and only recomputes md5(lang:voice:npc:sentence). It must NEVER
re-sanitize: it lacks the player's real name, so any local clean() would
compute IDs the plugin never requests.

--sanitizer-pin is a hard gate: it must equal the Sanitizer.Pin shown in
/omv Settings. The pin covers the vectors the miss rows were sanitized with;
a mismatch means the rows and this pack would disagree on IDs. Refusing to run.

Encoder pin (pack v1): libopus, 32 kbps, constrained VBR, 48 kHz mono.
Duration is read back from the encoded Ogg (granule), not the source WAV.

The model occasionally appends an invented sentence to otherwise correct
speech. Each synthesized WAV is measured; implausibly long audio is retried
with lower-temperature sampling and, after --max-attempts, left as a miss
rather than being installed.
"""
import argparse
import hashlib
import json
import re
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


_QWEN_PROMPTS = {}
_QWEN_LANGS = {
    "en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "de": "German", "fr": "French", "ru": "Russian", "pt": "Portuguese",
    "es": "Spanish", "it": "Italian",
}


def synthesize_qwen3(tts, args, p, xvector, wav_tmp, retry_kwargs):
    import numpy as np
    import soundfile as sf
    import torch
    from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem

    prompt = _QWEN_PROMPTS.get(str(xvector))
    if prompt is None:
        embedding = np.load(xvector, allow_pickle=False)
        if embedding.ndim != 1 or embedding.size != 2048:
            raise ValueError(f"bad x-vector shape {embedding.shape} in {xvector}")
        item = VoiceClonePromptItem(
            ref_code=None,
            ref_spk_embedding=torch.from_numpy(embedding).float(),
            x_vector_only_mode=True,
            icl_mode=False,
            ref_text=None,
        )
        prompt = [item]
        _QWEN_PROMPTS[str(xvector)] = prompt
    if args.qwen3_seed is not None:
        import random
        random.seed(args.qwen3_seed)
        np.random.seed(args.qwen3_seed)
        torch.manual_seed(args.qwen3_seed)
        torch.cuda.manual_seed_all(args.qwen3_seed)
    kwargs = {
        "text": p["sentence"],
        "language": _QWEN_LANGS.get(args.lang.lower(), "Auto"),
        "voice_clone_prompt": prompt,
        "non_streaming_mode": args.qwen3_non_streaming,
    }
    kwargs.update(retry_kwargs)
    wavs, sr = tts.generate_voice_clone(**kwargs)
    sf.write(str(wav_tmp), wavs[0], sr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True,
                    help="plugin events.jsonl; hit rows are ignored, misses are generated")
    ap.add_argument("--xvectors", required=True,
                    help="directory of <voice>.npy average speaker embeddings")
    ap.add_argument("--map", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--sanitizer-pin", required=True,
                    help="Runtime Sanitizer.Pin from the plugin build; generation refuses to run without it.")
    ap.add_argument("--qwen3-model", default="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    help="Qwen3-TTS Base model id or local dir (default: 1.7B Base).")
    ap.add_argument("--qwen3-attn", choices=["sdpa", "eager", "flash_attention_2"],
                    default="sdpa",
                    help="Qwen3 attention backend; sdpa works on ROCm.")
    ap.add_argument("--qwen3-non-streaming", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="Qwen non-streaming mode (default: upstream streaming simulation).")
    ap.add_argument("--qwen3-seed", type=int, default=None,
                    help="Seed Qwen sampling before each line (default: unseeded).")
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

    xvectors = Path(args.xvectors).expanduser()
    if not xvectors.is_dir():
        ap.error(f"--xvectors directory not found: {xvectors}")
    normalized_vectors = {}
    for vector_path in sorted(xvectors.glob("*.npy")):
        token = re.sub(r"[^a-z0-9]", "", vector_path.stem.lower())
        if token:
            normalized_vectors.setdefault(token, []).append(vector_path.stem)
    out = Path(args.out)
    voice_map = json.loads(Path(args.map).read_text())

    events = Path(args.events).expanduser()
    if not events.is_file():
        legacy = events.with_name("misses.jsonl")
        if events.name == "events.jsonl" and legacy.is_file():
            print(f"event log not found; reading legacy {legacy}", file=sys.stderr)
            events = legacy
        else:
            ap.error(f"--events file not found: {events}")

    all_rows = []
    for line in events.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            all_rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"skip torn line: {line[:80]}", file=sys.stderr)

    rows = [r for r in all_rows
            if str(r.get("Result", "miss")).lower() == "miss"]
    print(f"{len(all_rows)} event rows, {len(rows)} misses")

    if not re.fullmatch(r"v1:[0-9a-f]{64}", args.sanitizer_pin or ""):
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
            # Game tokens are lowercase ASCII (alphinaud, yshtola, wuklamat).
            # Use normalized names only when exactly one vector exists; an
            # ambiguous match must stay a worklist miss, never a wrong speaker.
            candidates = set()
            ambiguous = set()
            for key in (npc_key, cleaned_speaker, raw_speaker):
                token = re.sub(r"[^a-z0-9]", "", key.lower())
                matches = normalized_vectors.get(token, []) if token else []
                if len(matches) == 1:
                    candidates.add(matches[0])
                elif len(matches) > 1:
                    ambiguous.update(matches)
            if ambiguous:
                print(f"  ambiguous x-vector for {npc_key!r}: "
                      f"{', '.join(sorted(ambiguous))}; skipping", file=sys.stderr)
                continue
            if len(candidates) > 1:
                print(f"  ambiguous x-vector for {npc_key!r}: "
                      f"{', '.join(sorted(candidates))}; skipping", file=sys.stderr)
                continue
            if candidates:
                voice = next(iter(candidates))
        if voice is None:
            row_voice = r.get("VoiceId")
            if row_voice and row_voice != "Unknown":
                voice = row_voice
        if voice is None:
            print(f"  no x-vector mapping for {npc_key!r}; skipping", file=sys.stderr)
            continue
        sentence = r.get("Sentence", "")
        if not sentence:
            print(f"  skip row with empty sanitized sentence (raw: {raw_speaker})", file=sys.stderr)
            continue
        vid = voice_id(args.lang, voice, npc_key, sentence)
        plan.append({"id": vid, "voice": voice, "npc": npc_key,
                     "speaker": cleaned_speaker, "sentence": sentence})

    print(f"{len(plan)} planned, {len({p['id'] for p in plan})} unique ids")
    missing = sorted({p["voice"] for p in plan
                      if not (xvectors / f"{p['voice']}.npy").is_file()})
    if missing:
        print(f"{len(missing)} voice(s) lack x-vectors: {', '.join(missing)}",
              file=sys.stderr)
    for p in plan[:10]:
        print(f"  {p['id']} [{p['voice']}/{p['npc']}] {p['sentence'][:70]}")

    if args.dry_run:
        return

    from qwen_tts import Qwen3TTSModel
    import torch
    tts = Qwen3TTSModel.from_pretrained(
        str(Path(args.qwen3_model).expanduser()), device_map=args.device,
        dtype=torch.bfloat16, attn_implementation=args.qwen3_attn)
    if getattr(tts.model, "tts_model_type", None) != "base":
        sys.exit("REFUSING TO RUN: Qwen3-TTS voice cloning needs a "
                 "*-Base model (CustomVoice/VoiceDesign do not clone).")
    synthesize = synthesize_qwen3

    vdir = out / "voicelines"
    vdir.mkdir(parents=True, exist_ok=True)
    manifest = {"Schema": 1, "Language": args.lang, "Sanitizer": args.sanitizer_pin,
                "Backend": "qwen3tts",
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
            xvector = xvectors / (p["voice"] + ".npy")
            if not xvector.exists():
                print(f"  MISSING XVECTOR {xvector}; skipping {p['id']}", file=sys.stderr)
                continue
            wav_tmp = vdir / (p["id"] + ".tmp.wav")
            limit = max(4.5, len(p["sentence"].split()) / args.min_wps + 1.0)
            for attempt in range(1, args.max_attempts + 1):
                retry_kwargs = {} if attempt == 1 else {
                    "temperature": args.retry_temperature, "top_p": 0.80}
                synthesize(tts, args, p, xvector, wav_tmp, retry_kwargs)
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
