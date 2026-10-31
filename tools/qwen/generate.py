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
compute IDs the plugin never requests. The single exception is the leading
"(-Name-)" label that quest and custom sheets use to title a line: it stays
in Sentence and in the ID, and is dropped only from the spoken text.

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

Progress is printed as [completed/remaining], where remaining is the number
of lines that still need work after fingerprint checks. Reused and preserved
lines are summarized at startup and silent unless --verbose is given.

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


# Duration calibration for the runaway-output check. The old 1.05 grace
# rejected slow-but-valid dramatic deliveries. 1.47 covers the slowest of the
# 29-line 2026-10-25 audition: 7.45 s against a 5.09 s word-rate budget
# (1.463x), while still catching multi-second appended speech.
DURATION_LIMIT_FACTOR = 1.47


def duration_limit(text, min_wps):
    return max(4.5, len(text.split()) / min_wps + 1.0) * DURATION_LIMIT_FACTOR


# The canonical Sentence keeps the player-name macro so IDs stay stable. Only
# the text sent to TTS is rewritten. A vocative macro (`Greetings, _FIRSTNAME_.`)
# can simply disappear; a referential one (`_FIRSTNAME_ and I`) cannot. Pack
# schema 2 makes the root `<id>.ogg` the singular-they rendering and writes
# `<id>.she.ogg` / `<id>.he.ogg` beside it. This keeps neutral audio available
# without a fourth file.
_NAME_TOKEN_RE = re.compile(r"_(?:FIRSTNAME|LASTNAME)_(?:['’]s)?")

