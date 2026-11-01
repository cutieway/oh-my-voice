#!/usr/bin/env python3
"""Validate voice blueprints and render Qwen3-TTS VoiceDesign instructs.

A blueprint is a closed-vocabulary selection, not free prose: every value must
come from blueprints/vocabulary.json, every non-null field needs an evidence
record, and the rendered identity prompt may not contain emotion words.

    python3 tools/blueprint.py check                 # every blueprint
    python3 tools/blueprint.py check blueprints/characters/gerolt.json
    python3 tools/blueprint.py render gerolt         # neutral instruct
    python3 tools/blueprint.py render gerolt --moods # every mood instruct
    python3 tools/blueprint.py plan gerolt --out /tmp/plan

`plan` emits the JSON a generator needs: the shared reference script, the
VoiceDesign instruct per mood, and a deterministic seed per voice/mood pair.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_VOCABULARY = ROOT / "blueprints" / "vocabulary.json"
DEFAULT_CHARACTERS = ROOT / "blueprints" / "characters"

REQUIRED_IDENTITY = (
    "presentation",
    "pitch",
    "timbre_primary",
    "articulation",
    "speaking_pace",
    "intonation",
)

FIELD_TO_VOCAB = {
    "presentation": "presentation",
    "perceived_age": "perceived_age",
    "pitch": "pitch",
    "pitch_range": "pitch_range",
    "timbre_primary": "timbre_primary",
    "timbre_secondary": "timbre_secondary",
    "resonance": "resonance",
    "articulation": "articulation",
    "speaking_pace": "speaking_pace",
    "intonation": "intonation",
    "distinctive_traits": "distinctive_traits",
}

EVIDENCE_RULES = {
    "explicit": "states the value directly",
    "supported_inference": "several source statements agree",
    "creative_selection": "design choice, no source claim",
    "default": "project fallback",
    "unknown": "no reliable information",
}

# Values that must be backed by the source, not by taste.
EVIDENCE_SENSITIVE = {
    "pitch": {"very_low", "very_high"},
    "speaking_pace": {"very_slow", "very_fast"},
    "intonation": {"dramatic", "melodic", "monotone"},
    "articulation": {"slurred", "drawling", "staccato", "sharp"},
    "presentation": {"nonhuman", "synthetic"},
}

SOURCED_LEVELS = {"explicit", "supported_inference"}


class BlueprintError(Exception):
    pass


def load_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_vocabulary(path: pathlib.Path = DEFAULT_VOCABULARY) -> dict:
    return load_json(path)


def _values(vocab: dict, field: str) -> dict:
    """The allowed-value map for a vocabulary field, whatever its shape."""
    spec = vocab["identity"][FIELD_TO_VOCAB[field]]
    if field == "timbre_secondary":
        return vocab["identity"][spec["ref"]]["values"]
    return spec["values"]


def _render(vocab: dict, field: str, value: str) -> str:
    return _values(vocab, field)[value]["render"]


def _family(vocab: dict, field: str, value: str):
    entry = _values(vocab, field)[value]
    return entry.get("family")


def article(phrase: str) -> str:
    return "An" if phrase[:1].lower() in "aeiou" else "A"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render_identity(profile: dict, vocab: dict) -> str:
    identity = profile["identity"]
    parts = {
        "age": "",
        "pitch": "",
        "pitch_range": "",
        "timbre": "",
        "timbre_secondary": "",
        "resonance": "",
        "accent": "",
        "traits": "",
    }
    age = identity.get("perceived_age")
    age_phrase = f"{_render(vocab, 'perceived_age', age)} " if age else ""
    presentation = _render(vocab, "presentation", identity["presentation"])

    parts["age"] = age_phrase
    parts["pitch"] = vocab["render"]["identity_parts"]["pitch"].format(
        pitch=_render(vocab, "pitch", identity["pitch"])
    )
    if identity.get("pitch_range"):
        parts["pitch_range"] = vocab["render"]["identity_parts"]["pitch_range"].format(
            pitch_range=_render(vocab, "pitch_range", identity["pitch_range"])
        )
    parts["timbre"] = vocab["render"]["identity_parts"]["timbre"].format(
        timbre=_render(vocab, "timbre_primary", identity["timbre_primary"])
    )
    parts["articulation"] = _render(vocab, "articulation", identity["articulation"])
    parts["intonation"] = _render(vocab, "intonation", identity["intonation"])
    parts["pace"] = _render(vocab, "speaking_pace", identity["speaking_pace"])
    if identity.get("timbre_secondary"):
        parts["timbre_secondary"] = vocab["render"]["identity_parts"][
            "timbre_secondary"
        ].format(timbre_secondary=_render(vocab, "timbre_secondary", identity["timbre_secondary"]))
    if identity.get("resonance"):
        parts["resonance"] = vocab["render"]["identity_parts"]["resonance"].format(
            resonance=_render(vocab, "resonance", identity["resonance"])
        )
    accent = identity.get("accent")
    if accent:
        parts["accent"] = vocab["render"]["identity_parts"]["accent"].format(
            strength=vocab["identity"]["accent"]["strengths"][accent["strength"]]["render"],
            accent=vocab["identity"]["accent"]["profiles"][accent["profile"]]["render"],
        )
    traits = identity.get("distinctive_traits") or []
    if traits:
        rendered = [_render(vocab, "distinctive_traits", t) for t in traits]
        joined = rendered[0] if len(rendered) == 1 else " and ".join(rendered)
        parts["traits"] = vocab["render"]["identity_parts"]["traits"].format(traits=joined)

    return vocab["render"]["identity_template"].format(
        article=article(age_phrase + presentation), presentation=presentation, **parts
    ).strip()


def _mood_settings(profile: dict, vocab: dict, mood_id: str) -> dict:
    preset = vocab["moods"]["presets"][mood_id]
    overrides = (profile.get("moods") or {}).get(mood_id) or {}
    settings = {key: preset[key] for key in vocab["moods"]["override_fields"]}
    settings["phrase"] = preset["phrase"]
    settings.update(overrides)
    return settings


def render_mood(profile: dict, vocab: dict, mood_id: str) -> str:
    settings = _mood_settings(profile, vocab, mood_id)
    moods = vocab["moods"]
    return vocab["render"]["mood_template"].format(
        phrase=settings["phrase"],
        intensity=moods["intensities"][settings["intensity"]]["render"],
        energy=moods["energy"][settings["energy"]],
        projection=moods["projection"][settings["projection"]],
        pace=moods["pace"][settings["pace"]],
        pitch_movement=moods["pitch_movement"][settings["pitch_movement"]],
        articulation=moods["articulation"][settings["articulation"]],
        rhythm=moods["rhythm"][settings["rhythm"]],
    )


def mood_ids(profile: dict) -> list[str]:
    keys = list((profile.get("moods") or {}).keys())
    return keys or ["neutral"]


def pack_archon(profile: dict) -> str:
    """The single archon a pack uses for every line of this character."""
    return profile.get("pack_archon") or "neutral"


def render_instructs(profile: dict, vocab: dict) -> dict:
    identity = render_identity(profile, vocab)
    return {
        mood: identity if mood == "neutral" and not (profile.get("moods") or {}).get(mood)
        else f"{identity} {render_mood(profile, vocab, mood)}"
        for mood in mood_ids(profile)
    }


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def validate_profile(profile: dict, vocab: dict, name: str = "<profile>") -> list[str]:
    errors: list[str] = []

    def err(message: str) -> None:
        errors.append(f"{name}: {message}")

    if profile.get("schema_version") != vocab["schema_version"]:
        err(f"schema_version must be {vocab['schema_version']!r}")
    version = profile.get("profile_version")
    if not isinstance(version, int) or version < 1:
        err("profile_version must be a positive integer")

    character = profile.get("character") or {}
    for field in ("id", "name", "source_url"):
        if not character.get(field):
            err(f"character.{field} is required")
    if character.get("id") and not re.fullmatch(r"[a-z0-9][a-z0-9_]*", character["id"]):
        err("character.id must be lowercase letters, digits, and underscores")

    identity = profile.get("identity") or {}
    for field in REQUIRED_IDENTITY:
        if not identity.get(field):
            err(f"identity.{field} is required")
    for field in identity:
        if field not in FIELD_TO_VOCAB and field != "accent":
            err(f"identity.{field} = {identity[field]!r} is not in the vocabulary")

    # closed vocabulary + cardinality
    for field, vocab_field in FIELD_TO_VOCAB.items():
        value = identity.get(field)
        if value in (None, [], {}):
            if vocab["identity"][vocab_field].get("required") and field in REQUIRED_IDENTITY:
                err(f"identity.{field} may not be empty")
            continue
        allowed = _values(vocab, field)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item not in allowed:
                err(f"identity.{field} = {item!r} is not in the vocabulary")
        if vocab_field in ("timbre_secondary", "resonance", "pitch_range"):
            if len(values) > 1:
                err(f"identity.{field} allows exactly one value")
        if field == "distinctive_traits":
            spec = vocab["identity"]["distinctive_traits"]
            if len(values) > spec["max"]:
                err(f"identity.distinctive_traits allows at most {spec['max']} values")
            if len(set(values)) != len(values):
                err("identity.distinctive_traits contains duplicates")

    accent = identity.get("accent")
    if accent is not None:
        if set(accent) != {"profile", "strength"}:
            err("identity.accent needs exactly profile and strength")
        else:
            if accent["profile"] not in vocab["identity"]["accent"]["profiles"]:
                err(f"identity.accent.profile = {accent['profile']!r} is not in the vocabulary")
            if accent["strength"] not in vocab["identity"]["accent"]["strengths"]:
                err(f"identity.accent.strength = {accent['strength']!r} is not in the vocabulary")

    # acoustic-family conflicts
    primary = identity.get("timbre_primary")
    secondary = identity.get("timbre_secondary")
    if primary and secondary:
        if primary == secondary:
            err("timbre_secondary repeats timbre_primary")
        elif _family(vocab, "timbre_primary", primary) == _family(
            vocab, "timbre_primary", secondary
        ):
            err(
                f"timbre_secondary {secondary!r} shares the "
                f"{_family(vocab, 'timbre_primary', primary)!r} family with "
                f"timbre_primary {primary!r}; pick a different family"
            )
    trait_families = set()
    for trait in identity.get("distinctive_traits") or []:
        family = _family(vocab, "distinctive_traits", trait)
        if family in trait_families:
            err(f"distinctive_traits {trait!r} repeats the {family!r} family")
        trait_families.add(family)
        if primary and family == _family(vocab, "timbre_primary", primary):
            err(
                f"distinctive_traits {trait!r} repeats the timbre family "
                f"{family!r} already set by {primary!r}"
            )

    # evidence
    evidence = profile.get("evidence") or {}
    for field, level_spec in evidence.items():
        level = (level_spec or {}).get("level")
        if level not in vocab["evidence"]["values"]:
            err(f"evidence.{field}.level = {level!r} is not valid")
            continue
        if level in SOURCED_LEVELS:
            if not level_spec.get("source"):
                err(f"evidence.{field} level {level!r} needs a source")
            if not level_spec.get("quote"):
                err(f"evidence.{field} level {level!r} needs a quote")
    for field in FIELD_TO_VOCAB:
        value = identity.get(field)
        if value in (None, []):
            continue
        if field not in evidence:
            err(f"evidence.{field} is missing")
    if accent is not None and "accent" not in evidence:
        err("evidence.accent is missing")

    for field, values in EVIDENCE_SENSITIVE.items():
        current = identity.get(field)
        if current in values:
            level = (evidence.get(field) or {}).get("level")
            if level not in SOURCED_LEVELS:
                err(
                    f"identity.{field} = {current!r} needs explicit or "
                    f"supported_inference evidence, got {level!r}"
                )

    # moods
    allowed_moods = set(vocab["moods"]["allowed_presets"])
    archon = profile.get("pack_archon")
    if archon is not None:
        declared = set(profile.get("moods") or {}) | {"neutral"}
        if archon not in allowed_moods:
            err(f"pack_archon {archon!r} is not a preset")
        elif archon not in declared:
            err(f"pack_archon {archon!r} has no archon declared in moods")
    for mood, overrides in (profile.get("moods") or {}).items():
        if mood not in allowed_moods:
            err(f"moods.{mood!r} is not a preset; allowed: {sorted(allowed_moods)}")
            continue
        overrides = overrides or {}
        unknown = set(overrides) - set(vocab["moods"]["override_fields"])
        if unknown:
            err(f"moods.{mood} has unknown overrides {sorted(unknown)}")
        if len(overrides) > 2:
            err(f"moods.{mood} overrides {len(overrides)} fields; at most 2 are allowed")
        for key, value in overrides.items():
            table = vocab["moods"].get(key)
            if isinstance(table, dict) and value not in table:
                err(f"moods.{mood}.{key} = {value!r} is not in the vocabulary")

    # rendered prompt hygiene: unknown values make this raise
    if not errors:
        prompt = render_identity(profile, vocab)
        banned = vocab["render"]["banned_identity_words"]
        tokens = re.findall(r"[a-z]+", prompt.lower())
        hits = sorted({t for t in tokens if t in banned})
        if hits:
            err(f"identity prompt contains emotion words {hits}; move them to a mood")
        low, high = vocab["render"]["identity_word_target"]
        words = len(prompt.split())
        if not low <= words <= high:
            err(f"identity prompt is {words} words; target {low}-{high}")

    return errors


def profile_key(profile: dict) -> tuple:
    identity = profile["identity"]
    accent = identity.get("accent") or {}
    return (
        identity.get("presentation"),
        identity.get("perceived_age"),
        identity.get("pitch"),
        identity.get("pitch_range"),
        identity.get("timbre_primary"),
        identity.get("timbre_secondary"),
        identity.get("resonance"),
        identity.get("articulation"),
        identity.get("speaking_pace"),
        identity.get("intonation"),
        accent.get("profile"),
        accent.get("strength"),
        tuple(identity.get("distinctive_traits") or []),
    )


def find_collisions(profiles: list[dict], vocab: dict) -> list[tuple[tuple, list[str]]]:
    groups: dict[tuple, list[str]] = {}
    for profile in profiles:
        groups.setdefault(profile_key(profile), []).append(profile["character"]["id"])
    return [(key, ids) for key, ids in groups.items() if len(ids) > 1]


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def seed_for(voice_id: str, mood: str, base: int = 20261101) -> int:
    digest = hashlib.sha256(f"{voice_id}:{mood}".encode()).hexdigest()
    return base + int(digest[:8], 16) % 1_000_000


def build_plan(profile: dict, vocab: dict) -> dict:
    voice_id = profile["character"]["id"]
    instructs = render_instructs(profile, vocab)
    reference = vocab["reference"]
    return {
        "profile_id": voice_id,
        "profile_version": profile["profile_version"],
        "reference": {
            "script_id": reference["script_id"],
            "language": reference["language"],
            "text": reference["text"],
            "target_seconds": reference["target_seconds"],
        },
        "design_model": vocab["models"]["design"],
        "clone_model": vocab["models"]["clone"],
        "pack_archon": pack_archon(profile),
        "variants": {
            mood: {"instruct": instruct, "seed": seed_for(voice_id, mood)}
            for mood, instruct in instructs.items()
        },
    }


def resolve_profile(target: str) -> pathlib.Path:
    path = pathlib.Path(target)
    if path.is_file():
        return path
    candidate = DEFAULT_CHARACTERS / f"{target}.json"
    if candidate.is_file():
        return candidate
    raise BlueprintError(f"no blueprint named {target!r} ({candidate} does not exist)")


def load_profiles(targets: list[str]) -> list[pathlib.Path]:
    if targets:
        return [resolve_profile(target) for target in targets]
    return sorted(DEFAULT_CHARACTERS.glob("*.json"))


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def cmd_check(args) -> int:
    vocab = load_vocabulary(args.vocabulary)
    paths = load_profiles(args.targets)
    if not paths:
        print(f"no blueprints in {DEFAULT_CHARACTERS}; nothing to check")
        return 0
    profiles = []
    failures = 0
    for path in paths:
        try:
            profile = load_json(path)
        except json.JSONDecodeError as exc:
            print(f"{path}: invalid JSON: {exc}", file=sys.stderr)
            failures += 1
            continue
        errors = validate_profile(profile, vocab, path.stem)
        if path.stem != profile.get("character", {}).get("id"):
            errors.append(f"{path.stem}: file name does not match character.id")
        profiles.append(profile)
        if errors:
            failures += 1
            for error in errors:
                print(error, file=sys.stderr)
        elif args.verbose:
            prompt = render_identity(profile, vocab)
            print(f"ok {path.stem} ({len(prompt.split())} words)")
    for key, ids in find_collisions(profiles, vocab):
        print(f"warning: identical identity: {', '.join(ids)}", file=sys.stderr)
    if failures:
        print(f"\n{failures}/{len(paths)} blueprint(s) failed", file=sys.stderr)
        return 1
    print(f"ok {len(paths)} blueprint(s)")
    return 0


def cmd_render(args) -> int:
    vocab = load_vocabulary(args.vocabulary)
    profile = load_json(resolve_profile(args.target))
    errors = validate_profile(profile, vocab, profile.get("character", {}).get("id", args.target))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    instructs = render_instructs(profile, vocab)
    if args.json:
        payload = dict(instructs) if args.moods else instructs["neutral"]
        print(json.dumps(payload, indent=2))
        return 0
    if args.moods:
        for mood, text in instructs.items():
            print(f"[{mood}]\n{text}\n")
    else:
        print(instructs["neutral"])
    return 0


def cmd_plan(args) -> int:
    vocab = load_vocabulary(args.vocabulary)
    profile = load_json(resolve_profile(args.target))
    errors = validate_profile(profile, vocab, profile.get("character", {}).get("id", args.target))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    plan = build_plan(profile, vocab)
    text = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{plan['profile_id']}.plan.json"
        path.write_text(text, encoding="utf-8")
        print(path)
    else:
        print(text, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vocabulary", type=pathlib.Path, default=DEFAULT_VOCABULARY)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate blueprints")
    check.add_argument("targets", nargs="*")
    check.set_defaults(func=cmd_check)

    render = sub.add_parser("render", help="render the VoiceDesign instruct")
    render.add_argument("target")
    render.add_argument("--moods", action="store_true")
    render.add_argument("--json", action="store_true")
    render.set_defaults(func=cmd_render)

    plan = sub.add_parser("plan", help="emit the generation plan JSON")
    plan.add_argument("target")
    plan.add_argument("--out", type=pathlib.Path)
    plan.set_defaults(func=cmd_plan)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BlueprintError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
