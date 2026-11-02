"""Deterministic acoustic casting from game data.

The dialogue text does not carry acoustic information, and a small local model
asked to guess it collapses onto the middle band (measured 2026-11-01: `mid`
for 9/10 NPCs, `smooth` for 8/10). So acoustics are cast from facts the game
itself states — race, clan, sex, body type, height — plus a stable per-NPC
choice among equally plausible values.

Every value here is `creative_selection`: it describes the voice we are
creating, never the original actor. The rules are intentionally few and
readable; a human can override any field in the profile.
"""

from __future__ import annotations

import hashlib
from typing import Any

# ENpcBase.BodyType. Only the extremes are claims: 3 selects the aged model, 4
# the small model used by children and child-like Padjal. BodyType 1 is the
# ordinary model and says nothing about age, so it yields the project fallback
# band rather than a game-data fact. Measured 2026-11-01 against 12 NPCs with
# documented ages: BodyType alone matched 2/11, and `bt1 -> adult` was wrong
# for Merlwyb, Momodi, Hildibrand, Godbert and Kan-E-Senna.
BODY_TYPE_AGE = {
    3: "mature",
    4: "young_teen",
}

# ENpcBase.Height is a scale percentage whose 0 value means "no override" and
# whose meaning differs per race, so it is recorded but not used for casting.

# Each race gets a small family of plausible timbres; the NPC picks one
# deterministically so two members of the same race and sex are still distinct.
RACE_TIMBRE_FAMILY = {
    "Hyur": ("clear", "warm", "bright"),
    "Elezen": ("silvery", "bright", "clear"),
    "Lalafell": ("bright", "clear", "mellow"),
    "Miqo'te": ("warm", "bright", "husky"),
    "Roegadyn": ("rich", "dark", "gravelly"),
    "Au Ra": ("smooth", "dark", "clear"),
    "Hrothgar": ("gravelly", "rich", "dark"),
    "Viera": ("bright", "clear", "warm"),
}
DEFAULT_TIMBRE_FAMILY = ("clear", "warm", "mellow")

RACE_PITCH_SHIFT = {
    "Roegadyn": -1,
    "Hrothgar": -1,
    "Lalafell": 1,
}

RACE_ARTICULATION = {
    "Elezen": "precise",
    "Roegadyn": "heavy",
    "Hrothgar": "heavy",
    "Lalafell": "delicate",
    "Au Ra": "crisp",
}

RACE_RESONANCE = {
    "Roegadyn": "deep_resonance",
    "Hrothgar": "deep_resonance",
    "Au Ra": "chest_resonant",
    "Elezen": "forward_resonant",
    "Miqo'te": "forward_resonant",
    "Lalafell": "head_resonant",
    "Viera": "rounded",
    "Hyur": "rounded",
}

RACE_INTONATION = {
    "Lalafell": "expressive",
    "Miqo'te": "expressive",
    "Elezen": "restrained",
    "Roegadyn": "falling",
}

RACE_RANGE = {
    "Roegadyn": "narrow_range",
    "Hrothgar": "narrow_range",
    "Elezen": "narrow_range",
    "Lalafell": "wide_range",
    "Miqo'te": "wide_range",
}

PITCH_LADDER = (
    "very_low", "low", "low_mid", "mid", "mid_high", "high", "very_high",
)

# Equally plausible alternatives, chosen per NPC so two Hyur Midlander men do not
# receive byte-identical acoustics.
ADULT_PACE = ("measured", "moderate")
ADULT_ARTICULATION = ("relaxed", "rounded", "fluid")
ADULT_INTONATION = ("natural", "restrained", "expressive")


AGE_PITCH_SHIFT = {
    "child": 2,
    "young_teen": 1,
    "older_teen": 1,
    "young_adult": 0,
    "adult": 0,
    "middle_aged": -1,
    "mature": -1,
    "elderly": -2,
    "ageless": 0,
    "ancient": -1,
}

AGE_PACE = {
    "child": "brisk",
    "young_teen": "brisk",
    "older_teen": "brisk",
    "elderly": "slow",
    "mature": "slow",
}

AGE_RESONANCE = {
    "child": "light_resonance",
    "young_teen": "light_resonance",
    "mature": "chest_resonant",
    "elderly": "chest_resonant",
}

AGE_RANGE = {
    "child": "wide_range",
    "young_teen": "wide_range",
    "mature": "narrow_range",
    "elderly": "narrow_range",
}

FALLBACK_AGE = "adult"