# Quest and custom sheets prepend the speaker's display name to the text as
# "(-Name-)" ("(-Krile-)Worked, did it?"). That label titles the text box; it
# is not dialogue, so it must never be spoken. It stays in the canonical
# Sentence and the line ID: the plugin hashes the sanitized text verbatim.
# Only the label prefix is removed: a line that names itself again right after
# it ("(-Jandelaine-)Jandelaine shall hasten to the scene.") keeps that name,
# because the authored sentence needs it.
_SPEAKER_LABEL_RE = re.compile(r"^\(-(?:.*?)-\)[ \t]*")
_VOCATIVE_FOLLOW = set(",.;:!?-–—")
_VOCATIVE_BEFORE = set("([{)]}.!?;:,-–—")
_PREPOSITIONS = {
    "to", "of", "for", "with", "about", "on", "at", "from", "by", "in",
    "into", "toward", "towards", "upon", "against", "over", "under",
    "between", "among", "without", "within", "behind", "beside", "near",
}
_OBJECT_PREV = _PREPOSITIONS | {
    "entrusted", "address", "addressed", "test", "tested", "take", "took",
    "ask", "asked", "leave", "leaves", "left", "expected", "expect",
    "trust", "trusted",
}
_OBJECT_PRONOUNS = {"you", "me", "us", "them", "him", "her", "it"}
# A name coordinated with another person ("_FIRSTNAME_ and Raha") takes the
# case of the whole noun phrase, never the case of its own slot. The pronoun
# in the other slot settles it; otherwise the governing slot decides: a
# predicate verb makes the coordination a subject, a preposition or an
# already-finished clause makes it an object.
_COORD_SUBJECT_PRONOUNS = {"i", "we", "he", "she", "they"}
_COORD_OBJECT_PRONOUNS = {"me", "us", "him", "her", "them"}
_COORD_NAME = r"[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)*"
_COORD_PARTNER = rf"(?:{_COORD_NAME}|I|we|he|she|they|you|me|us|him|her|them)"
_COPULA_AUXILIARY = {
    "is", "are", "was", "were", "be", "been", "being", "am", "will",
    "would", "shall", "can", "could", "should", "must", "may", "might",
    "has", "have", "had", "do", "does", "did",
}
_COORD_SUBORDINATORS = {
    "while", "when", "whenever", "if", "as", "because", "though",
    "although", "that", "until", "unless", "whereas", "whilst",
}
_SUBJECT_NEXT = {
    "is", "was", "were", "has", "had", "have", "will", "would", "can",
    "could", "should", "must", "may", "might", "does", "did", "do",
    "makes", "make", "made", "returns", "return", "returned", "fails",
    "fail", "failed", "learned", "learns", "explained", "explains",
    "arrived", "arrives", "comes", "came", "goes", "went", "knows",
    "knew", "thinks", "thought", "wants", "wanted", "needs", "needed",
    "likes", "liked", "seems", "seemed", "remains", "remained", "takes",
    "took", "gives", "gave", "says", "said", "tells", "told", "asks",
    "asked", "finds", "found", "keeps", "kept", "looks", "looked",
    "feels", "felt", "lives", "lived", "works", "worked",
}
_QUESTION_WORDS = {
    "what", "who", "whom", "whose", "where", "when", "why", "how",
}
_COORD_FINITE = _SUBJECT_NEXT | _COPULA_AUXILIARY | {
    "aren't", "isn't", "wasn't", "weren't", "don't", "doesn't", "didn't",
    "won't", "wouldn't", "can't", "couldn't", "shouldn't", "mustn't",
    "haven't", "hasn't", "hadn't", "'re", "'ve", "'ll", "'d", "'m",
}
# Singular auxiliaries before the player when it is the subject of an inverted
# question: "Does _FIRSTNAME_ know?" -> "Do they know?"
_THEY_BEFORE_AGREEMENT = {
    "is": "are", "was": "were", "has": "have", "does": "do",
    "isn't": "aren't", "wasn't": "weren't", "hasn't": "haven't",
    "doesn't": "don't",
}
# Third-person-singular verbs after a singular-they subject. Past tense and
# modals are absent on purpose: "they told", "they will" do not change.
_THEY_AFTER_AGREEMENT = {
    "is": "are", "was": "were", "has": "have", "does": "do",
    "makes": "make", "returns": "return", "fails": "fail",
    "learns": "learn", "explains": "explain", "arrives": "arrive",
    "comes": "come", "goes": "go", "knows": "know", "thinks": "think",
    "wants": "want", "needs": "need", "likes": "like", "seems": "seem",
    "remains": "remain", "takes": "take", "gives": "give", "says": "say",
    "tells": "tell", "asks": "ask", "finds": "find", "keeps": "keep",
    "looks": "look", "feels": "feel", "lives": "live", "works": "work",
    "stops": "stop", "defeats": "defeat", "claims": "claim",
    "crafts": "craft", "stays": "stay", "plays": "play", "leaves": "leave",
    "needs": "need", "watches": "watch", "passes": "pass", "misses": "miss",
    "wishes": "wish", "catches": "catch", "teaches": "teach",
    "reaches": "reach", "touches": "touch", "finishes": "finish",
    "pushes": "push", "fixes": "fix", "carries": "carry",
    "tries": "try", "worries": "worry", "hurries": "hurry",
    "studies": "study", "applies": "apply", "replies": "reply",
}


def _words(text):
    return [word.lower() for word in re.findall(r"[A-Za-z']+", text)]


def _clause_head(before, auxiliary):
    """Text before an auxiliary, with sentence punctuation trimmed."""
    match = re.search(r"\b" + re.escape(auxiliary) + r"\s*$", before,
                      re.I)
    if match is None:
        return None
    head = before[:match.start()].strip()
    head = re.sub(r"^[\s\"'“”‘’(\[]+", "", head)
    head = re.sub(r"[,;:–—-]+$", "", head).strip()
    return head


def _is_inverted_subject(before, auxiliary):
    head = _clause_head(before, auxiliary)
    if head is None:
        return False
    if not head:
        return True
    lowered = head.lower()
    return lowered in _QUESTION_WORDS | {"but", "and", "so", "or", "yet"}


