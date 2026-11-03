from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from statistics import mean, median
from typing import Any

from .config import (
    DIALOGUE_SAMPLER_VERSION,
    STYLE_EVIDENCE_TIERS,
    STYLE_FIELD_EVIDENCE_MULTIPLIERS,
)

WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
CONTRACTION_RE = re.compile(r"\b[\w’]+(?:n't|'re|'ve|'ll|'d|'m|'s)\b", re.IGNORECASE)
HONORIFIC_RE = re.compile(
    r"\b(?:sir|madam|lord|lady|master|mistress|ser|dame|my liege)\b"
    r"|[-–](?:san|sama|kun|chan|dono)\b",
    re.IGNORECASE,
)
CLAUSE_RE = re.compile(
    r"\b(?:although|because|before|if|since|that|though|unless|until|when|where|while|which|who)\b",
    re.IGNORECASE,
)
ALL_CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")
ELLIPSIS_RE = re.compile(r"\.\.\.|…")
DASH_RE = re.compile(r"\s[—–]\s")

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for",
    "from", "had", "has", "have", "he", "her", "hers", "him", "his", "i",
    "if", "in", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "she", "so", "that", "the", "their", "them", "there", "they", "this",
    "to", "was", "we", "were", "what", "when", "where", "which", "who",
    "will", "with", "you", "your",
}


_TERM_CACHE: dict[str, re.Pattern[str]] = {}


def _term_pattern(term: str) -> re.Pattern[str]:
    pattern = _TERM_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
        _TERM_CACHE[term] = pattern
    return pattern


def terms_in(text: str, terms) -> list[str]:
    """Terms from a closed list that appear as whole words in `text`."""
    return [str(term) for term in terms or [] if _term_pattern(str(term)).search(text)]


def words(text: str) -> list[str]:
    return [word.casefold() for word in WORD_RE.findall(text or "")]


