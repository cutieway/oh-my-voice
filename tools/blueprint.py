#!/usr/bin/env python3
"""Validate voice blueprints and render Qwen3-TTS VoiceDesign instructs.

A blueprint is a closed-vocabulary selection, not free prose: every value must
come from blueprints/vocabulary.json, every non-null field needs an evidence
record, and the rendered identity prompt may not contain emotion words.

    python3 tools/blueprint.py check                 # every blueprint
    python3 tools/blueprint.py check blueprints/characters/gerolt.json
    python3 tools/blueprint.py render gerolt         # full instruct
    python3 tools/blueprint.py plan gerolt --out /tmp/plan

`plan` emits the JSON a generator needs: the shared reference script, the
VoiceDesign instruct, and its deterministic seed.
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
DEFAULT_ARCHETYPES = ROOT / "blueprints" / "archetypes"
DEFAULT_GENERATED = ROOT / "data" / "blueprints" / "profiles"
LEGACY_VOCABULARY = ROOT / "blueprints" / "migrations" / "vocabulary-1.0.json"

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


def vocabulary_for_profile(profile: dict, fallback: dict | None = None) -> dict:
    """Keep schema 1.0 meaning pinned to its recorded vocabulary."""
    version = profile.get("schema_version")
    if version == "1.0" and LEGACY_VOCABULARY.is_file():
        return load_vocabulary(LEGACY_VOCABULARY)
    legacy_11 = ROOT / "blueprints" / "migrations" / "vocabulary-1.1.json"
    if version == "1.1" and legacy_11.is_file():
        return load_vocabulary(legacy_11)
    return fallback or load_vocabulary()


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


def render_style(profile: dict, vocab: dict) -> str:
    """Render the speech-style clause, or "" when the profile carries no style.

    Listening on 2026-11-03 preferred styled takes over identity-only ones for
    every sampled voice, so the clause is part of the default render. It is
    appended, never merged: the identity sentence stays byte-identical.
    """
    style = profile.get("speech_style") or {}
    dimensions = vocab.get("speech_style", {}).get("dimensions") or {}
    values = {}
    for field in ("register", "vocabulary_complexity", "sentence_complexity", "colloquialism", "rhetorical_style"):
        label = style.get(field)
        if not label:
            return ""
        entry = (dimensions.get(field, {}).get("values") or {}).get(label) or {}
        values[field] = entry.get("render") or str(label).replace("_", " ")
    return vocab["render"]["style_template"].format(**values)


def render_instruct(profile: dict, vocab: dict) -> str:
    """Identity sentence, then the style clause when the profile has one."""
    return f"{render_identity(profile, vocab)}{render_style(profile, vocab)}"


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def validate_profile(profile: dict, vocab: dict, name: str = "<profile>") -> list[str]:
    profile_vocab = vocabulary_for_profile(profile, vocab)
    vocab = profile_vocab
    errors: list[str] = []

    def err(message: str) -> None:
        errors.append(f"{name}: {message}")

    if profile.get("schema_version") != vocab["schema_version"]:
        err(f"schema_version must be {vocab['schema_version']!r}")
    if profile.get("schema_version") not in {"1.0", "1.1", "1.2"}:
        err("schema_version must be a supported profile version (1.0, 1.1 or 1.2)")
    is_v11 = profile.get("schema_version") in {"1.1", "1.2"}
    if is_v11 and profile.get("profile_kind") not in {"character", "archetype"}:
        err("profile_kind must be character or archetype")
    version = profile.get("profile_version")
    if not isinstance(version, int) or version < 1:
        err("profile_version must be a positive integer")

    character = profile.get("character") or {}
    for field in ("id", "name", "source_url"):
        if not character.get(field):
            err(f"character.{field} is required")
    if character.get("id") and not re.fullmatch(r"[a-z0-9][a-z0-9_]*", character["id"]):
        err("character.id must be lowercase letters, digits, and underscores")

    if profile.get("schema_version") == "1.2":
        for key in ("moods", "pack_archon", "archon_pin"):
            if key in profile:
                err(f"{key} was removed in schema 1.2; use reference_pin")
        if "lore_age" in character:
            err("character.lore_age was removed in schema 1.2")
        style = profile.get("speech_style")
        if isinstance(style, dict):
            for key in ("diction_style", "contraction_frequency", "speech_habits"):
                if key in style:
                    err(f"speech_style.{key} is not a schema 1.2 field")
        pin = profile.get("reference_pin")
        if pin is not None:
            if not isinstance(pin, dict):
                err("reference_pin must be an object")
            else:
                for field in ("design_seed", "reference_sha256", "ref_seconds", "pinned_on"):
                    if field not in pin:
                        err(f"reference_pin.{field} is required")

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

    # Textual speech style is a separate, closed classification. It never
    # reaches render_identity and therefore cannot change acoustic identity.
    if is_v11 and profile.get("speech_style") is not None:
        style = profile.get("speech_style") or {}
        dimensions = vocab.get("speech_style", {}).get("dimensions", {})
        for field, value in style.items():
            if field not in dimensions:
                err(f"speech_style.{field} is not in the vocabulary")
                continue
            if value is None:
                continue
            allowed = dimensions[field]["values"]
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item not in allowed:
                    err(f"speech_style.{field} = {item!r} is not in the vocabulary")
            if field == "speech_habits":
                if len(values) > dimensions[field].get("max_items", 5):
                    err("speech_style.speech_habits has too many values")
                if len(set(values)) != len(values):
                    err("speech_style.speech_habits contains duplicates")

    if is_v11:
        lifecycle = profile.get("lifecycle") or {}
        allowed_statuses = set(vocab.get("profile_lifecycle", {}).get("statuses", []))
        status = lifecycle.get("status")
        if status not in allowed_statuses:
            err(f"lifecycle.status {status!r} is not valid")
        confidence = lifecycle.get("classification_confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            err("lifecycle.classification_confidence must be null or between 0 and 1")
        if not isinstance(lifecycle.get("manual_override"), bool):
            err("lifecycle.manual_override must be boolean")
        approval = lifecycle.get("approval")
        if status == "approved":
            if not isinstance(approval, dict):
                err("approved profiles require human lifecycle.approval details")
            else:
                for field in ("approved_by", "approved_at", "reference_sha256"):
                    if not approval.get(field):
                        err(f"lifecycle.approval.{field} is required for approval")
                pin = profile.get("reference_pin") or {}
                if pin.get("reference_sha256") != approval.get("reference_sha256"):
                    err("approved reference_sha256 must match the pinned reference clip")
        elif approval is not None:
            err("unapproved profiles may not carry human lifecycle.approval details")
        if profile.get("profile_kind") == "archetype":
            if status == "approved" and not approval:
                err("an archetype cannot be marked approved without human audition approval")
            metadata = profile.get("archetype_metadata") or {}
            if metadata.get("audition_status") == "approved" and status != "approved":
                err("archetype_metadata cannot claim approval before lifecycle approval")
        if lifecycle.get("manual_override") and not any(
            row.get("action") in {"edit", "audition", "approve", "reject"}
            for row in profile.get("manual_history", [])
            if isinstance(row, dict)
        ):
            err("manual_override requires a human action in manual_history")

    for field, values in EVIDENCE_SENSITIVE.items():
        current = identity.get(field)
        if current in values:
            level = (evidence.get(field) or {}).get("level")
            if level not in SOURCED_LEVELS:
                err(
                    f"identity.{field} = {current!r} needs explicit or "
                    f"supported_inference evidence, got {level!r}"
                )

    # rendered prompt hygiene: unknown values make this raise
    if not errors:
        prompt = render_identity(profile, vocab)
        banned = vocab["render"]["banned_identity_words"]
        tokens = re.findall(r"[a-z]+", prompt.lower())
        hits = sorted({t for t in tokens if t in banned})
        if hits:
            err(f"identity prompt contains emotion words {hits}")
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


def seed_for(voice_id: str, base: int = 20261101) -> int:
    digest = hashlib.sha256(voice_id.encode()).hexdigest()
    return base + int(digest[:8], 16) % 1_000_000


def build_plan(profile: dict, vocab: dict) -> dict:
    voice_id = profile["character"]["id"]
    instruct = render_instruct(profile, vocab)
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
        "instruct": instruct,
        "seed": seed_for(voice_id),
    }


def resolve_profile(target: str) -> pathlib.Path:
    path = pathlib.Path(target)
    if path.is_file():
        return path
    roots = (DEFAULT_CHARACTERS, DEFAULT_ARCHETYPES, DEFAULT_GENERATED)
    matches = []
    for root in roots:
        candidate = root / f"{target}.json"
        if candidate.is_file():
            profile_paths = [candidate]
        else:
            profile_paths = []
        if root.exists():
            profile_paths.extend(sorted(root.glob("*.json")))
        for profile_path in set(profile_paths):
            try:
                profile = load_json(profile_path)
            except (OSError, json.JSONDecodeError):
                continue
            if profile.get("character", {}).get("id") == target:
                history = profile.get("manual_history") or []
                last_human_action = max(
                    (str(row.get("timestamp") or "") for row in history if isinstance(row, dict)),
                    default="",
                )
                timestamp = last_human_action or str(
                    (profile.get("classification") or {}).get("timestamp") or ""
                )
                matches.append((timestamp, int(profile.get("profile_version", 1)), profile_path.stat().st_mtime_ns, profile_path))
    if matches:
        matches.sort(key=lambda row: row[:3])
        return matches[-1][3]
    raise BlueprintError(f"no blueprint named {target!r} in characters, archetypes, or generated profiles")


def load_profiles(targets: list[str]) -> list[pathlib.Path]:
    if targets:
        return [resolve_profile(target) for target in targets]
    return sorted(
        list(DEFAULT_CHARACTERS.glob("*.json"))
        + list(DEFAULT_ARCHETYPES.glob("*.json"))
    )


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
        errors = validate_profile(profile, vocab, profile.get("character", {}).get("id", path.stem))
        if path.parent in {DEFAULT_CHARACTERS, DEFAULT_ARCHETYPES} and path.stem != profile.get("character", {}).get("id"):
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
    vocab = vocabulary_for_profile(profile, vocab)
    errors = validate_profile(profile, vocab, profile.get("character", {}).get("id", args.target))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    instruct = render_instruct(profile, vocab)
    if args.json:
        print(json.dumps(instruct, indent=2))
        return 0
    print(instruct)
    return 0


def cmd_plan(args) -> int:
    vocab = load_vocabulary(args.vocabulary)
    profile = load_json(resolve_profile(args.target))
    vocab = vocabulary_for_profile(profile, vocab)
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
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise BlueprintError(f"refusing to overwrite existing plan: {path}")
        else:
            path.write_text(text, encoding="utf-8")
        print(path)
    else:
        print(text, end="")
    return 0


def migrate_profile_1_0(profile: dict) -> dict:
    if profile.get("schema_version") != "1.0":
        raise BlueprintError("only schema 1.0 profiles can use this migration")
    migrated = json.loads(json.dumps(profile))
    migrated["schema_version"] = "1.1"
    migrated["profile_kind"] = "character"
    migrated["speech_style"] = None
    migrated["speech_style_analysis"] = None
    migrated["classification"] = {
        "provider": "schema-migration",
        "model_id": "not-classified",
        "dossier_hash": "",
        "configuration_hash": "",
        "source_audit_hash": "",
        "timestamp": "",
    }
    migrated["lifecycle"] = {
        "status": "draft",
        "classification_confidence": None,
        "manual_override": False,
        "approval": None,
    }
    # A schema 1.0 QC flag cannot prove who approved the reference. Preserve
    # every pin, but require a fresh human approval in the new lifecycle.
    if isinstance(migrated.get("qc"), dict):
        migrated["qc"]["identity_approved"] = False
    note = "Explicitly migrated from schema 1.0; identity values and reference text were preserved without reinterpretation. Approval must be recorded again by a human."
    migrated["notes"] = (migrated.get("notes", "").strip() + "\n\n" + note).strip()
    return migrated


def migrate_profile_1_1(profile: dict) -> dict:
    if profile.get("schema_version") != "1.1":
        raise BlueprintError("only schema 1.1 profiles can use this migration")
    migrated = json.loads(json.dumps(profile))
    migrated["schema_version"] = "1.2"
    migrated.pop("moods", None)
    migrated.pop("pack_archon", None)
    migrated.pop("archon_pin", None)
    style = migrated.get("speech_style")
    if isinstance(style, dict):
        for key in ("diction_style", "contraction_frequency", "speech_habits"):
            style.pop(key, None)
    character = migrated.get("character")
    if isinstance(character, dict):
        character.pop("lore_age", None)
    note = "Explicitly migrated from schema 1.1; the mood/archon layer and wiki-derived lore fields were removed. Identity, style and lifecycle values were preserved."
    migrated["notes"] = (migrated.get("notes", "").strip() + "\n\n" + note).strip()
    return migrated


def cmd_migrate(args) -> int:
    from blueprint_pipeline.cache import write_immutable_json

    source_path = resolve_profile(args.target)
    profile = load_json(source_path)
    version = profile.get("schema_version")
    if version not in {"1.0", "1.1"}:
        raise BlueprintError(f"only schema 1.0 or 1.1 profiles can be migrated, got {version!r}")
    migrated = migrate_profile_1_0(profile) if version == "1.0" else profile
    migrated = migrate_profile_1_1(migrated)
    destination = pathlib.Path(args.out) if args.out else (
        ROOT / "data" / "blueprints" / "profiles" / f"{profile['character']['id']}.json"
    )
    written = write_immutable_json(destination, migrated)
    print(written)
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
    render.add_argument("--json", action="store_true")
    render.set_defaults(func=cmd_render)

    plan = sub.add_parser("plan", help="emit the generation plan JSON")
    plan.add_argument("target")
    plan.add_argument("--out", type=pathlib.Path)
    plan.set_defaults(func=cmd_plan)

    migrate = sub.add_parser("migrate", help="explicitly migrate one profile to schema 1.2")
    migrate.add_argument("target")
    migrate.add_argument("--out", type=pathlib.Path)
    migrate.set_defaults(func=cmd_migrate)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BlueprintError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
