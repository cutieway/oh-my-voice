"""Prepare and run the standalone speech-style benchmark v1.

Benchmark artifacts are immutable snapshots under data/blueprints/evaluation.
The annotator forms deliberately contain no NPC identity, demographic fields,
model outputs, deterministic labels, or train/test split information.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
import random
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .cache import object_hash, read_json, write_immutable_json
from .config import (
    DEFAULT_DB,
    STYLE_PROMPT_VERSION,
    DIALOGUE_SAMPLER_VERSION,
    STYLE_TAXONOMY_VERSION,
)
from .dossier import build_dossiers
from .inventory import build_inventory
from .dialogue import analyze_dialogue, evidence_sufficiency, words
from .jev import JevError, JevProvider, build_questions as build_style_questions

try:
    import blueprint as blueprint_renderer
except ModuleNotFoundError:
    from tools import blueprint as blueprint_renderer


BENCHMARK_VERSION = "speech-style-benchmark-v2"
PROTOCOL_VERSION = "annotation-protocol-v2"
DEFAULT_ROOT = Path("data/blueprints/evaluation/benchmark_v2")
DEFAULT_SEED = "oh-my-voice-benchmark-v1-2026-11-01"
# v2 keeps the v1 population, seed, and frozen sample lines so the protocol change
# is the only difference between the two benchmarks. The experimental
# `diction_style` field is removed: it overlapped vocabulary/diction and was the
# least stable field in the v1 stability run.
FIELDS: dict[str, dict[str, str]] = {
    "register": {
        "formal": "Predominantly formal constructions, honorifics, or elevated forms of address.",
        "neutral": "Predominantly ordinary wording without strong formal or conversational markers.",
        "conversational": "Predominantly informal, conversational wording or constructions.",
        "mixed": "Substantial, recurring shifts between contrasting registers.",
        "insufficient_evidence": "Too little or unrepresentative text to judge a predominant register.",
    },
    "vocabulary_complexity": {
        "plain": "Mostly common, familiar words and direct phrasing.",
        "ordinary": "Everyday vocabulary with a typical range.",
        "advanced": "Frequent less-common or demanding words; this describes the wording, not the speaker.",
        "specialized": "Repeated technical or domain-specific vocabulary.",
        "poetic": "Unusually figurative, image-rich, or lyrical word choice.",
        "mixed": "Meaningful variation between simpler and more demanding vocabulary.",
        "insufficient_evidence": "Too little usable text to judge vocabulary complexity.",
    },
    "sentence_complexity": {
        "simple": "Predominantly short, structurally simple utterances.",
        "mixed": "A substantial mixture of simple and multi-clause utterances.",
        "complex": "Predominantly long or structurally multi-clause utterances.",
        "insufficient_evidence": "Too little usable sentence material to judge structure.",
    },
    "colloquialism": {
        "low": "Colloquial expressions and constructions are rare.",
        "moderate": "Colloquial wording is noticeable but not dominant.",
        "high": "Colloquial expressions and constructions recur frequently.",
        "mixed": "Colloquial wording varies substantially across the sample.",
        "insufficient_evidence": "Too little usable text to judge colloquial wording.",
    },
    "rhetorical_style": {
        "measured": "Balanced, deliberate statements are the recurring pattern.",
        "direct": "Short, plain statements that get to the point are the recurring pattern.",
        "flowing": "Long linked clauses and continuous phrasing recur.",
        "fragmented": "Short fragments or interrupted phrases recur.",
        "inquisitive": "Questions form a prominent recurring pattern.",
        "emphatic": "Exclamations or marked emphasis form a prominent recurring pattern.",
        "mixed": "No single rhetorical pattern predominates across the sample.",
        "insufficient_evidence": "Too little usable text to judge rhetorical pattern.",
    },
}
FIELD_SCALE_DISPLAY = {
    "register": "nominal",
    "vocabulary_complexity": "ordinal, plain < ordinary < advanced, plus specialized/poetic",
    "sentence_complexity": "ordinal, simple < mixed < complex",
    "colloquialism": "ordinal, low < moderate < high",
    "rhetorical_style": "nominal",
}
FIELD_SCALES = {
    "register": "nominal",
    "vocabulary_complexity": "ordinal_core_plus_off_scale",
    "sentence_complexity": "ordinal",
    "colloquialism": "ordinal",
    "rhetorical_style": "nominal",
}
# Only the ordered core of each ordinal field participates in weighted kappa.
ORDINAL_FIELD_LABELS = {
    "vocabulary_complexity": ["plain", "ordinary", "advanced"],
    "sentence_complexity": ["simple", "mixed", "complex"],
    "colloquialism": ["low", "moderate", "high"],
}
# Decision rules the annotator must apply before choosing a label.
EVIDENCE_MIN_UTTERANCES = 8
EVIDENCE_MIN_WORDS = 100
MIXED_MIN_UTTERANCE_SHARE = 0.30
MIXED_MIN_UTTERANCES_PER_SIDE = 3
MAX_CITED_LINES = 3
PILOT_ITEMS_PER_STRATUM = 3
ADJUDICATION_REASONS = (
    "annotator_agreement",
    "majority_of_annotators",
    "tie_break_rule",
    "insufficient_evidence",
    "taxonomy_revision",
)
PILOT_MIN_EXACT_AGREEMENT = 0.75
PILOT_MIN_KAPPA = 0.60
CONDITIONS = ("text_only", "assisted")
SYSTEM_PREDICTION_DIRS = {
    "rules_full": "rules_full",
    "rules_sample": "rules_sample",
    "nimble_assisted": "nimble/assisted",
    "nimble_text_only": "nimble/text_only",
    "qwen_base_assisted": "qwen_base/assisted",
    "qwen_base_text_only": "qwen_base/text_only",
    "jev": "jev",
}
PROBABILITY_SYSTEMS = ("nimble_assisted", "nimble_text_only")
CONDITION_PAIRS = (
    ("nimble_assisted", "nimble_text_only"),
    ("qwen_base_assisted", "qwen_base_text_only"),
)
# Boundary rules that keep the overlapping fields separable.
FIELD_BOUNDARIES = {
    "register": "Rate formality of address and construction only. Do not raise the label because a line is long (sentence complexity) or contains rare words (vocabulary complexity).",
    "vocabulary_complexity": "Rate word rarity and specialization only. Do not raise the label because a sentence has many clauses (sentence complexity) or because the wording is formal (register).",
    "sentence_complexity": "Rate length and clause structure only. Do not raise the label because the words are rare (vocabulary complexity) or the tone is formal (register).",
    "colloquialism": "Rate slang, idioms, contractions, and informal constructions only. A blunt or direct line is not colloquial unless it uses informal wording; a single interjection is not enough.",
    "rhetorical_style": "Rate the recurring utterance shape only (questions, exclamations, fragments, linked clauses, balanced statements). Do not re-score formality or word rarity here.",
}
FIELD_GUIDANCE: dict[str, list[dict[str, str]]] = {
    "register": [
        {"label": "formal", "text": "I shall inform the steward at once. Pray forgive my intrusion.", "why": "Elevated constructions and honorific address."},
        {"label": "neutral", "text": "The steward is in the hall. I will tell him you arrived.", "why": "Ordinary wording, no strong formality or slang."},
        {"label": "conversational", "text": "Yeah, he's in the hall. I'll let him know you're here.", "why": "Contractions and informal phrasing throughout."},
        {"label": "mixed", "text": "Yeah, he's in the hall. I shall inform the steward at once, if it pleases you.", "why": "Two contrasting registers recur, not one stray line."},
    ],
    "vocabulary_complexity": [
        {"label": "plain", "text": "Take the box. Put it there. Come back later.", "why": "Common words only."},
        {"label": "ordinary", "text": "Leave the crate by the door and come back after the meeting.", "why": "Everyday vocabulary."},
        {"label": "advanced", "text": "The discrepancy in the ledger demands a meticulous accounting of every consignment.", "why": "Repeated less-common words."},
        {"label": "specialized", "text": "The aetherial condenser's pressure differential must not exceed the calibrated threshold.", "why": "Domain terms recur."},
        {"label": "poetic", "text": "The tide remembers every name the shore has taken.", "why": "Figurative, lyrical wording."},
    ],
    "sentence_complexity": [
        {"label": "simple", "text": "Wait here. He is coming. Do not speak.", "why": "Short, single-clause utterances."},
        {"label": "mixed", "text": "Wait here. He is coming, and when he arrives you should tell him what you saw at the gate.", "why": "Simple and multi-clause utterances both recur."},
        {"label": "complex", "text": "When he arrives, you should tell him what you saw at the gate, unless the guards have already told him themselves.", "why": "Multi-clause structure throughout."},
    ],
    "colloquialism": [
        {"label": "low", "text": "The shipment has not arrived. I will inquire at the gate.", "why": "No informal wording."},
        {"label": "moderate", "text": "The shipment hasn't arrived yet. I'll ask around at the gate.", "why": "Contractions and light informality, no slang."},
        {"label": "high", "text": "Ain't no shipment yet. I'll poke around the gate, yeah?", "why": "Slang and informal constructions recur."},
    ],
    "rhetorical_style": [
        {"label": "measured", "text": "The gate opens at dawn. We will be ready by then. There is no cause for alarm.", "why": "Balanced statements are the pattern."},
        {"label": "direct", "text": "Gate. Dawn. Be ready.", "why": "Short, plain statements that get to the point."},
        {"label": "flowing", "text": "The gate opens at dawn, and we will be ready by then, though there is no cause for alarm, and no reason to hurry.", "why": "Linked clauses recur."},
        {"label": "fragmented", "text": "The gate. Dawn. Ready, or not.", "why": "Fragments and interrupted phrases recur."},
        {"label": "inquisitive", "text": "Who told you that? When did the gate open? Did anyone else see it?", "why": "Questions form the prominent pattern."},
        {"label": "emphatic", "text": "The gate opens at dawn! Be ready! There is no cause for alarm!", "why": "Exclamations and marked emphasis recur."},
    ],
}
MEASUREMENT_KEYS = (
    "line_count", "word_count", "unique_word_count", "vocabulary_diversity",
    "average_word_length", "words_per_utterance", "clauses_per_utterance",
    "contractions", "contraction_rate_per_word", "formal_signal_hits",
    "formal_signal_rate_per_word", "colloquial_signal_hits",
    "colloquial_signal_rate_per_word", "punctuation", "question_count",
    "statement_count", "exclamation_count", "honorific_and_form_of_address_count",
)
RULE_ALIASES = {"educated": "advanced"}
QUESTION_INSTRUCTIONS = {
    "register": "Classify the predominant register of the supplied dialogue. Judge formality of address and construction only; ignore word rarity, sentence length, and the character's identity, occupation, status, or personality.",
    "vocabulary_complexity": "Classify the vocabulary in the supplied dialogue. Judge word rarity and specialization only; ignore sentence length, formality of tone, and the speaker's intelligence, education, or social class.",
    "sentence_complexity": "Classify the structural complexity of the supplied utterances by length and clause structure only; ignore word rarity and formality of tone.",
    "colloquialism": "Classify how often the supplied dialogue uses colloquial wording, idioms, slang, contractions, and informal constructions; ignore bluntness and a single isolated interjection.",
    "rhetorical_style": "Classify the recurring utterance shape in the supplied dialogue (questions, exclamations, fragments, linked clauses, balanced statements). Do not re-score formality or word rarity, and do not infer character personality.",
}


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _upper_median(values: list[float | int]) -> float | int | None:
    if not values:
        return None
    return sorted(values)[len(values) // 2]


def _nearest_rank_percentile(values: list[float | int], percentile: float) -> float | int | None:
    if not values:
        return None
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in the interval (0, 1]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _usage_totals(results: list[dict]) -> dict[str, float | int]:
    totals: dict[str, float | int] = {}
    for result in results:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals


def _stable_key(seed: str, *parts: str) -> str:
    return hashlib.sha256("\0".join((seed, *parts)).encode("utf-8")).hexdigest()


def _write_text_immutable(path: Path, value: str) -> Path:
    return _write_bytes_immutable(path, value.encode("utf-8"))


def _write_bytes_immutable(path: Path, encoded: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path
    if candidate.exists():
        if candidate.read_bytes() == encoded:
            return candidate
        digest = hashlib.sha256(encoded).hexdigest()[:12]
        candidate = path.with_name(f"{path.stem}.{digest}{path.suffix}")
        serial = 1
        while candidate.exists():
            if candidate.read_bytes() == encoded:
                return candidate
            candidate = path.with_name(f"{path.stem}.{digest}.{serial}{path.suffix}")
            serial += 1
    with candidate.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
    return candidate


def _measurement_view(analysis: dict) -> dict:
    measurements = analysis.get("measurements") or {}
    result = {key: measurements[key] for key in MEASUREMENT_KEYS if key in measurements}
    repeated = measurements.get("repeated_expressions") or []
    result["repeated_expression_types_observed"] = len(repeated)
    result["repeated_expression_max_frequency"] = max((row.get("count", 0) for row in repeated), default=0)
    return result


def _sample_measurement_view(analysis: dict, samples: list[dict]) -> dict:
    result = _measurement_view(analysis)
    lengths = [len(words(str(sample.get("text") or ""))) for sample in samples]
    short_count = sum(length <= 5 for length in lengths)
    result["short_utterance_count_le_5_words"] = short_count
    result["short_utterance_rate_le_5_words"] = round(short_count / len(lengths), 4) if lengths else 0.0
    return result


def build_speech_style_state(benchmark_dossier: dict, *, condition: str = "assisted") -> dict:
    """Build an identity-free, sample-only state for text-style classification.

    ``text_only`` withholds every sample-derived measurement so the comparison
    against the measurement-based rules baseline is not circular.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")
    samples = benchmark_dossier.get("dialogue_samples") or []
    state = {
        "task": "Classify observable linguistic patterns in the supplied dialogue only. Do not infer acoustic traits.",
        "dialogue_samples": [
            {"id": sample["id"], "text": sample["text"]}
            for sample in samples
        ],
        "taxonomy_version": STYLE_TAXONOMY_VERSION,
        "prompt_version": STYLE_PROMPT_VERSION,
        "sampler_version": DIALOGUE_SAMPLER_VERSION,
        "evidence_sufficiency": evidence_sufficiency(
            {"unique_line_count": len(samples)}, samples,
            vocabulary=blueprint_renderer.load_vocabulary(),
        ),
    }
    if condition == "assisted":
        sample_analysis = analyze_dialogue(samples, blueprint_renderer.load_vocabulary())
        state["measurements"] = _sample_measurement_view(sample_analysis, samples)
    forbidden = {"name", "npc_id", "canonical_id", "race", "clan", "sex", "occupation", "faction", "personality", "interpretation", "lore", "voice"}
    if forbidden.intersection(state):
        raise ValueError("speech-style state contains a prohibited identity or interpretation key")
    if len(samples) != len(state["dialogue_samples"]):
        raise ValueError("speech-style state lost dialogue samples")
    if any(not sample["id"] or not isinstance(sample["text"], str) for sample in state["dialogue_samples"]):
        raise ValueError("every sample must have an ID and text")
    return state


def build_questions() -> dict[str, dict]:
    return {
        field: {
            "type": "choice",
            "instructions": QUESTION_INSTRUCTIONS[field],
            "criteria": options,
        }
        for field, options in FIELDS.items()
    }


def _blank_annotation(benchmark_id: str, sample_count: int) -> dict:
    return {
        "benchmark_id": benchmark_id,
        "sample_count": sample_count,
        "overall_evidence_sufficiency": "",
        "fields": {
            field: {"label": "", "evidence_sufficient": "", "evidence_line_ids": [], "notes": ""}
            for field in FIELDS
        },
        "annotator_notes": "",
    }


def _annotation_guide() -> str:
    lines = [
        "# Oh My Voice speech-style benchmark v2 — annotation guide",
        "",
        "Annotate only the text lines shown in your form. You are rating wording, not a character, actor,",
        "personality, job, social position, race, sex, or voice. Do not look up names or characters: the",
        "benchmark ID is arbitrary. Do not discuss labels with the other annotator.",
        "",
        "The supplied lines are a fixed representative sample, not a full transcript. Do not infer pitch,",
        "timbre, accent, age, or any other acoustic feature. The number of lines and the benchmark ID carry",
        "no information about the expected label; ignore both when choosing a label.",
        "",
        "## Procedure (apply in this order for every field)",
        "",
        f"1. **Evidence floor.** If the item has fewer than {EVIDENCE_MIN_UTTERANCES} usable utterances or fewer",
        f"   than {EVIDENCE_MIN_WORDS} words, set that field's `label` to `insufficient_evidence` and",
        "   `evidence_sufficient` to `no`. Do not guess. A sample above the floor may still be marked",
        "   insufficient if the lines do not show the pattern you are rating.",
        "2. **Count the pattern.** Read every line and decide, line by line, whether it shows label X or label Y.",
        "   A label needs the clear majority of lines, not a single memorable line.",
        f"3. **Mixed test.** Use `mixed` only when two contrasting labels each cover at least about",
        f"   {int(MIXED_MIN_UTTERANCE_SHARE * 100)}% of the lines and each has at least {MIXED_MIN_UTTERANCES_PER_SIDE} lines of its own.",
        "   One stray exception, or a single line that could be read either way, does not make a sample mixed.",
        "4. **Tie-break.** If neither label reaches a majority and the mixed test fails, choose the label supported",
        "   by the larger share of lines. If the two shares are indistinguishable, choose the milder label",
        "   (`neutral`, `ordinary`, `moderate`, or `measured`) rather than the extreme one.",
        "5. **Cite evidence.** Record 1 to 3 line IDs that actually support the chosen label, using the IDs printed",
        f"   with each line (maximum {MAX_CITED_LINES}). Cite at least 2 lines when you choose `mixed`, one per side.",
        "   Do not cite a line that contradicts the label.",
        "",
        "`insufficient_evidence` is not a neutral label and `mixed` is not a hedge. `evidence_sufficient` must be",
        "`no` exactly when the label is `insufficient_evidence`, and `yes` or `uncertain` otherwise.",
        "",
        "## Field boundaries (the fields overlap, so keep them separate)",
        "",
    ]
    for field, boundary in FIELD_BOUNDARIES.items():
        lines.append(f"- `{field}` ({FIELD_SCALE_DISPLAY[field]}): {boundary}")
    lines += [
        "",
        "## Fields, labels, and boundary examples",
        "",
    ]
    for field, options in FIELDS.items():
        lines.extend([f"### `{field}` — {FIELD_SCALE_DISPLAY[field]}", ""])
        for label, description in options.items():
            lines.append(f"- `{label}` — {description}")
        lines.append("")
        examples = FIELD_GUIDANCE.get(field) or []
        if examples:
            lines.append("Boundary examples (illustrative text, not benchmark items):")
            lines.append("")
            for example in examples:
                lines.append(f"- `{example['label']}`: \"{example['text']}\" — {example['why']}")
            lines.append("")
    lines += [
        "## Evidence fields",
        "",
        "`overall_evidence_sufficiency` is a separate judgement for the five fields as a group: `sufficient`,",
        "`insufficient`, or `uncertain`. Per-field `evidence_sufficient` is `yes`, `no`, or `uncertain`.",
        "Use `notes` for a short reason when the case is ambiguous, when you chose `mixed`, or when you chose",
        "`insufficient_evidence`; do not write an essay.",
        "",
        "## Agreement gates (why this matters)",
        "",
        f"Before the full set is annotated, both annotators label a {PILOT_ITEMS_PER_STRATUM * 4}-item pilot. A field",
        f"proceeds only if exact agreement is at least {PILOT_MIN_EXACT_AGREEMENT:.2f} and Cohen's kappa is at least",
        f"{PILOT_MIN_KAPPA:.2f} (linear-weighted kappa for ordinal fields). A field below the gate is revised or",
        "dropped instead of being annotated in bulk.",
        "",
        "Items that both annotators mark `evidence_sufficient: no` are excluded from model accuracy gates and",
        "reported separately; they are not treated as model errors.",
        "",
        "Return the completed form without adding names, source locations, or guesses about identity.",
    ]
    return "\n".join(lines) + "\n"