def _quantile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _distribution(values: list[int]) -> dict[str, int | float | None]:
    return {
        "count": len(values),
        "mean": round(mean(values), 3) if values else None,
        "median": round(median(values), 3) if values else None,
        "p10": _quantile(values, 0.10),
        "p90": _quantile(values, 0.90),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


SPEAKER_LABEL_RE = re.compile(r"^\(-([^-]{1,60})-\)\s*")


def split_speaker_label(text: str) -> tuple[str, str | None]:
    """Separate a `(-Label-)` stage direction from the spoken text.

    8,149 English lines carry such a label (aliases, `???`, crowd descriptions).
    It names who speaks, not what is said, so it must not reach the analysis or
    the model as if it were dialogue.
    """
    value = text or ""
    match = SPEAKER_LABEL_RE.match(value)
    if match is None:
        return value, None
    return value[match.end():], match.group(1)


def analyze_dialogue(lines: list[dict], vocabulary: dict | None = None) -> dict[str, Any]:
    distinct = {}
    for line in lines:
        line_id = line.get("voice_line_id") or line.get("dialog_id")
        if line_id is None:
            line_id = hashlib.sha256((line.get("text") or "").encode()).hexdigest()
        distinct.setdefault(str(line_id), line)
    texts = [
        split_speaker_label(str(line.get("text") or ""))[0] for line in distinct.values()
    ]
    raw_texts = [
        split_speaker_label(str(line.get("text") or ""))[0]
        for line in lines
    ]
    normalized_raw = [" ".join(words(text)) for text in raw_texts if words(text)]
    raw_counts = Counter(normalized_raw)
    duplicate_groups = {
        text: count for text, count in raw_counts.items() if count > 1
    }
    duplicate_rows = sum(count - 1 for count in duplicate_groups.values())
    duplicate_line_ids: dict[str, list[str]] = defaultdict(list)
    for line in lines:
        text = " ".join(words(split_speaker_label(str(line.get("text") or ""))[0]))
        if text in duplicate_groups:
            line_id = str(line.get("voice_line_id") or line.get("dialog_id") or "")
            if line_id:
                duplicate_line_ids[text].append(line_id)
    repeated_utterances = [
        {
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "count": count,
            "line_ids": sorted(duplicate_line_ids.get(text, []))[:12],
        }
        for text, count in sorted(
            duplicate_groups.items(), key=lambda item: (-item[1], item[0])
        )[:12]
    ]
    tokenized = [words(text) for text in texts]
    flat = [token for row in tokenized for token in row]
    sentence_lengths = [len(row) for row in tokenized if row]
    clauses = [1 + len(CLAUSE_RE.findall(text)) + text.count(";") for text in texts if words(text)]
    punctuation = Counter(char for text in texts for char in text if char in "?!,;:—–-…")
    contractions = sum(len(CONTRACTION_RE.findall(text)) for text in texts)
    honorifics = sum(len(HONORIFIC_RE.findall(text)) for text in texts)
    questions = sum(text.count("?") for text in texts)
    exclamations = sum(text.count("!") for text in texts)
    statements = max(0, len(texts) - questions - exclamations)
    vocab = (vocabulary or {}).get("speech_style", {})
    signals = vocab.get("signals", {})
    colloquial_terms = [str(term).casefold() for term in signals.get("colloquial_terms", [])]
    formal_terms = [str(term).casefold() for term in signals.get("formal_terms", [])]
    colloquial_hits = sum(len(terms_in(text, colloquial_terms)) for text in texts)
    formal_hits = sum(len(terms_in(text, formal_terms)) for text in texts)
    total_words = max(1, len(flat))
    colloquial_rate = colloquial_hits / total_words
    formal_rate = formal_hits / total_words
    contraction_rate = contractions / total_words
    average_word_length = mean([len(word) for word in flat]) if flat else 0.0
    unique_tokens = len(set(flat))
    vocabulary_diversity = unique_tokens / len(flat) if flat else 0.0
    avg_clauses = mean(clauses) if clauses else 0.0
    average_sentence_length = mean(sentence_lengths) if sentence_lengths else 0.0

    if formal_rate >= 0.025 and colloquial_rate < 0.035:
        register = "formal"
    elif colloquial_rate >= 0.07 or contraction_rate >= 0.055:
        register = "conversational"
    else:
        register = "neutral"

    if average_word_length < 4.0 and vocabulary_diversity < 0.35:
        lexical_complexity = "plain"
    elif average_word_length >= 6.2 or (
        average_word_length >= 5.5 and vocabulary_diversity >= 0.5
    ):
        lexical_complexity = "advanced"
    else:
        lexical_complexity = "ordinary"

    sentence_complexity = "complex" if avg_clauses >= 2.0 or average_sentence_length >= 24 else (
        "mixed" if avg_clauses >= 1.35 or average_sentence_length >= 12 else "simple"
    )
    colloquialism = "high" if colloquial_rate >= 0.07 else (
        "moderate" if colloquial_rate >= 0.025 or contraction_rate >= 0.04 else "low"
    )
    diction_style = "precise" if formal_rate >= 0.035 else (
        "relaxed" if colloquialism == "high" else "plain"
    )
    narrative_markers = sum(
        len(re.findall(r"\b(?:after|before|then|once|when|while|later|finally)\b", text, re.IGNORECASE))
        for text in texts
    )
    reflective_markers = sum(
        len(re.findall(r"\b(?:perhaps|maybe|wonder|remember|regret|if only|suppose|doubt)\b", text, re.IGNORECASE))
        for text in texts
    )
    if questions / max(1, len(texts)) >= 0.35:
        discourse_style = "inquisitive"
    elif exclamations / max(1, len(texts)) >= 0.2 and sentence_complexity != "complex":
        discourse_style = "exhortative"
    elif narrative_markers / max(1, len(texts)) >= 1.0 and avg_clauses >= 1.35:
        discourse_style = "narrative"
    elif reflective_markers / max(1, len(texts)) >= 0.5:
        discourse_style = "reflective"
    elif sentence_complexity == "simple":
        discourse_style = "direct"
    else:
        discourse_style = "explanatory"

    contraction_frequency = "high" if contraction_rate >= 0.055 else (
        "moderate" if contraction_rate >= 0.02 else "low"
    )
    habits = []
    habit_signals = signals.get("habit_rules", {})
    if contractions >= int(habit_signals.get("contractions_min", 5)):
        habits.append("habitual_contractions")
    if honorifics >= int(habit_signals.get("honorifics_min", 3)):
        habits.append("honorific_address")
    if questions / max(1, len(texts)) >= float(habit_signals.get("question_ratio_min", 0.35)):
        habits.append("rhetorical_questions")
    if exclamations >= int(habit_signals.get("exclamations_min", 5)):
        habits.append("frequent_exclamations")
    if len(sentence_lengths) and sum(length <= 5 for length in sentence_lengths) / len(sentence_lengths) >= 0.45:
        habits.append("short_utterances")
    if not habits:
        habits = ["none_observed"]

    non_stop = [token for token in flat if token not in STOP_WORDS and len(token) > 2]
    unigram_counts = Counter(non_stop)
    bigram_counts = Counter(
        f"{left} {right}" for row in tokenized
        for left, right in zip(row, row[1:])
        if left not in STOP_WORDS and right not in STOP_WORDS
    )
    formal_consistency = round(max(formal_rate, colloquial_rate, 1 - min(1, formal_rate + colloquial_rate)), 3)

    # Text-only cues a listener would actually hear read aloud. They are counts
    # over the whole corpus, never a label, so the model can weigh them itself.
    line_total = max(1, len(texts))
    length_share = {
        bucket: round(sum(_length_bucket(text) == bucket for text in texts) / line_total, 4)
        for bucket in ("short", "medium", "long")
    }
    dialect_terms = list(signals.get("dialect_terms") or [])
    signal_lines = {
        "dialect": sum(bool(terms_in(text, dialect_terms)) for text in texts),
        "colloquial": sum(bool(terms_in(text, colloquial_terms)) for text in texts),
        "formal": sum(bool(terms_in(text, formal_terms)) for text in texts),
        "contraction": sum(bool(CONTRACTION_RE.search(text)) for text in texts),
        "honorific": sum(bool(HONORIFIC_RE.search(text)) for text in texts),
        "question": sum("?" in text for text in texts),
        "exclamation": sum("!" in text for text in texts),
        "all_caps": sum(bool(ALL_CAPS_RE.search(text)) for text in texts),
        "ellipsis": sum(bool(ELLIPSIS_RE.search(text)) for text in texts),
    }
    per_line_rates = {
        f"{name}_lines": round(count / line_total, 4) for name, count in signal_lines.items()
    }
    measurements = {
        "raw_line_count": len(lines),
        "line_count": len(texts),
        "word_count": len(flat),
        "duplicate": {
            "exact_duplicate_rows": duplicate_rows,
            "exact_duplicate_groups": len(duplicate_groups),
            "exact_duplicate_rate": round(duplicate_rows / max(1, len(raw_texts)), 5),
            "repeated_utterances": repeated_utterances,
            "note": "Duplicate frequency is retained separately from syntax/style sampling so repeated catchphrases remain measurable habits.",
        },
        "unique_word_count": unique_tokens,
        "vocabulary_diversity": round(vocabulary_diversity, 4),
        "average_word_length": round(average_word_length, 3),
        "words_per_utterance": _distribution(sentence_lengths),
        "clauses_per_utterance": _distribution(clauses),
        "contractions": contractions,
        "contraction_rate_per_word": round(contraction_rate, 5),
        "formal_signal_hits": formal_hits,
        "formal_signal_rate_per_word": round(formal_rate, 5),
        "colloquial_signal_hits": colloquial_hits,
        "colloquial_signal_rate_per_word": round(colloquial_rate, 5),
        "punctuation": dict(sorted(punctuation.items())),
        "question_count": questions,
        "statement_count": statements,
        "exclamation_count": exclamations,
        "honorific_and_form_of_address_count": honorifics,
        "utterance_length_share": length_share,
        "signal_line_counts": signal_lines,
        "signal_line_rates": per_line_rates,
        "stylistic_consistency": {
            "formal_or_colloquial_signal_dominance": formal_consistency,
            "method": "deterministic rate comparison over the closed signal lists in vocabulary.json",
        },
        "repeated_words": [
            {"value": value, "count": count}
            for value, count in unigram_counts.most_common(12) if count > 1
        ],
        "repeated_expressions": [
            {"value": value, "count": count}
            for value, count in bigram_counts.most_common(12) if count > 1
        ],
        "distinctive_vocabulary": [
            {"value": value, "count": count}
            for value, count in unigram_counts.most_common(20)
        ],
    }
    interpretation = {
        "register": register,
        "lexical_complexity": lexical_complexity,
        "sentence_complexity": sentence_complexity,
        "colloquialism": colloquialism,
        "discourse_style": discourse_style,
        "lexical_features": {
            "diction_style": diction_style,
            "contraction_frequency": contraction_frequency,
            "technical_or_domain_specific": (
                "present" if vocabulary_diversity >= 0.6 else "not_observed"
            ),
            "poetic_or_figurative": (
                "present" if vocabulary_diversity >= 0.58 and average_word_length >= 5.5
                else "not_observed"
            ),
            "archaic_or_formulaic": (
                "present" if formal_rate >= 0.025 and formal_hits >= 3 else "not_observed"
            ),
            "regional_or_dialectal_spelling": (
                "present" if signal_lines["dialect"] >= 2 else "not_observed"
            ),
        },
        "verbal_habits": habits,
        "speech_habits": habits,
        "method": "rules-derived interpretation from the measurements above; not acoustic evidence",
    }
    return {
        "measurements": measurements,
        "interpretation": interpretation,
    }


def _length_bucket(text: str) -> str:
    count = len(words(text))
    if count <= 8:
        return "short"
    if count <= 24:
        return "medium"
    return "long"


# Public name for the chunk planner, which stratifies by the same buckets.
length_bucket = _length_bucket


def _line_key(line: dict) -> str:
    return str(line.get("voice_line_id") or line.get("dialog_id") or "")


def _rank(canonical_id: str, line: dict) -> str:
    token = f"{canonical_id}:{_line_key(line)}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sample_id(canonical_id: str, line: dict) -> str:
    """Stable opaque ID safe to send to a text-only classifier."""
    key = _line_key(line)
    if not key:
        # Hand-built or trimmed rows may carry no database id. Hash the text so
        # distinct lines never collapse into one opaque request id.
        key = "text:" + hashlib.sha256(
            str(line.get("text") or "").encode("utf-8")
        ).hexdigest()
    token = f"{canonical_id}:{key}:{DIALOGUE_SAMPLER_VERSION}"
    return "L" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16].upper()


