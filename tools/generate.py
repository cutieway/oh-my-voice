#!/usr/bin/env python3
"""oh-my-voice pack generator (P2): misses.jsonl + refs -> voicelines/*.ogg + manifest.json.

Usage:
  generate.py --misses <misses.jsonl> --refs ~/oh-my-voice-refs \\
      --map voice-map.json --out <packdir> --sanitizer-pin <v1:sha> [--lang en] \\
      [--backend xtts|indextts2|qwen3tts]

Backends:
  indextts2  IndexTTS-2.5, loaded from the ROCm image (default). Pass
             --indextts-model-dir /models/checkpoints.
  qwen3tts   Qwen3-TTS Base (0.6B/1.7B) voice clone, loaded from the
             tts-qwen image. Pass --qwen3-model, or use the compose service.
             A sibling <voiceId>.txt supplies the reference transcript; without
             it, Qwen falls back to x-vector-only cloning (lower quality).
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

All backends occasionally append an invented sentence to otherwise correct
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


def force_fp16_s2mel(torch):
    """Route the s2mel/BigVGAN block through float16 autocast.

    Upstream wraps that block in ``autocast(enabled=False, dtype=None)``,
    which pins it to fp32. Only disabled autocast regions are rewritten, so
    the GPT stage keeps its bf16 autocast.
    """
    real_autocast = torch.amp.autocast

    def autocast(device_type, enabled=True, dtype=None, **kwargs):
        if dtype is None and not enabled:
            return real_autocast(device_type, enabled=True,
                                 dtype=torch.float16, **kwargs)
        return real_autocast(device_type, enabled=enabled, dtype=dtype,
                             **kwargs)

    torch.amp.autocast = autocast


def override_cfm(tts, steps, cfg_rate):
    """Override CFM diffusion steps / inference CFG rate at call time."""
    cfm = tts.s2mel.models["cfm"]
    original = cfm.inference

    def inference(mu, x_lens, prompt, style, f0, n_timesteps,
                  temperature=1.0, inference_cfg_rate=0.5):
        return original(mu, x_lens, prompt, style, f0,
                        steps if steps is not None else n_timesteps,
                        temperature,
                        cfg_rate if cfg_rate is not None else inference_cfg_rate)

    cfm.inference = inference


_QWEN_PROMPTS = {}
_QWEN_LANGS = {
    "en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "de": "German", "fr": "French", "ru": "Russian", "pt": "Portuguese",
    "es": "Spanish", "it": "Italian",
}


def qwen3_ref_text(ref, force_x_vector):
    if force_x_vector:
        return None
    sidecar = ref.with_suffix(".txt")
    if sidecar.is_file():
        text = sidecar.read_text(encoding="utf-8").strip()
        if text:
            return text
    return None


def synthesize_qwen3(tts, args, p, ref, wav_tmp, retry_kwargs):
    import soundfile as sf

    key = (str(ref), args.qwen3_x_vector_only)
    prompt = _QWEN_PROMPTS.get(key)
    if prompt is None:
        ref_text = qwen3_ref_text(ref, args.qwen3_x_vector_only)
        x_vector_only = ref_text is None
        if x_vector_only and not args.qwen3_x_vector_only:
            print(f"  qwen3: no transcript for {ref.name}; "
                  "x-vector-only clone (lower quality)", file=sys.stderr)
        prompt = tts.create_voice_clone_prompt(
            ref_audio=str(ref), ref_text=ref_text,
            x_vector_only_mode=x_vector_only)
        _QWEN_PROMPTS[key] = prompt
    kwargs = {
        "text": p["sentence"],
        "language": _QWEN_LANGS.get(args.lang.lower(), "Auto"),
        "voice_clone_prompt": prompt,
        "non_streaming_mode": args.qwen3_non_streaming,
    }
    kwargs.update(retry_kwargs)
    wavs, sr = tts.generate_voice_clone(**kwargs)
    sf.write(str(wav_tmp), wavs[0], sr)


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
    ap.add_argument("--backend", choices=["xtts", "indextts2", "qwen3tts"], default="indextts2")
    ap.add_argument("--xtts-model", default="tts_models/multilingual/multi-dataset/xtts_v2")
    ap.add_argument("--indextts-model-dir", default="checkpoints",
                    help="Directory containing config.yaml + gpt.pth (IndexTTS-2.5).")
    ap.add_argument("--indextts-cfg", default=None,
                    help="Override config.yaml; defaults to <model-dir>/config.yaml.")
    ap.add_argument("--indextts-bf16", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="Use bfloat16 for the GPT stage (default: true). "
                         "s2mel/BigVGAN stay fp32 unless --indextts-fp16.")
    ap.add_argument("--indextts-fp16", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="Experimental: autocast s2mel/BigVGAN to float16. "
                         "Measured 1.56x warm on RX 9060 XT; quality not "
                         "audited (default: false).")
    ap.add_argument("--indextts-torch-compile", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="Experimental: torch.compile the s2mel DiT estimator. "
                         "The first line pays compilation; later lines are "
                         "faster (default: false).")
    ap.add_argument("--indextts-steps", type=int, default=None,
                    help="Override CFM diffusion steps (model default: 25). "
                         "Fewer steps is faster, lower quality.")
    ap.add_argument("--indextts-cfg-rate", type=float, default=None,
                    help="Override CFM inference CFG rate (model default: 0.7). "
                         "0 halves the DiT batch; lower quality.")
    ap.add_argument("--indextts-emo-vector", type=parse_emo_vector, default=None,
                    help="Optional 8-float emotion vector; default uses reference emotion.")
    ap.add_argument("--indextts-duration-factor", type=float, default=1.0,
                    help="IndexTTS duration factor, 0.5-2.0 (default: 1.0).")
    ap.add_argument("--qwen3-model", default="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    help="Qwen3-TTS model id or local dir (default: 1.7B Base).")
    ap.add_argument("--qwen3-attn", choices=["sdpa", "eager", "flash_attention_2"],
                    default="sdpa",
                    help="Qwen3 attention backend; sdpa works on ROCm.")
    ap.add_argument("--qwen3-x-vector-only", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="Ignore <ref>.txt and clone from the speaker embedding only.")
    ap.add_argument("--qwen3-non-streaming", action=argparse.BooleanOptionalAction,
                    default=False,
                    help="Qwen non-streaming mode (default: upstream streaming simulation).")
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
    if args.indextts_steps is not None and args.indextts_steps < 1:
        ap.error("--indextts-steps must be >= 1")
    if args.indextts_cfg_rate is not None and not 0.0 <= args.indextts_cfg_rate <= 1.0:
        ap.error("--indextts-cfg-rate must be between 0.0 and 1.0")

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
    elif args.backend == "qwen3tts":
        from qwen_tts import Qwen3TTSModel
        import torch
        tts = Qwen3TTSModel.from_pretrained(
            str(Path(args.qwen3_model).expanduser()), device_map=args.device,
            dtype=torch.bfloat16, attn_implementation=args.qwen3_attn)
        synthesize = synthesize_qwen3
    else:
        model_dir = Path(args.indextts_model_dir).expanduser()
        cfg = (Path(args.indextts_cfg).expanduser() if args.indextts_cfg
               else model_dir / "config.yaml")
        if not (model_dir / "config.yaml").is_file():
            sys.exit(f"REFUSING TO RUN: {model_dir / 'config.yaml'} missing; "
                     "download IndexTeam/IndexTTS-2.5 into that directory first.")
        from indextts.infer_v2_5 import IndexTTS2
        import torch
        tts = IndexTTS2(cfg_path=str(cfg), model_dir=str(model_dir),
                        use_bf16=args.indextts_bf16, use_cuda_kernel=False,
                        use_torch_compile=args.indextts_torch_compile,
                        device=args.device)
        if args.indextts_fp16:
            force_fp16_s2mel(torch)
            print("indextts: fp16 autocast on (s2mel/BigVGAN)")
        if args.indextts_steps is not None or args.indextts_cfg_rate is not None:
            override_cfm(tts, args.indextts_steps, args.indextts_cfg_rate)
            print(f"indextts: CFM steps={args.indextts_steps} "
                  f"cfg_rate={args.indextts_cfg_rate}")
        if args.indextts_torch_compile:
            print("indextts: torch.compile on (first line pays compilation)")
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
