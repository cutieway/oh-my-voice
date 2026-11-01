"""Dialogue-only mood: stratified chunks, independent calls, one aggregate.

The local model has an 8k context, so a 1,552-line character cannot be judged in
one call. Feeding the whole corpus is not an option and feeding one 24-line
sample makes a single emotional scene look like a baseline. This module splits
the signal-ranked dialogue pool into several chunk-sized windows, asks the same
typed mood questions on each, and accepts a preset only when the chunks agree.

Nothing here reads wiki text. Lore is still fetched for identity fields, but a
mood decision is made from in-game dialogue alone.
"""

from __future__ import annotations

import math
from .dialogue import BUCKETS, length_bucket, select_representative_samples

MOOD_QUESTION_IDS = ("mood.preset", "mood.justified")


def plan_chunk_count(unique_lines: int, policy: dict) -> int:
    """How many independent calls the volume justifies.

    0 means no mood question is asked at all: below the floor the pack stays
    neutral by construction rather than by an unconvincing answer.
    """
    floor = int(policy["min_dialogue_lines_for_mood"])
    if unique_lines < floor:
        return 0
    cap = max(1, int(policy.get("mood_max_chunks", 8)))
    if unique_lines < 80:
        count = 1
    elif unique_lines < 120:
        count = 2
    elif unique_lines < 300:
        count = 3
    elif unique_lines < 500:
        count = 4
    elif unique_lines < 800:
        count = 5
    elif unique_lines < 1200:
        count = 6
    elif unique_lines < 2000:
        count = 7
    else:
        count = 8
    return min(count, cap)


def pool_budget(chunk_count: int, policy: dict) -> int:
    """Characters of dialogue the dossier must carry for `chunk_count` windows."""
    if chunk_count <= 0:
        return 0
    chunk_chars = int(policy["mood_chunk_chars"])
    overlap = float(policy["mood_chunk_overlap"])
    return round(chunk_chars * (1 + (chunk_count - 1) * (1 - overlap)))


def select_mood_pool(
    canonical_id: str,
    lines: list[dict],
    *,
    chunk_count: int,
    policy: dict,
    vocabulary: dict,
) -> list[dict]:
    """Signal-ranked, stratified, deduplicated dialogue for chunked mood calls.

    Reuses the same selector as the style samples, with a much larger budget, so
    chunking never invents a second sampling convention.
    """
    if chunk_count <= 0:
        return []
    return select_representative_samples(
        canonical_id,
        lines,
        max_count=chunk_count * 500,
        max_chars=pool_budget(chunk_count, policy),
        vocabulary=vocabulary,
    )


def _weighted_interleave(pool: list[dict]) -> list[dict]:
    """Order the pool so any window keeps the corpus short/medium/long mix.

    Buckets are drained in proportion to their share of the pool, not one line
    each in turn: short lines are a small minority of a real corpus and equal
    rotation would over-represent them inside every chunk.
    """
    by_bucket: dict[str, list[dict]] = {bucket: [] for bucket in BUCKETS}
    for line in pool:
        by_bucket[length_bucket(str(line.get("text") or ""))].append(line)
    present = {bucket: rows for bucket, rows in by_bucket.items() if rows}
    if not present:
        return []
    total = sum(len(rows) for rows in present.values())
    shares = {bucket: len(rows) / total for bucket, rows in present.items()}
    order = [bucket for bucket in BUCKETS if bucket in present]
    cursors = {bucket: 0 for bucket in present}
    deficit = {bucket: 0.0 for bucket in present}
    sequence: list[dict] = []
    while True:
        available = [bucket for bucket in order if cursors[bucket] < len(present[bucket])]
        if not available:
            break
        for bucket in present:
            deficit[bucket] += shares[bucket]
        chosen = max(available, key=lambda bucket: (deficit[bucket], -order.index(bucket)))
        sequence.append(present[chosen][cursors[chosen]])
        cursors[chosen] += 1
        deficit[chosen] -= 1.0
    return sequence


def build_mood_chunks(pool: list[dict], chunk_count: int, policy: dict) -> list[dict]:
    """Cut the interleaved pool into `chunk_count` windows with a light overlap.

    Consecutive chunks repeat the tail of the previous window so a register that
    only appears at a window boundary is still seen by both neighbours.
    """
    if chunk_count <= 0 or not pool:
        return []
    chunk_chars = max(200, int(policy["mood_chunk_chars"]))
    overlap_ratio = min(0.5, max(0.0, float(policy["mood_chunk_overlap"])))
    sequence = _weighted_interleave(pool)
    chunks: list[dict] = []
    cursor = 0
    for index in range(chunk_count):
        lines: list[dict] = []
        seen: set[int] = set()
        chars = 0
        if chunks:
            previous = chunks[-1]["lines"]
            overlap = max(1, round(len(previous) * overlap_ratio))
            for line in previous[-overlap:]:
                text = str(line.get("text") or "")
                lines.append(line)
                seen.add(id(line))
                chars += len(text)
        added = 0
        while cursor < len(sequence):
            line = sequence[cursor]
            text = str(line.get("text") or "")
            if chars + len(text) > chunk_chars and added:
                break
            cursor += 1
            if id(line) in seen:
                continue
            lines.append(line)
            seen.add(id(line))
            chars += len(text)
            added += 1
        if not added and (index or not lines):
            # Pool exhausted: a chunk made only of repeated overlap would count
            # as a second opinion on evidence already judged.
            break
        chunks.append({"index": index, "lines": lines, "chars": chars, "new_lines": added})
    return chunks


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _entropy(distribution: dict[str, float]) -> float:
    """Normalised entropy of a probability vector: 0 = one option, 1 = flat."""
    values = [value for value in distribution.values() if value > 0]
    if len(values) <= 1:
        return 0.0
    entropy = -sum(value * math.log(value) for value in values)
    return round(entropy / math.log(len(distribution)), 4)


