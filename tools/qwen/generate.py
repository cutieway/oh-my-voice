#!/usr/bin/env python3
"""oh-my-voice Qwen3-TTS pack generator: events.jsonl + x-vectors -> ogg + manifest.

Usage:
  generate.py --events <events.jsonl> --xvectors <xvectors> --map voice-map.json \
      --out <packdir> --sanitizer-pin <v1:sha> [--lang en] \
      [--qwen3-model <id-or-dir>] [--force] [--merge]

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
Ogg writes are crash-safe: ffmpeg targets <id>.tmp.ogg in the pack directory,
ffprobe validates it, then it is atomically renamed to <id>.ogg. Orphaned
.tmp.wav/.tmp.ogg files are removed before any line is encoded.

Existing .ogg files are reused only when the manifest's per-line
GenerationFingerprint still matches the exact model revision, generation
settings, encoder settings, and that voice's x-vector hash. --force rebuilds
every planned line. A line with a missing x-vector keeps its existing audio
(it cannot be validated or rebuilt) and is skipped when it has none.

The manifest is checkpointed atomically after every completed line. Stopping
the process is safe: rerun the same command to resume, and completed lines
are skipped. A checkpoint is a valid partial pack; do not reload it into the
plugin mid-run unless a partial pack is intentional.

--merge carries lines and metadata from the existing <out>/manifest.json into
a speaker-scoped run. It only carries a line whose .ogg still exists, drops
entries whose audio is already missing, refuses to mix
Language/Sanitizer/Backend/Model/Generation/Encoder settings, and refuses to
lose a line that had audio. This is the safe way to add one speaker to a
multi-speaker pack.

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


GENERATION_SCHEMA = 1
HASH_CHUNK = 1024 * 1024
RETRY_TOP_P = 0.80
MODEL_IGNORED_FILES = {"README.md", ".gitattributes"}
ENCODER_SETTINGS = {
    "Codec": "opus", "Container": "ogg", "Encoder": "libopus",
    "BitrateKbps": 32, "SampleRateHz": 48000, "Channels": 1,
    "Vbr": "constrained", "Application": "voip",
}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_revision(model):
    """Return an exact local model digest, or the cached HF snapshot revision."""
    path = Path(model).expanduser()
    if path.is_dir():
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

    # A repo id has no local weights to hash. If it is already cached, the
    # snapshot directory name is the exact HF commit revision.
    try:
        from huggingface_hub import try_to_load_from_cache
        cached = try_to_load_from_cache(model, "config.json")
        if isinstance(cached, str):
            parts = Path(cached).parts
            if "snapshots" in parts:
                return parts[parts.index("snapshots") + 1]
    except Exception:
        pass
    return "unresolved"


def generation_settings(args):
    return {
        "Attention": args.qwen3_attn,
        "Seed": args.qwen3_seed,
        "NonStreaming": args.qwen3_non_streaming,
        "Device": args.device,
        "DType": "bfloat16",
        "Language": args.lang,
        "MinWps": args.min_wps,
        "MaxAttempts": args.max_attempts,
        "RetryTemperature": args.retry_temperature,
        "RetryTopP": RETRY_TOP_P,
    }


def line_generation_profile(args, model_key, voice_sha):
    return {
        "Schema": GENERATION_SCHEMA,
        "Model": {"Key": model_key},
        "Generation": generation_settings(args),
        "Encoder": ENCODER_SETTINGS,
        "VoiceEmbedding": voice_sha,
    }


def profile_fingerprint(profile):
    canonical = json.dumps(profile, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_previous_manifest(out):
    """Load a previous pack manifest, or None when the pack is new.

    Merge callers treat a corrupt manifest as fatal: silently replacing it
    would drop audio the pack still contains.
    """
    path = out / "manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read previous manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"previous manifest is not an object: {path}")
    return manifest


def load_previous_lines(out):
    try:
        manifest = read_previous_manifest(out)
    except ValueError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return {}
    if manifest is None:
        return {}
    lines = manifest.get("Voicelines")
    return lines if isinstance(lines, dict) else {}


def write_manifest(out, manifest):
    """Atomically checkpoint the manifest.

    A long run can be stopped at any time: every completed line is already in
    the manifest, and the next run reuses it. The temp file is replaced, so a
    crash never leaves a half-written manifest.
    """
    target = out / "manifest.json"
    tmp = target.with_name("manifest.json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    tmp.replace(target)


MERGE_KEYS = ("Schema", "Language", "Sanitizer", "Backend", "Model",
              "Generation", "Encoder")


def manifest_merge_error(previous, current):
    for key in MERGE_KEYS:
        if previous.get(key) != current.get(key):
            return (f"{key} differs: previous={previous.get(key)!r}, "
                    f"current={current.get(key)!r}")
    return None


def merge_previous_manifest(manifest, previous, vdir):
    """Carry existing lines and metadata into a speaker-scoped manifest.

    Only lines whose Ogg still exists are carried. Metadata for a carried line
    is refreshed when its byte size changed, so the plugin's size check does
    not reject a line that was re-encoded outside this generator.
    """
    added = 0
    missing = 0
    previous_lines = previous.get("Voicelines")
    if isinstance(previous_lines, dict):
        for vid, entry in previous_lines.items():
            if vid in manifest["Voicelines"]:
                continue
            target = vdir / f"{vid}.ogg"
            if not target.is_file():
                missing += 1
                continue
            entry = dict(entry) if isinstance(entry, dict) else {}
            size = target.stat().st_size
            if entry.get("Bytes") != size:
                entry["Bytes"] = size
                entry["DurationSeconds"] = media_duration(target)
            entry.setdefault("Codec", "opus")
            manifest["Voicelines"][vid] = entry
            added += 1

    for key in ("Voices", "Npcs"):
        previous_map = previous.get(key)
        if isinstance(previous_map, dict):
            merged = dict(previous_map)
            merged.update(manifest[key])
            manifest[key] = merged

    previous_ignored = previous.get("IgnoredSpeakers")
    if isinstance(previous_ignored, list):
        manifest["IgnoredSpeakers"] = list(dict.fromkeys(
            previous_ignored + manifest["IgnoredSpeakers"]))

    previous_embeddings = previous.get("VoiceEmbeddings")
    if isinstance(previous_embeddings, dict):
        merged = dict(previous_embeddings)
        merged.update(manifest["VoiceEmbeddings"])
        manifest["VoiceEmbeddings"] = merged

    return added, missing


def merge_line_loss(previous_count, missing, after_count):
    """Lines with existing audio that disappeared during a merge.

    ``missing`` entries had no audio to begin with, so dropping them repairs
    the manifest instead of losing a voiced line.
    """
    return max(0, previous_count - missing - after_count)


def cached_line_status(previous, target, profile_fp):
    if profile_fp is None:
        return False, "x-vector missing"
    if not previous:
        return False, "generation profile missing"
    if previous.get("GenerationFingerprint") != profile_fp:
        return False, "generation profile changed"
    try:
        size = target.stat().st_size
    except OSError:
        return False, "cached file missing"
    if previous.get("Bytes") is not None and previous["Bytes"] != size:
        return False, "cached file size changed"
    return True, "generation profile matches"


def line_entry(p, target, generation_fp):
    return {
        "Lang": p["lang"], "VoiceId": p["voice"], "NpcId": p["npc"],
        "Sentence": p["sentence"], "Bytes": target.stat().st_size,
        "DurationSeconds": media_duration(target), "Codec": "opus",
        "GenerationFingerprint": generation_fp,
    }


def remove_orphan_temps(vdir):
    """Delete temporary WAV/Ogg files left by an interrupted earlier run."""
    removed = 0
    for pattern in ("*.tmp.wav", "*.tmp.ogg"):
        for path in vdir.glob(pattern):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def encode_ogg(wav_path, target):
    """Encode to a sibling temp file, validate it, then atomically publish it."""
    tmp = target.with_name(target.stem + ".tmp.ogg")
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav_path),
                        "-c:a", "libopus", "-b:a", "32k", "-vbr", "constrained",
                        "-ar", "48000", "-ac", "1",
                        "-application", "voip", str(tmp)], check=True)
        duration = media_duration(tmp)
        if not duration > 0:
            raise RuntimeError(f"ffprobe reported invalid duration for {tmp}")
        tmp.replace(target)
        return duration
    finally:
        tmp.unlink(missing_ok=True)


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


def auto_max_new_tokens(sentence, min_wps):
    words = max(1, len(sentence.split()))
    seconds = max(4.5, words / min_wps + 1.0)
    # 12 tokens/s codec, 25% headroom, plus a small floor.
    return max(64, min(2048, int(seconds * 12 * 1.25) + 32))


def synthesize_qwen3(tts, args, p, xvector, wav_tmp, retry_kwargs,
                     max_new_tokens):
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
        # The upstream default is 2048 tokens. A single stuck/looping line
        # can then spend many minutes decoding. Cap by the same duration
        # budget the retry loop already uses (Qwen3-TTS codec is 12 Hz).
        "max_new_tokens": max_new_tokens,
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
    ap.add_argument("--force", action="store_true",
                    help="regenerate every planned line even when its cached generation profile matches")
    ap.add_argument("--merge", action="store_true",
                    help="carry existing lines/metadata from --out/manifest.json into "
                         "this speaker-scoped run; refuses to lose lines or mix pack settings")
    ap.add_argument("--min-wps", type=float, default=2.2,
                    help="Retry audio slower than this words/sec (default 2.2).")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="Synthesis attempts before leaving a line as a miss.")
    ap.add_argument("--retry-temperature", type=float, default=0.55,
                    help="Sampling temperature for retry attempts.")
    ap.add_argument("--max-new-tokens", type=int, default=0,
                    help="Hard cap per attempt; 0 derives a cap from the sentence's "
                         "duration limit (12 Hz codec).")
    args = ap.parse_args()

    if args.min_wps <= 0:
        ap.error("--min-wps must be > 0")
    if args.max_attempts < 1:
        ap.error("--max-attempts must be >= 1")
    if args.max_new_tokens < 0:
        ap.error("--max-new-tokens must be >= 0")

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
                     "speaker": cleaned_speaker, "sentence": sentence,
                     "lang": args.lang})

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

    previous_manifest = None
    if args.merge:
        try:
            previous_manifest = read_previous_manifest(out)
        except ValueError as exc:
            ap.error(str(exc))
        if previous_manifest is None:
            previous_lines = {}
        else:
            previous_lines = previous_manifest.get("Voicelines")
            if not isinstance(previous_lines, dict):
                ap.error("previous manifest has no Voicelines object; refusing to merge")
    else:
        previous_lines = load_previous_lines(out)
    previous_count = len(previous_lines)

    vdir = out / "voicelines"
    vdir.mkdir(parents=True, exist_ok=True)
    orphans = remove_orphan_temps(vdir)
    if orphans:
        print(f"removed {orphans} orphaned temp file(s)", file=sys.stderr)
    model_id = args.qwen3_model
    if plan:
        revision = model_revision(model_id)
        voice_hashes = {}
        for voice in sorted({p["voice"] for p in plan}):
            xvector = xvectors / f"{voice}.npy"
            if xvector.is_file():
                voice_hashes[voice] = file_sha256(xvector)
    else:
        revision = "unresolved"
        voice_hashes = {}
    model_key = revision if revision != "unresolved" else f"unresolved:{model_id}"

    from qwen_tts import Qwen3TTSModel
    import torch
    tts = Qwen3TTSModel.from_pretrained(
        str(Path(args.qwen3_model).expanduser()), device_map=args.device,
        dtype=torch.bfloat16, attn_implementation=args.qwen3_attn)
    if getattr(tts.model, "tts_model_type", None) != "base":
        sys.exit("REFUSING TO RUN: Qwen3-TTS voice cloning needs a "
                 "*-Base model (CustomVoice/VoiceDesign do not clone).")
    if revision == "unresolved":
        # A repo-id model may only become resolvable after from_pretrained()
        # populates the HF cache. Re-check before writing fingerprints.
        resolved = model_revision(model_id)
        if resolved != "unresolved":
            revision = resolved
            model_key = resolved
        else:
            print("warning: model revision unresolved; generation profile uses the model id only",
                  file=sys.stderr)
    synthesize = synthesize_qwen3

    manifest = {"Schema": 1, "Language": args.lang, "Sanitizer": args.sanitizer_pin,
                "Backend": "qwen3tts",
                "Model": {"Id": model_id, "Revision": revision},
                "Generation": generation_settings(args),
                "VoiceEmbeddings": voice_hashes,
                "Encoder": dict(ENCODER_SETTINGS),
                "Voices": {}, "Npcs": {}, "IgnoredSpeakers": [], "Voicelines": {}}
    for voice in {p["voice"] for p in plan}:
        manifest["Voices"][voice] = {"IsGeneric": voice.startswith("generic-")}
    for p in plan:
        # PackStore.ResolveNpc looks up Npcs by the runtime-cleaned speaker
        # name, not by the numeric/string NpcId used in the line hash. Keying
        # by p["npc"] here silently missed every line when NpcId was numeric.
        npc_lookup = p["speaker"] or p["npc"]
        manifest["Npcs"].setdefault(npc_lookup, {"NpcId": p["npc"], "VoiceId": p["voice"]})

    missing = 0
    if previous_manifest is not None:
        merge_error = manifest_merge_error(previous_manifest, manifest)
        if merge_error:
            sys.exit(f"REFUSING TO MERGE: {merge_error}. "
                     "Generate into a fresh --out instead.")
        # Merge before generation so every checkpoint already contains the
        # untouched speakers. An interrupted run can then be resumed safely.
        added, missing = merge_previous_manifest(manifest, previous_manifest, vdir)
        if missing:
            print(f"warning: dropping {missing} existing line(s) whose audio "
                  f"is already missing", file=sys.stderr)
        print(f"merged {added} existing line(s)")
        lost = merge_line_loss(previous_count, missing,
                               len(manifest["Voicelines"]))
        if lost:
            sys.exit(f"REFUSING TO WRITE: merged pack would lose {lost} "
                     f"line(s) that had audio ({previous_count} previous, "
                     f"{missing} broken).")
    write_manifest(out, manifest)

    rejected = 0
    for p in plan:
        target = vdir / (p["id"] + ".ogg")
        previous = previous_lines.get(p["id"])
        if not isinstance(previous, dict):
            previous = {}
        voice_sha = voice_hashes.get(p["voice"])
        generation_fp = (
            profile_fingerprint(line_generation_profile(
                args, model_key, voice_sha))
            if voice_sha else None
        )

        if target.exists() and generation_fp is None:
            # The audio exists but the x-vector needed to validate or rebuild
            # it is gone. Preserve the working line rather than dropping it.
            kept_fp = previous.get("GenerationFingerprint") if previous else "unverified"
            print(f"  keep {p['id']}: x-vector missing; not revalidated",
                  file=sys.stderr)
            manifest["Voicelines"][p["id"]] = line_entry(
                p, target, kept_fp or "unverified")
            write_manifest(out, manifest)
            continue

        if target.exists() and not args.force:
            reusable, reason = cached_line_status(previous, target, generation_fp)
            if reusable:
                print(f"  skip {p['id']}: {reason}")
                manifest["Voicelines"][p["id"]] = line_entry(
                    p, target, generation_fp)
                write_manifest(out, manifest)
                continue
            print(f"  rebuild {p['id']}: {reason}", file=sys.stderr)
        elif target.exists():
            print(f"  rebuild {p['id']}: --force")

        xvector = xvectors / (p["voice"] + ".npy")
        if not xvector.exists():
            print(f"  MISSING XVECTOR {xvector}; skipping {p['id']}", file=sys.stderr)
            continue
        wav_tmp = vdir / (p["id"] + ".tmp.wav")
        # 5% grace: punctuation and dramatic pauses routinely land just over
        # the words/sec estimate. The check still catches appended sentences.
        limit = max(4.5, len(p["sentence"].split()) / args.min_wps + 1.0) * 1.05
        max_new_tokens = (args.max_new_tokens
                          or auto_max_new_tokens(p["sentence"], args.min_wps))
        try:
            for attempt in range(1, args.max_attempts + 1):
                retry_kwargs = {} if attempt == 1 else {
                    "temperature": args.retry_temperature, "top_p": RETRY_TOP_P}
                synthesize(tts, args, p, xvector, wav_tmp, retry_kwargs,
                           max_new_tokens)
                dur = media_duration(wav_tmp)
                if dur <= limit:
                    break
                print(f"  retry {p['id']}: {dur:.2f}s > {limit:.2f}s "
                      f"(attempt {attempt})", file=sys.stderr)
            else:
                print(f"  REJECT {p['id']}: {dur:.2f}s > {limit:.2f}s "
                      f"after {args.max_attempts} attempts", file=sys.stderr)
                rejected += 1
                continue
            encoded_duration = encode_ogg(wav_tmp, target)
        finally:
            wav_tmp.unlink(missing_ok=True)
        print(f"  wrote {p['id']} ({encoded_duration:.2f}s)")
        manifest["Voicelines"][p["id"]] = line_entry(
            p, target, generation_fp)
        write_manifest(out, manifest)

    if rejected:
        print(f"{rejected} line(s) rejected for implausible duration; "
              f"they remain in the worklist.", file=sys.stderr)
    write_manifest(out, manifest)
    print(f"pack: {len(manifest['Voicelines'])} lines -> {out}")


if __name__ == "__main__":
    main()