def prepare_benchmark(
    *,
    db: Path | str = DEFAULT_DB,
    cache: Path | str = "data/blueprints/cache",
    root: Path | str = DEFAULT_ROOT,
    seed: str = DEFAULT_SEED,
    model_path: Path | str = "data/models/qwen3.5-9b/Bespoke-Nimble-9B-v3-Q4_K_M.gguf",
) -> dict:
    root = Path(root)
    db = Path(db)
    cache = Path(cache)
    model_path = Path(model_path)
    if (root / "manifest.json").exists():
        raise FileExistsError(
            f"benchmark already frozen at {root}; choose a new versioned root rather than replacing it"
        )
    inventory = build_inventory(db)
    eligible = [c for c in inventory["candidates"] if c["identity_status"] == "eligible"]
    strata = {
        "5-20": lambda n: 5 <= n <= 20,
        "21-50": lambda n: 21 <= n <= 50,
        "51-200": lambda n: 51 <= n <= 200,
        "200+": lambda n: n > 200,
    }
    chosen = []
    for stratum, predicate in strata.items():
        pool = [c for c in eligible if predicate(int(c["dialogue_count"]))]
        pool.sort(key=lambda c: (_stable_key(seed, stratum, c["canonical_id"]), c["canonical_id"]))
        if len(pool) < 20:
            raise RuntimeError(f"stratum {stratum} has only {len(pool)} eligible candidates")
        chosen.extend((stratum, c) for c in pool[:20])

    selected_ids = {c["canonical_id"] for _, c in chosen}
    dossier_records = build_dossiers(db, out=cache, only=selected_ids, sample_count=24)
    by_identity = {record["canonical_id"]: record for record in dossier_records}
    if set(by_identity) != selected_ids:
        raise RuntimeError(f"dossier builder omitted {sorted(selected_ids - set(by_identity))}")

    # First freeze the blinded dossiers in memory. A fixed shuffle hides stratum
    # and source ordering from the annotators while remaining reproducible.
    dossier_payloads: list[tuple[str, dict, dict]] = []
    private_rows = []
    for stratum, candidate in chosen:
        canonical_id = candidate["canonical_id"]
        source_dossier = read_json(Path(by_identity[canonical_id]["path"]))
        analysis = analyze_dialogue(source_dossier.get("dialogue_samples") or [], blueprint_renderer.load_vocabulary())
        benchmark_id = "B" + _stable_key(seed, "benchmark-id", canonical_id)[:8].upper()
        ordered_samples = sorted(
            source_dossier.get("dialogue_samples") or [],
            key=lambda sample: _stable_key(seed, "sample-order", canonical_id, str(sample.get("voice_line_id") or sample.get("dialog_id") or "")),
        )
        anonymous_samples = []
        private_sample_map = []
        for index, sample in enumerate(ordered_samples, 1):
            sample_id = f"{benchmark_id}-L{index:02d}"
            anonymous_samples.append({"id": sample_id, "text": str(sample.get("text") or "")})
            private_sample_map.append({
                "id": sample_id,
                "dialog_id": sample.get("dialog_id"),
                "voice_line_id": sample.get("voice_line_id"),
                "source": sample.get("source"),
                "sheet": sample.get("sheet"),
                "row_id": sample.get("row_id"),
            })
        benchmark_dossier = {
            "benchmark_id": benchmark_id,
            "dialogue_samples": anonymous_samples,
            "sample_measurements": _sample_measurement_view(analysis, anonymous_samples),
        }
        benchmark_dossier["input_hash"] = object_hash(benchmark_dossier)
        dossier_payloads.append((benchmark_id, benchmark_dossier, {
            "candidate": candidate,
            "source_dossier": source_dossier,
            "sample_map": private_sample_map,
            "sample_analysis": analysis,
            "stratum": stratum,
        }))

    # Freeze a 20/60 split independently within each dialogue-volume stratum.
    split_by_id = {}
    for stratum in strata:
        ids = [bid for bid, _, priv in dossier_payloads if priv["stratum"] == stratum]
        ids.sort(key=lambda bid: (_stable_key(seed, "split", stratum, bid), bid))
        split_by_id.update({bid: "development" if i < 5 else "locked_test" for i, bid in enumerate(ids)})

    # The pilot is drawn from development items only, so a failed gate never
    # consumes locked-test annotation effort and never tunes on locked test.
    stratum_by_id = {bid: priv["stratum"] for bid, _payload, priv in dossier_payloads}
    pilot_ids: list[str] = []
    for stratum in strata:
        development_ids = sorted(
            (bid for bid in split_by_id if split_by_id[bid] == "development" and stratum_by_id[bid] == stratum),
            key=lambda bid: (_stable_key(seed, "pilot", stratum, bid), bid),
        )
        if len(development_ids) < PILOT_ITEMS_PER_STRATUM:
            raise RuntimeError(f"stratum {stratum} has only {len(development_ids)} development items for the pilot")
        pilot_ids.extend(development_ids[:PILOT_ITEMS_PER_STRATUM])
    pilot_id_set = set(pilot_ids)

    for directory in (
        root / "dossiers", root / "annotations", root / "annotations" / "pilot", root / "adjudicated",
        root / "predictions" / "rules_full", root / "predictions" / "rules_sample",
        root / "predictions" / "nimble" / "text_only", root / "predictions" / "nimble" / "assisted",
        root / "predictions" / "qwen_base" / "text_only", root / "predictions" / "qwen_base" / "assisted",
        root / "predictions" / "jev", root / "reports",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    public_dossiers = []
    private_manifest = []
    rules_full = []
    rules_sample = []
    class_fields = list(FIELDS)
    for benchmark_id, payload, private in dossier_payloads:
        dossier_path = write_immutable_json(root / "dossiers" / f"{benchmark_id}.json", payload)
        public_dossiers.append({
            "benchmark_id": benchmark_id,
            "input_hash": payload["input_hash"],
            "dialogue_sample_count": len(payload["dialogue_samples"]),
            "sample_line_ids": [s["id"] for s in payload["dialogue_samples"]],
            "dossier_path": str(dossier_path),
        })
        candidate = private["candidate"]
        source_dossier = private["source_dossier"]
        private_manifest.append({
            "benchmark_id": benchmark_id,
            "canonical_id": candidate["canonical_id"],
            "npc_id": candidate.get("npc_id"),
            "display_name": candidate.get("name"),
            "dialogue_row_count": candidate["dialogue_count"],
            "stratum": private["stratum"],
            "split": split_by_id[benchmark_id],
            "source_dossier_path": by_identity[candidate["canonical_id"]]["path"],
            "source_dossier_hash": source_dossier.get("input_hash"),
            "source_counts": candidate.get("source_counts", {}),
            "expansion_counts": candidate.get("expansion_counts", {}),
            "unique_line_count": candidate.get("unique_line_count", 0),
            "sample_to_source": private["sample_map"],
        })
        full = (source_dossier.get("dialogue_analysis") or {}).get("interpretation") or {}
        sample = (private["sample_analysis"].get("interpretation") or {})
        full_labels = {f: RULE_ALIASES.get(full.get(f), full.get(f)) for f in class_fields}
        sample_labels = {f: RULE_ALIASES.get(sample.get(f), sample.get(f)) for f in class_fields}
        for system, labels, measurements, evidence_scope in (
            ("rules_full", full_labels, (source_dossier.get("dialogue_analysis") or {}).get("measurements") or {}, "full_dialogue_corpus"),
            ("rules_sample", sample_labels, private["sample_analysis"].get("measurements") or {}, "frozen_representative_sample"),
        ):
            pred = {
                "benchmark_id": benchmark_id,
                "system": system,
                "evidence_scope": evidence_scope,
                "labels": labels,
                "probabilities": None,
                "confidence_note": "Deterministic rules are compared by labels only; no calibrated probabilities are claimed.",
                "measurements": _measurement_view({"measurements": measurements}),
                "input_hash": (
                    source_dossier.get("input_hash")
                    if system == "rules_full"
                    else payload["input_hash"]
                ),
            }
            path = write_immutable_json(root / "predictions" / system / f"{benchmark_id}.json", pred)
            (rules_full if system == "rules_full" else rules_sample).append(str(path))

    # Both blinded forms contain identical items and no split or predictions.
    form_items = []
    for benchmark_id, payload, _private in dossier_payloads:
        form_items.append({
            **_blank_annotation(benchmark_id, len(payload["dialogue_samples"])),
            "dialogue_samples": payload["dialogue_samples"],
        })
    form_items.sort(key=lambda row: (_stable_key(seed, "form-order", row["benchmark_id"]), row["benchmark_id"]))
    pilot_items = [item for item in form_items if item["benchmark_id"] in pilot_id_set]
    if len(pilot_items) != len(pilot_ids):
        raise RuntimeError("pilot item selection does not match the frozen dossiers")

    def _form(items: list[dict], scope: str) -> dict:
        return {
            "schema_version": "2.0",
            "benchmark": BENCHMARK_VERSION,
            "protocol": PROTOCOL_VERSION,
            "scope": scope,
            "instructions": "Complete labels independently using annotation_guide.md. Cite 1-3 supporting line IDs per field. Do not look up benchmark IDs or share labels.",
            "items": items,
        }

    for annotator in ("annotator_a", "annotator_b"):
        write_immutable_json(root / "annotations" / f"{annotator}.json", _form(form_items, "full"))
        write_immutable_json(root / "annotations" / "pilot" / f"{annotator}.json", _form(pilot_items, "pilot"))
    write_immutable_json(root / "adjudicated" / "gold.json", {
        "schema_version": "2.0",
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "status": "pending_independent_annotations_and_adjudication",
        "items": [
            {
                "benchmark_id": item["benchmark_id"],
                "annotator_a_labels": {},
                "annotator_b_labels": {},
                "adjudicated_labels": {field: "" for field in FIELDS},
                "adjudication_reasons": {field: "" for field in FIELDS},
                "adjudication_notes": "",
            }
            for item in form_items
        ],
    })
    _write_text_immutable(root / "annotations" / "annotation_guide.md", _annotation_guide())

    # Record unavailable model baselines explicitly; do not fabricate predictions.
    for condition in CONDITIONS:
        write_immutable_json(root / "predictions" / "nimble" / condition / "status.json", {
            "status": "not_run",
            "condition": condition,
            "reason": "Model runs are deferred until the pilot agreement gate passes; run `nimble --condition ...` against a live /v1/systemone endpoint.",
        })
        write_immutable_json(root / "predictions" / "qwen_base" / condition / "status.json", {
            "status": "not_run",
            "condition": condition,
            "reason": "Model runs are deferred until the pilot agreement gate passes; run `qwen-base --condition ...` against a live chat-completions endpoint.",
        })
    write_immutable_json(root / "predictions" / "jev" / "status.json", {
        "status": "not_run",
        "reason": "No Jev endpoint/key is configured in this process. The previously pasted key is not persisted or reused.",
    })

    model_metadata = {
        "path": str(model_path),
        "available": model_path.is_file(),
        "sha256": _digest_file(model_path) if model_path.is_file() else None,
        "license_note": "Bespoke-Nimble GGUF model card declares CC BY-NC 4.0; evaluate commercial-use restrictions before deployment.",
    }
    manifest = {
        "schema_version": "2.0",
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": seed,
        "database_path": str(db.resolve()),
        "database_sha256": _digest_file(db),
        "population_counts": inventory["counts"],
        "selection": {
            "rule": "Eligible canonical NPC identities selected by seeded SHA-256 order, 20 per dialogue-row stratum.",
            "strata": {label: 20 for label in strata},
            "development_per_stratum": 5,
            "locked_test_per_stratum": 15,
            "dialogue_sample_cap": 24,
            "note": "The 5-20-line stratum can contain fewer than 24 unique lines. The benchmark uses all selected representative unique lines available and records the actual count; no lines are duplicated to reach 24.",
            "not_claimed": "Named/minor/recurring/quest archetype labels were not inferred from identity or lore. Seeded population sampling and source diversity are reported instead.",
        },
        "annotation_scopes": {
            "pilot": {
                "item_ids": sorted(pilot_ids),
                "items_per_stratum": PILOT_ITEMS_PER_STRATUM,
                "split": "development",
                "gate": {
                    "min_exact_agreement": PILOT_MIN_EXACT_AGREEMENT,
                    "min_cohen_kappa": PILOT_MIN_KAPPA,
                    "ordinal_statistic": "linear-weighted Cohen's kappa",
                    "policy": "Every field must pass before full annotation opens; a failing field is revised or dropped.",
                },
            },
            "full": {
                "item_ids": [item["benchmark_id"] for item in form_items],
                "note": "The full form repeats the pilot items; only full-scope submissions feed gold labels.",
            },
        },
        "evidence_protocol": {
            "min_utterances": EVIDENCE_MIN_UTTERANCES,
            "min_words": EVIDENCE_MIN_WORDS,
            "mixed_min_utterance_share": MIXED_MIN_UTTERANCE_SHARE,
            "mixed_min_utterances_per_side": MIXED_MIN_UTTERANCES_PER_SIDE,
            "max_cited_lines": MAX_CITED_LINES,
            "citation_required": True,
            "adjudication_reasons": list(ADJUDICATION_REASONS),
            "insufficient_evidence_implies_evidence_sufficient_no": True,
            "low_evidence_exclusion_rule": "A field is excluded from model accuracy gates when both annotators record evidence_sufficient: no.",
        },
        "conditions": {
            "text_only": "The classifier receives frozen dialogue lines and IDs only.",
            "assisted": "The classifier also receives sample-derived measurements.",
            "rules_note": "The deterministic rules baseline consumes measurements by construction, so it is a measurement-only baseline and cannot serve as a text-only condition.",
        },
        "model": model_metadata,
        "fields": FIELDS,
        "field_scales": FIELD_SCALES,
        "ordinal_field_labels": ORDINAL_FIELD_LABELS,
        "public_dossiers": public_dossiers,
        "private_selection": private_manifest,
        "annotator_blinding": "Forms contain anonymous IDs and frozen text only; no demographics, full-corpus rules, model predictions, or split labels.",
        "human_labels": "Not fabricated. Two independent annotations and adjudication are pending.",
        "rules_prediction_files": {"full": rules_full, "sample": rules_sample},
    }
    manifest_path = write_immutable_json(root / "manifest.json", manifest)
    report = {
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "manifest_path": str(manifest_path),
        "dossier_count": len(public_dossiers),
        "fields": list(FIELDS),
        "pilot_item_count": len(pilot_ids),
        "split_counts": {
            name: sum(row["split"] == name for row in private_manifest)
            for name in ("development", "locked_test")
        },
        "stratum_counts": {
            label: sum(row["stratum"] == label for row in private_manifest)
            for label in strata
        },
        "sample_count_min": min(row["dialogue_sample_count"] for row in public_dossiers),
        "sample_count_max": max(row["dialogue_sample_count"] for row in public_dossiers),
        "sample_count_below_24": sum(row["dialogue_sample_count"] < 24 for row in public_dossiers),
        "sample_below_evidence_floor": [
            row["benchmark_id"] for row in public_dossiers
            if row["dialogue_sample_count"] < EVIDENCE_MIN_UTTERANCES
        ],
        "source_coverage": {
            source: sum(bool(row.get("source_counts", {}).get(source)) for row in private_manifest)
            for source in sorted({source for row in private_manifest for source in row.get("source_counts", {})})
        },
        "source_rows_in_selected_population": {
            source: sum(row.get("source_counts", {}).get(source, 0) for row in private_manifest)
            for source in sorted({source for row in private_manifest for source in row.get("source_counts", {})})
        },
        "quest_source_npcs": sum(bool(set(row.get("source_counts", {})) & {"quest", "custom", "raid"}) for row in private_manifest),
        "human_annotation_status": "pending_pilot",
        "model_status": {system: "not_run" for system in SYSTEM_PREDICTION_DIRS if system not in {"rules_full", "rules_sample"}},
        "jev_status": "unconfigured",
    }
    write_immutable_json(root / "reports" / "preparation.json", report)
    write_immutable_json(root / "reports" / "protocol.json", {
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "changes_from_v1": {
            "fields": "diction_style removed (overlap with vocabulary_complexity and lowest stability in v1); five core fields retained.",
            "boundaries": "Per-field boundary rules added to the guide and to model instructions to separate register / vocabulary / sentence complexity.",
            "decision_rules": "Evidence floor, majority rule, mixed threshold, tie-break, and mandatory 1-3 line citations are operational and validated on import.",
            "scales": FIELD_SCALES,
            "ordinal_labels": ORDINAL_FIELD_LABELS,
            "conditions": list(CONDITIONS),
            "pilot": f"{len(pilot_ids)} development items gate the full annotation run.",
        },
        "not_claimed": [
            "Model stability numbers (repeats, reordering, alternate samples) are robustness diagnostics, not correctness evidence.",
            "Rules baselines are measurement-only; they are not a text-only condition.",
        ],
    })
    return report


def _validate_choice_response(
    payload: dict, questions: dict, sample_ids: list[str] | None = None,
) -> dict:
    if not isinstance(payload.get("model"), str) or not isinstance(payload.get("answers"), dict):
        raise ValueError("native response missing model or answers")
    if set(payload["answers"]) != set(questions):
        raise ValueError("native response answer IDs do not match the request")
    parsed = {}
    for field, question in questions.items():
        answer = payload["answers"].get(field) or {}
        options = question["criteria"]
        choice = answer.get("choice")
        probs = answer.get("probabilities")
        if choice not in options or not isinstance(probs, dict) or set(probs) != set(options):
            raise ValueError(f"invalid choice/probability keys for {field}")
        probabilities = {key: float(probs[key]) for key in options}
        if any(not math.isfinite(v) or v < 0 or v > 1 for v in probabilities.values()):
            raise ValueError(f"invalid probability for {field}")
        if abs(sum(probabilities.values()) - 1.0) > 0.02:
            raise ValueError(f"probabilities do not sum to one for {field}")
        if probabilities[choice] + 1e-9 < max(probabilities.values()):
            raise ValueError(f"choice is not the maximum-probability option for {field}")
        confidence = float(answer.get("confidence"))
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError(f"invalid confidence for {field}")
        supporting = answer.get("supporting_line_ids") or []
        counterexamples = answer.get("counterexample_line_ids") or []
        for label, ids in (("supporting", supporting), ("counterexample", counterexamples)):
            if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                raise ValueError(f"invalid {label} line IDs for {field}")
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} line IDs for {field}")
            if sample_ids is not None:
                unknown = [item for item in ids if item not in sample_ids]
                if unknown:
                    raise ValueError(f"{label} line IDs not in request for {field}: {unknown}")
        parsed[field] = {
            "label": choice,
            "probabilities": probabilities,
            "confidence": confidence,
            "status": answer.get("status") or (
                "insufficient_evidence" if choice == "insufficient_evidence" else "unresolved"
            ),
            "supporting_line_ids": supporting,
            "counterexample_line_ids": counterexamples,
            "rationale": answer.get("rationale"),
            "model_self_reported_confidence": answer.get("model_self_reported_confidence"),
        }
    return parsed