def _top(distribution: dict[str, float]) -> str:
    # Ties go to `none`: the safe answer wins any ambiguity.
    return max(distribution, key=lambda key: (distribution[key], key == "none"))


def aggregate_mood(
    chunk_answers: list[dict],
    policy: dict,
    *,
    line_count: int,
    provider: str,
    chunk_lines: list[int] | None = None,
) -> dict:
    """Combine per-chunk typed answers into one archon decision.

    Gates, in order of what they protect against: no winner (dispersion and
    `none`), one loud chunk (consistency), a confident but weak signal
    (probability and confidence), and scene emotion mistaken for a baseline
    (`mood.justified`).
    """
    if not chunk_answers:
        return insufficient_volume(line_count, policy, provider)
    presets = [answer["mood.preset"] for answer in chunk_answers]
    options = list(presets[0]["probabilities"])
    count = len(presets)
    mean_vector = {
        option: round(_mean([answer["probabilities"].get(option, 0.0) for answer in presets]), 4)
        for option in options
    }
    winner = _top(mean_vector)
    margin = float(policy.get("mood_consistency_margin", 0.05))
    agreeing = 0
    per_chunk = []
    for index, answer in enumerate(presets):
        distribution = answer["probabilities"]
        top = _top(distribution)
        if top == winner or distribution[winner] >= distribution[top] - margin:
            agreeing += 1
        per_chunk.append({
            "chunk": index,
            "lines": (chunk_lines or [None] * count)[index],
            "top": top,
            "selected_probability": round(distribution[winner], 4),
            "confidence": round(float(answer["confidence"]), 4),
            "justified": round(float(chunk_answers[index]["mood.justified"]["noul"]), 4),
            "probabilities": distribution,
        })
    mean_selected = round(_mean([item["selected_probability"] for item in per_chunk]), 4)
    mean_confidence = round(_mean([float(answer["confidence"]) for answer in presets]), 4)
    mean_justified = round(
        _mean([float(answer["mood.justified"]["noul"]) for answer in chunk_answers]), 4
    )
    dispersion = _entropy(mean_vector)
    required = max(1, math.ceil(count * float(policy.get("mood_consistency_ratio", 0.6))))

    blocked_by = []
    if winner == "none":
        blocked_by.append("none_winner")
    if agreeing < required:
        blocked_by.append("low_consistency")
    if mean_selected < float(policy["mood_probability"]):
        blocked_by.append("low_probability")
    if mean_confidence < float(policy["mood_confidence"]):
        blocked_by.append("low_confidence")
    if dispersion > float(policy.get("mood_dispersion_limit", 0.55)):
        blocked_by.append("high_dispersion")
    if mean_justified < float(policy["noul_yes"]):
        blocked_by.append("not_justified")
    accepted = not blocked_by
    return {
        "preset": winner if accepted else "none",
        "archon": winner if accepted else "neutral",
        "accepted": accepted,
        "justified": mean_justified >= float(policy["noul_yes"]),
        "gate": {
            "selected": winner,
            "confidence": mean_confidence,
            "selected_probability": mean_selected,
            "choice_accepted": accepted,
            "yes_probability": mean_justified,
            "line_count": line_count,
            "min_dialogue_lines": int(policy["min_dialogue_lines_for_mood"]),
            "chunks": count,
            "agreeing_chunks": agreeing,
            "required_chunks": required,
            "dispersion": dispersion,
            "thresholds": {
                "confidence": policy["mood_confidence"],
                "probability": policy["mood_probability"],
                "consistency_ratio": policy.get("mood_consistency_ratio", 0.6),
                "dispersion_limit": policy.get("mood_dispersion_limit", 0.55),
            },
        },
        "blocked_by": blocked_by,
        "probabilities": mean_vector,
        "aggregate": {
            "mean_probabilities": mean_vector,
            "agreeing_chunks": agreeing,
            "required_chunks": required,
            "mean_selected_probability": mean_selected,
            "mean_confidence": mean_confidence,
            "mean_justified": mean_justified,
            "dispersion": dispersion,
        },
        "chunks": per_chunk,
        "method": f"{provider}_chunks" if accepted else "default",
    }


def insufficient_volume(
    line_count: int, policy: dict, provider: str, reason: str = "insufficient_volume"
) -> dict:
    """No mood question is asked; the reason is recorded as a stable token.

    `insufficient_volume` means the corpus is below the floor. `insufficient_pool`
    means the dossier predates the mood pool, so the only dialogue it carries is
    the 24-line style sample - chunking that would be the single-scene guess the
    chunked path exists to prevent.
    """
    return {
        "preset": "none",
        "archon": "neutral",
        "accepted": False,
        "justified": False,
        "gate": {
            "selected": None,
            "confidence": None,
            "selected_probability": None,
            "choice_accepted": False,
            "yes_probability": None,
            "line_count": line_count,
            "min_dialogue_lines": int(policy["min_dialogue_lines_for_mood"]),
            "chunks": 0,
            "agreeing_chunks": 0,
            "required_chunks": 0,
            "dispersion": None,
        },
        "blocked_by": [reason],
        "probabilities": None,
        "chunks": [],
        "method": "default",
        "provider": provider,
    }
