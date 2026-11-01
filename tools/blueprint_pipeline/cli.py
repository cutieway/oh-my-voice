#!/usr/bin/env python3
"""Command line entry point for the synthetic voice blueprint pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import blueprint as blueprint_renderer

from blueprint_pipeline.archetypes import (
    build_assignment,
    load_archetypes,
    resolve_effective_profile,
    select_archetype,
    write_assignment_snapshot,
)
from blueprint_pipeline.cache import (
    json_files,
    object_hash,
    read_json,
    safe_component,
    write_content_addressed,
    write_immutable_json,
)
from blueprint_pipeline.classifier import RulesProvider, classify_dossier
from blueprint_pipeline.config import (
    DEFAULT_CACHE,
    DEFAULT_DB,
    DEFAULT_JEV_ENDPOINT,
    DEFAULT_JEV_INPUT_USD_PER_MILLION_TOKENS,
    DEFAULT_JEV_KEY_ENV,
    DEFAULT_JEV_MODEL,
    DEFAULT_OUT,
    DEFAULT_THRESHOLDS,
    DEFAULT_WIKI_API,
)
from blueprint_pipeline.dossier import build_dossiers, latest_dossiers
from blueprint_pipeline.game_data import load_game_index
from blueprint_pipeline.archetypes import load_archetypes
from blueprint_pipeline.compose import compose_archetype_style_profile
from blueprint_pipeline.casting import cast_identity
from blueprint_pipeline.generator import build_individual_profile, validate_generated_profile
from blueprint_pipeline.identity import canonical_id
from blueprint_pipeline.inventory import build_inventory
from blueprint_pipeline.jev import JevProvider, build_questions, compact_state
from blueprint_pipeline.reporting import build_pipeline_report, review_report
from blueprint_pipeline.review import (
    apply_manual_changes,
    approve_profile,
    mark_auditioned,
    preserve_manual_profile,
    reject_profile,
)
from blueprint_pipeline.wiki import MediaWikiProvider


def _base_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--db", type=Path, default=DEFAULT_DB)
    command.add_argument("--out", type=Path, default=DEFAULT_OUT)
    command.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    command.add_argument("--game-index", type=Path)
    command.add_argument("--limit", type=int)
    command.add_argument("--offset", type=int, default=0)
    command.add_argument("--only", nargs="+", help="canonical IDs, NPC names, or speaker IDs")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--force", action="store_true")
    command.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    command.add_argument("--allow-network", action="store_true")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auditable FFXIV synthetic voice blueprint pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("inventory", "build the conservative synthetic NPC inventory"),
        ("dossiers", "aggregate dialogue and build character dossiers"),
        ("enrich", "resolve and enrich dossier lore using MediaWiki"),
        ("classify", "classify eligible dossiers with rules or Jev"),
        ("compose", "compose draft profiles from archetype acoustics plus classified speech style"),
        ("inspect", "print a dossier, classification, profile, and decision audit"),
        ("validate", "validate curated and generated blueprint profiles"),
        ("report", "summarize inventory, classifications, assignments, and review work"),
        ("review", "edit, audition, approve, or reject a draft profile"),
    ):
        sub = commands.add_parser(name, help=help_text)
        _base_options(sub)
        if name == "classify":
            sub.add_argument("--provider", choices=("rules", "jev"), default="rules")
            sub.add_argument("--model", default=DEFAULT_JEV_MODEL)
            sub.add_argument("--endpoint", default=DEFAULT_JEV_ENDPOINT)
            sub.add_argument("--api-key-env", default=DEFAULT_JEV_KEY_ENV)
            sub.add_argument("--min-confidence", type=float)
            sub.add_argument(
                "--input-usd-per-million-tokens",
                type=float,
                default=DEFAULT_JEV_INPUT_USD_PER_MILLION_TOKENS,
                help="price used for the approximate Jev input-cost estimate",
            )
            sub.add_argument("--allow-bulk", action="store_true", help="allow more than 25 dossiers in one invocation")
        elif name == "enrich":
            sub.add_argument("--wiki-api", default=DEFAULT_WIKI_API)
            sub.add_argument("--user-agent")
            sub.add_argument("--requests-per-second", type=float, default=1.0)
            sub.add_argument("--allow-bulk", action="store_true", help="allow more than 25 wiki lookups in one invocation")
        elif name == "inspect":
            sub.add_argument("target", help="canonical ID, NPC name, speaker ID, or profile ID")
            sub.add_argument("--review-report", action="store_true")
        elif name == "review":
            sub.add_argument("action", choices=("edit", "audition", "approve", "reject"))
            sub.add_argument("profile", help="profile ID or JSON path")
            sub.add_argument("--by", required=True)
            sub.add_argument("--changes", type=Path, help="JSON object mapping dotted fields to values")
            sub.add_argument("--reference", type=Path, help="auditioned VoiceDesign WAV")
            sub.add_argument("--seed", type=int)
            sub.add_argument("--archon", default="neutral")
            sub.add_argument("--reason")
            sub.add_argument("--note")
        if name in {"inventory", "dossiers", "enrich", "classify", "compose", "inspect", "validate", "report", "review"}:
            sub.add_argument("--vocabulary", type=Path, default=ROOT / "blueprints" / "vocabulary.json")
    return parser


def _selected(records: list[dict], args) -> list[dict]:
    selected = records
    if args.only:
        only = set(args.only)
        def matches(row: dict) -> bool:
            identity = row.get("identity") or {}
            canonical = row.get("canonical_id") or identity.get("canonical_id")
            name = row.get("name") or identity.get("name")
            speakers = row.get("speaker_ids") or identity.get("speaker_ids", [])
            aliases = row.get("aliases") or identity.get("aliases", [])
            return canonical in only or name in only or any(value in only for value in speakers) or any(value in only for value in aliases)
        selected = [row for row in selected if matches(row)]
    start = max(0, int(args.offset or 0))
    stop = start + args.limit if args.limit is not None else None
    return selected[start:stop]


def _latest_records(directory: Path, pattern: str) -> dict[str, tuple[dict, Path]]:
    result = {}
    for path in json_files(directory, pattern):
        try:
            record = read_json(path)
        except (OSError, ValueError):
            continue
        key = record.get("canonical_id")
        if key:
            result[str(key)] = (record, path)
    return result


def _inventory(args) -> tuple[dict, Path | None]:
    index = load_game_index(args.game_index)
    inventory = build_inventory(args.db, game_index=index)
    if args.dry_run:
        return inventory, None
    path = write_content_addressed(args.out / "inventory", "synthetic-candidates", inventory)
    return inventory, path


def cmd_inventory(args) -> int:
    inventory, path = _inventory(args)
    print(json.dumps(inventory["counts"], indent=2, sort_keys=True))
    if path:
        print(f"inventory snapshot: {path}")
    else:
        print("dry run: no files written")
    return 0


def cmd_dossiers(args) -> int:
    inventory = build_inventory(args.db, game_index=load_game_index(args.game_index))
    rows = _selected(inventory["candidates"], args)
    print(f"dossiers planned: {len(rows)} (eligible: {sum(row['identity_status'] == 'eligible' for row in rows)})")
    if args.dry_run:
        print("dry run: no dossier files written")
        return 0
    inventory_path = write_content_addressed(args.out / "inventory", "synthetic-candidates", inventory)
    print(f"inventory snapshot: {inventory_path}")
    output = build_dossiers(
        args.db,
        out=args.cache_dir,
        game_index=load_game_index(args.game_index),
        only=set(args.only) if args.only else None,
        offset=max(0, args.offset),
        limit=args.limit,
    )
    print(f"dossier snapshots written or reused: {len(output)} under {args.cache_dir / 'dossiers'}")
    return 0


def cmd_enrich(args) -> int:
    if not args.allow_network and not args.dry_run:
        raise ValueError("wiki enrichment requires --allow-network")
    latest = latest_dossiers(args.cache_dir)
    records = [
        dossier for dossier, _ in latest.values()
        if (dossier.get("identity") or {}).get("npc_id") is not None
        and (dossier.get("identity") or {}).get("resolution_status") == "eligible"
    ]
    records.sort(key=lambda item: item["identity"]["canonical_id"])
    records = _selected(records, args)
    limit = args.limit if args.limit is not None else 25
    if limit <= 0:
        raise ValueError("wiki enrichment requires a positive --limit")
    if args.limit is None:
        records = records[:25]
    if len(records) > 25 and not args.allow_bulk:
        raise ValueError("more than 25 wiki lookups requires --allow-bulk after reviewing the workload")
    print(f"wiki workload: up to {len(records)} canonical NPC lookups at {args.requests_per_second:g} request/s")
    if args.dry_run:
        print("dry run: no network requests made")
        return 0
    provider = MediaWikiProvider(
        args.wiki_api,
        cache_dir=args.cache_dir,
        user_agent=args.user_agent or __import__("blueprint_pipeline.config", fromlist=["DEFAULT_USER_AGENT"]).DEFAULT_USER_AGENT,
        requests_per_second=args.requests_per_second,
    )
    written = 0
    for dossier in records:
        if args.resume and not args.force and dossier.get("wiki_resolution"):
            continue
        enriched = provider.enrich_dossier(dossier)
        enriched["input_hash"] = object_hash({key: value for key, value in enriched.items() if key != "input_hash"})
        write_content_addressed(args.cache_dir / "dossiers", safe_component(dossier["identity"]["canonical_id"]), enriched)
        written += 1
    print(f"wiki dossier snapshots updated: {written}")
    return 0


def _classification_request_key(dossier: dict, provider: str, model: str, vocabulary: dict, min_confidence: float | None) -> str:
    policy = dict(DEFAULT_THRESHOLDS)
    policy.update(vocabulary.get("classification", {}).get("thresholds", {}))
    if min_confidence is not None:
        policy["choice_confidence"] = min_confidence
    config = {
        "provider": provider,
        "model_requested": model,
        "thresholds": policy,
        "questions": build_questions(vocabulary),
        "vocabulary_hash": object_hash(vocabulary),
    }
    return object_hash({"dossier_hash": dossier.get("input_hash"), "config": config})


def _save_profile(profile: dict, out: Path, force: bool) -> tuple[dict, Path]:
    profile_id = profile["character"]["id"]
    existing = None
    candidates = []
    for root in (ROOT / "blueprints" / "characters", out / "profiles"):
        if root.exists():
            for path in root.glob("*.json"):
                try:
                    value = read_json(path)
                except (OSError, ValueError):
                    continue
                if value.get("character", {}).get("id") == profile_id:
                    candidates.append((path.stat().st_mtime_ns, value, path))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        existing = candidates[-1][1]
    effective = preserve_manual_profile(existing, profile, force=force)
    path = write_content_addressed(out / "profiles", profile_id, effective)
    return effective, path


def _load_assignments(out: Path) -> dict[str, dict]:
    latest = {}
    for path in json_files(out / "assignments", "registry.*.json"):
        try:
            payload = read_json(path)
        except (OSError, ValueError):
            continue
        for row in payload.get("assignments") or []:
            latest[row.get("canonical_id")] = row
    return latest


def _confidence(classification: dict) -> float | None:
    values = [
        float(answer["confidence"])
        for answer in (classification.get("answers") or {}).values()
        if isinstance(answer, dict) and answer.get("type") in {"choice", "score"}
        and answer.get("confidence") is not None
    ]
    return min(values) if values else None


def _route_classification(dossier: dict, classification: dict, vocabulary: dict, out: Path, force: bool) -> tuple[dict, Path | None]:
    candidate = {
        "canonical_id": classification.get("canonical_id"),
        "npc_id": classification.get("npc_id"),
        "speaker_ids": classification.get("speaker_ids", []),
        "aliases": classification.get("aliases", []),
        "normalized_names": (dossier.get("identity") or {}).get("normalized_names", []),
        "name": classification.get("name"),
        "sex": (dossier.get("identity") or {}).get("sex", "unknown"),
    }
    archetypes = load_archetypes()
    archetype_assignment = select_archetype(candidate, archetypes, classification=classification)
    profile_path = None
    if classification.get("distinctive") and classification.get("status") == "ready_for_audition":
        profile = build_individual_profile(
            dossier, classification, cast_identity(dossier.get("identity") or {}), vocabulary,
        )
        errors = validate_generated_profile(profile, vocabulary)
        if errors:
            classification.setdefault("review_reasons", []).append(
                "generated individual profile failed validation and was routed to a shared archetype"
            )
            classification["profile_validation_errors"] = errors
        else:
            profile, profile_path = _save_profile(profile, out, force)
            assignment = build_assignment(
                candidate, classification,
                profile_id=profile["character"]["id"],
                reason="individual design supported by distinctiveness score and sufficient evidence",
                confidence=_confidence(classification),
                status="draft",
            )
            assignment["profile_kind"] = "character"
            return assignment, profile_path

    assignment = build_assignment(
        candidate,
        classification,
        profile_id=archetype_assignment["profile_id"],
        reason=("shared archetype preferred by distinctiveness and evidence thresholds"),
        confidence=_confidence(classification),
        status="needs_review" if classification.get("status") == "needs_review" else "draft",
    )
    assignment["profile_kind"] = "archetype"
    assignment["archetype_selection"] = archetype_assignment
    return assignment, profile_path


def cmd_classify(args) -> int:
    if args.provider == "jev" and not args.allow_network and not args.dry_run:
        raise ValueError("Jev classification requires --allow-network")
    if args.min_confidence is not None and not 0 <= args.min_confidence <= 1:
        raise ValueError("--min-confidence must be between 0 and 1")
    if args.input_usd_per_million_tokens < 0:
        raise ValueError("--input-usd-per-million-tokens must be nonnegative")
    vocabulary = blueprint_renderer.load_vocabulary(args.vocabulary)
    latest = latest_dossiers(args.cache_dir)
    records = [value for value, _ in latest.values()]
    records.sort(key=lambda value: value.get("identity", {}).get("canonical_id", ""))
    records = [
        item for item in records
        if item.get("identity", {}).get("resolution_status") == "eligible"
        and item.get("identity", {}).get("npc_id") is not None
        and not item.get("identity", {}).get("has_se_voice_reference")
        and not item.get("identity", {}).get("identity_ambiguous")
    ]
    records = _selected(records, args)
    limit = args.limit if args.limit is not None else 25
    if limit <= 0:
        raise ValueError("classification requires a positive --limit")
    if args.limit is None:
        records = records[:25]
    if len(records) > 25 and not args.allow_bulk:
        raise ValueError("more than 25 dossiers requires --allow-bulk after reviewing the workload")
    questions = build_questions(vocabulary)
    cache_root = args.cache_dir / "classifications"
    cached = _latest_records(cache_root, "*.json")
    provider_model = args.model if args.provider == "jev" else RulesProvider(vocabulary).model_id
    cache_keys = [
        _classification_request_key(dossier, args.provider, provider_model, vocabulary, args.min_confidence)
        for dossier in records
    ]
    reusable = [
        args.resume and not args.force
        and any(value.get("cache_key") == cache_key for value, _ in cached.values())
        for cache_key in cache_keys
    ]
    request_bytes = [
        len(json.dumps({
            "model": args.model,
            "state": compact_state(item),
            "questions": questions,
        }, ensure_ascii=False).encode())
        for item, can_reuse in zip(records, reusable)
        if not can_reuse
    ]
    request_count = sum(not can_reuse for can_reuse in reusable) if args.provider == "jev" else 0
    estimated_tokens = math.ceil(sum(request_bytes) / 4) if request_count else 0
    estimated_cost = estimated_tokens / 1_000_000 * args.input_usd_per_million_tokens
    print(
        f"classification workload: {len(records)} dossiers, {request_count} Jev requests, "
        f"{sum(request_bytes)} request bytes ({max(request_bytes, default=0)} max per dossier)"
    )
    if request_count:
        print(
            f"rough Jev estimate: about {estimated_tokens:,} input tokens and "
            f"${estimated_cost:.6f} at ${args.input_usd_per_million_tokens:g}/million; "
            "actual tokenization and usage are recorded from the API response"
        )
    if args.dry_run:
        print("dry run: no API requests or classification snapshots written")
        return 0

    # Every actual classification run first records the current, read-only
    # identity inventory so its population gate is auditable.
    inventory = build_inventory(args.db, game_index=load_game_index(args.game_index))
    inventory_path = write_content_addressed(args.out / "inventory", "synthetic-candidates", inventory)
    print(f"inventory snapshot: {inventory_path}")

    provider = (
        JevProvider(endpoint=args.endpoint, model=args.model, api_key_env=args.api_key_env)
        if args.provider == "jev" else RulesProvider(vocabulary)
    )
    assignments = _load_assignments(args.out)
    completed = 0
    reused = 0
    for dossier, cache_key in zip(records, cache_keys):
        old = next((value for value, _ in cached.values() if value.get("cache_key") == cache_key), None)
        if old and args.resume and not args.force:
            classification = old
            reused += 1
        else:
            classification = classify_dossier(
                dossier,
                provider,
                vocabulary,
                model_id=args.model if args.provider == "jev" else provider.model_id,
                min_confidence=args.min_confidence,
            )
            classification["cache_key"] = cache_key
            path = write_content_addressed(cache_root, safe_component(classification.get("canonical_id") or "unknown"), classification)
            print(f"classified {classification.get('canonical_id')}: {classification.get('status')} ({path.name})")
            completed += 1
        if classification.get("assignment") is not None or classification.get("status") in {"ready_for_audition", "needs_review"}:
            assignment, profile_path = _route_classification(dossier, classification, vocabulary, args.out, args.force)
            assignments[assignment["canonical_id"]] = assignment
            if profile_path:
                print(f"profile snapshot: {profile_path}")
    if assignments:
        path = write_assignment_snapshot(list(assignments.values()), args.out)
        print(f"assignment registry: {path}")
    print(f"classification snapshots: {completed} new, {reused} reused")
    return 0


def _find_dossier(args, target: str) -> tuple[dict, Path] | None:
    latest = latest_dossiers(args.cache_dir)
    normalized = target.casefold()
    for key, item in latest.items():
        dossier, path = item
        identity = dossier.get("identity") or {}
        if target in {key, str(identity.get("npc_id")), identity.get("name")} or target in identity.get("speaker_ids", []) or normalized == str(identity.get("name") or "").casefold():
            return dossier, path
    return None


def _find_profile(target: str, out: Path) -> tuple[dict, Path] | None:
    maybe = Path(target)
    if maybe.is_file():
        return read_json(maybe), maybe
    roots = (ROOT / "blueprints" / "characters", ROOT / "blueprints" / "archetypes", out / "profiles")
    matches = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            try:
                profile = read_json(path)
            except (OSError, ValueError):
                continue
            if target in {path.stem, profile.get("character", {}).get("id")}:
                matches.append((path.stat().st_mtime_ns, profile, path))
    if not matches:
        return None
    matches.sort(key=lambda row: row[0])
    return matches[-1][1], matches[-1][2]


def cmd_compose(args) -> int:
    """Compose a draft character profile: archetype acoustics plus classified style."""
    vocabulary = blueprint_renderer.load_vocabulary(args.vocabulary)
    latest = latest_dossiers(args.cache_dir)
    dossiers = {value.get("identity", {}).get("canonical_id"): value for value, _ in latest.values()}
    classifications = _latest_records(args.cache_dir / "classifications", "*.json")
    by_canonical = {}
    for value, _ in classifications.values():
        by_canonical[value.get("canonical_id")] = value
    assignments = _load_assignments(args.out)
    archetypes = {item["character"]["id"]: item for item in load_archetypes()}
    selected = _selected(
        [dossiers[key] for key in sorted(dossiers) if key in by_canonical],
        args,
    )
    written = 0
    for dossier in selected:
        canonical_id = dossier["identity"]["canonical_id"]
        classification = by_canonical[canonical_id]
        assignment = assignments.get(canonical_id) or {}
        archetype_id = assignment.get("profile_id")
        archetype = archetypes.get(archetype_id)
        if archetype is None:
            print(f"skip {canonical_id}: no archetype assignment", flush=True)
            continue
        profile = compose_archetype_style_profile(archetype, classification, dossier, vocabulary)
        errors = validate_generated_profile(profile, vocabulary)
        if errors:
            print(f"invalid {canonical_id}: {errors[0]}", flush=True)
            continue
        effective, path = _save_profile(profile, args.out, args.force)
        print(f"composed {canonical_id} -> {path} ({archetype_id})", flush=True)
        written += 1
    print(f"composed profiles: {written}")
    return 0


def cmd_inspect(args) -> int:
    found = _find_dossier(args, args.target)
    profile_found = _find_profile(args.target, args.out)
    dossier = found[0] if found else None
    profile = profile_found[0] if profile_found else None
    classifications = _latest_records(args.cache_dir / "classifications", "*.json")
    classification = None
    canonical_id = (dossier or {}).get("identity", {}).get("canonical_id") or (profile or {}).get("character", {}).get("id")
    if canonical_id and canonical_id in classifications:
        classification = classifications[canonical_id][0]
    assignments = _load_assignments(args.out)
    assignment = assignments.get(canonical_id) if canonical_id else None
    if profile is None and assignment and assignment.get("speaker_ids"):
        for speaker_id in assignment["speaker_ids"]:
            try:
                resolved = resolve_effective_profile(speaker_id, {"assignments": list(assignments.values())})
            except FileNotFoundError:
                continue
            if resolved:
                profile = resolved["profile"]
                assignment = resolved["assignment"]
                break
    if not dossier and not profile:
        raise ValueError(f"no dossier or blueprint found for {args.target!r}")
    if args.review_report:
        report = review_report(
            dossier or {"identity": profile.get("character", {})}, classification, profile,
            blueprint_renderer, blueprint_renderer.load_vocabulary(args.vocabulary), assignment,
        )
    else:
        report = {"dossier": dossier, "classification": classification, "assignment": assignment, "profile": profile}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_validate(args) -> int:
    vocab = blueprint_renderer.load_vocabulary(args.vocabulary)
    paths = (
        list((ROOT / "blueprints" / "characters").glob("*.json"))
        + list((ROOT / "blueprints" / "archetypes").glob("*.json"))
        + list((args.out / "profiles").glob("*.json"))
    )
    failures = []
    profiles = []
    for path in sorted(set(paths)):
        try:
            profile = read_json(path)
        except (OSError, ValueError) as exc:
            failures.append(f"{path}: {exc}")
            continue
        errors = blueprint_renderer.validate_profile(profile, vocab, profile.get("character", {}).get("id", path.stem))
        failures.extend(errors)
        profiles.append(profile)
    collisions = blueprint_renderer.find_collisions(profiles, vocab)
    for _, ids in collisions:
        failures.append("identical acoustic blueprint collision: " + ", ".join(ids))
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"valid profiles: {len(profiles)}; acoustic collisions: {len(collisions)}")
    return 0


def cmd_report(args) -> int:
    inventory = build_inventory(args.db, game_index=load_game_index(args.game_index))
    report, json_path, markdown_path = build_pipeline_report(inventory, args.cache_dir, args.out)
    print(json.dumps({
        "inventory_counts": report["inventory_counts"],
        "classification_count": report["classification_count"],
        "assignment_count": report["assignment_count"],
        "review_count": len(report["review_candidates"]),
        "json": str(json_path),
        "markdown": str(markdown_path),
    }, indent=2))
    return 0


def cmd_review(args) -> int:
    found = _find_profile(args.profile, args.out)
    if not found:
        raise ValueError(f"profile not found: {args.profile}")
    profile, _ = found
    if args.action == "edit":
        if not args.changes:
            raise ValueError("review edit requires --changes JSON_FILE")
        changes = read_json(args.changes)
        profile = apply_manual_changes(profile, changes, by=args.by, note=args.note)
    elif args.action == "audition":
        if not args.reference or args.seed is None:
            raise ValueError("review audition requires --reference WAV and --seed")
        profile = mark_auditioned(
            profile,
            reference_file=args.reference,
            design_seed=args.seed,
            by=args.by,
            archon=args.archon,
            note=args.note,
        )
    elif args.action == "approve":
        profile = approve_profile(profile, by=args.by, archon=args.archon)
    elif args.action == "reject":
        if not args.reason:
            raise ValueError("review reject requires --reason")
        profile = reject_profile(profile, by=args.by, reason=args.reason)
    errors = blueprint_renderer.validate_profile(profile, blueprint_renderer.load_vocabulary(args.vocabulary))
    if errors:
        raise ValueError("review action produced an invalid profile:\n" + "\n".join(errors))
    path = write_content_addressed(args.out / "profiles", profile["character"]["id"], profile)
    print(f"reviewed profile snapshot: {path}")
    print(f"lifecycle: {profile.get('lifecycle', {}).get('status')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    commands = {
        "inventory": cmd_inventory,
        "dossiers": cmd_dossiers,
        "enrich": cmd_enrich,
        "classify": cmd_classify,
        "compose": cmd_compose,
        "inspect": cmd_inspect,
        "validate": cmd_validate,
        "report": cmd_report,
        "review": cmd_review,
    }
    try:
        return commands[args.command](args)
    except (FileNotFoundError, OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