def run_nimble(
    *,
    root: Path | str = DEFAULT_ROOT,
    endpoint: str = "http://127.0.0.1:18081/v1/systemone",
    condition: str = "assisted",
    limit: int | None = None,
    only: set[str] | None = None,
    timeout: float = 600,
) -> dict:
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")
    root = Path(root)
    prediction_dir = root / "predictions" / SYSTEM_PREDICTION_DIRS[f"nimble_{condition}"]
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    dossiers = manifest.get("public_dossiers") or []
    if only:
        dossiers = [d for d in dossiers if d["benchmark_id"] in only]
    if limit is not None:
        dossiers = dossiers[:limit]
    questions = build_questions()
    results = []
    failures = []
    reused_count = 0
    request_sizes = []
    state_sizes = []
    run_started = time.perf_counter()
    for entry in dossiers:
        dossier = read_json(Path(entry["dossier_path"]))
        state = build_speech_style_state(dossier, condition=condition)
        state_bytes = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        body = json.dumps({"state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")
        request_bytes = len(body)
        request_sizes.append(request_bytes)
        state_sizes.append(state_bytes)
        target_path = prediction_dir / f"{entry['benchmark_id']}.json"
        request_sha = hashlib.sha256(body).hexdigest()
        if target_path.is_file():
            prior = read_json(target_path)
            if prior.get("request_sha256") == request_sha and prior.get("model_sha256") == (manifest.get("model") or {}).get("sha256"):
                reused_count += 1
                results.append({
                    "benchmark_id": entry["benchmark_id"],
                    "latency_ms": prior.get("latency_ms"),
                    "request_bytes": request_bytes,
                    "state_bytes": state_bytes,
                    "usage": prior.get("usage") or {},
                    "prediction_path": str(target_path),
                    "reused": True,
                })
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
                continue
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            payload = json.loads(raw.decode("utf-8"))
            answers = _validate_choice_response(
                payload, questions,
                [sample["id"] for sample in state["dialogue_samples"]],
            )
            record = {
                "benchmark_id": entry["benchmark_id"],
                "system": "nimble_native_systemone",
                "model": payload["model"],
                "model_sha256": (manifest.get("model") or {}).get("sha256"),
                "quantization": "Q4_K_M",
                "llama_cpp_revision": "dde879bc4f954b809892fd0d660515f42536394a",
                "configuration_sha256": object_hash({"fields": FIELDS, "instructions": QUESTION_INSTRUCTIONS, "condition": condition}),
                "condition": condition,
                "endpoint": endpoint,
                "http_status": status,
                "latency_ms": elapsed_ms,
                "request_sha256": request_sha,
                "input_hash": entry["input_hash"],
                "sample_line_ids": [
                    sample["id"] for sample in state.get("dialogue_samples") or []
                ],
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "usage": payload.get("usage") or {},
                "answers": answers,
            }
            path = write_immutable_json(prediction_dir / f"{entry['benchmark_id']}.json", record)
            results.append({
                "benchmark_id": entry["benchmark_id"],
                "latency_ms": elapsed_ms,
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "usage": record["usage"],
                "prediction_path": str(path),
            })
        except Exception as exc:
            failure = {
                "benchmark_id": entry["benchmark_id"],
                "system": "nimble_native_systemone",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "input_hash": entry["input_hash"],
                "sample_line_ids": [
                    sample["id"] for sample in state.get("dialogue_samples") or []
                ],
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
            }
            path = write_immutable_json(prediction_dir / f"{entry['benchmark_id']}.failure.json", failure)
            failures.append({
                "benchmark_id": entry["benchmark_id"],
                "latency_ms": failure["latency_ms"],
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "failure_path": str(path),
                "error_type": type(exc).__name__,
            })
        print(json.dumps(results[-1] if not failures or failures[-1].get("benchmark_id") != entry["benchmark_id"] else failures[-1], ensure_ascii=False), flush=True)
    latencies = [result["latency_ms"] for result in results + failures if isinstance(result.get("latency_ms"), (int, float))]
    median_latency = _upper_median(latencies)
    report = {
        "system": "nimble_native_systemone",
        "endpoint": endpoint,
        "condition": condition,
        "requested": len(dossiers),
        "completed": len(results),
        "failure_count": len(failures),
        "failure_rate": len(failures) / len(dossiers) if dossiers else None,
        "reused_identical_predictions": reused_count,
        "median_latency_ms": median_latency,
        "p95_latency_ms": _nearest_rank_percentile(latencies, 0.95),
        "estimated_seconds_for_100_median": round((median_latency / 1000) * 100, 3) if median_latency is not None else None,
        "estimated_seconds_for_1000_median": round((median_latency / 1000) * 1000, 3) if median_latency is not None else None,
        "sum_of_item_latency_seconds": round(sum(latencies) / 1000, 3),
        "wall_elapsed_seconds": round(time.perf_counter() - run_started, 3),
        "request_bytes_median": _upper_median(request_sizes),
        "request_bytes_p95": _nearest_rank_percentile(request_sizes, 0.95),
        "state_bytes_median": _upper_median(state_sizes),
        "state_bytes_p95": _nearest_rank_percentile(state_sizes, 0.95),
        "usage_totals": _usage_totals(results),
        "prediction_files": results,
        "failures": failures,
        "latency_note": "Reused identical predictions contribute their original measured item latency; wall_elapsed_seconds records this invocation's elapsed time.",
        "note": "Probabilities are model scores and are not assumed calibrated for this benchmark.",
    }
    report_path = write_immutable_json(root / "reports" / f"nimble_{condition}_run.json", report)
    return {**report, "report_path": str(report_path)}


def _reject_invalid_jev_citations(answers: dict, sample_ids: set[str]) -> dict:
    """Apply the production citation rules to frozen benchmark answers."""
    for question_id, answer in answers.items():
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            continue
        if answer.get("status") not in {"supported", "mixed", "provisional"}:
            continue
        supporting = list(answer.get("supporting_line_ids") or [])
        counterexamples = list(answer.get("counterexample_line_ids") or [])
        invalid = [
            line_id for line_id in supporting + counterexamples
            if line_id not in sample_ids
        ]
        reason = None
        if invalid:
            reason = f"Cited line IDs absent from the request: {invalid[:3]}."
        elif len(supporting) < 2:
            reason = "Supported Style fields require at least two cited lines."
        elif answer.get("status") == "mixed" and not counterexamples:
            reason = "A mixed result requires at least one counterexample line."
        if reason:
            answer["choice"] = None
            if "label" in answer:
                answer["label"] = None
            answer["status"] = "rejected"
            answer["rationale"] = reason
    return answers


def run_jev(
    *,
    root: Path | str = DEFAULT_ROOT,
    endpoint: str = "http://127.0.0.1:8081/v1/systemone",
    model: str = "jev-1.14.0",
    api_key_env: str = "TYPESAFE_API_KEY",
    limit: int | None = None,
    only: set[str] | None = None,
    timeout: float = 600,
    force: bool = False,
) -> dict:
    """Run the production identity-blind JEV classifier over frozen benchmark dossiers."""
    root = Path(root)
    prediction_dir = root / "predictions" / SYSTEM_PREDICTION_DIRS["jev"]
    manifest = read_json(root / "manifest.json")
    dossiers = manifest.get("public_dossiers") or []
    if only:
        dossiers = [entry for entry in dossiers if entry["benchmark_id"] in only]
    if limit is not None:
        dossiers = dossiers[:limit]
    questions = build_style_questions(blueprint_renderer.load_vocabulary())
    provider = JevProvider(
        endpoint=endpoint,
        model=model,
        api_key_env=api_key_env,
        timeout=timeout,
        vocabulary=blueprint_renderer.load_vocabulary(),
    )
    results = []
    failures = []
    reused_count = 0
    request_sizes = []
    state_sizes = []
    run_started = time.perf_counter()
    for entry in dossiers:
        dossier = read_json(Path(entry["dossier_path"]))
        state = build_speech_style_state(dossier, condition="text_only")
        state_bytes = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        body = json.dumps(
            {"model": model, "state": state, "questions": questions},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request_bytes = len(body)
        request_sizes.append(request_bytes)
        state_sizes.append(state_bytes)
        request_sha = hashlib.sha256(body).hexdigest()
        target_path = prediction_dir / f"{entry['benchmark_id']}.json"
        if target_path.is_file() and not force:
            prior = read_json(target_path)
            if prior.get("request_sha256") == request_sha:
                reused_count += 1
                results.append({
                    "benchmark_id": entry["benchmark_id"],
                    "latency_ms": prior.get("latency_ms"),
                    "request_bytes": request_bytes,
                    "state_bytes": state_bytes,
                    "usage": prior.get("usage") or {},
                    "prediction_path": str(target_path),
                    "reused": True,
                })
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
                continue
        started = time.perf_counter()
        try:
            payload = provider.classify_state(state, questions)
            state_ids = {
                str(sample.get("id"))
                for sample in state.get("dialogue_samples") or []
                if sample.get("id")
            }
            _reject_invalid_jev_citations(payload.get("answers") or {}, state_ids)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            record = {
                "benchmark_id": entry["benchmark_id"],
                "system": "jev",
                "model": payload.get("model") or model,
                "condition": "text_only",
                "endpoint": endpoint,
                "latency_ms": elapsed_ms,
                "request_sha256": request_sha,
                "input_hash": entry["input_hash"],
                "sample_line_ids": [
                    sample["id"] for sample in state.get("dialogue_samples") or []
                ],
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "usage": payload.get("usage") or {},
                "answers": payload["answers"],
            }
            path = write_immutable_json(target_path, record)
            results.append({
                "benchmark_id": entry["benchmark_id"],
                "latency_ms": elapsed_ms,
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "usage": record["usage"],
                "prediction_path": str(path),
                "reused": False,
            })
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        except (JevError, ValueError, OSError) as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            failure = {
                "benchmark_id": entry["benchmark_id"],
                "system": "jev",
                "condition": "text_only",
                "endpoint": endpoint,
                "error": f"{type(exc).__name__}: {exc}",
                "request_sha256": request_sha,
                "input_hash": entry["input_hash"],
                "latency_ms": elapsed_ms,
            }
            path = write_immutable_json(
                target_path.with_name(f"{entry['benchmark_id']}.failure.json"), failure,
            )
            failures.append({**failure, "failure_path": str(path)})
            print(json.dumps(failures[-1], ensure_ascii=False), flush=True)
    report = {
        "system": "jev",
        "condition": "text_only",
        "benchmark": BENCHMARK_VERSION,
        "model_requested": model,
        "endpoint": endpoint,
        "run_started": dt.datetime.now(dt.timezone.utc).isoformat(),
        "wall_elapsed_seconds": round(time.perf_counter() - run_started, 3),
        "request_count": len(results) + len(failures),
        "reused_count": reused_count,
        "failure_count": len(failures),
        "failures": failures,
        "request_bytes_median": _upper_median(request_sizes),
        "request_bytes_p95": _nearest_rank_percentile(request_sizes, 0.95),
        "state_bytes_median": _upper_median(state_sizes),
        "state_bytes_p95": _nearest_rank_percentile(state_sizes, 0.95),
        "usage_totals": _usage_totals(results),
        "prediction_files": results,
        "latency_note": "Reused identical predictions contribute their original measured item latency; wall_elapsed_seconds records this invocation's elapsed time.",
    }
    report_path = write_immutable_json(root / "reports" / "jev_run.json", report)
    return {**report, "report_path": str(report_path)}


def run_nimble_stability(
    *,
    root: Path | str = DEFAULT_ROOT,
    cache: Path | str = "data/blueprints/cache",
    endpoint: str = "http://127.0.0.1:18081/v1/systemone",
    condition: str = "assisted",
    split: str = "locked_test",
    only: set[str] | None = None,
    timeout: float = 600,
) -> dict:
    """Run predeclared sample perturbations without tuning on the selected split."""
    if split not in {"development", "locked_test"}:
        raise ValueError("split must be 'development' or 'locked_test'")
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    public = {item["benchmark_id"]: item for item in manifest["public_dossiers"]}
    selection = [row for row in manifest["private_selection"] if row["split"] == split]
    if only:
        selection = [row for row in selection if row["benchmark_id"] in only]
    if not selection:
        raise ValueError("no development NPCs selected for stability runs")
    extended_records = build_dossiers(
        manifest["database_path"],
        out=cache,
        only={row["canonical_id"] for row in selection},
        sample_count=36,
    )
    extended_by_id = {row["canonical_id"]: read_json(Path(row["path"])) for row in extended_records}
    questions = build_questions()
    stability_root = root / "predictions" / "nimble" / "stability" / condition
    stability_manifest = []
    result_rows = []
    baseline_labels: dict[str, dict] = {}
    agreements: dict[str, list[bool]] = {}
    probability_deltas = []
    started_all = time.perf_counter()

    for identity in selection:
        benchmark_id = identity["benchmark_id"]
        base_entry = public[benchmark_id]
        original = read_json(Path(base_entry["dossier_path"]))
        extended_source = extended_by_id[identity["canonical_id"]]
        original_samples = original["dialogue_samples"]
        extended_samples = extended_source.get("dialogue_samples") or []
        variants: dict[str, list[dict]] = {
            "original": original_samples,
            "repeat": original_samples,
            "reordered": list(reversed(original_samples)),
            "fixed_12": original_samples[:12],
        }
        if len(extended_samples) >= 36:
            variants["alternate_24"] = extended_samples[12:36]
            variants["expanded_36"] = extended_samples[:36]
        sample_maps = {}
        for variant, source_samples in variants.items():
            anonymous = []
            source_map = []
            for index, sample in enumerate(source_samples, 1):
                if variant in {"original", "repeat", "reordered", "fixed_12"}:
                    sample_id = str(sample["id"])
                elif variant == "alternate_24":
                    sample_id = f"{benchmark_id}-A{index:02d}"
                else:
                    sample_id = f"{benchmark_id}-L{index:02d}"
                anonymous.append({"id": sample_id, "text": str(sample.get("text") or "")})
                source_map.append({
                    "sample_id": sample_id,
                    "dialog_id": sample.get("dialog_id"),
                    "voice_line_id": sample.get("voice_line_id"),
                    "source": sample.get("source"),
                    "sheet": sample.get("sheet"),
                    "row_id": sample.get("row_id"),
                })
            sample_maps[variant] = source_map
            temporary_dossier = {
                "benchmark_id": benchmark_id,
                "dialogue_samples": anonymous,
                "sample_measurements": {},
            }
            state = build_speech_style_state(temporary_dossier, condition=condition)
            body = json.dumps({"state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")
            request = Request(endpoint, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
            started = time.perf_counter()
            try:
                with urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                    status = response.status
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                payload = json.loads(raw.decode("utf-8"))
                answers = _validate_choice_response(
                    payload, questions,
                    [sample["id"] for sample in anonymous],
                )
                record = {
                    "benchmark_id": benchmark_id,
                    "variant": variant,
                    "system": "nimble_native_systemone",
                    "model": payload["model"],
                    "model_sha256": (manifest.get("model") or {}).get("sha256"),
                    "quantization": "Q4_K_M",
                    "llama_cpp_revision": "dde879bc4f954b809892fd0d660515f42536394a",
                    "configuration_sha256": object_hash({"fields": FIELDS, "instructions": QUESTION_INSTRUCTIONS, "condition": condition}),
                    "condition": condition,
                    "http_status": status,
                    "latency_ms": elapsed_ms,
                    "request_sha256": hashlib.sha256(body).hexdigest(),
                    "input_hash": object_hash(temporary_dossier),
                    "sample_line_ids": [sample["id"] for sample in anonymous],
                    "state_bytes": len(json.dumps(state, ensure_ascii=False).encode("utf-8")),
                    "usage": payload.get("usage") or {},
                    "answers": answers,
                }
                target = stability_root / benchmark_id / f"{variant}.json"
                path = write_immutable_json(target, record)
                result_rows.append({"benchmark_id": benchmark_id, "variant": variant, "latency_ms": elapsed_ms, "prediction_path": str(path)})
                if variant == "original":
                    baseline_labels[benchmark_id] = answers
                elif benchmark_id in baseline_labels:
                    agrees = [answers[field]["label"] == baseline_labels[benchmark_id][field]["label"] for field in FIELDS]
                    agreements.setdefault(variant, []).extend(agrees)
                    if variant == "repeat":
                        probability_deltas.extend(
                            abs(answers[field]["probabilities"][label] - baseline_labels[benchmark_id][field]["probabilities"][label])
                            for field in FIELDS
                            for label in FIELDS[field]
                        )
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                failure = {
                    "benchmark_id": benchmark_id,
                    "variant": variant,
                    "system": "nimble_native_systemone",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "latency_ms": elapsed_ms,
                    "request_sha256": hashlib.sha256(body).hexdigest(),
                    "sample_line_ids": [sample["id"] for sample in anonymous],
                }
                path = write_immutable_json(stability_root / benchmark_id / f"{variant}.failure.json", failure)
                result_rows.append({"benchmark_id": benchmark_id, "variant": variant, "failure_path": str(path), "error_type": type(exc).__name__})
            print(json.dumps(result_rows[-1], ensure_ascii=False), flush=True)
        stability_manifest.append({
            "benchmark_id": benchmark_id,
            "split": split,
            "variants": {variant: source_map for variant, source_map in sample_maps.items()},
        })

    variant_summary = {}
    for variant, values in agreements.items():
        variant_summary[variant] = {
            "field_decisions_compared": len(values),
            "agreement_with_original": sum(values) / len(values) if values else None,
        }
    if probability_deltas:
        variant_summary["repeat_probability_max_abs_delta"] = max(probability_deltas)
        variant_summary["repeat_probability_mean_abs_delta"] = sum(probability_deltas) / len(probability_deltas)
    elapsed_total = round(time.perf_counter() - started_all, 3)
    artifact_manifest = write_immutable_json(root / "reports" / f"stability_manifest_{condition}.json", {
        "benchmark": BENCHMARK_VERSION,
        "condition": condition,
        "split": split,
        "npc_count": len(selection),
        "source_maps": stability_manifest,
    })
    report = {
        "system": "nimble_native_systemone",
        "endpoint": endpoint,
        "condition": condition,
        "split": split,
        "npc_count": len(selection),
        "request_count": len(result_rows),
        "failure_count": sum("failure_path" in row for row in result_rows),
        "elapsed_seconds": elapsed_total,
        "variant_agreement": variant_summary,
        "requests": result_rows,
        "stability_manifest_path": str(artifact_manifest),
        "note": "Robustness results are descriptive only. Do not use locked-test outputs to tune prompts or thresholds.",
    }
    report_path = write_immutable_json(root / "reports" / f"nimble_stability_{condition}.json", report)
    return {**report, "report_path": str(report_path)}


def analyze_nimble_stability(
    *,
    root: Path | str = DEFAULT_ROOT,
    report_path: Path | str,
) -> dict:
    """Summarize per-field decision and score stability from a completed run."""
    root = Path(root)
    report_path = Path(report_path)
    report = read_json(report_path)
    predictions: dict[tuple[str, str], dict] = {}
    for row in report.get("requests") or []:
        path = row.get("prediction_path")
        if path:
            record = read_json(Path(path))
            predictions[(record["benchmark_id"], record["variant"])] = record

    variants = sorted({variant for _, variant in predictions} - {"original"})
    per_variant: dict[str, dict] = {}
    for variant in variants:
        by_field: dict[str, dict] = {}
        for field in FIELDS:
            compared = []
            for (benchmark_id, base_variant), baseline in predictions.items():
                if base_variant != "original":
                    continue
                other = predictions.get((benchmark_id, variant))
                if other is None:
                    continue
                left = baseline["answers"][field]["label"]
                right = other["answers"][field]["label"]
                compared.append((benchmark_id, left, right))
            changed = [benchmark_id for benchmark_id, left, right in compared if left != right]
            row = {
                "n": len(compared),
                "agreement_rate": (len(compared) - len(changed)) / len(compared) if compared else None,
                "changed_count": len(changed),
                "changed_benchmark_ids": changed,
            }
            if variant == "repeat":
                probability_deltas = []
                for (benchmark_id, base_variant), original in predictions.items():
                    if base_variant != "original":
                        continue
                    repeated = predictions.get((benchmark_id, variant))
                    if repeated is None:
                        continue
                    original_probs = original["answers"][field]["probabilities"]
                    repeated_probs = repeated["answers"][field]["probabilities"]
                    probability_deltas.extend(
                        abs(original_probs[label] - repeated_probs[label])
                        for label in FIELDS[field]
                    )
                row["probability_delta_mean_abs"] = (
                    sum(probability_deltas) / len(probability_deltas) if probability_deltas else None
                )
                row["probability_delta_max_abs"] = max(probability_deltas) if probability_deltas else None
            by_field[field] = row
        per_variant[variant] = by_field

    analysis = {
        "benchmark": BENCHMARK_VERSION,
        "system": report.get("system"),
        "condition": report.get("condition"),
        "split": report.get("split"),
        "npc_count": report.get("npc_count"),
        "source_report": str(report_path),
        "source_report_sha256": _digest_file(report_path),
        "per_variant_per_field": per_variant,
        "note": "Agreement compares each robustness variant with its same-NPC original frozen input; these results are descriptive and are not used to tune the locked test set.",
    }
    output_path = write_immutable_json(root / "reports" / f"nimble_stability_field_analysis_{report.get('condition', 'assisted')}.json", analysis)
    return {**analysis, "report_path": str(output_path)}


def audit_rules_baseline_provenance(*, root: Path | str = DEFAULT_ROOT) -> dict:
    """Compare frozen rule-record hashes with their actual evidence sources."""
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    public = {row["benchmark_id"]: row for row in manifest["public_dossiers"]}
    findings = []
    for selection in manifest["private_selection"]:
        benchmark_id = selection["benchmark_id"]
        source_dossier = read_json(Path(selection["source_dossier_path"]))
        expected = {
            "rules_full": source_dossier.get("input_hash"),
            "rules_sample": public[benchmark_id].get("input_hash"),
        }
        for system, expected_hash in expected.items():
            prediction_path = root / "predictions" / system / f"{benchmark_id}.json"
            prediction = read_json(prediction_path)
            recorded_hash = prediction.get("input_hash")
            findings.append({
                "benchmark_id": benchmark_id,
                "system": system,
                "prediction_path": str(prediction_path),
                "prediction_sha256": _digest_file(prediction_path),
                "recorded_input_hash": recorded_hash,
                "expected_input_hash": expected_hash,
                "matches_expected": recorded_hash == expected_hash,
            })
    report = {
        "benchmark": BENCHMARK_VERSION,
        "prediction_count": len(findings),
        "mismatch_count": sum(not row["matches_expected"] for row in findings),
        "findings": findings,
        "note": "This audit does not rewrite frozen predictions. Full rules records should hash their source dossier; sample rules records should hash the frozen anonymous dossier.",
    }
    report_path = write_immutable_json(root / "reports" / "baseline_input_provenance.json", report)
    return {**report, "report_path": str(report_path)}


def run_qwen_base(
    *,
    root: Path | str = DEFAULT_ROOT,
    endpoint: str = "http://127.0.0.1:18082/v1/chat/completions",
    condition: str = "assisted",
    model: str = "Qwen3.5-9B-Base-Q4_K_M",
    model_path: Path | str = "data/models/qwen3.5-9b/Qwen3.5-9B-Base.Q4_K_M.gguf",
    limit: int | None = None,
    only: set[str] | None = None,
    timeout: float = 600,
) -> dict:
    """Run constrained JSON generation using the separate Qwen base GGUF."""
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")
    root = Path(root)
    prediction_dir = root / "predictions" / SYSTEM_PREDICTION_DIRS[f"qwen_base_{condition}"]
    model_path = Path(model_path)
    model_hash = _digest_file(model_path) if model_path.is_file() else None
    manifest = read_json(root / "manifest.json")
    dossiers = manifest.get("public_dossiers") or []
    if only:
        dossiers = [d for d in dossiers if d["benchmark_id"] in only]
    if limit is not None:
        dossiers = dossiers[:limit]
    properties = {
        field: {"type": "string", "enum": list(options)}
        for field, options in FIELDS.items()
    }
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(FIELDS),
        "additionalProperties": False,
    }
    rubric = "\n".join(
        f"{field}: " + "; ".join(f"{label} = {description}" for label, description in options.items())
        for field, options in FIELDS.items()
    )
    system = (
        "Classify only observable wording in the supplied dialogue. Do not infer voice acoustics, identity, "
        "demographics, occupation, status, or personality. For each field select the predominant label; "
        "use mixed for recurring contrasting patterns and insufficient_evidence when the text is too sparse. "
        "Return only the requested JSON object.\n\nRubric:\n" + rubric
    )
    results = []
    request_sizes = []
    state_sizes = []
    reused_count = 0
    run_started = time.perf_counter()
    for entry in dossiers:
        dossier = read_json(Path(entry["dossier_path"]))
        state = build_speech_style_state(dossier, condition=condition)
        state_bytes = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        user_content = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        body_value = {
            "model": model,
            "model_source": "mradermacher/Qwen3.5-9B-Base-GGUF (Q4_K_M quant of Qwen/Qwen3.5-9B-Base)",
            "model_path": str(model_path),
            "model_sha256": model_hash,
            "quantization": "Q4_K_M",
            "configuration_sha256": object_hash({"fields": FIELDS, "system": system, "schema": schema, "condition": condition}),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_schema", "schema": schema},
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0,
            "top_p": 1,
            "max_tokens": 512,
            "stream": False,
        }
        body = json.dumps(body_value, ensure_ascii=False).encode("utf-8")
        request_bytes = len(body)
        request_sizes.append(request_bytes)
        state_sizes.append(state_bytes)
        request_hash = hashlib.sha256(body).hexdigest()
        target = prediction_dir / f"{entry['benchmark_id']}.json"
        if target.exists():
            existing = read_json(target)
            if existing.get("request_sha256") == request_hash:
                reused_count += 1
                results.append({
                    "benchmark_id": entry["benchmark_id"],
                    "latency_ms": existing.get("latency_ms"),
                    "request_bytes": request_bytes,
                    "state_bytes": state_bytes,
                    "usage": existing.get("usage") or {},
                    "prediction_path": str(target),
                    "reused": True,
                })
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
                continue
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        started = time.perf_counter()
        response_payload = None
        generated = None
        status = None
        finish_reason = None
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
            response_payload = json.loads(raw.decode("utf-8"))
            choices = response_payload.get("choices") or []
            if not choices or not isinstance(choices[0].get("message", {}).get("content"), str):
                raise ValueError("response lacks a string message content")
            finish_reason = choices[0].get("finish_reason")
            generated = choices[0]["message"]["content"]
            if not generated.strip():
                raise ValueError(f"response content is empty (finish_reason={finish_reason!r})")
            labels = json.loads(generated)
            if not isinstance(labels, dict) or set(labels) != set(FIELDS):
                raise ValueError("Qwen base returned the wrong JSON fields")
            for field, label in labels.items():
                if label not in FIELDS[field]:
                    raise ValueError(f"Qwen base returned invalid {field} label {label!r}")
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            record = {
                "benchmark_id": entry["benchmark_id"],
                "system": "qwen3_5_9b_base_constrained_generation",
                "model": model,
                "condition": condition,
                "endpoint": endpoint,
                "http_status": status,
                "finish_reason": finish_reason,
                "latency_ms": elapsed_ms,
                "request_sha256": request_hash,
                "input_hash": entry["input_hash"],
                "sample_line_ids": entry["sample_line_ids"],
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "usage": response_payload.get("usage") or {},
                "labels": labels,
                "probabilities": None,
                "confidence_note": "Constrained text generation returns labels only; no confidence distribution is claimed.",
                "raw_generation": generated,
            }
            path = write_immutable_json(target, record)
            results.append({
                "benchmark_id": entry["benchmark_id"],
                "latency_ms": elapsed_ms,
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "usage": record["usage"],
                "prediction_path": str(path),
            })
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            failure = {
                "benchmark_id": entry["benchmark_id"],
                "system": "qwen3_5_9b_base_constrained_generation",
                "model": model,
                "endpoint": endpoint,
                "http_status": status,
                "finish_reason": finish_reason,
                "latency_ms": elapsed_ms,
                "request_sha256": request_hash,
                "input_hash": entry["input_hash"],
                "error_type": type(exc).__name__,
                "error": str(exc),
                "raw_generation": generated,
                "usage": (response_payload or {}).get("usage") or {},
            }
            path = write_immutable_json(prediction_dir / f"{entry['benchmark_id']}.failure.json", failure)
            results.append({
                "benchmark_id": entry["benchmark_id"],
                "latency_ms": elapsed_ms,
                "request_bytes": request_bytes,
                "state_bytes": state_bytes,
                "usage": failure["usage"],
                "failure_path": str(path),
                "error_type": type(exc).__name__,
            })
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    latencies = [result["latency_ms"] for result in results if isinstance(result.get("latency_ms"), (int, float))]
    median_latency = _upper_median(latencies)
    report = {
        "system": "qwen3_5_9b_base_constrained_generation",
        "model": model,
        "condition": condition,
        "endpoint": endpoint,
        "requested": len(dossiers),
        "completed": sum("prediction_path" in result for result in results),
        "failure_count": sum("failure_path" in result for result in results),
        "failure_rate": sum("failure_path" in result for result in results) / len(dossiers) if dossiers else None,
        "reused_identical_predictions": reused_count,
        "median_latency_ms": median_latency,
        "p95_latency_ms": _nearest_rank_percentile(latencies, 0.95),
        "estimated_seconds_for_100_median": round((median_latency / 1000) * 100, 3) if median_latency is not None else None,
        "estimated_seconds_for_1000_median": round((median_latency / 1000) * 1000, 3) if median_latency is not None else None,
        "sum_of_item_latency_seconds": round(sum(latencies) / 1000, 3),
        "wall_elapsed_seconds": round(time.perf_counter() - run_started, 3),
        "request_bytes_median": _upper_median(request_sizes),
        "request_bytes_p95": _nearest_rank_percentile(request_sizes, 0.95),
        "state_bytes_median": _upper_median(state_sizes),
        "state_bytes_p95": _nearest_rank_percentile(state_sizes, 0.95),
        "usage_totals": _usage_totals(results),
        "prediction_files": results,
        "latency_note": "Reused identical predictions contribute their original measured item latency; wall_elapsed_seconds records this invocation's elapsed time.",
    }
    report_path = write_immutable_json(root / "reports" / f"qwen_base_{condition}_run.json", report)
    return {**report, "report_path": str(report_path)}


def _cohen_kappa(left: list[str], right: list[str], order: list[str] | None = None) -> float | None:
    if not left or len(left) != len(right):
        return None
    labels = order or sorted(set(left) | set(right))
    n = len(left)
    observed = sum(a == b for a, b in zip(left, right)) / n
    left_counts = {label: left.count(label) for label in labels}
    right_counts = {label: right.count(label) for label in labels}
    expected = sum(left_counts[label] * right_counts[label] for label in labels) / (n * n)
    return (observed - expected) / (1 - expected) if expected < 1 else (1.0 if observed == 1 else 0.0)


def _weighted_cohen_kappa(left: list[str], right: list[str], order: list[str]) -> float | None:
    """Linear weighted kappa; one-rank errors cost less than larger errors."""
    if not left or len(left) != len(right) or len(order) < 2:
        return None
    rank = {label: index for index, label in enumerate(order)}
    if any(label not in rank for label in (*left, *right)):
        raise ValueError("weighted kappa inputs must all be in the supplied ordinal order")
    n = len(left)
    observed_disagreement = 0.0
    expected_disagreement = 0.0
    for actual in order:
        actual_count = sum(value == actual for value in left)
        for predicted in order:
            predicted_count = sum(value == predicted for value in right)
            weight = abs(rank[actual] - rank[predicted]) / (len(order) - 1)
            observed_disagreement += weight * sum(a == actual and p == predicted for a, p in zip(left, right)) / n
            expected_disagreement += weight * actual_count * predicted_count / (n * n)
    if expected_disagreement == 0:
        return 1.0 if observed_disagreement == 0 else 0.0
    return 1.0 - observed_disagreement / expected_disagreement


def _ordinal_metrics(actual: list[str], predicted: list[str], order: list[str]) -> dict:
    if len(actual) != len(predicted):
        raise ValueError("ordinal actual and predicted labels must have equal lengths")
    pairs = [(a, p) for a, p in zip(actual, predicted) if a in order and p in order]
    if not pairs:
        return {
            "n": 0,
            "excluded_count": len(actual),
            "weighted_cohen_kappa_linear": None,
            "mean_absolute_label_distance": None,
        }
    rank = {label: index for index, label in enumerate(order)}
    left = [a for a, _ in pairs]
    right = [p for _, p in pairs]
    distance = sum(abs(rank[a] - rank[p]) for a, p in pairs) / len(pairs)
    return {
        "n": len(pairs),
        "excluded_count": len(actual) - len(pairs),
        "weighted_cohen_kappa_linear": _weighted_cohen_kappa(left, right, order),
        "mean_absolute_label_distance": distance,
        "distance_unit": "ordinal steps",
    }


def _confusion_metrics(gold: list[str], predicted: list[str], labels: list[str]) -> dict:
    all_classes = sorted(set(labels) | set(gold) | set(predicted))
    matrix = {actual: {guess: 0 for guess in all_classes} for actual in all_classes}
    for actual, guess in zip(gold, predicted):
        matrix[actual][guess] += 1
    per_class = {}
    for label in all_classes:
        tp = matrix.get(label, {}).get(label, 0)
        fp = sum(matrix.get(actual, {}).get(label, 0) for actual in all_classes if actual != label)
        support = sum(matrix.get(label, {}).values())
        fn = support - tp
        predicted_count = tp + fp
        precision = tp / predicted_count if predicted_count else None
        recall = tp / support if support else None
        f1_denominator = 2 * tp + fp + fn
        f1 = 2 * tp / f1_denominator if f1_denominator else None
        per_class[label] = {
            "support": support,
            "predicted_count": predicted_count,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    present = [label for label in all_classes if per_class[label]["support"]]
    predicted_present = [label for label in all_classes if per_class[label]["predicted_count"]]
    macro_labels = sorted(set(gold) | set(predicted))
    macro_values = [per_class[label]["f1"] for label in macro_labels if per_class[label]["f1"] is not None]
    n = len(gold)
    return {
        "n": n,
        "accuracy": sum(a == p for a, p in zip(gold, predicted)) / n if n else None,
        "balanced_accuracy": sum(per_class[label]["recall"] for label in present) / len(present) if present else None,
        "macro_f1": sum(macro_values) / len(macro_values) if macro_values else None,
        "labels_in_macro_f1": macro_labels,
        "labels_present_in_gold": present,
        "labels_present_in_predictions": predicted_present,
        "per_class": per_class,
        "confusion_matrix": matrix,
    }


def _bootstrap_intervals(
    gold: list[str],
    predicted: list[str],
    labels: list[str],
    *,
    seed: int,
    draws: int = 1500,
    ordinal_order: list[str] | None = None,
) -> dict:
    if not gold:
        return {}
    rng = random.Random(seed)
    accuracy_values = []
    macro_values = []
    ordinal_kappa_values = []
    ordinal_distance_values = []
    per_class_values = {
        label: {metric: [] for metric in ("precision", "recall", "f1")}
        for label in sorted(set(labels) | set(gold) | set(predicted))
    }
    bootstrap_labels = sorted(set(labels) | set(gold) | set(predicted))
    n = len(gold)
    for _ in range(draws):
        indices = [rng.randrange(n) for _ in range(n)]
        metrics = _confusion_metrics([gold[i] for i in indices], [predicted[i] for i in indices], bootstrap_labels)
        if metrics["accuracy"] is not None:
            accuracy_values.append(metrics["accuracy"])
        if metrics["macro_f1"] is not None:
            macro_values.append(metrics["macro_f1"])
        for label in per_class_values:
            for metric in per_class_values[label]:
                value = metrics["per_class"][label][metric]
                if value is not None:
                    per_class_values[label][metric].append(value)
        if ordinal_order:
            ordinal = _ordinal_metrics([gold[i] for i in indices], [predicted[i] for i in indices], ordinal_order)
            if ordinal["weighted_cohen_kappa_linear"] is not None:
                ordinal_kappa_values.append(ordinal["weighted_cohen_kappa_linear"])
            if ordinal["mean_absolute_label_distance"] is not None:
                ordinal_distance_values.append(ordinal["mean_absolute_label_distance"])
    def interval(values: list[float]) -> list[float] | None:
        if not values:
            return None
        values.sort()
        lo = max(0, math.floor(0.025 * len(values)))
        hi = min(len(values) - 1, math.ceil(0.975 * len(values)) - 1)
        return [values[lo], values[hi]]
    result = {
        "accuracy_95_percentile_ci": interval(accuracy_values),
        "macro_f1_95_percentile_ci": interval(macro_values),
        "bootstrap_draws": draws,
    }
    if ordinal_order:
        result["ordinal_weighted_kappa_linear_95_percentile_ci"] = interval(ordinal_kappa_values)
        result["ordinal_mean_absolute_label_distance_95_percentile_ci"] = interval(ordinal_distance_values)
    result["per_class_95_percentile_ci"] = {
        label: {metric: interval(values) for metric, values in metrics.items()}
        for label, metrics in per_class_values.items()
    }
    result["per_class_interval_defined_draws"] = {
        label: {metric: len(values) for metric, values in metrics.items()}
        for label, metrics in per_class_values.items()
    }
    result["per_class_interval_note"] = "Per-class intervals omit resamples where that metric has a zero denominator."
    return result


def _bootstrap_acceptance_intervals(
    rows: list[tuple[bool, bool]],
    *,
    seed: int,
    draws: int = 1500,
) -> dict:
    """Bootstrap test coverage and precision for fixed development-set decisions."""
    if not rows:
        return {
            "coverage_95_percentile_ci": None,
            "accepted_precision_95_percentile_ci": None,
            "bootstrap_draws": draws,
            "accepted_precision_defined_draws": 0,
            "accepted_precision_interval_note": "Precision intervals condition on bootstrap samples with at least one accepted item.",
        }
    rng = random.Random(seed)
    n = len(rows)
    coverages = []
    precisions = []
    for _ in range(draws):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        accepted = [row for row in sample if row[1]]
        coverages.append(len(accepted) / n)
        if accepted:
            precisions.append(sum(correct for correct, _is_accepted in accepted) / len(accepted))

    def interval(values: list[float]) -> list[float] | None:
        if not values:
            return None
        values.sort()
        low = max(0, math.floor(0.025 * len(values)))
        high = min(len(values) - 1, math.ceil(0.975 * len(values)) - 1)
        return [values[low], values[high]]

    return {
        "coverage_95_percentile_ci": interval(coverages),
        "accepted_precision_95_percentile_ci": interval(precisions),
        "bootstrap_draws": draws,
        "accepted_precision_defined_draws": len(precisions),
        "accepted_precision_interval_note": "Precision intervals condition on bootstrap samples with at least one accepted item.",
    }


def _bootstrap_macro_f1_margin(
    gold: list[str],
    candidate: list[str],
    rule_baselines: dict[str, list[str]],
    labels: list[str],
    *,
    seed: int,
    draws: int = 1500,
) -> dict:
    if not gold or len(candidate) != len(gold) or not rule_baselines:
        return {"n": 0, "margin": None, "confidence_interval_95_percentile": None}
    if any(len(predictions) != len(gold) for predictions in rule_baselines.values()):
        raise ValueError("paired baseline predictions must align with candidate and gold labels")
    candidate_f1 = _confusion_metrics(gold, candidate, labels)["macro_f1"]
    baseline_f1 = {
        name: _confusion_metrics(gold, predictions, labels)["macro_f1"]
        for name, predictions in rule_baselines.items()
    }
    if candidate_f1 is None or not any(value is not None for value in baseline_f1.values()):
        return {"n": len(gold), "margin": None, "confidence_interval_95_percentile": None}
    best_baseline_name = max((name for name, value in baseline_f1.items() if value is not None), key=lambda name: baseline_f1[name])
    margin = candidate_f1 - baseline_f1[best_baseline_name]
    rng = random.Random(seed)
    margins = []
    n = len(gold)
    for _ in range(draws):
        indices = [rng.randrange(n) for _ in range(n)]
        sampled_gold = [gold[index] for index in indices]
        candidate_metric = _confusion_metrics(sampled_gold, [candidate[index] for index in indices], labels)["macro_f1"]
        baseline_metrics = [
            _confusion_metrics(sampled_gold, [predictions[index] for index in indices], labels)["macro_f1"]
            for predictions in rule_baselines.values()
        ]
        available_baselines = [value for value in baseline_metrics if value is not None]
        if candidate_metric is not None and available_baselines:
            margins.append(candidate_metric - max(available_baselines))
    margins.sort()
    if margins:
        low = max(0, math.floor(0.025 * len(margins)))
        high = min(len(margins) - 1, math.ceil(0.975 * len(margins)) - 1)
        interval = [margins[low], margins[high]]
    else:
        interval = None
    return {
        "n": n,
        "candidate_macro_f1": candidate_f1,
        "rule_baseline_macro_f1": baseline_f1,
        "best_rule_baseline": best_baseline_name,
        "margin": margin,
        "confidence_interval_95_percentile": interval,
        "bootstrap_draws": draws,
        "baseline_selection": "best-performing rules baseline on the same locked-test cases",
    }


def _prediction_label(answer: dict) -> str | None:
    """Normalize benchmark answers; an abstention is a label even without a choice."""
    if not isinstance(answer, dict):
        return None
    if answer.get("status") == "insufficient_evidence":
        return "insufficient_evidence"
    return answer.get("label") or answer.get("choice")


def _validated_probabilities(answer: dict, labels: list[str]) -> dict[str, float] | None:
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(labels):
        return None
    values = {}
    for label in labels:
        value = probabilities[label]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            return None
        values[label] = float(value)
    if not math.isclose(sum(values.values()), 1.0, rel_tol=0.0, abs_tol=0.001):
        return None
    return values


def _calibration_report(gold: list[str], prediction_records: list[dict], field: str, labels: list[str]) -> dict | None:
    rows = []
    for actual, record in zip(gold, prediction_records):
        if not isinstance(record, dict):
            continue
        answer = (record.get("answers") or {}).get(field)
        if answer is None:
            continue
        probabilities = _validated_probabilities(answer, labels)
        if probabilities is None:
            continue
        reported_choice = _prediction_label(answer)
        rows.append((actual, probabilities, reported_choice))
    if not rows:
        return None
    brier = sum(
        sum((probabilities[label] - (1.0 if actual == label else 0.0)) ** 2 for label in labels)
        for actual, probabilities, _reported_choice in rows
    ) / len(rows)
    bucket_rows = []
    ece = 0.0
    confidence_rows = []
    decision_distribution_mismatches = 0
    for bucket in range(10):
        selected = []
        for actual, probabilities, _reported_choice in rows:
            label = max(labels, key=lambda key: probabilities[key])
            confidence = probabilities[label]
            if (bucket / 10 <= confidence < (bucket + 1) / 10) or (bucket == 9 and confidence == 1):
                selected.append((actual == label, confidence))
        if selected:
            accuracy = sum(ok for ok, _ in selected) / len(selected)
            confidence = sum(c for _, c in selected) / len(selected)
            ece += len(selected) / len(rows) * abs(accuracy - confidence)
            bucket_rows.append({"range": [bucket / 10, (bucket + 1) / 10], "n": len(selected), "accuracy": accuracy, "mean_confidence": confidence})
    for actual, probabilities, reported_choice in rows:
        ranked = sorted(probabilities.values(), reverse=True)
        choice = max(labels, key=lambda key: probabilities[key])
        if reported_choice != choice:
            decision_distribution_mismatches += 1
        margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
        confidence_rows.append((actual == choice, margin))
    margin_buckets = []
    margin_ranges = ((0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 1.01))
    for lower, upper in margin_ranges:
        selected = [(correct, margin) for correct, margin in confidence_rows if lower <= margin < upper]
        if selected:
            margin_buckets.append({
                "range": [lower, min(1.0, upper)],
                "n": len(selected),
                "accuracy": sum(correct for correct, _ in selected) / len(selected),
                "mean_margin": sum(margin for _, margin in selected) / len(selected),
            })
    return {
        "n": len(rows),
        "multiclass_brier": brier,
        "ece_10_bins": ece,
        "confidence_buckets": bucket_rows,
        "top_choice_margin_accuracy_buckets": margin_buckets,
        "decision_distribution_mismatch_count": decision_distribution_mismatches,
    }


def _select_development_threshold(
    *,
    root: Path,
    system: str,
    development_ids: list[str],
    gold_by_id: dict[str, dict],
    field: str,
    labels: list[str],
    precision_target: float = 0.90,
    coverage_target: float = 0.50,
) -> dict:
    """Choose the widest-coverage threshold meeting precision and coverage on dev only."""
    rows = []
    valid_probability_count = 0
    abstention_count = 0
    decision_distribution_mismatch_count = 0
    for benchmark_id in development_ids:
        path = root / "predictions" / system / f"{benchmark_id}.json"
        if not path.is_file():
            continue
        record = read_json(path)
        answer = (record.get("answers") or {}).get(field)
        if not isinstance(answer, dict):
            continue
        probabilities = _validated_probabilities(answer, labels)
        if probabilities is None:
            continue
        valid_probability_count += 1
        predicted = max(labels, key=lambda label: probabilities[label])
        reported_choice = _prediction_label(answer)
        if predicted != reported_choice:
            decision_distribution_mismatch_count += 1
            continue
        if predicted == "insufficient_evidence":
            abstention_count += 1
            continue
        actual = gold_by_id[benchmark_id]["adjudicated_labels"][field]
        if actual not in labels:
            raise ValueError(f"adjudicated label is invalid for {benchmark_id} {field}: {actual!r}")
        rows.append((actual, predicted, probabilities[predicted]))

    if not rows:
        return {
            "method": "maximize development coverage subject to precision and coverage targets",
            "status": "no_eligible_development_scores",
            "precision_target": precision_target,
            "coverage_target": coverage_target,
            "development_n": len(development_ids),
            "valid_probability_count": valid_probability_count,
            "abstention_count": abstention_count,
            "decision_distribution_mismatch_count": decision_distribution_mismatch_count,
            "selected_threshold": None,
        }
    eligible = []
    for threshold in sorted({row[2] for row in rows}, reverse=True):
        accepted = [row for row in rows if row[2] >= threshold]
        precision = sum(actual == predicted for actual, predicted, _ in accepted) / len(accepted)
        coverage = len(accepted) / len(development_ids) if development_ids else 0.0
        if precision >= precision_target and coverage >= coverage_target:
            eligible.append((len(accepted), threshold, precision))
    if not eligible:
        status = "no_threshold_met_development_precision_and_coverage_targets"
        selected_threshold = None
        accepted_count = 0
        accepted_precision = None
    else:
        accepted_count, selected_threshold, accepted_precision = max(eligible, key=lambda row: (row[0], -row[1]))
        status = "selected"
    return {
        "method": "maximize development coverage subject to precision and coverage targets",
        "status": status,
        "precision_target": precision_target,
        "coverage_target": coverage_target,
        "development_n": len(development_ids),
        "eligible_development_decisions": len(rows),
        "valid_probability_count": valid_probability_count,
        "abstention_count": abstention_count,
        "decision_distribution_mismatch_count": decision_distribution_mismatch_count,
        "selected_threshold": selected_threshold,
        "development_accepted_count": accepted_count,
        "development_coverage": accepted_count / len(development_ids) if development_ids else None,
        "development_accepted_precision": accepted_precision,
    }


def build_share_packets(*, root: Path | str = DEFAULT_ROOT, scope: str = "pilot") -> dict:
    """Package a blind form plus the guide into a self-contained annotator ZIP."""
    if scope not in {"pilot", "full"}:
        raise ValueError("annotation scope must be 'pilot' or 'full'")
    root = Path(root)
    guide_path = root / "annotations" / "annotation_guide.md"
    if not guide_path.is_file():
        raise FileNotFoundError(f"annotation guide not found: {guide_path}")
    guide = guide_path.read_text(encoding="utf-8")
    form_root = root / "annotations" / "pilot" if scope == "pilot" else root / "annotations"
    packets = {}
    for annotator in ("annotator_a", "annotator_b"):
        form_path = form_root / f"{annotator}.json"
        if not form_path.is_file():
            raise FileNotFoundError(f"frozen form not found: {form_path}")
        form_bytes = form_path.read_bytes()
        form = json.loads(form_bytes.decode("utf-8"))
        item_count = len(form["items"])
        readme = "\n".join([
            f"# Speech-style annotation packet — {scope} form, {annotator}",
            "",
            "Annotate independently. Do not look up character names and do not compare labels with the other annotator.",
            "",
            "Files in this packet:",
            "",
            "- `annotation_form.json` — fill this in place.",
            "- `annotation_guide.md` — the full rubric, decision rules, and boundary examples.",
            "",
            "For every item and every field, fill:",
            "",
            "- `label` — one label from the rubric for that field.",
            "- `evidence_sufficient` — `yes`, `no`, or `uncertain`. It must be `no` exactly when `label` is `insufficient_evidence`.",
            "- `evidence_line_ids` — 1 to 3 line IDs from that item that support the label; at least 2 for `mixed`; empty only for `insufficient_evidence`.",
            "- `notes` — a short reason for `mixed`, `insufficient_evidence`, or any ambiguous case.",
            "",
            f"Also fill `overall_evidence_sufficiency` for the {item_count} items in this packet.",
            "",
            "Do not edit `benchmark_id`, `sample_count`, `dialogue_samples`, or any other field: the import rejects changed samples.",
            "Return the completed `annotation_form.json`, or this ZIP with the form replaced.",
            "",
            f"Items in this packet: {item_count}.",
            "",
        ])
        members = {
            "annotation_form.json": form_bytes,
            "annotation_guide.md": guide.encode("utf-8"),
            "README.md": readme.encode("utf-8"),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(members):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, members[name])
        path = _write_bytes_immutable(root / "annotations" / "share_packets" / f"{scope}_{annotator}.zip", buffer.getvalue())
        packets[annotator] = {"path": str(path), "item_count": item_count, "sha256": hashlib.sha256(buffer.getvalue()).hexdigest()}
    report = {
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "scope": scope,
        "packets": packets,
        "note": "Packets contain the blind form and guide only; no identities, strata, splits, predictions, or gold labels.",
    }
    report_path = write_immutable_json(root / "annotations" / "share_packets" / f"{scope}_manifest.json", report)
    return {**report, "report_path": str(report_path)}


def _read_annotation_source(path: Path | str) -> tuple[bytes, bytes, dict, bool]:
    """Return original bytes, form bytes, parsed form, and whether input was a ZIP."""
    source_path = Path(path)
    if source_path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError(f"annotation source is unexpectedly large: {source_path}")
    source_bytes = source_path.read_bytes()
    is_zip = source_path.suffix.lower() == ".zip" or source_bytes.startswith(b"PK\x03\x04")
    form_bytes = source_bytes
    if is_zip:
        try:
            with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
                matches = [name for name in archive.namelist() if Path(name).name == "annotation_form.json"]
                if len(matches) != 1:
                    raise ValueError("ZIP must contain exactly one annotation_form.json")
                info = archive.getinfo(matches[0])
                if info.file_size > 10 * 1024 * 1024:
                    raise ValueError("annotation_form.json is unexpectedly large")
                form_bytes = archive.read(info)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"invalid annotation ZIP: {source_path}") from exc
    try:
        payload = json.loads(form_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"annotation form is not valid UTF-8 JSON: {source_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("annotation form must be a JSON object")
    return source_bytes, form_bytes, payload, is_zip


def _expected_annotation_items(root: Path, scope: str = "full") -> tuple[dict[str, dict], dict[str, dict]]:
    if scope not in {"pilot", "full"}:
        raise ValueError("annotation scope must be 'pilot' or 'full'")
    manifest = read_json(root / "manifest.json")
    public = {row["benchmark_id"]: row for row in manifest["public_dossiers"]}
    selection = {row["benchmark_id"]: row for row in manifest["private_selection"]}
    if set(public) != set(selection):
        raise ValueError("public dossier IDs do not match the private selection")
    scope_ids = set((manifest.get("annotation_scopes") or {}).get(scope, {}).get("item_ids") or public)
    if not scope_ids <= set(public):
        raise ValueError(f"{scope} scope references benchmark IDs outside the frozen dossiers")
    expected_items = {}
    for benchmark_id in sorted(scope_ids):
        dossier = read_json(Path(public[benchmark_id]["dossier_path"]))
        expected_items[benchmark_id] = {
            "sample_count": len(dossier["dialogue_samples"]),
            "dialogue_samples": dossier["dialogue_samples"],
        }
    return expected_items, selection


def _validate_annotation_form(
    payload: dict,
    expected_items: dict[str, dict],
    *,
    label: str,
    scope: str | None = None,
) -> None:
    required_top = {"benchmark", "instructions", "items", "schema_version", "protocol", "scope"}
    if (
        set(payload) != required_top
        or payload.get("benchmark") != BENCHMARK_VERSION
        or payload.get("protocol") != PROTOCOL_VERSION
        or payload.get("schema_version") != "2.0"
        or payload.get("scope") not in {"pilot", "full"}
    ):
        raise ValueError(f"{label}: unexpected form schema, protocol, or benchmark version")
    if scope is not None and payload.get("scope") != scope:
        raise ValueError(f"{label}: form scope {payload.get('scope')!r} does not match the requested {scope!r} scope")
    if not isinstance(payload.get("instructions"), str):
        raise ValueError(f"{label}: instructions must be text")
    if not isinstance(payload.get("items"), list):
        raise ValueError(f"{label}: items must be a list")
    item_ids = [item.get("benchmark_id") for item in payload["items"] if isinstance(item, dict)]
    if any(not isinstance(benchmark_id, str) or not benchmark_id for benchmark_id in item_ids):
        raise ValueError(f"{label}: every item must have a non-empty string benchmark_id")
    if len(item_ids) != len(payload["items"]) or len(set(item_ids)) != len(item_ids):
        raise ValueError(f"{label}: item IDs must be present and unique")
    if set(item_ids) != set(expected_items):
        missing = sorted(set(expected_items) - set(item_ids))
        extra = sorted(set(item_ids) - set(expected_items))
        raise ValueError(f"{label}: item set differs from frozen benchmark (missing={missing[:3]}, extra={extra[:3]})")
    evidence_values = {"yes", "no", "uncertain"}
    overall_values = {"sufficient", "insufficient", "uncertain"}
    required_item = {
        "annotator_notes", "benchmark_id", "dialogue_samples", "fields",
        "overall_evidence_sufficiency", "sample_count",
    }
    for item in payload["items"]:
        benchmark_id = item["benchmark_id"]
        expected = expected_items[benchmark_id]
        if set(item) != required_item:
            raise ValueError(f"{label} {benchmark_id}: unexpected item fields")
        if (
            not isinstance(item["sample_count"], int)
            or isinstance(item["sample_count"], bool)
            or item["sample_count"] != expected["sample_count"]
            or item["dialogue_samples"] != expected["dialogue_samples"]
        ):
            raise ValueError(f"{label} {benchmark_id}: dialogue samples differ from the frozen dossier")
        if (
            not isinstance(item["overall_evidence_sufficiency"], str)
            or item["overall_evidence_sufficiency"] not in overall_values
        ):
            raise ValueError(f"{label} {benchmark_id}: overall evidence sufficiency is incomplete or invalid")
        if not isinstance(item["annotator_notes"], str):
            raise ValueError(f"{label} {benchmark_id}: annotator_notes must be text")
        fields = item.get("fields")
        if not isinstance(fields, dict) or set(fields) != set(FIELDS):
            raise ValueError(f"{label} {benchmark_id}: field set differs from the rubric")
        sample_ids = [sample["id"] for sample in expected["dialogue_samples"]]
        sample_id_set = set(sample_ids)
        for field, options in FIELDS.items():
            answer = fields[field]
            if not isinstance(answer, dict) or set(answer) != {"evidence_line_ids", "evidence_sufficient", "label", "notes"}:
                raise ValueError(f"{label} {benchmark_id} {field}: unexpected answer shape")
            if not isinstance(answer["label"], str) or answer["label"] not in options:
                raise ValueError(f"{label} {benchmark_id} {field}: label is incomplete or invalid")
            if not isinstance(answer["evidence_sufficient"], str) or answer["evidence_sufficient"] not in evidence_values:
                raise ValueError(f"{label} {benchmark_id} {field}: evidence sufficiency is incomplete or invalid")
            if not isinstance(answer["notes"], str):
                raise ValueError(f"{label} {benchmark_id} {field}: notes must be text")
            cited = answer["evidence_line_ids"]
            if not isinstance(cited, list) or any(not isinstance(line_id, str) for line_id in cited):
                raise ValueError(f"{label} {benchmark_id} {field}: evidence_line_ids must be a list of line IDs")
            if len(set(cited)) != len(cited) or len(cited) > MAX_CITED_LINES:
                raise ValueError(f"{label} {benchmark_id} {field}: cite at most {MAX_CITED_LINES} distinct lines")
            unknown = sorted(set(cited) - sample_id_set)
            if unknown:
                raise ValueError(f"{label} {benchmark_id} {field}: cited lines are not in this item ({unknown[:3]})")
            insufficient = answer["label"] == "insufficient_evidence"
            if insufficient and answer["evidence_sufficient"] != "no":
                raise ValueError(f"{label} {benchmark_id} {field}: insufficient_evidence requires evidence_sufficient='no'")
            if answer["evidence_sufficient"] == "no" and not insufficient:
                raise ValueError(f"{label} {benchmark_id} {field}: evidence_sufficient='no' requires label insufficient_evidence")
            if not insufficient and not cited:
                raise ValueError(f"{label} {benchmark_id} {field}: at least one supporting line ID is required")
            if answer["label"] == "mixed" and len(cited) < 2:
                raise ValueError(f"{label} {benchmark_id} {field}: mixed requires at least two cited lines")


def _write_new_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)
        handle.flush()


def _new_revision_dir(parent: Path, prefix: str, digest: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    base_name = f"{prefix}-{digest[:12]}"
    candidate = parent / base_name
    suffix = 1
    while candidate.exists():
        candidate = parent / f"{base_name}.{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def _annotation_run_directories(root: Path, scope: str | None = None) -> list[tuple[Path, dict]]:
    parent = root / "annotations" / "submissions"
    runs = []
    if not parent.exists():
        return runs
    for manifest_path in parent.glob("*/manifest.json"):
        manifest = read_json(manifest_path)
        if manifest.get("benchmark") != BENCHMARK_VERSION:
            continue
        if scope is not None and manifest.get("scope") != scope:
            continue
        runs.append((manifest_path.parent, manifest))
    return sorted(runs, key=lambda row: (int(row[1].get("imported_epoch_ns", 0)), row[1].get("imported_utc", "")))


def _resolve_annotation_run(
    root: Path,
    annotation_run: str | None,
    *,
    scope: str = "full",
) -> tuple[Path, dict] | None:
    runs = _annotation_run_directories(root, scope)
    if annotation_run is None:
        return runs[-1] if runs else None
    candidate = Path(annotation_run)
    if not candidate.is_dir():
        candidate = root / "annotations" / "submissions" / annotation_run
    manifest_path = candidate / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"annotation import run not found: {annotation_run}")
    manifest = read_json(manifest_path)
    if manifest.get("benchmark") != BENCHMARK_VERSION:
        raise ValueError("annotation import run has the wrong benchmark version")
    if manifest.get("scope") != scope:
        raise ValueError(f"annotation import run has scope {manifest.get('scope')!r}, not {scope!r}")
    if manifest.get("annotation_run") != candidate.name:
        raise ValueError("annotation import run ID does not match its directory")
    return candidate, manifest


def import_annotation_forms(
    *,
    root: Path | str = DEFAULT_ROOT,
    annotator_a: Path | str,
    annotator_b: Path | str,
    scope: str = "full",
) -> dict:
    """Validate and preserve both returned forms as a new immutable import run."""
    if scope not in {"pilot", "full"}:
        raise ValueError("annotation scope must be 'pilot' or 'full'")
    root = Path(root)
    expected_items, _selection = _expected_annotation_items(root, scope)
    source_a, form_a_bytes, form_a, zip_a = _read_annotation_source(annotator_a)
    source_b, form_b_bytes, form_b, zip_b = _read_annotation_source(annotator_b)
    _validate_annotation_form(form_a, expected_items, label="annotator_a", scope=scope)
    _validate_annotation_form(form_b, expected_items, label="annotator_b", scope=scope)
    source_digest = hashlib.sha256(source_a + b"\0" + source_b).hexdigest()
    imported_epoch_ns = time.time_ns()
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = _new_revision_dir(root / "annotations" / "submissions", timestamp, source_digest)
    _write_new_bytes(run_dir / "annotator_a.json", form_a_bytes)
    _write_new_bytes(run_dir / "annotator_b.json", form_b_bytes)
    if zip_a:
        _write_new_bytes(run_dir / "annotator_a_source.zip", source_a)
    if zip_b:
        _write_new_bytes(run_dir / "annotator_b_source.zip", source_b)
    manifest = {
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "scope": scope,
        "annotation_run": run_dir.name,
        "imported_epoch_ns": imported_epoch_ns,
        "imported_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_digest": source_digest,
        "annotators": {
            "annotator_a": {
                "source_name": Path(annotator_a).name,
                "source_sha256": hashlib.sha256(source_a).hexdigest(),
                "form_sha256": hashlib.sha256(form_a_bytes).hexdigest(),
                "source_was_zip": zip_a,
            },
            "annotator_b": {
                "source_name": Path(annotator_b).name,
                "source_sha256": hashlib.sha256(source_b).hexdigest(),
                "form_sha256": hashlib.sha256(form_b_bytes).hexdigest(),
                "source_was_zip": zip_b,
            },
        },
        "item_count_per_annotator": len(expected_items),
        "field_label_count_per_annotator": len(expected_items) * len(FIELDS),
        "raw_forms_preserved": True,
    }
    _write_new_bytes(run_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return {**manifest, "run_path": str(run_dir)}


def prepare_adjudication_form(
    *,
    root: Path | str = DEFAULT_ROOT,
    annotation_run: str | None = None,
) -> dict:
    """Create a separate adjudication copy that includes both raw label sets."""
    root = Path(root)
    resolved = _resolve_annotation_run(root, annotation_run)
    if resolved is None:
        raise FileNotFoundError("no imported independent annotation run is available")
    run_dir, run_manifest = resolved
    forms = {
        name: read_json(run_dir / f"{name}.json")
        for name in ("annotator_a", "annotator_b")
    }
    expected_items, _selection = _expected_annotation_items(root)
    for name, payload in forms.items():
        _validate_annotation_form(payload, expected_items, label=name, scope="full")
    by_annotator = {
        name: {item["benchmark_id"]: item for item in payload["items"]}
        for name, payload in forms.items()
    }
    items = []
    for benchmark_id in sorted(expected_items):
        annotation_a = by_annotator["annotator_a"][benchmark_id]
        annotation_b = by_annotator["annotator_b"][benchmark_id]
        labels_a = {field: annotation_a["fields"][field]["label"] for field in FIELDS}
        labels_b = {field: annotation_b["fields"][field]["label"] for field in FIELDS}
        review_a = {
            "overall_evidence_sufficiency": annotation_a["overall_evidence_sufficiency"],
            "field_evidence": {
                field: {
                    "evidence_sufficient": annotation_a["fields"][field]["evidence_sufficient"],
                    "evidence_line_ids": annotation_a["fields"][field]["evidence_line_ids"],
                    "notes": annotation_a["fields"][field]["notes"],
                }
                for field in FIELDS
            },
            "annotator_notes": annotation_a["annotator_notes"],
        }
        review_b = {
            "overall_evidence_sufficiency": annotation_b["overall_evidence_sufficiency"],
            "field_evidence": {
                field: {
                    "evidence_sufficient": annotation_b["fields"][field]["evidence_sufficient"],
                    "evidence_line_ids": annotation_b["fields"][field]["evidence_line_ids"],
                    "notes": annotation_b["fields"][field]["notes"],
                }
                for field in FIELDS
            },
            "annotator_notes": annotation_b["annotator_notes"],
        }
        items.append({
            "benchmark_id": benchmark_id,
            "sample_count": expected_items[benchmark_id]["sample_count"],
            "dialogue_samples": expected_items[benchmark_id]["dialogue_samples"],
            "annotator_a_labels": labels_a,
            "annotator_a_evidence": review_a,
            "annotator_b_labels": labels_b,
            "annotator_b_evidence": review_b,
            "adjudicated_labels": {field: "" for field in FIELDS},
            "adjudication_reasons": {field: "" for field in FIELDS},
            "adjudication_notes": "",
        })
    payload = {
        "schema_version": "2.0",
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "source_annotation_run": run_manifest["annotation_run"],
        "status": "pending_manual_adjudication",
        "items": items,
    }
    output_path = write_immutable_json(root / "adjudicated" / "forms" / run_manifest["annotation_run"] / "gold.json", payload)
    return {"annotation_run": run_manifest["annotation_run"], "adjudication_form_path": str(output_path), "item_count": len(items)}


def _adjudication_run_directories(root: Path, annotation_run: str) -> list[tuple[Path, dict]]:
    parent = root / "adjudicated" / "submissions" / annotation_run
    runs = []
    if not parent.exists():
        return runs
    for manifest_path in parent.glob("*/manifest.json"):
        manifest = read_json(manifest_path)
        if manifest.get("benchmark") == BENCHMARK_VERSION:
            runs.append((manifest_path.parent, manifest))
    return sorted(runs, key=lambda row: (int(row[1].get("imported_epoch_ns", 0)), row[1].get("imported_utc", "")))


def import_adjudication(
    *,
    root: Path | str = DEFAULT_ROOT,
    source: Path | str,
    annotation_run: str | None = None,
) -> dict:
    """Validate and preserve a completed adjudication without replacing templates."""
    root = Path(root)
    resolved = _resolve_annotation_run(root, annotation_run)
    if resolved is None:
        raise FileNotFoundError("no imported independent annotation run is available")
    source_run_dir, run_manifest = resolved
    run_id = run_manifest["annotation_run"]
    source_path = Path(source)
    if source_path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError(f"adjudication submission is unexpectedly large: {source_path}")
    raw = source_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("adjudication submission must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "benchmark", "protocol", "source_annotation_run", "status", "items"}:
        raise ValueError("adjudication submission has an unexpected schema")
    if (
        payload.get("schema_version") != "2.0"
        or payload.get("protocol") != PROTOCOL_VERSION
        or payload.get("benchmark") != BENCHMARK_VERSION
        or payload.get("source_annotation_run") != run_id
        or payload.get("status") not in {"pending_manual_adjudication", "complete_manual_adjudication"}
    ):
        raise ValueError("adjudication submission does not match the selected annotation run")
    expected_items, _selection = _expected_annotation_items(root, "full")
    expected_ids = set(expected_items)
    submitted_items = payload.get("items")
    if not isinstance(submitted_items, list):
        raise ValueError("adjudication items must be a list")
    item_ids = [item.get("benchmark_id") for item in submitted_items if isinstance(item, dict)]
    if (
        len(item_ids) != len(submitted_items)
        or any(not isinstance(benchmark_id, str) or not benchmark_id for benchmark_id in item_ids)
        or len(set(item_ids)) != len(item_ids)
        or set(item_ids) != expected_ids
    ):
        raise ValueError("adjudication item IDs do not match the frozen benchmark")
    forms = {name: read_json(source_run_dir / f"{name}.json") for name in ("annotator_a", "annotator_b")}
    for name, form in forms.items():
        _validate_annotation_form(form, expected_items, label=name, scope="full")
    raw_labels = {
        name: {item["benchmark_id"]: {field: item["fields"][field]["label"] for field in FIELDS} for item in form["items"]}
        for name, form in forms.items()
    }
    raw_evidence = {}
    for name, form in forms.items():
        raw_evidence[name] = {}
        for form_item in form["items"]:
            raw_evidence[name][form_item["benchmark_id"]] = {
                "overall_evidence_sufficiency": form_item["overall_evidence_sufficiency"],
                "field_evidence": {
                    field: {
                        "evidence_sufficient": form_item["fields"][field]["evidence_sufficient"],
                        "evidence_line_ids": form_item["fields"][field]["evidence_line_ids"],
                        "notes": form_item["fields"][field]["notes"],
                    }
                    for field in FIELDS
                },
                "annotator_notes": form_item["annotator_notes"],
            }
    for item in submitted_items:
        benchmark_id = item["benchmark_id"]
        expected_item_keys = {
            "benchmark_id", "sample_count", "dialogue_samples",
            "annotator_a_labels", "annotator_a_evidence",
            "annotator_b_labels", "annotator_b_evidence",
            "adjudicated_labels", "adjudication_reasons", "adjudication_notes",
        }
        if set(item) != expected_item_keys:
            raise ValueError(f"adjudication {benchmark_id}: unexpected item fields")
        if (
            item["sample_count"] != expected_items[benchmark_id]["sample_count"]
            or item["dialogue_samples"] != expected_items[benchmark_id]["dialogue_samples"]
        ):
            raise ValueError(f"adjudication {benchmark_id}: dialogue samples differ from the frozen dossier")
        if item["annotator_a_labels"] != raw_labels["annotator_a"][benchmark_id]:
            raise ValueError(f"adjudication {benchmark_id}: annotator A raw labels were changed")
        if item["annotator_a_evidence"] != raw_evidence["annotator_a"][benchmark_id]:
            raise ValueError(f"adjudication {benchmark_id}: annotator A evidence was changed")
        if item["annotator_b_labels"] != raw_labels["annotator_b"][benchmark_id]:
            raise ValueError(f"adjudication {benchmark_id}: annotator B raw labels were changed")
        if item["annotator_b_evidence"] != raw_evidence["annotator_b"][benchmark_id]:
            raise ValueError(f"adjudication {benchmark_id}: annotator B evidence was changed")
        if not isinstance(item["adjudication_notes"], str):
            raise ValueError(f"adjudication {benchmark_id}: notes must be text")
        adjudicated = item["adjudicated_labels"]
        if not isinstance(adjudicated, dict) or set(adjudicated) != set(FIELDS):
            raise ValueError(f"adjudication {benchmark_id}: field set differs from the rubric")
        reasons = item["adjudication_reasons"]
        if not isinstance(reasons, dict) or set(reasons) != set(FIELDS):
            raise ValueError(f"adjudication {benchmark_id}: adjudication_reasons must cover every field")
        for field, options in FIELDS.items():
            if not isinstance(adjudicated[field], str) or adjudicated[field] not in options:
                raise ValueError(f"adjudication {benchmark_id} {field}: label is incomplete or invalid")
            if not isinstance(reasons[field], str) or reasons[field] not in ("", *ADJUDICATION_REASONS):
                raise ValueError(f"adjudication {benchmark_id} {field}: reason is invalid")
            if adjudicated[field] and not reasons[field]:
                raise ValueError(f"adjudication {benchmark_id} {field}: a reason is required for every adjudicated label")

    digest = hashlib.sha256(raw).hexdigest()
    imported_epoch_ns = time.time_ns()
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    revision_dir = _new_revision_dir(root / "adjudicated" / "submissions" / run_id, timestamp, digest)
    _write_new_bytes(revision_dir / "gold.json", raw)
    import_manifest = {
        "benchmark": BENCHMARK_VERSION,
        "annotation_run": run_id,
        "adjudication_revision": revision_dir.name,
        "imported_epoch_ns": imported_epoch_ns,
        "imported_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_name": Path(source).name,
        "source_sha256": digest,
        "adjudicated_item_count": len(payload["items"]),
        "adjudicated_field_count": len(payload["items"]) * len(FIELDS),
    }
    _write_new_bytes(revision_dir / "manifest.json", json.dumps(import_manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return {**import_manifest, "gold_path": str(revision_dir / "gold.json")}


def _load_adjudication_source(root: Path, annotation_run: str | None) -> tuple[dict, str]:
    if annotation_run:
        revisions = _adjudication_run_directories(root, annotation_run)
        if revisions:
            revision_dir, _manifest = revisions[-1]
            return read_json(revision_dir / "gold.json"), str(revision_dir / "gold.json")
        form_path = root / "adjudicated" / "forms" / annotation_run / "gold.json"
        if form_path.is_file():
            return read_json(form_path), str(form_path)
    template_path = root / "adjudicated" / "gold.json"
    return read_json(template_path), str(template_path)


def _agreement_tables(by_annotator: dict[str, dict[str, dict]], ids: list[str]) -> tuple[dict, dict]:
    agreement = {}
    for field in FIELDS:
        a = [by_annotator["annotator_a"][bid]["fields"][field]["label"] for bid in ids]
        b = [by_annotator["annotator_b"][bid]["fields"][field]["label"] for bid in ids]
        row = {
            "n": len(ids),
            "exact_agreement": sum(left == right for left, right in zip(a, b)) / len(ids) if ids else None,
            "cohen_kappa": _cohen_kappa(a, b),
            "scale": FIELD_SCALES[field],
        }
        ordinal = ORDINAL_FIELD_LABELS.get(field)
        if ordinal:
            ordinal_agreement = _ordinal_metrics(a, b, ordinal)
            row["ordinal_weighted_cohen_kappa"] = ordinal_agreement["weighted_cohen_kappa_linear"]
            row["ordinal_mean_absolute_label_distance"] = ordinal_agreement["mean_absolute_label_distance"]
            row["ordinal_n_scored"] = ordinal_agreement["n"]
            row["ordinal_n_excluded"] = ordinal_agreement["excluded_count"]
            row["ordinal_weighting"] = "linear"
        agreement[field] = row
    evidence_agreement = {}
    for field in FIELDS:
        evidence_a = [by_annotator["annotator_a"][bid]["fields"][field]["evidence_sufficient"] for bid in ids]
        evidence_b = [by_annotator["annotator_b"][bid]["fields"][field]["evidence_sufficient"] for bid in ids]
        evidence_agreement[field] = {
            "n": len(ids),
            "cohen_kappa": _cohen_kappa(evidence_a, evidence_b),
            "exact_agreement": sum(left == right for left, right in zip(evidence_a, evidence_b)) / len(ids) if ids else None,
            "annotator_a_counts": {label: evidence_a.count(label) for label in ("yes", "no", "uncertain")},
            "annotator_b_counts": {label: evidence_b.count(label) for label in ("yes", "no", "uncertain")},
        }
    overall_a = [by_annotator["annotator_a"][bid]["overall_evidence_sufficiency"] for bid in ids]
    overall_b = [by_annotator["annotator_b"][bid]["overall_evidence_sufficiency"] for bid in ids]
    evidence_agreement["overall_evidence_sufficiency"] = {
        "n": len(ids),
        "cohen_kappa": _cohen_kappa(overall_a, overall_b),
        "exact_agreement": sum(left == right for left, right in zip(overall_a, overall_b)) / len(ids) if ids else None,
        "annotator_a_counts": {label: overall_a.count(label) for label in ("sufficient", "insufficient", "uncertain")},
        "annotator_b_counts": {label: overall_b.count(label) for label in ("sufficient", "insufficient", "uncertain")},
    }
    return agreement, evidence_agreement


def _pilot_gate(agreement: dict) -> dict:
    fields = {}
    for field, row in agreement.items():
        ordinal = ORDINAL_FIELD_LABELS.get(field)
        kappa = row.get("ordinal_weighted_cohen_kappa") if ordinal else row.get("cohen_kappa")
        kappa_name = "ordinal_weighted_cohen_kappa" if ordinal else "cohen_kappa"
        exact = row.get("exact_agreement")
        passed = (
            isinstance(exact, float) and exact >= PILOT_MIN_EXACT_AGREEMENT
            and isinstance(kappa, float) and kappa >= PILOT_MIN_KAPPA
        )
        fields[field] = {
            "exact_agreement": exact,
            "kappa": kappa,
            "kappa_statistic": kappa_name,
            "min_exact_agreement": PILOT_MIN_EXACT_AGREEMENT,
            "min_kappa": PILOT_MIN_KAPPA,
            "passed": passed,
            "verdict": "proceed" if passed else "revise_or_drop_field",
        }
    return {
        "status": "pass" if all(row["passed"] for row in fields.values()) else "revise_taxonomy",
        "policy": "Every field must pass before the full annotation run opens; a failing field is revised or dropped.",
        "fields": fields,
    }


def _precision_at_coverage_curve(rows: list[tuple[bool, float, bool]]) -> dict:
    """Descriptive operating-point curve over non-abstaining decisions."""
    scored = [(correct, confidence) for correct, confidence, abstained in rows if not abstained]
    if not scored:
        return {"n": 0, "points": [], "note": "No non-abstaining decisions with probabilities."}
    points = []
    for threshold in sorted({confidence for _correct, confidence in scored}, reverse=True):
        accepted = [(correct, confidence) for correct, confidence in scored if confidence >= threshold]
        points.append({
            "threshold": threshold,
            "accepted": len(accepted),
            "coverage": len(accepted) / len(rows) if rows else None,
            "precision": sum(correct for correct, _ in accepted) / len(accepted),
        })
    return {
        "n": len(rows),
        "scored_count": len(scored),
        "points": points,
        "note": "Descriptive only; the deployed operating point is selected on development annotations, never on locked-test results.",
    }


def _bootstrap_macro_f1_delta(
    gold: list[str],
    left: list[str],
    right: list[str],
    labels: list[str],
    *,
    seed: int,
    draws: int = 1500,
) -> dict:
    if not gold or len(left) != len(gold) or len(right) != len(gold):
        return {"n": 0, "delta": None, "confidence_interval_95_percentile": None}
    left_f1 = _confusion_metrics(gold, left, labels)["macro_f1"]
    right_f1 = _confusion_metrics(gold, right, labels)["macro_f1"]
    if left_f1 is None or right_f1 is None:
        return {"n": len(gold), "delta": None, "confidence_interval_95_percentile": None}
    rng = random.Random(seed)
    n = len(gold)
    deltas = []
    for _ in range(draws):
        indices = [rng.randrange(n) for _ in range(n)]
        sampled_gold = [gold[index] for index in indices]
        sampled_left = _confusion_metrics(sampled_gold, [left[index] for index in indices], labels)["macro_f1"]
        sampled_right = _confusion_metrics(sampled_gold, [right[index] for index in indices], labels)["macro_f1"]
        if sampled_left is not None and sampled_right is not None:
            deltas.append(sampled_left - sampled_right)
    deltas.sort()
    if deltas:
        low = max(0, math.floor(0.025 * len(deltas)))
        high = min(len(deltas) - 1, math.ceil(0.975 * len(deltas)) - 1)
        interval = [deltas[low], deltas[high]]
    else:
        interval = None
    return {
        "n": n,
        "left_macro_f1": left_f1,
        "right_macro_f1": right_f1,
        "delta": left_f1 - right_f1,
        "confidence_interval_95_percentile": interval,
        "bootstrap_draws": draws,
        "delta_definition": "left minus right macro-F1 on the same locked-test items",
    }


def _sample_evidence_floor_violations(root: Path, selection: dict[str, dict], by_annotator: dict[str, dict[str, dict]]) -> dict:
    """Items below the guide's evidence floor that still received a positive label."""
    violations = []
    floor_ids = []
    for benchmark_id in sorted(selection):
        dossier = read_json(Path(selection[benchmark_id]["source_dossier_path"]))
        samples = dossier.get("dialogue_samples") or []
        word_count = sum(len(words(str(sample.get("text") or ""))) for sample in samples)
        below = len(samples) < EVIDENCE_MIN_UTTERANCES or word_count < EVIDENCE_MIN_WORDS
        if not below:
            continue
        floor_ids.append(benchmark_id)
        for annotator, items in by_annotator.items():
            item = items.get(benchmark_id) or {}
            for field in FIELDS:
                label = ((item.get("fields") or {}).get(field) or {}).get("label")
                if label and label != "insufficient_evidence":
                    violations.append({"benchmark_id": benchmark_id, "annotator": annotator, "field": field, "label": label})
    return {
        "evidence_floor": {"min_utterances": EVIDENCE_MIN_UTTERANCES, "min_words": EVIDENCE_MIN_WORDS},
        "items_below_floor": floor_ids,
        "positive_label_count_below_floor": len(violations),
        "violations_preview": violations[:30],
        "note": "Guide-rule compliance check; it does not rewrite submitted labels.",
    }


def _chi_square_independence(groups: list[list[str]], labels: list[str]) -> float:
    total = sum(len(group) for group in groups)
    if not total:
        return 0.0
    overall = {label: sum(group.count(label) for group in groups) / total for label in labels}
    statistic = 0.0
    for group in groups:
        for label in labels:
            expected = len(group) * overall[label]
            if expected <= 0:
                continue
            statistic += (group.count(label) - expected) ** 2 / expected
    return statistic


def audit_annotation_leakage(
    *,
    root: Path | str = DEFAULT_ROOT,
    annotation_run: str | None = None,
    scope: str = "full",
    permutation_draws: int = 2000,
) -> dict:
    """Test whether labels track dialogue volume, which annotators should ignore."""
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    selection = {row["benchmark_id"]: row for row in manifest["private_selection"]}
    expected_items, _private = _expected_annotation_items(root, scope)
    resolved = _resolve_annotation_run(root, annotation_run, scope=scope)
    form_root = root / "annotations" / "pilot" if scope == "pilot" else root / "annotations"
    if resolved is not None:
        form_root, _manifest = resolved
    by_annotator = {}
    for name in ("annotator_a", "annotator_b"):
        payload = read_json(form_root / f"{name}.json")
        if resolved is not None:
            _validate_annotation_form(payload, expected_items, label=name, scope=scope)
        by_annotator[name] = {item["benchmark_id"]: item for item in payload["items"]}

    ids = sorted(expected_items)
    strata = sorted({selection[bid]["stratum"] for bid in ids})
    rng = random.Random(int(hashlib.sha256(f"leakage:{scope}".encode()).hexdigest()[:8], 16))
    per_field = {}
    for field in FIELDS:
        for annotator in ("annotator_a", "annotator_b"):
            labels = [by_annotator[annotator][bid]["fields"][field]["label"] for bid in ids]
            if any(not label for label in labels):
                per_field.setdefault(field, {})[annotator] = {"status": "incomplete_labels"}
                continue
            observed_groups = [[labels[i] for i, bid in enumerate(ids) if selection[bid]["stratum"] == stratum] for stratum in strata]
            label_values = sorted(set(labels))
            observed = _chi_square_independence(observed_groups, label_values)
            at_least = 0
            shuffled = list(labels)
            for _ in range(permutation_draws):
                rng.shuffle(shuffled)
                groups = [[shuffled[i] for i, bid in enumerate(ids) if selection[bid]["stratum"] == stratum] for stratum in strata]
                if _chi_square_independence(groups, label_values) >= observed - 1e-12:
                    at_least += 1
            p_value = (at_least + 1) / (permutation_draws + 1)
            counts = {
                stratum: {label: group.count(label) for label in label_values}
                for stratum, group in zip(strata, observed_groups)
            }
            per_field.setdefault(field, {})[annotator] = {
                "n": len(labels),
                "stratum_label_counts": counts,
                "chi_square_statistic": observed,
                "permutation_p_value": p_value,
                "permutation_draws": permutation_draws,
                "flag": p_value < 0.01,
                "flag_note": "Low p-value is suggestive, not proof: style may genuinely correlate with dialogue volume.",
            }
    allowed_item_keys = {
        "annotator_notes", "benchmark_id", "dialogue_samples", "fields",
        "overall_evidence_sufficiency", "sample_count",
    }
    allowed_field_keys = {"evidence_line_ids", "evidence_sufficient", "label", "notes"}
    residual = [
        {"benchmark_id": item["benchmark_id"], "unexpected_keys": sorted(set(item) - allowed_item_keys)}
        for items in by_annotator.values()
        for item in items.values()
        if set(item) - allowed_item_keys
        or any(set(answer) - allowed_field_keys for answer in (item.get("fields") or {}).values())
    ]
    report = {
        "benchmark": BENCHMARK_VERSION,
        "scope": scope,
        "annotation_run": resolved[1]["annotation_run"] if resolved else None,
        "item_count": len(ids),
        "volume_shortcut_test": per_field,
        "residual_metadata_findings": residual[:30],
        "residual_metadata_clean": not residual,
        "note": "Annotators see line text, line IDs, and line counts only. Volume is visible through the sample size, so this audit tests whether labels track it.",
    }
    path = write_immutable_json(root / "reports" / f"annotation_leakage_audit_{scope}.json", report)
    return {**report, "report_path": str(path)}


def _prediction_citation_validity(
    root: Path,
    manifest: dict,
    benchmark_ids: list[str],
    fields: dict[str, dict[str, str]],
) -> dict[str, dict[str, dict[str, Any]]]:
    public = {
        entry["benchmark_id"]: entry for entry in manifest.get("public_dossiers") or []
    }
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for system, prediction_dir in SYSTEM_PREDICTION_DIRS.items():
        records = {}
        for benchmark_id in benchmark_ids:
            path = root / "predictions" / prediction_dir / f"{benchmark_id}.json"
            if path.is_file():
                records[benchmark_id] = read_json(path)
        if not records:
            result[system] = {"status": "not_run"}
            continue
        system_result: dict[str, Any] = {}
        for field in fields:
            total = with_citations = valid = 0
            for benchmark_id, record in records.items():
                entry = public.get(benchmark_id)
                if entry is None:
                    continue
                labels = record.get("labels") or record.get("answers") or {}
                answer = labels.get(field)
                if not isinstance(answer, dict):
                    continue
                total += 1
                supporting = answer.get("supporting_line_ids") or []
                counterexamples = answer.get("counterexample_line_ids") or []
                if not supporting and not counterexamples:
                    continue
                with_citations += 1
                sample_ids = set(record.get("sample_line_ids") or [])
                if not sample_ids:
                    dossier = read_json(Path(entry["dossier_path"]))
                    sample_ids = {
                        sample["id"] for sample in dossier.get("dialogue_samples") or []
                    }
                if all(item in sample_ids for item in supporting + counterexamples):
                    valid += 1
            system_result[field] = {
                "predictions": total,
                "with_citations": with_citations,
                "valid_citations": valid,
                "citation_rate": round(with_citations / total, 4) if total else None,
                "citation_validity_rate": round(valid / with_citations, 4) if with_citations else None,
                "status": "measured" if with_citations else "not_available",
            }
        result[system] = system_result
    return result


def evaluate_benchmark(
    *,
    root: Path | str = DEFAULT_ROOT,
    auto_threshold: float | None = None,
    annotation_run: str | None = None,
    scope: str = "full",
) -> dict:
    if scope not in {"pilot", "full"}:
        raise ValueError("annotation scope must be 'pilot' or 'full'")
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    selection = {row["benchmark_id"]: row for row in manifest["private_selection"]}
    resolved_annotation = _resolve_annotation_run(root, annotation_run, scope=scope)
    selected_annotation_run = None
    if resolved_annotation is None:
        form_root = root / "annotations" / "pilot" if scope == "pilot" else root / "annotations"
    else:
        form_root, annotation_manifest = resolved_annotation
        selected_annotation_run = annotation_manifest["annotation_run"]
    expected_items, _private_selection = _expected_annotation_items(root, scope)
    forms = {}
    for name in ("annotator_a", "annotator_b"):
        payload = read_json(form_root / f"{name}.json")
        if resolved_annotation is not None:
            _validate_annotation_form(payload, expected_items, label=name, scope=scope)
        forms[name] = payload["items"]
    by_annotator = {
        name: {item["benchmark_id"]: item for item in items}
        for name, items in forms.items()
    }
    ids = sorted(expected_items)
    missing = []
    for benchmark_id in ids:
        for annotator in by_annotator:
            item = by_annotator[annotator].get(benchmark_id) or {}
            for field in FIELDS:
                if not ((item.get("fields") or {}).get(field) or {}).get("label"):
                    missing.append({"benchmark_id": benchmark_id, "annotator": annotator, "field": field})
    report: dict[str, Any] = {
        "benchmark": BENCHMARK_VERSION,
        "protocol": PROTOCOL_VERSION,
        "scope": scope,
        "annotation_run": selected_annotation_run,
        "annotation_status": "incomplete" if missing else "raw_annotations_complete",
        "missing_annotation_count": len(missing),
        "missing_annotations_preview": missing[:30],
        "auto_accept_threshold_override": auto_threshold,
        "auto_accept_threshold_policy": "By default, each probability-bearing classifier's per-field threshold is selected on development labels to maximize coverage while meeting the 0.90 precision and 0.50 coverage targets, then evaluated on locked-test items. --auto-threshold overrides this with one fixed threshold for all fields.",
        "stability_not_correctness": "Repeat, reordering, and alternate-sample stability numbers are robustness diagnostics. They are not correctness evidence and never substitute for gold labels.",
        "note": "No gold labels or classifier-accuracy metrics are produced until both independent forms are complete and gold labels are manually adjudicated.",
    }
    report["evidence_citation_validity"] = _prediction_citation_validity(
        root, manifest, ids, FIELDS,
    )
    if scope == "pilot":
        report["annotation_status"] = "pilot_incomplete" if missing else "pilot_raw_annotations_complete"
        if not missing:
            agreement, evidence_agreement = _agreement_tables(by_annotator, ids)
            report["inter_annotator_agreement"] = agreement
            report["inter_annotator_evidence_agreement"] = evidence_agreement
            report["pilot_gate"] = _pilot_gate(agreement)
            report["note"] = "Pilot agreement only. Full annotation opens only when every field passes the gate."
        path = write_immutable_json(root / "reports" / "pilot_evaluation.json", report)
        return {**report, "report_path": str(path)}
    if missing:
        path = write_immutable_json(root / "reports" / "evaluation_pending.json", report)
        return {**report, "report_path": str(path)}

    agreement, evidence_agreement = _agreement_tables(by_annotator, ids)
    report["inter_annotator_agreement"] = agreement
    report["inter_annotator_evidence_agreement"] = evidence_agreement
    report["pilot_gate"] = _pilot_gate(agreement)
    report["guide_rule_compliance"] = _sample_evidence_floor_violations(root, selection, by_annotator)

    gold_payload, gold_source_path = _load_adjudication_source(root, selected_annotation_run)
    report["adjudication_source"] = gold_source_path
    gold_by_id = {item["benchmark_id"]: item for item in gold_payload.get("items", [])}
    gold_missing = [
        {"benchmark_id": bid, "field": field}
        for bid in ids
        for field in FIELDS
        if not ((gold_by_id.get(bid, {}).get("adjudicated_labels") or {}).get(field))
    ]
    if gold_missing:
        report.update({
            "annotation_status": "raw_annotations_complete_adjudication_pending",
            "missing_adjudicated_count": len(gold_missing),
            "missing_adjudicated_preview": gold_missing[:30],
            "note": "Inter-annotator agreement is available; model-versus-gold metrics wait for completed manual adjudication.",
        })
        path = write_immutable_json(root / "reports" / "evaluation.json", report)
        return {**report, "report_path": str(path)}

    locked = sorted(bid for bid, row in selection.items() if row["split"] == "locked_test")
    development_ids = sorted(bid for bid, row in selection.items() if row["split"] == "development")
    gold_metrics = {}
    scored_rows: dict[str, dict[str, dict[str, tuple[str, str]]]] = {}
    evidence_exclusions: dict[str, list[str]] = {}
    for field in FIELDS:
        evidence_exclusions[field] = sorted(
            bid for bid in locked
            if by_annotator["annotator_a"][bid]["fields"][field]["evidence_sufficient"] == "no"
            and by_annotator["annotator_b"][bid]["fields"][field]["evidence_sufficient"] == "no"
        )
    for system, prediction_dir in SYSTEM_PREDICTION_DIRS.items():
        records = {}
        for bid in locked:
            path = root / "predictions" / prediction_dir / f"{bid}.json"
            if path.is_file():
                records[bid] = read_json(path)
        if not records:
            gold_metrics[system] = {"status": "not_run"}
            continue
        metrics_for_fields = {}
        for field, options in FIELDS.items():
            excluded = set(evidence_exclusions[field])
            eligible_ids = [bid for bid in locked if bid not in excluded]
            available = []
            for bid in eligible_ids:
                record = records.get(bid)
                labels_payload = (record.get("labels") or (record.get("answers") or {})) if record else {}
                prediction = labels_payload.get(field)
                if isinstance(prediction, dict):
                    prediction = _prediction_label(prediction)
                actual = gold_by_id[bid]["adjudicated_labels"][field]
                if actual not in options:
                    raise ValueError(f"adjudicated label is invalid for {bid} {field}: {actual!r}")
                valid_prediction = isinstance(prediction, str) and prediction in options
                scored_prediction = prediction if valid_prediction else "__missing_or_invalid_prediction__"
                available.append((bid, actual, scored_prediction, record, valid_prediction))
            actuals = [row[1] for row in available]
            predictions = [row[2] for row in available]
            scored_rows.setdefault(system, {})[field] = {row[0]: (row[1], row[2]) for row in available}
            labels = list(options)
            field_metrics = _confusion_metrics(actuals, predictions, labels)
            valid_prediction_count = sum(row[4] for row in available)
            field_metrics["scored_n"] = len(available)
            field_metrics["prediction_coverage"] = valid_prediction_count / len(available) if available else None
            field_metrics["missing_or_invalid_prediction_count"] = len(available) - valid_prediction_count
            field_metrics["low_evidence_exclusion"] = {
                "rule": "both annotators recorded evidence_sufficient: no",
                "excluded_count": len(excluded),
                "excluded_benchmark_ids": sorted(excluded),
                "note": "Excluded items are scored separately and never counted as model errors.",
            }
            field_metrics["cohen_kappa"] = _cohen_kappa(actuals, predictions)
            field_metrics["abstention"] = {
                "label": "insufficient_evidence",
                "count": sum(prediction == "insufficient_evidence" for prediction in predictions),
                "rate_among_valid_predictions": sum(prediction == "insufficient_evidence" for prediction in predictions) / valid_prediction_count if valid_prediction_count else None,
                "decision_coverage_among_valid_predictions": sum(row[4] and row[2] != "insufficient_evidence" for row in available) / valid_prediction_count if valid_prediction_count else None,
            }
            selective = [
                (actual, prediction) for actual, prediction in zip(actuals, predictions)
                if prediction != "insufficient_evidence"
            ]
            field_metrics["selective_accuracy"] = {
                "n": len(selective),
                "accuracy": (
                    sum(actual == prediction for actual, prediction in selective) / len(selective)
                    if selective else None
                ),
                "coverage": len(selective) / len(available) if available else None,
                "note": "Accuracy over predictions that emitted a label; abstentions are excluded from the numerator and denominator.",
            }
            field_metrics["mixed_detection"] = field_metrics["per_class"].get("mixed")
            field_metrics["insufficient_detection"] = field_metrics["per_class"].get("insufficient_evidence")
            ordinal_order = ORDINAL_FIELD_LABELS.get(field)
            if ordinal_order:
                field_metrics["ordinal_metrics"] = _ordinal_metrics(actuals, predictions, ordinal_order)
            field_metrics["by_dialogue_volume_stratum"] = {}
            for stratum in sorted({selection[bid]["stratum"] for bid in eligible_ids}):
                stratum_rows = [row for row in available if selection[row[0]]["stratum"] == stratum]
                stratum_metrics = _confusion_metrics(
                    [row[1] for row in stratum_rows],
                    [row[2] for row in stratum_rows],
                    labels,
                )
                stratum_valid_count = sum(row[4] for row in stratum_rows)
                stratum_metrics["prediction_coverage"] = stratum_valid_count / len(stratum_rows) if stratum_rows else None
                field_metrics["by_dialogue_volume_stratum"][stratum] = stratum_metrics
            field_metrics["confidence_intervals"] = _bootstrap_intervals(
                actuals,
                predictions,
                labels,
                seed=int(hashlib.sha256(f"{system}:{field}".encode()).hexdigest()[:8], 16),
                ordinal_order=ordinal_order,
            )
            prediction_records = [row[3] for row in available]
            calibration = _calibration_report(actuals, prediction_records, field, labels)
            if calibration:
                field_metrics["calibration"] = calibration
                threshold_selection = None
                selected_threshold = auto_threshold
                if auto_threshold is None:
                    threshold_selection = _select_development_threshold(
                        root=root,
                        system=prediction_dir,
                        development_ids=development_ids,
                        gold_by_id=gold_by_id,
                        field=field,
                        labels=labels,
                    )
                    selected_threshold = threshold_selection["selected_threshold"]
                else:
                    threshold_selection = {
                        "method": "fixed caller override; not selected from test results",
                        "status": "fixed_override",
                        "selected_threshold": auto_threshold,
                    }
                scored = []
                acceptance_rows = []
                curve_rows = []
                for actual, record in zip(actuals, prediction_records):
                    if not isinstance(record, dict):
                        acceptance_rows.append((False, False))
                        continue
                    answer = (record.get("answers") or {}).get(field)
                    probabilities = _validated_probabilities(answer, labels) if isinstance(answer, dict) else None
                    if probabilities is None:
                        acceptance_rows.append((False, False))
                        continue
                    predicted = max(labels, key=lambda label: probabilities[label])
                    reported_choice = _prediction_label(answer)
                    is_accepted = (
                        selected_threshold is not None
                        and probabilities[predicted] >= selected_threshold
                        and predicted != "insufficient_evidence"
                        and reported_choice == predicted
                    )
                    scored.append((actual, predicted, probabilities[predicted], reported_choice))
                    curve_rows.append((actual == predicted, probabilities[predicted], predicted == "insufficient_evidence"))
                    acceptance_rows.append((actual == predicted, is_accepted))
                accepted_count = sum(is_accepted for _correct, is_accepted in acceptance_rows)
                accepted_correct = sum(correct and is_accepted for correct, is_accepted in acceptance_rows)
                field_metrics["auto_accept"] = {
                    "threshold": selected_threshold,
                    "threshold_selection": threshold_selection,
                    "coverage": accepted_count / len(actuals) if actuals else None,
                    "accepted_precision": accepted_correct / accepted_count if accepted_count else None,
                    "coverage_and_precision_confidence_intervals": _bootstrap_acceptance_intervals(
                        acceptance_rows,
                        seed=int(hashlib.sha256(f"{system}:{field}:auto-accept".encode()).hexdigest()[:8], 16),
                    ),
                    "accepted_count": accepted_count,
                    "manual_review_count": len(actuals) - accepted_count,
                    "manual_review_rate": (len(actuals) - accepted_count) / len(actuals) if actuals else None,
                    "probability_distribution_coverage": len(scored) / len(actuals) if actuals else None,
                    "abstentions_excluded_from_auto_accept": sum(predicted == "insufficient_evidence" for _, predicted, _, _reported in scored),
                    "decision_distribution_mismatch_count": sum(predicted != reported for _, predicted, _, reported in scored),
                }
                field_metrics["precision_at_coverage"] = _precision_at_coverage_curve(curve_rows)
            metrics_for_fields[field] = field_metrics
        gold_metrics[system] = {
            "status": "scored",
            "condition": system.rsplit("_", 1)[-1] if system.startswith(("nimble_", "qwen_base_")) else None,
            "test_npc_count": len(locked),
            "prediction_record_count": len(records),
            "missing_prediction_ids": sorted(set(locked) - set(records)),
            "prediction_record_coverage": len(records) / len(locked) if locked else None,
            "fields": metrics_for_fields,
        }
    for system in SYSTEM_PREDICTION_DIRS:
        if system not in gold_metrics or gold_metrics[system].get("status") != "scored":
            continue
        for field, options in FIELDS.items():
            candidate_rows = scored_rows.get(system, {}).get(field, {})
            baseline_rows = {
                baseline: scored_rows.get(baseline, {}).get(field, {})
                for baseline in ("rules_full", "rules_sample")
                if scored_rows.get(baseline, {}).get(field)
            }
            if not candidate_rows or not baseline_rows:
                continue
            common_ids = set(candidate_rows)
            for rows in baseline_rows.values():
                common_ids.intersection_update(rows)
            common_ids = sorted(common_ids)
            if not common_ids:
                continue
            gold = [candidate_rows[bid][0] for bid in common_ids]
            candidate_predictions = [candidate_rows[bid][1] for bid in common_ids]
            baseline_predictions = {
                baseline: [rows[bid][1] for bid in common_ids]
                for baseline, rows in baseline_rows.items()
            }
            comparison = _bootstrap_macro_f1_margin(
                gold,
                candidate_predictions,
                baseline_predictions,
                list(options),
                seed=int(hashlib.sha256(f"{system}:{field}:rules-margin".encode()).hexdigest()[:8], 16),
            )
            comparison["target_margin"] = 0.05
            comparison["point_estimate_meets_target"] = (
                comparison["margin"] >= comparison["target_margin"]
                if comparison["margin"] is not None else None
            )
            comparison["baseline_note"] = "Rules baselines consume measurements by construction; they are measurement-only, not a text-only condition."
            gold_metrics[system]["fields"][field]["comparison_to_rules"] = comparison
    condition_comparison = {}
    for assisted, text_only in CONDITION_PAIRS:
        if gold_metrics.get(assisted, {}).get("status") != "scored" or gold_metrics.get(text_only, {}).get("status") != "scored":
            condition_comparison[assisted.replace("_assisted", "")] = {"status": "requires_both_conditions"}
            continue
        per_field = {}
        for field, options in FIELDS.items():
            assisted_rows = scored_rows.get(assisted, {}).get(field, {})
            text_rows = scored_rows.get(text_only, {}).get(field, {})
            common_ids = sorted(set(assisted_rows) & set(text_rows))
            if not common_ids:
                per_field[field] = {"n": 0}
                continue
            per_field[field] = _bootstrap_macro_f1_delta(
                [assisted_rows[bid][0] for bid in common_ids],
                [assisted_rows[bid][1] for bid in common_ids],
                [text_rows[bid][1] for bid in common_ids],
                list(options),
                seed=int(hashlib.sha256(f"{assisted}:{field}:condition".encode()).hexdigest()[:8], 16),
            )
        condition_comparison[assisted.replace("_assisted", "")] = {
            "status": "scored",
            "assisted_system": assisted,
            "text_only_system": text_only,
            "fields": per_field,
            "note": "Positive delta means the measurement-assisted condition scored higher on the same items. This isolates whether the supplied measurements, not the text, drive the decision.",
        }
    report.update({
        "annotation_status": "complete_and_adjudicated",
        "locked_test_npc_count": len(locked),
        "low_evidence_excluded_items": evidence_exclusions,
        "locked_test_sampling": {
            "stratum_counts": {
                stratum: sum(selection[bid]["stratum"] == stratum for bid in locked)
                for stratum in sorted({selection[bid]["stratum"] for bid in locked})
            },
            "aggregate_metrics_are_population_weighted": False,
            "interpretation": "The benchmark allocates equal counts to dialogue-volume strata. Overall locked-test metrics weight those strata equally; see per-stratum results before applying them to the full eligible NPC population.",
        },
        "mixed_and_insufficient_reporting": "mixed and insufficient_evidence are scored as distinct classes in accuracy and macro-F1; insufficient_evidence is additionally reported as abstention with decision coverage.",
        "gold_metrics": gold_metrics,
        "condition_comparison": condition_comparison,
        "gates": {
            "inter_annotator_kappa_target": 0.70,
            "pilot_exact_agreement_target": PILOT_MIN_EXACT_AGREEMENT,
            "pilot_kappa_target": PILOT_MIN_KAPPA,
            "macro_f1_target": 0.85,
            "rules_margin_target": 0.05,
            "accepted_precision_target": 0.90,
            "coverage_target": 0.50,
            "common_class_recall_target": 0.75,
            "order_stability_target": 0.95,
            "status": "requires_human_review_of_report_and_stability_runs",
        },
    })
    path = write_immutable_json(root / "reports" / "evaluation.json", report)
    return {**report, "report_path": str(path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prep = subparsers.add_parser("prepare", help="freeze dossiers, forms, and rules baselines")
    prep.add_argument("--db", default=str(DEFAULT_DB))
    prep.add_argument("--cache", default="data/blueprints/cache")
    prep.add_argument("--root", default=str(DEFAULT_ROOT))
    prep.add_argument("--seed", default=DEFAULT_SEED)
    prep.add_argument("--model", default="data/models/qwen3.5-9b/Bespoke-Nimble-9B-v3-Q4_K_M.gguf")
    nimble = subparsers.add_parser("nimble", help="run frozen dossiers through native /v1/systemone")
    nimble.add_argument("--root", default=str(DEFAULT_ROOT))
    nimble.add_argument("--endpoint", default="http://127.0.0.1:18081/v1/systemone")
    nimble.add_argument("--condition", choices=CONDITIONS, default="assisted")
    nimble.add_argument("--limit", type=int)
    nimble.add_argument("--only", action="append")
    nimble.add_argument("--timeout", type=float, default=600)
    jev = subparsers.add_parser("jev", help="run the production identity-blind JEV classifier on frozen dossiers")
    jev.add_argument("--root", default=str(DEFAULT_ROOT))
    jev.add_argument("--endpoint", default="http://127.0.0.1:8081/v1/systemone")
    jev.add_argument("--model", default="jev-1.14.0")
    jev.add_argument("--api-key-env", default="TYPESAFE_API_KEY")
    jev.add_argument("--limit", type=int)
    jev.add_argument("--only", action="append")
    jev.add_argument("--timeout", type=float, default=600)
    jev.add_argument("--force", action="store_true")
    stability = subparsers.add_parser("nimble-stability", help="run sample-size/order/repeat stability on a selected split")
    stability.add_argument("--root", default=str(DEFAULT_ROOT))
    stability.add_argument("--cache", default="data/blueprints/cache")
    stability.add_argument("--endpoint", default="http://127.0.0.1:18081/v1/systemone")
    stability.add_argument("--condition", choices=CONDITIONS, default="assisted")
    stability.add_argument("--split", choices=("development", "locked_test"), default="locked_test")
    stability.add_argument("--only", action="append")
    stability.add_argument("--timeout", type=float, default=600)
    stability_analysis = subparsers.add_parser("analyze-stability", help="report per-field agreement from a stability report")
    stability_analysis.add_argument("--root", default=str(DEFAULT_ROOT))
    stability_analysis.add_argument("--report", required=True)
    baseline_audit = subparsers.add_parser("audit-baseline-provenance", help="check rules prediction hashes against evidence sources")
    baseline_audit.add_argument("--root", default=str(DEFAULT_ROOT))
    import_annotations = subparsers.add_parser("import-annotations", help="validate and immutably import two completed blind forms")
    import_annotations.add_argument("--root", default=str(DEFAULT_ROOT))
    import_annotations.add_argument("--annotator-a", required=True)
    import_annotations.add_argument("--annotator-b", required=True)
    import_annotations.add_argument("--scope", choices=("pilot", "full"), default="full")
    prepare_adjudication = subparsers.add_parser("prepare-adjudication", help="create a comparison form from an imported annotation run")
    prepare_adjudication.add_argument("--root", default=str(DEFAULT_ROOT))
    prepare_adjudication.add_argument("--annotation-run")
    import_gold = subparsers.add_parser("import-adjudication", help="validate and immutably import completed adjudication")
    import_gold.add_argument("--root", default=str(DEFAULT_ROOT))
    import_gold.add_argument("--source", required=True)
    import_gold.add_argument("--annotation-run")
    qwen = subparsers.add_parser("qwen-base", help="run constrained JSON generation through a local OpenAI-compatible endpoint")
    qwen.add_argument("--root", default=str(DEFAULT_ROOT))
    qwen.add_argument("--endpoint", default="http://127.0.0.1:18082/v1/chat/completions")
    qwen.add_argument("--condition", choices=CONDITIONS, default="assisted")
    qwen.add_argument("--model", default="Qwen3.5-9B-Base-Q4_K_M")
    qwen.add_argument("--model-path", default="data/models/qwen3.5-9b/Qwen3.5-9B-Base.Q4_K_M.gguf")
    qwen.add_argument("--limit", type=int)
    qwen.add_argument("--only", action="append")
    qwen.add_argument("--timeout", type=float, default=600)
    evaluate = subparsers.add_parser("evaluate", help="score human agreement and adjudicated labels when available")
    evaluate.add_argument("--root", default=str(DEFAULT_ROOT))
    evaluate.add_argument("--annotation-run", help="import run ID or path; defaults to the newest imported run")
    evaluate.add_argument("--scope", choices=("pilot", "full"), default="full")
    evaluate.add_argument(
        "--auto-threshold",
        type=float,
        help="fixed global threshold override; default selects per-field thresholds on development annotations",
    )
    packets = subparsers.add_parser("share-packets", help="package a blind form plus the guide for an annotator")
    packets.add_argument("--root", default=str(DEFAULT_ROOT))
    packets.add_argument("--scope", choices=("pilot", "full"), default="pilot")
    leakage = subparsers.add_parser("audit-leakage", help="test whether labels track dialogue volume or residual metadata")
    leakage.add_argument("--root", default=str(DEFAULT_ROOT))
    leakage.add_argument("--annotation-run")
    leakage.add_argument("--scope", choices=("pilot", "full"), default="full")
    leakage.add_argument("--permutation-draws", type=int, default=2000)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_benchmark(db=args.db, cache=args.cache, root=args.root, seed=args.seed, model_path=args.model)
    elif args.command == "nimble":
        result = run_nimble(root=args.root, endpoint=args.endpoint, condition=args.condition, limit=args.limit, only=set(args.only or []), timeout=args.timeout)
    elif args.command == "jev":
        result = run_jev(
            root=args.root, endpoint=args.endpoint, model=args.model,
            api_key_env=args.api_key_env, limit=args.limit,
            only=set(args.only or []), timeout=args.timeout, force=args.force,
        )
    elif args.command == "nimble-stability":
        result = run_nimble_stability(root=args.root, cache=args.cache, endpoint=args.endpoint, condition=args.condition, split=args.split, only=set(args.only or []), timeout=args.timeout)
    elif args.command == "analyze-stability":
        result = analyze_nimble_stability(root=args.root, report_path=args.report)
    elif args.command == "audit-baseline-provenance":
        result = audit_rules_baseline_provenance(root=args.root)
    elif args.command == "import-annotations":
        result = import_annotation_forms(root=args.root, annotator_a=args.annotator_a, annotator_b=args.annotator_b, scope=args.scope)
    elif args.command == "prepare-adjudication":
        result = prepare_adjudication_form(root=args.root, annotation_run=args.annotation_run)
    elif args.command == "import-adjudication":
        result = import_adjudication(root=args.root, source=args.source, annotation_run=args.annotation_run)
    elif args.command == "qwen-base":
        result = run_qwen_base(root=args.root, endpoint=args.endpoint, condition=args.condition, model=args.model, model_path=args.model_path, limit=args.limit, only=set(args.only or []), timeout=args.timeout)
    elif args.command == "share-packets":
        result = build_share_packets(root=args.root, scope=args.scope)
    elif args.command == "audit-leakage":
        result = audit_annotation_leakage(root=args.root, annotation_run=args.annotation_run, scope=args.scope, permutation_draws=args.permutation_draws)
    else:
        result = evaluate_benchmark(root=args.root, auto_threshold=args.auto_threshold, annotation_run=args.annotation_run, scope=args.scope)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
