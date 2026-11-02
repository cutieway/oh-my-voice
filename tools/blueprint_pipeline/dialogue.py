from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from statistics import mean, median
from typing import Any

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
        vocabulary_complexity = "plain"
    elif average_word_length >= 6.4:
        vocabulary_complexity = "educated"
    elif average_word_length >= 5.5 and vocabulary_diversity >= 0.48:
        vocabulary_complexity = "specialized"
    elif vocabulary_diversity >= 0.58:
        vocabulary_complexity = "poetic"
    else:
        vocabulary_complexity = "ordinary"

    sentence_complexity = "complex" if avg_clauses >= 2.0 or average_sentence_length >= 24 else (
        "mixed" if avg_clauses >= 1.35 or average_sentence_length >= 12 else "simple"
    )
    colloquialism = "high" if colloquial_rate >= 0.07 else (
        "moderate" if colloquial_rate >= 0.025 or contraction_rate >= 0.04 else "low"
    )
    diction_style = "precise" if formal_rate >= 0.035 else (
        "relaxed" if colloquialism == "high" else "plain"
    )
    if avg_clauses >= 1.8 and punctuation[","] / max(1, len(texts)) >= 0.6:
        rhetorical_style = "flowing"
    elif questions / max(1, len(texts)) >= 0.35:
        rhetorical_style = "inquisitive"
    elif sentence_complexity == "simple":
        rhetorical_style = "direct"
    elif sum(len(row) <= 5 for row in tokenized) / max(1, len(texts)) >= 0.45:
        rhetorical_style = "fragmented"
    else:
        rhetorical_style = "measured"

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
        "line_count": len(texts),
        "word_count": len(flat),
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
        "vocabulary_complexity": vocabulary_complexity,
        "sentence_complexity": sentence_complexity,
        "colloquialism": colloquialism,
        "diction_style": diction_style,
        "rhetorical_style": rhetorical_style,
        "contraction_frequency": contraction_frequency,
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


def _rank(canonical_id: str, line: dict) -> str:
    token = f"{canonical_id}:{line.get('voice_line_id') or line.get('dialog_id')}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


def select_representative_samples(
    canonical_id: str,
    lines: list[dict],
    *,
    max_count: int = 24,
    max_chars: int = 6000,
    vocabulary: dict | None = None,
) -> list[dict]:
    by_id = {}
    for line in lines:
        key = str(line.get("voice_line_id") or line.get("dialog_id") or "")
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
    signals = ((vocabulary or {}).get("speech_style") or {}).get("signals") or {}
    for line in unique:
        text = str(line.get("text") or "")
        line["_bucket"] = _length_bucket(text)
        line["_rank"] = _rank(canonical_id, line)
        line["signals"] = _signal_hits(text, signals)
        line["_weight"] = _signal_weight(line["signals"])
        line["_signature"] = _duplicate_signature(text)
        line["_duplicate_keys"] = _duplicate_keys(line["_signature"])
        line.setdefault("provenance", [{
            "dialog_id": line.get("dialog_id"),
            "source": line.get("source"),
            "sheet": line.get("sheet"),
            "row_id": line.get("row_id"),
            "voice_line_id": line.get("voice_line_id"),
        }])
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for line in unique:
        groups[(str(line.get("source") or "unknown"), str(line.get("sheet") or "unknown"))].append(line)
    for rows in groups.values():
        # Strongest stylistic evidence first, then a stable per-NPC order, so the
        # same dossier always yields the same sample set.
        rows.sort(key=lambda line: (-line["_weight"], line["_rank"]))

    selected = []
    consumed = set()
    consumed_chars = 0
    selected_signatures = []
    duplicate_index: dict[Any, list[int]] = defaultdict(list)

    def consider(line: dict) -> bool:
        nonlocal consumed_chars
        key = str(line.get("voice_line_id") or line.get("dialog_id"))
        text = str(line.get("text") or "")
        if key in consumed or not text or consumed_chars + len(text) > max_chars:
            return False
        signature = line["_signature"]
        candidates = set()
        for duplicate_key in line["_duplicate_keys"]:
            candidates.update(duplicate_index.get(duplicate_key, ()))
        if any(
            _near_duplicate_signature(signature, selected_signatures[index])
            for index in candidates
        ):
            return False
        cleaned = {k: v for k, v in line.items() if not k.startswith("_")}
        selected.append(cleaned)
        selected_signatures.append(signature)
        for duplicate_key in line["_duplicate_keys"]:
            duplicate_index[duplicate_key].append(len(selected) - 1)
        consumed.add(key)
        consumed_chars += len(text)
        return True

    # Bucket coverage is allocated by corpus share, not by iteration order.
    # Taking one line per (source, sheet) group inside a bucket saturates the
    # quota from the first bucket whenever an NPC has many sheets: Gerolt has
    # 200+ sheets and 181 short / 953 medium / 947 long lines, and the old loop
    # filled all 24 slots with short lines before reaching `medium`.
    by_bucket = {bucket: [] for bucket in BUCKETS}
    for line in unique:
        by_bucket[line["_bucket"]].append(line)
    for rows in by_bucket.values():
        rows.sort(key=lambda line: (-line["_weight"], line["_rank"]))
    quotas = _bucket_quotas({bucket: len(rows) for bucket, rows in by_bucket.items()}, max_count)

    for bucket, quota in quotas.items():
        grouped = defaultdict(list)
        for line in by_bucket[bucket]:
            grouped[(str(line.get("source") or "unknown"), str(line.get("sheet") or "unknown"))].append(line)
        cursors = {group: 0 for group in sorted(grouped)}
        taken = 0
        while taken < quota and len(selected) < max_count:
            progressed = False
            for group in sorted(grouped):
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

    # Leftover slots (dedupe blocked, or a bucket ran dry) come from the
    # strongest lines overall, so the sample still fills up on rich characters.
    for line in sorted(unique, key=lambda item: (-item["_weight"], item["_rank"])):
        if len(selected) >= max_count:
            break
        consider(line)
    return selected