def _coordinated_case(before, after):
    """Case of a name coordinated with another person, else None.

    "_FIRSTNAME_ and Raha with me" is an object because the whole noun
    phrase is governed by "brought"; the source text's own "and I" in
    "_FIRSTNAME_ and I" does not make every coordination a subject.
    """
    following = re.match(rf"\s+and\s+({_COORD_PARTNER})\b", after)
    if following is not None:
        name_first = True
        lead = before
        partner = following.group(1)
        tail = after[following.end():]
    else:
        preceding = re.search(
            rf"(?:^|\s)({_COORD_PARTNER})\s*,?\s+and\s+$", before)
        if preceding is None:
            return None
        name_first = False
        lead = before[:preceding.start()]
        partner = preceding.group(1)
        tail = after

    partner_words = _words(partner)
    partner_word = partner_words[0] if partner_words else ""
    if partner_word in _COORD_SUBJECT_PRONOUNS:
        partner_case = "subj"
    elif partner_word in _COORD_OBJECT_PRONOUNS:
        partner_case = "obj"
    else:
        partner_case = None

    # The head must not come from a previous sentence: "...thinking of you.
    # _FIRSTNAME_ and Estinien, come here!" is not governed by "of".
    lead_words = _words(re.split(r"(?<=[.!?;])\s+", lead)[-1])
    head = lead_words[-1] if lead_words else ""
    tail_words = _words(tail)
    after_word = tail_words[0] if tail_words else ""
    punct = bool(re.match(r"^\s*[,.;:!?–—-]", tail))

    if not punct:
        if after_word in _PREPOSITIONS:
            return "obj"
        if (after_word in _COORD_FINITE
                or re.fullmatch(r"[a-z]{3,}ed", after_word)):
            return "subj"
    if head in _PREPOSITIONS or head == "than":
        return "obj"
    if partner_case is not None:
        return partner_case
    demonstrative_that = (
        head == "that" and len(lead_words) > 1
        and lead_words[-2] in _COPULA_AUXILIARY)
    if demonstrative_that:
        # "Is that X and Y I spy?" is a predicate nominal, not a clause.
        return "obj"
    if head in _COPULA_AUXILIARY or head in _COORD_SUBORDINATORS:
        return "subj"
    # Without positive object evidence a name-first coordination stays a
    # subject: "I think _FIRSTNAME_ and Raha together can do it" is an
    # embedded clause, not a governed object.
    return "subj" if name_first else "obj"


