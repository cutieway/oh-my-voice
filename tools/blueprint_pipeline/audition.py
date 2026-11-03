"""Prepare matched Identity-only and Identity+Style reference audition plans."""
from __future__ import annotations

import json
import pathlib
from typing import Any


def build_audition_plans(
    profile: dict,
    vocabulary: dict,
    *,
    seeds: int = 1,
    seed_step: int = 1000,
) -> list[dict[str, Any]]:
    """Return paired plans with one matched seed schedule.

    Both conditions carry the production plan's seed so a difference between
    the clips is attributable to the instruction, not to an unrelated offset.
    """
    try:
        import blueprint as renderer
    except ModuleNotFoundError:
        from tools import blueprint as renderer

    plan = renderer.build_plan(profile, vocabulary)
    draws = max(1, int(seeds))
    rows = []
    for index in range(draws):
        seed = plan["seed"] + index * int(seed_step)
        common = {
            "profile_id": plan["profile_id"],
            "profile_version": plan["profile_version"],
            "reference": plan["reference"],
            "design_model": plan["design_model"],
            "identity_prompt_hash": plan["identity_prompt_hash"],
            "style_profile_hash": plan["style_profile_hash"],
            "combined_instruct_hash": plan["combined_instruct_hash"],
            "taxonomy_version": plan.get("taxonomy_version"),
            "classifier_version": plan.get("classifier_version"),
            "prompt_version": plan.get("prompt_version"),
            "sampler_version": plan.get("sampler_version"),
            "seed": seed,
            "seed_index": index,
            "seed_step": int(seed_step),
        }
        rows.append({
            **common,
            "condition": "identity_only",
            "instruct": plan["identity_prompt"],
            "instruct_hash": plan["identity_prompt_hash"],
        })
        rows.append({
            **common,
            "condition": "identity_style",
            "instruct": plan["instruct"],
            "instruct_hash": plan["combined_instruct_hash"],
        })
    return rows


def write_audition_plans(
    profile: dict,
    vocabulary: dict,
    out: pathlib.Path | str,
    *,
    seeds: int = 1,
    seed_step: int = 1000,
) -> list[pathlib.Path]:
    directory = pathlib.Path(out) / "plans"
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for plan in build_audition_plans(
        profile, vocabulary, seeds=seeds, seed_step=seed_step,
    ):
        path = directory / f"{plan['condition']}.seed-{plan['seed']}.plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def audit_entries(report: dict) -> list[dict]:
    entries = []
    for voice in report.get("voices") or []:
        for take in voice.get("takes") or []:
            entries.append({
                "profile_id": voice.get("profile_id"),
                "condition": take.get("condition") or voice.get("condition"),
                "path": take.get("ref"),
                "seed": take.get("seed"),
                "seconds": take.get("ref_seconds"),
                "sha256": take.get("ref_sha256"),
                "instruct": take.get("instruct") or voice.get("instruct"),
                "in_range": take.get("ref_in_range"),
            })
    return entries