def _age_band(identity: dict, body_type: Any) -> tuple[str, str | None, str, str]:
    """Return (band, rule, evidence level, note) for the perceived age."""
    band = BODY_TYPE_AGE.get(body_type) if isinstance(body_type, int) else None
    if band is not None:
        return (
            band,
            "body_type_age",
            "creative_selection",
            f"Game BodyType {body_type} selects the child or aged model, which bounds the voice age.",
        )
    return (
        FALLBACK_AGE,
        None,
        "default",
        "Game data carries no age fact for this NPC; the adult band is the project fallback.",
    )


def _rank(value: str) -> int:
    try:
        return PITCH_LADDER.index(value)
    except ValueError:
        raise ValueError(f"unknown pitch value: {value}") from None


def _shift(pitch: str, steps: int) -> str:
    index = min(max(_rank(pitch) + steps, 0), len(PITCH_LADDER) - 1)
    return PITCH_LADDER[index]


def _stable_index(seed: str, length: int) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % length


def cast_identity(identity: dict, *, npc_id: int | None = None) -> dict[str, dict[str, Any]]:
    """Return one decision per acoustic identity field.

    Each decision is `{"value", "rule", "facts"}`. `rule` names the deterministic
    rule that produced the value, so a reader can audit or replace it.
    """
    sex = str(identity.get("sex") or "").strip().casefold()
    race = identity.get("race")
    clan = identity.get("clan")
    body_type = identity.get("body_type")
    height = identity.get("height")
    seed = str(identity.get("canonical_id") or npc_id or identity.get("name") or race or "unknown")

    age, age_rule, age_level, age_note = _age_band(identity, body_type)
    age_known = age_rule is not None

    base_pitch = {"male": "low_mid", "female": "mid_high"}.get(sex, "mid")
    pitch = _shift(base_pitch, RACE_PITCH_SHIFT.get(race, 0))
    if age_known:
        pitch = _shift(pitch, AGE_PITCH_SHIFT.get(age, 0))

    pace = AGE_PACE.get(age) if age_known else None
    if pace is None:
        pace = ADULT_PACE[_stable_index(f"{seed}:pace", len(ADULT_PACE))]

    articulation = RACE_ARTICULATION.get(race) or ADULT_ARTICULATION[
        _stable_index(f"{seed}:articulation", len(ADULT_ARTICULATION))
    ]
    intonation = RACE_INTONATION.get(race) or ADULT_INTONATION[
        _stable_index(f"{seed}:intonation", len(ADULT_INTONATION))
    ]

    resonance = AGE_RESONANCE.get(age) if age_known else None
    if resonance is None:
        resonance = RACE_RESONANCE.get(race, "rounded")

    if age_known and AGE_RANGE.get(age):
        pitch_range = AGE_RANGE[age]
    else:
        pitch_range = RACE_RANGE.get(race) or (
            "wide_range" if _stable_index(f"{seed}:range", 2) else "narrow_range"
        )

    timbre_family = RACE_TIMBRE_FAMILY.get(race, DEFAULT_TIMBRE_FAMILY)

    presentation = {"male": "masculine", "female": "feminine"}.get(sex, "neutral")

    facts = {
        "sex": sex or "unknown",
        "race": race,
        "clan": clan,
        "body_type": body_type,
        "height": height,
    }
    return {
        "presentation": {
            "value": presentation,
            "rule": "sex_presentation",
            "facts": facts,
        },
        "perceived_age": {
            "value": age,
            "rule": age_rule,
            "level": age_level,
            "note": age_note,
            "facts": facts,
        },
        "pitch": {
            "value": pitch,
            "rule": "sex_race_age_pitch_ladder" if age_known else "sex_race_pitch_ladder",
            "facts": facts,
        },
        "pitch_range": {
            "value": pitch_range,
            "rule": "age_race_range",
            "facts": facts,
        },
        "resonance": {
            "value": resonance,
            "rule": "age_race_resonance",
            "facts": facts,
        },
        "timbre_primary": {
            "value": timbre_family[
                _stable_index(f"{seed}:timbre", len(timbre_family))
            ],
            "rule": "race_timbre_family",
            "facts": facts,
        },
        "articulation": {
            "value": articulation,
            "rule": "race_or_stable_articulation",
            "facts": facts,
        },
        "speaking_pace": {
            "value": pace,
            "rule": "age_pace",
            "facts": facts,
        },
        "intonation": {
            "value": intonation,
            "rule": "race_or_stable_intonation",
            "facts": facts,
        },
    }