def name_use_kind(sentence, match):
    """Classify one macro occurrence as vocative or referential."""
    before = sentence[:match.start()]
    after = sentence[match.end():]
    token = match.group(0)
    if token.endswith("'s") or token.endswith("’s"):
        rest = after.lstrip()
        return "poss_det" if rest and rest[0].isalpha() else "poss_ind"

    stripped = after.lstrip()
    if not stripped:
        return "voc"

    # "Nashu, _FIRSTNAME_, and I..." is a coordinated list, not a vocative.
    # The pronoun case follows the list's role: "with Nashu, _FIRSTNAME_,
    # and I" is an object list even though the source uses "I".
    if re.match(
            r"\s*,\s*(?:[A-Z][^,.;!?]*,\s*)*and\s+"
            r"(?:I|myself|the\b|[A-Z][\w'’-]*)", after):
        lead = re.split(r"(?<=[.!?;])\s+", before)[-1]
        last_name = re.search(r"([A-Z][\w'’-]*)\s*,\s*$", lead)
        lead_words = _words(lead)
        list_lead = (
            not before.strip()
            or (last_name is not None
                and (len(lead_words) < 2
                     or lead_words[-2] not in {"of", "in", "at", "from"})))
        if list_lead:
            prev_words = _words(before)
            if any(word in _OBJECT_PREV for word in prev_words[-2:]):
                return "obj"
            return "subj"

    prev_words = _words(before)
    prev = prev_words[-1] if prev_words else ""
    prev2 = prev_words[-2] if len(prev_words) > 1 else ""
    next_words = _words(after)
    nxt = next_words[0] if next_words else ""
    prev_char = before.rstrip()[-1:] if before.rstrip() else ""

    # Inverted questions put the auxiliary before the subject. "Is
    # _FIRSTNAME_ here?" and "What has _FIRSTNAME_ learned?" need a plural
    # auxiliary for singular they; "This is _FIRSTNAME_" does not.
    if (prev in _THEY_BEFORE_AGREEMENT
            or prev in {"are", "were", "have", "do"}) \
            and _is_inverted_subject(before, prev):
        return "subj"

    if stripped[0] in _VOCATIVE_FOLLOW:
        if prev_char in _VOCATIVE_BEFORE or prev_char == "":
            return "voc"
        if re.search(
                r"(?:^|[.!?;,–—-]\s*)(?:but|or|so|yet)\s*$",
                before, re.I):
            return "voc"
        if re.fullmatch(r"\s*and\s*", before, re.I):
            return "voc"
        if prev in {"adventurer", "warrior", "hero", "savior", "saviour",
                    "friend", "comrade", "dear"}:
            return "voc"
        if prev == "different":
            return "different"
        if prev in _OBJECT_PREV or prev in {"companions", "companion"}:
            return "obj"
        if prev in {"did", "does", "do", "can", "could", "will", "would",
                    "should", "must", "may", "might"}:
            return "subj"
        if prev == "and":
            prev3 = prev_words[-3] if len(prev_words) > 2 else ""
            prev4 = prev_words[-4] if len(prev_words) > 3 else ""
            if (prev2 in _OBJECT_PRONOUNS
                    or prev2 in {"companions", "companion"}
                    or prev3 in _OBJECT_PREV or prev4 in _OBJECT_PREV):
                return "obj"
            return "subj"
        # `I know _FIRSTNAME_, and...` is an object, not a vocative. A comma
        # before the macro still means vocative (`Do not fail, _FIRSTNAME_!`).
        return "obj"

    if re.match(r"and\s+band\b", stripped, re.I):
        return "subj_band"
    if prev == "different":
        return "different"
    if prev in {"companions", "companion"}:
        return "obj"
    if re.match(r"and\s+[A-Z][\w'’-]*['’]s\b", stripped):
        # "this is _FIRSTNAME_ and Estinien's first time": joint possessive.
        # A pronoun cannot simply replace the first name here.
        return "obj"
    if re.match(r"and\s+", stripped):
        coordinated = _coordinated_case(before, after)
        if coordinated is not None:
            return coordinated
        if (prev in _OBJECT_PREV or prev2 in _OBJECT_PRONOUNS
                or prev in {"companions", "companion"}):
            return "obj"
        return "subj"
    if prev == "and":
        coordinated = _coordinated_case(before, after)
        if coordinated is not None:
            return coordinated
        if prev2 in _OBJECT_PRONOUNS or prev2 in {"companions", "companion"}:
            return "obj"
        return "subj"
    if prev == "you" and nxt == "and":
        return "obj"
    if prev in {"did", "does", "do", "can", "could", "will", "would",
                "should", "must", "may", "might"}:
        return "subj"
    if prev in _OBJECT_PREV:
        return "obj"
    if prev == "personally":
        return "obj"
    if prev == "were":
        return "subj"
    if re.search(r"\bthis is\s*$", before, re.I):
        return "obj"
    if re.search(r"\bis\s*$", before, re.I) and re.match(r"\s*the only\b", after):
        return "subj"
    if nxt in _SUBJECT_NEXT:
        return "subj"
    if nxt in {"too", "for", "to", "with", "and", "or"}:
        return "obj"
    return "obj"