def sample_manifest_hash(
    canonical_id: str,
    samples: list[dict],
    *,
    corpus_hash: str | None = None,
    sampler_version: str = DIALOGUE_SAMPLER_VERSION,
) -> str:
    payload = {
        "canonical_id": canonical_id,
        "sampler_version": sampler_version,
        "corpus_hash": corpus_hash,
        "samples": [
            {
                "id": sample.get("id") or sample_id(canonical_id, sample),
                "text_sha256": hashlib.sha256(
                    str(sample.get("text") or "").encode("utf-8")
                ).hexdigest(),
            }
            for sample in samples
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_UNKNOWN_CONTEXT_VALUES = {"", "unknown", "none", "null", "n/a", "na"}


def known_context_part(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    return None if text in _UNKNOWN_CONTEXT_VALUES else text


def independent_context_key(sample: dict) -> str | None:
    """Canonical local context key for one sampled line.

    A source/sheet pair is the preferred identity. If only one side is known,
    it is used alone; unknown placeholders never create a fake independent
    context. Case and repeated exports collapse to one key.
    """
    source = known_context_part(sample.get("source"))
    sheet = known_context_part(sample.get("sheet"))
    if source and sheet:
        return f"{source}::{sheet}"
    if source:
        return f"{source}::*"
    if sheet:
        return f"*::{sheet}"
    return None


def independent_context_count(samples: list[dict]) -> int:
    return len({
        key for sample in samples
        if (key := independent_context_key(sample)) is not None
    })


NEAR_DUPLICATE_JACCARD = 0.82
# SE reuses quest templates and swaps one noun ("...the contours jus' right" /
# "...the texture jus' right"). Trigram Jaccard sits at 0.70 there, so word
# containment catches the template and the truncated re-recording too.
NEAR_DUPLICATE_CONTAINMENT = 0.80


def _duplicate_signature(text: str) -> tuple[frozenset[str], frozenset[tuple[str, ...]], int]:
    """Word set, word-trigram set, and word count, computed once per line.

    Pool selection compares every candidate against everything already chosen.
    Rebuilding these sets inside the comparison made a 1,300-line pool cost
    hundreds of thousands of redundant set constructions, so the predicate takes
    the precomputed signature instead.
    """
    word_list = words(text)
    return (
        frozenset(word_list),
        frozenset(zip(word_list, word_list[1:], word_list[2:])),
        len(word_list),
    )


def _near_duplicate_signature(left: tuple, right: tuple) -> bool:
    left_set, left_grams, left_length = left
    right_set, right_grams, right_length = right
    if not left_set or not right_set:
        return True
    if not left_grams and not right_grams:
        return left_set == right_set
    intersection = len(left_grams & right_grams)
    union = max(1, len(left_grams | right_grams))
    if intersection / union >= NEAR_DUPLICATE_JACCARD:
        return True
    containment = len(left_set & right_set) / max(1, min(len(left_set), len(right_set)))
    length_ratio = min(left_length, right_length) / max(left_length, right_length)
    return containment >= NEAR_DUPLICATE_CONTAINMENT and length_ratio >= 0.5


def _near_duplicate(left: str, right: str) -> bool:
    return _near_duplicate_signature(_duplicate_signature(left), _duplicate_signature(right))


def _duplicate_keys(signature: tuple) -> frozenset:
    """Index keys that a near-duplicate pair is guaranteed to share."""
    _, grams, _ = signature
    return grams if grams else signature[0]


BUCKETS = ("short", "medium", "long")


def _bucket_quotas(counts: dict[str, int], max_count: int) -> dict[str, int]:
    """Split `max_count` across the buckets that exist, by corpus share.

    Every present bucket gets at least one slot, so a character with any long
    lines is never judged on short ones alone.
    """
    present = {bucket: counts.get(bucket, 0) for bucket in BUCKETS if counts.get(bucket, 0)}
    total = sum(present.values())
    if not total:
        return {}
    quotas = {bucket: max(1, round(max_count * count / total)) for bucket, count in present.items()}
    while sum(quotas.values()) > max_count:
        largest = max(sorted(quotas), key=lambda bucket: quotas[bucket])
        quotas[largest] -= 1
        if not quotas[largest]:
            del quotas[largest]
    order = sorted(present, key=lambda bucket: -present[bucket])
    index = 0
    while sum(quotas.values()) < max_count:
        quotas[order[index % len(order)]] += 1
        index += 1
    return quotas


def _signal_hits(text: str, signals: dict) -> list[str]:
    """Name the closed-vocabulary signals a line actually contains.

    These names travel with the sample, so a style answer can be traced
    back to the lines that supported it instead of an opaque "the model read
    something".
    """
    hits = []
    for label in ("colloquial", "formal", "dialect"):
        for term in terms_in(text, signals.get(f"{label}_terms") or []):
            hits.append(f"{label}:{term}")
    if CONTRACTION_RE.search(text):
        hits.append("contraction")
    if HONORIFIC_RE.search(text):
        hits.append("honorific")
    if "?" in text:
        hits.append("question")
    if "!" in text:
        hits.append("exclamation")
    if ALL_CAPS_RE.search(text):
        hits.append("all_caps")
    if ELLIPSIS_RE.search(text):
        hits.append("ellipsis")
    return hits


def _signal_weight(hits: list[str]) -> int:
    weights = {
        "colloquial": 3, "formal": 3, "dialect": 3, "all_caps": 2, "honorific": 2,
        "exclamation": 1, "question": 1, "ellipsis": 1, "contraction": 1,
    }
    return sum(weights.get(hit.split(":", 1)[0], 0) for hit in hits)


def _prepare_lines(canonical_id: str, lines: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for line in lines:
        key = _line_key(line)
        if not key:
            continue
        text = str(line.get("text") or "")
        if not text.strip():
            continue
        text, speaker_label = split_speaker_label(text)
        current = by_id.get(key)
        if current is None:
            entry = dict(line)
            entry["text"] = text
            if speaker_label:
                entry["speaker_label"] = speaker_label
            by_id[key] = entry
        else:
            provenance = current.setdefault("provenance", [])
            provenance.extend(line.get("provenance", []))
    unique = list(by_id.values())
    for line in unique:
        text = str(line.get("text") or "")
        line["_bucket"] = _length_bucket(text)
        line["_rank"] = _rank(canonical_id, line)
        line["_signature"] = _duplicate_signature(text)
        line["_duplicate_keys"] = _duplicate_keys(line["_signature"])
        line["id"] = sample_id(canonical_id, line)
        line.setdefault("provenance", [{
            "dialog_id": line.get("dialog_id"),
            "source": line.get("source"),
            "sheet": line.get("sheet"),
            "row_id": line.get("row_id"),
            "voice_line_id": line.get("voice_line_id"),
        }])
    return unique


def _public_sample(line: dict, *, signals: bool = False) -> dict:
    sample = {
        key: value for key, value in line.items()
        if not key.startswith("_") and key not in {"signals", "signal_weight"}
        and value not in (None, "", [])
    }
    sample["length_bucket"] = line.get("_bucket")
    if signals:
        sample["signals"] = list(line.get("signals") or [])
        sample["signal_weight"] = int(line.get("_weight") or 0)
    return sample


def _group_key(line: dict) -> str:
    context = independent_context_key(line)
    return context if context is not None else "*::unknown"


def _group_rank(canonical_id: str, group: str) -> str:
    return hashlib.sha256(f"{canonical_id}:{group}".encode("utf-8")).hexdigest()


def select_representative_samples(
    canonical_id: str,
    lines: list[dict],
    *,
    max_count: int = 40,
    max_chars: int = 12000,
    vocabulary: dict | None = None,
) -> list[dict]:
    """Select a deterministic, signal-free sample for primary Style classification.

    Selection is stratified by utterance length and round-robins across
    (source, sheet) groups. Regex signal scores never influence this pool; a
    separate targeted pool exists for rare-habit verification.
    """
    del vocabulary  # kept for API compatibility; the primary pool is signal-free
    unique = _prepare_lines(canonical_id, lines)
    selected: list[dict] = []
    consumed: set[str] = set()
    consumed_chars = 0
    selected_signatures: list[tuple] = []
    duplicate_index: dict[Any, list[int]] = defaultdict(list)
    near_duplicate_rejections = 0

    def consider(line: dict) -> bool:
        nonlocal consumed_chars, near_duplicate_rejections
        key = _line_key(line)
        text = str(line.get("text") or "")
        if key in consumed or not text or consumed_chars + len(text) > max_chars:
            return False
        signature = line["_signature"]
        candidates: set[int] = set()
        for duplicate_key in line["_duplicate_keys"]:
            candidates.update(duplicate_index.get(duplicate_key, ()))
        if any(
            _near_duplicate_signature(signature, selected_signatures[index])
            for index in candidates
        ):
            near_duplicate_rejections += 1
            return False
        selected.append(_public_sample(line))
        selected_signatures.append(signature)
        for duplicate_key in line["_duplicate_keys"]:
            duplicate_index[duplicate_key].append(len(selected) - 1)
        consumed.add(key)
        consumed_chars += len(text)
        return True

    by_bucket: dict[str, list[dict]] = {bucket: [] for bucket in BUCKETS}
    for line in unique:
        by_bucket[line["_bucket"]].append(line)
    for rows in by_bucket.values():
        rows.sort(key=lambda line: line["_rank"])
    quotas = _bucket_quotas(
        {bucket: len(rows) for bucket, rows in by_bucket.items()}, max_count
    )

    for bucket, quota in quotas.items():
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for line in by_bucket[bucket]:
            grouped[_group_key(line)].append(line)
        groups = sorted(grouped, key=lambda group: _group_rank(canonical_id, group))
        for rows in grouped.values():
            rows.sort(key=lambda line: line["_rank"])
        cursors = {group: 0 for group in groups}
        taken = 0
        while taken < quota and len(selected) < max_count:
            progressed = False
            for group in groups:
                rows = grouped[group]
                while cursors[group] < len(rows):
                    line = rows[cursors[group]]
                    cursors[group] += 1
                    if consider(line):
                        taken += 1
                        progressed = True
                        break
                if taken >= quota or len(selected) >= max_count:
                    break
            if not progressed:
                break

    for line in sorted(unique, key=lambda item: item["_rank"]):
        if len(selected) >= max_count:
            break
        consider(line)
    for sample in selected:
        sample["near_duplicate_rejections"] = near_duplicate_rejections
    return selected


def select_targeted_samples(
    canonical_id: str,
    lines: list[dict],
    *,
    max_count: int = 24,
    max_chars: int = 4000,
    vocabulary: dict | None = None,
) -> list[dict]:
    """Select signal-focused examples for habit verification only.

    This pool is never merged into the representative distribution or used to
    estimate the character's typical Style.
    """
    unique = _prepare_lines(canonical_id, lines)
    signals = ((vocabulary or {}).get("speech_style") or {}).get("signals") or {}
    for line in unique:
        line["signals"] = _signal_hits(str(line.get("text") or ""), signals)
        line["_weight"] = _signal_weight(line["signals"])
    candidates = [line for line in unique if line["signals"]]
    candidates.sort(key=lambda line: (-line["_weight"], line["_rank"]))

    selected: list[dict] = []
    selected_signatures: list[tuple] = []
    duplicate_index: dict[Any, list[int]] = defaultdict(list)
    consumed_chars = 0
    for line in candidates:
        if len(selected) >= max_count:
            break
        text = str(line.get("text") or "")
        if not text or consumed_chars + len(text) > max_chars:
            continue
        signature = line["_signature"]
        candidates_index: set[int] = set()
        for duplicate_key in line["_duplicate_keys"]:
            candidates_index.update(duplicate_index.get(duplicate_key, ()))
        if any(
            _near_duplicate_signature(signature, selected_signatures[index])
            for index in candidates_index
        ):
            continue
        selected.append(_public_sample(line, signals=True))
        selected_signatures.append(signature)
        for duplicate_key in line["_duplicate_keys"]:
            duplicate_index[duplicate_key].append(len(selected) - 1)
        consumed_chars += len(text)
    return selected


def build_dialogue_sample_pools(
    canonical_id: str,
    lines: list[dict],
    *,
    vocabulary: dict | None = None,
    max_count: int = 40,
    max_chars: int = 12000,
    targeted_count: int = 24,
    targeted_chars: int = 4000,
    corpus_hash: str | None = None,
) -> dict[str, Any]:
    representative = select_representative_samples(
        canonical_id, lines, max_count=max_count, max_chars=max_chars,
        vocabulary=vocabulary,
    )
    targeted = select_targeted_samples(
        canonical_id, lines, max_count=targeted_count, max_chars=targeted_chars,
        vocabulary=vocabulary,
    )
    def summary(samples: list[dict]) -> dict[str, Any]:
        return {
            "count": len(samples),
            "hash": sample_manifest_hash(
                canonical_id, samples, corpus_hash=corpus_hash,
            ),
            "ids": [sample["id"] for sample in samples],
            "buckets": dict(sorted(Counter(
                sample.get("length_bucket") for sample in samples
            ).items())),
            "sources": dict(sorted(Counter(
                str(sample.get("source") or "unknown") for sample in samples
            ).items())),
            "sheets": len({
                (str(sample.get("source") or "unknown"), str(sample.get("sheet") or "unknown"))
                for sample in samples
            }),
        }
    return {
        "sampler_version": DIALOGUE_SAMPLER_VERSION,
        "corpus_hash": corpus_hash,
        "representative": summary(representative),
        "targeted": summary(targeted),
        "targeted_signal_counts": dict(sorted(Counter(
            signal.split(":", 1)[0]
            for sample in targeted for signal in sample.get("signals") or []
        ).items())),
        "note": "Representative and targeted pools are independent; targeted evidence never enters the primary distribution.",
        "representative_samples": representative,
        "targeted_samples": targeted,
    }


def evidence_sufficiency(
    statistics: dict,
    samples: list[dict],
    *,
    vocabulary: dict | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    """Return the evidence tier for the lines the classifier actually sees."""
    sampled_lines = len(samples)
    sampled_words = sum(len(words(str(sample.get("text") or ""))) for sample in samples)
    sources = {
        source for sample in samples
        if (source := known_context_part(sample.get("source"))) is not None
    }
    contexts = {
        key for sample in samples
        if (key := independent_context_key(sample)) is not None
    }
    unique_corpus_lines = int(
        statistics.get("unique_line_count") or statistics.get("dialogue_count") or 0
    )
    configured = ((vocabulary or {}).get("speech_style") or {}).get("sufficiency") or {}
    tiers = {
        name: {**STYLE_EVIDENCE_TIERS[name], **configured.get(name, {})}
        for name in STYLE_EVIDENCE_TIERS
    }
    multiplier = float(
        configured.get("field_multipliers", {}).get(
            field, STYLE_FIELD_EVIDENCE_MULTIPLIERS.get(field, 1.0)
        )
    ) if field else 1.0
    def met(level: str) -> bool:
        rule = tiers[level]
        return (
            sampled_lines >= math.ceil(int(rule["lines"]) * multiplier)
            and sampled_words >= math.ceil(int(rule["words"]) * multiplier)
            and len(contexts) >= int(rule["contexts"])
        )
    if met("strong"):
        tier = "strong"
    elif met("supported"):
        tier = "supported"
    elif met("minimum"):
        tier = "provisional"
    else:
        tier = "insufficient"
    return {
        "tier": tier,
        "field": field,
        "sampled_lines": sampled_lines,
        "sampled_words": sampled_words,
        "sources": sorted(sources),
        "source_count": len(sources),
        "context_count": len(contexts),
        "unique_corpus_lines": unique_corpus_lines,
        "thresholds": tiers,
        "field_multiplier": multiplier,
    }


def prompt_evidence_sufficiency(
    statistics: dict,
    samples: list[dict],
    *,
    vocabulary: dict | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    """Evidence metadata for a model request, without source or sheet names.

    Source and context names remain available in the application's audit record;
    the classifier only needs aggregate counts to judge sufficiency.
    """
    evidence = evidence_sufficiency(
        statistics, samples, vocabulary=vocabulary, field=field,
    )
    evidence.pop("sources", None)
    return evidence