def _cleanup_speech(text):
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([.!?])([A-Z])", r"\1 \2", text)
    for _ in range(6):
        old = text
        text = re.sub(r",\s*[,;:]", ",", text)
        text = re.sub(r"([,;:])\s*\.", r".", text)
        text = re.sub(r"([,;:])\s*([!?])", r"\2", text)
        text = re.sub(r"([.!?])\s*[,;:]", r"\1", text)
        text = re.sub(r"([.!?])\s+\1", r"\1", text)
        text = re.sub(r",\s*[-–—]\s*", ", ", text)
        text = re.sub(r"\s*[-–—]\s*,\s*", ", ", text)
        text = re.sub(r"\b(And|But|Or|So|Yet)\s*[-–—]\s*(?=[a-z])",
                      r"\1 ", text)
        text = re.sub(r"([.!?])\s*[-–—]\s*", r"\1 ", text)
        text = re.sub(r"\)\s*[-–—]\s*", ") ", text)
        text = re.sub(r"\)\s*[,.;:!?]+\s*", ") ", text)
        text = re.sub(r"^\s*[-–—,;:.!?]+\s*", "", text)
        text = re.sub(r"\s*[-–—]\s*$", "", text)
        if text == old:
            break
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([.!?])\s+([a-z])",
                  lambda m: m.group(1) + " " + m.group(2).upper(), text)
    text = re.sub(r"^([\"“”'‘’]?)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), text)
    return text


_PRONOUN_FORMS = {
    "they": {"subj": "they", "obj": "them", "poss_det": "their",
             "poss_ind": "theirs", "different": "person"},
    "she": {"subj": "she", "obj": "her", "poss_det": "her",
            "poss_ind": "hers", "different": "woman"},
    "he": {"subj": "he", "obj": "him", "poss_det": "his",
           "poss_ind": "his", "different": "man"},
}


def _agree_they_before_name(prefix):
    """Make an inverted singular auxiliary plural before singular they."""
    match = re.search(
        r"\b(" + "|".join(map(re.escape, _THEY_BEFORE_AGREEMENT)) + r")\s*$",
        prefix, re.I)
    if match is None:
        return prefix
    word = match.group(1)
    replacement = _THEY_BEFORE_AGREEMENT[word.lower()]
    if word[:1].isupper():
        replacement = replacement[:1].upper() + replacement[1:]
    return prefix[:match.start(1)] + replacement + prefix[match.end(1):]


def _agree_they_after_name(chunk):
    """Fix a nearby third-person-singular verb after singular they."""
    match = re.match(
        r"^(\s*[\"'“”‘’]*)((?:[A-Za-z']+\s+){0,2}[A-Za-z']+)", chunk)
    if match is None:
        return chunk
    for word in re.finditer(r"[A-Za-z']+", match.group(2)):
        lowered = word.group(0).lower()
        if lowered not in _THEY_AFTER_AGREEMENT:
            continue
        replacement = _THEY_AFTER_AGREEMENT[lowered]
        if word.group(0)[:1].isupper():
            replacement = replacement[:1].upper() + replacement[1:]
        offset = match.start(2) + word.start()
        return chunk[:offset] + replacement + chunk[offset + len(word.group(0)):]
    return chunk


def _postprocess_special(text, pronoun):
    obj = _PRONOUN_FORMS[pronoun]["obj"]
    subj = _PRONOUN_FORMS[pronoun]["subj"]
    poss = _PRONOUN_FORMS[pronoun]["poss_det"]
    text = re.sub(
        r"\bcompanions,? (?:him|her|them) and (Cid Garlond|Y'shtola)\b",
        lambda m: f"companions, {m.group(1)} and {obj}", text)
    text = re.sub(r"\b(?:He|She|They) and band\b",
                  f"{subj} and {poss} band", text)
    text = re.sub(r"\ba different (?:him|her|them)\b",
                  f"a different {_PRONOUN_FORMS[pronoun]['different']}", text)
    return text


def _render_name_variant(sentence, matches, kinds, pronoun):
    forms = _PRONOUN_FORMS[pronoun]
    parts = []
    pos = 0
    pending_agreement = False
    for match, kind in zip(matches, kinds):
        prefix = sentence[pos:match.start()]
        if pronoun == "they" and kind == "subj":
            prefix = _agree_they_before_name(prefix)
        if pending_agreement:
            prefix = _agree_they_after_name(prefix)
            pending_agreement = False
        parts.append(prefix)
        end = match.end()
        if kind == "voc":
            replacement = ""
        elif kind == "different":
            replacement = forms["different"]
        elif kind == "poss_det":
            replacement = forms["poss_det"]
        elif kind == "poss_ind":
            replacement = forms["poss_ind"]
        elif kind == "subj":
            replacement = forms["subj"]
            if pronoun == "they":
                pending_agreement = True
        elif kind == "subj_band":
            replacement = f"{forms['subj']} and {forms['poss_det']} band"
            band = re.match(r"\s+and\s+band\b", sentence[match.end():], re.I)
            if band:
                end = match.end() + band.end()
        else:
            replacement = forms["obj"]

        if replacement:
            prefix = sentence[:match.start()]
            if (not prefix.strip(" \t\n\"“”'‘’")
                    or re.search(r"[.!?]\s*$", prefix)):
                replacement = replacement[0].upper() + replacement[1:]
        parts.append(replacement)
        pos = end
    suffix = sentence[pos:]
    if pending_agreement:
        suffix = _agree_they_after_name(suffix)
    parts.append(suffix)
    text = _postprocess_special(_cleanup_speech("".join(parts)), pronoun)
    if any(kind == "voc" for kind in kinds):
        text = re.sub(
            r"\b(but|and)\s*([?!])",
            lambda m: f"{m.group(1)} you{m.group(2)}", text,
            flags=re.I)
    return text


def planned_speech(sentence):
    """Return the TTS plan for one canonical sentence, or None to skip it.

    A leading "(-Name-)" speaker label is removed first: it titles the game's
    text box, so speaking it would voice the character's own name. A line that
    contains nothing but that label has no speech and is skipped.
    """
    spoken = _SPEAKER_LABEL_RE.sub("", sentence)
    speech = {kind: text for kind, text in speech_variants(spoken).items()
              if text}
    if not speech:
        return None
    if set(speech) == {"they", "she", "he"}:
        return {"variants": speech}
    return {"speech_text": speech.get("base")
            or next(iter(speech.values()))}


def speech_variants(sentence):
    """Return {"base": text} or singular-they/she/he renderings.

    The base form is used when no third-person reference remains. If the
    sentence contains only player-name references, the result is empty and
    the line should be skipped rather than generated as silence. The root
    OGG is the they/them rendering; she and he are explicit variants.
    """
    matches = list(_NAME_TOKEN_RE.finditer(sentence))
    if not matches:
        return {"base": sentence}
    kinds = [name_use_kind(sentence, match) for match in matches]
    if all(kind == "voc" for kind in kinds):
        base = _render_name_variant(sentence, matches, kinds, "they")
        return {"base": base} if base else {}
    return {
        pronoun: _render_name_variant(sentence, matches, kinds, pronoun)
        for pronoun in ("they", "she", "he")
    }


GENERATION_SCHEMA = 2
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
            entry = dict(entry) if isinstance(entry, dict) else {}
            target = vdir / f"{vid}.ogg"
            variants = entry.get("Variants")
            variant_files = []
            if isinstance(variants, dict):
                for variant in variants.values():
                    if not isinstance(variant, dict):
                        continue
                    name = variant.get("File")
                    if (isinstance(name, str) and Path(name).name == name
                            and (vdir / name).is_file()):
                        variant_files.append(name)

            has_root = target.is_file()
            if not has_root and not variant_files:
                missing += 1
                continue
            if has_root:
                size = target.stat().st_size
                if entry.get("Bytes") != size:
                    entry["Bytes"] = size
                    entry["DurationSeconds"] = media_duration(target)
            else:
                # A migrated variant line may have she/he audio before its
                # neutral root is generated. Keep the playable variants
                # instead of dropping the whole line from the merged pack.
                entry["Bytes"] = 0
                entry["DurationSeconds"] = 0
                entry["GenerationFingerprint"] = ""
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



def previous_unit_meta(previous, pronoun):
    """Metadata for one generated unit: the root file or a sexed variant."""
    if not isinstance(previous, dict):
        return None
    if pronoun == "they":
        return previous
    variants = previous.get("Variants")
    if not isinstance(variants, dict):
        return None
    return variants.get(pronoun)


def reusable_unit(previous, pronoun, filename, target, generation_fp):
    """Return a cached duration when this unit is still trustworthy."""
    if generation_fp is None or not target.is_file():
        return None
    meta = previous_unit_meta(previous, pronoun)
    if not isinstance(meta, dict):
        return None
    if meta.get("File") is not None and meta.get("File") != filename:
        return None
    if meta.get("GenerationFingerprint") != generation_fp:
        return None
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if meta.get("Bytes") is not None and meta["Bytes"] != size:
        return None
    return meta.get("DurationSeconds") or media_duration(target)


def reusable_variant_set(previous, p, vdir, generation_fp):
    if generation_fp is None:
        return False
    return all(
        reusable_unit(previous, pronoun, filename, vdir / filename,
                      generation_fp) is not None
        for pronoun, filename, _ in speech_units(p)
    )


def line_entry(p, target, generation_fp):
    entry = {
        "Lang": p["lang"], "VoiceId": p["voice"], "NpcId": p["npc"],
        "Sentence": p["sentence"], "Bytes": target.stat().st_size,
        "DurationSeconds": media_duration(target), "Codec": "opus",
        "GenerationFingerprint": generation_fp,
    }
    variants = p.get("variants")
    if variants:
        entry["Variants"] = {
            pronoun: variant_entry(
                target.parent / (p["id"] + f".{pronoun}.ogg"),
                generation_fp)
            for pronoun in ("she", "he")
        }
    return entry


def variant_entry(target, generation_fp):
    return {
        "File": target.name,
        "Bytes": target.stat().st_size,
        "DurationSeconds": media_duration(target),
        "GenerationFingerprint": generation_fp,
    }


def speech_units(p):
    """(pronoun, output filename, spoken text) for one canonical line."""
    variants = p.get("variants")
    if variants:
        return [
            ("they", p["id"] + ".ogg", variants["they"]),
            ("she", p["id"] + ".she.ogg", variants["she"]),
            ("he", p["id"] + ".he.ogg", variants["he"]),
        ]
    return [("base", p["id"] + ".ogg",
             p.get("speech_text") or p["sentence"])]


def reused_line_entry(p, action):
    """Manifest entry for a line whose audio is already reusable.

    Reuse the previous entry when possible so a no-op resume does not run
    ffprobe over every line or rewrite equivalent metadata.
    """
    previous = action["previous"]
    if isinstance(previous, dict) and previous:
        return previous
    if action["kind"] == "keep":
        kept_fp = (previous.get("GenerationFingerprint")
                   if previous else "unverified")
        return line_entry(p, action["target"], kept_fp or "unverified")
    return line_entry(p, action["target"], action["generation_fp"])


def store_reused_line(manifest, p, action):
    if p["id"] not in manifest["Voicelines"]:
        manifest["Voicelines"][p["id"]] = reused_line_entry(p, action)


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
    # 12 tokens/s codec. Match the duration calibration so an accepted slow
    # line is not truncated by the decode cap.
    return max(64, min(2048, int(seconds * 12 * DURATION_LIMIT_FACTOR) + 32))


def synthesize_qwen3(tts, args, p, xvector, wav_tmp, retry_kwargs,
                     max_new_tokens, text=None):
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
        "text": text if text is not None else p["sentence"],
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
    ap.add_argument("--verbose", action="store_true",
                    help="print every reused/kept line instead of only a summary")
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
    name_only = 0
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
        speech = planned_speech(sentence)
        if speech is None:
            name_only += 1
            continue
        planned = {"id": vid, "voice": voice, "npc": npc_key,
                   "speaker": cleaned_speaker, "sentence": sentence,
                   "lang": args.lang} | speech
        plan.append(planned)

    print(f"{len(plan)} planned, {len({p['id'] for p in plan})} unique ids")
    if name_only:
        print(f"{name_only} line(s) skipped: player name only")
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

    manifest = {"Schema": GENERATION_SCHEMA, "Language": args.lang,
                "Sanitizer": args.sanitizer_pin,
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

    def line_action(p):
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
        action = {"target": target, "previous": previous,
                  "generation_fp": generation_fp}

        if p.get("variants"):
            targets = [vdir / filename for _, filename, _ in speech_units(p)]
            all_exist = all(path.exists() for path in targets)
            if all_exist and generation_fp is None:
                return action | {"kind": "keep",
                                 "reason": "x-vector missing; not revalidated"}
            if (all_exist and not args.force
                    and reusable_variant_set(previous, p, vdir,
                                             generation_fp)):
                return action | {"kind": "skip",
                                 "reason": "pronoun variants match"}
            if all_exist:
                return action | {"kind": "rebuild",
                                 "reason": "--force" if args.force
                                 else "pronoun variant metadata missing"}
            if not (xvectors / (p["voice"] + ".npy")).is_file():
                return action | {"kind": "blocked", "reason": "x-vector missing"}
            return action | {"kind": "generate", "reason": ""}

        if target.exists() and generation_fp is None:
            # The audio exists but the x-vector needed to validate or rebuild
            # it is gone. Preserve the working line rather than dropping it.
            return action | {"kind": "keep",
                             "reason": "x-vector missing; not revalidated"}
        if target.exists() and not args.force:
            reusable, reason = cached_line_status(previous, target,
                                                  generation_fp)
            return action | {"kind": "skip" if reusable else "rebuild",
                             "reason": reason}
        if target.exists():
            return action | {"kind": "rebuild", "reason": "--force"}
        if not (xvectors / (p["voice"] + ".npy")).is_file():
            return action | {"kind": "blocked", "reason": "x-vector missing"}
        return action | {"kind": "generate", "reason": ""}

    actions = [line_action(p) for p in plan]
    remaining_total = sum(a["kind"] in ("generate", "rebuild")
                          for a in actions)
    blocked = sum(a["kind"] == "blocked" for a in actions)
    reused = len(plan) - remaining_total - blocked
    blocked_voices = sorted({p["voice"] for p, a in zip(plan, actions)
                             if a["kind"] == "blocked"})
    print(f"{reused} line(s) already present; {remaining_total} remaining")
    if blocked:
        print(f"warning: {blocked} line(s) skipped (missing x-vector: "
              f"{', '.join(blocked_voices)})", file=sys.stderr)
    if remaining_total == 0:
        for p, action in zip(plan, actions):
            if action["kind"] in ("skip", "keep"):
                store_reused_line(manifest, p, action)
        write_manifest(out, manifest)
        print(f"pack: {len(manifest['Voicelines'])} lines -> {out} "
              f"(nothing to generate)")
        return

    rejected = 0
    completed = 0
    for p, action in zip(plan, actions):
        kind = action["kind"]
        target = action["target"]
        generation_fp = action["generation_fp"]

        if kind in ("skip", "keep"):
            if args.verbose:
                stream = sys.stderr if kind == "keep" else sys.stdout
                print(f"  {kind} {p['id']}: {action['reason']}",
                      file=stream)
            store_reused_line(manifest, p, action)
            continue

        if kind == "blocked":
            print(f"  MISSING XVECTOR {xvectors / (p['voice'] + '.npy')}; "
                  f"skipping {p['id']}", file=sys.stderr)
            continue

        if kind == "rebuild":
            print(f"  rebuild {p['id']}: {action['reason']}", file=sys.stderr)

        xvector = xvectors / (p["voice"] + ".npy")
        failed = False
        encoded = []
        for pronoun, filename, text in speech_units(p):
            out_target = vdir / filename
            reused = (None if args.force else reusable_unit(
                previous=action["previous"], pronoun=pronoun,
                filename=filename, target=out_target,
                generation_fp=generation_fp))
            if reused is not None:
                encoded.append((pronoun, reused))
                continue
            wav_tmp = vdir / (out_target.stem + ".tmp.wav")
            # Calibrated grace: punctuation and dramatic pauses routinely
            # land over the words/sec estimate. The check still catches
            # appended sentences.
            limit = duration_limit(text, args.min_wps)
            max_new_tokens = (args.max_new_tokens
                              or auto_max_new_tokens(text, args.min_wps))
            try:
                for attempt in range(1, args.max_attempts + 1):
                    retry_kwargs = {} if attempt == 1 else {
                        "temperature": args.retry_temperature,
                        "top_p": RETRY_TOP_P}
                    synthesize(tts, args, p, xvector, wav_tmp, retry_kwargs,
                               max_new_tokens, text=text)
                    dur = media_duration(wav_tmp)
                    if dur <= limit:
                        break
                    print(f"  retry {p['id']} [{pronoun}]: {dur:.2f}s > "
                          f"{limit:.2f}s (attempt {attempt})", file=sys.stderr)
                else:
                    print(f"  REJECT {p['id']} [{pronoun}]: {dur:.2f}s > "
                          f"{limit:.2f}s after {args.max_attempts} attempts",
                          file=sys.stderr)
                    rejected += 1
                    failed = True
                    break
                encoded.append((pronoun, encode_ogg(wav_tmp, out_target)))
            finally:
                wav_tmp.unlink(missing_ok=True)
        if failed:
            continue
        completed += 1
        detail = ", ".join(f"{pronoun} {duration:.2f}s"
                           for pronoun, duration in encoded)
        print(f"  [{completed}/{remaining_total}] wrote {p['id']} "
              f"({detail})")
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
