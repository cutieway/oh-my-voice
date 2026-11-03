"""Voice-identity casting from verified game facts.

The model is deliberately split into three layers:

1. **Facts** copied from the current game export: sex, race, clan, body type,
   height, scale, and the raw appearance fields. A fact is never invented, and
   a default byte is not a fact: sex is null unless a playable-race model
   actually carries it.
2. **Voice identity** inferred from those facts only for NPCs with no recorded
   voice. The inference is a weighted vote, not a single hard rule, and every
   decision keeps its contributing factors.
3. **Audio evidence** wins. If an NPC has recorded voice lines, appearance is
   not used to infer voice identity; those fields stay null until measured from
   the audio.

Every value that the game does not supply, or that has no approved mapping, is
null. Downstream rendering omits null fields instead of substituting defaults.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# ENpcResident.Sex. The game's `unknown` value and missing data both become
# null: there is no neutral sex fact.
SEX_VALUES = ("male", "female")

PLAYABLE_RACES = (
    "Hyur",
    "Elezen",
    "Lalafell",
    "Miqo'te",
    "Roegadyn",
    "Au Ra",
    "Viera",
    "Hrothgar",
)

# ENpcBase.Tribe, exact game names. A clan is valid only for its race.
PLAYABLE_CLANS = {
    "Hyur": ("Midlander", "Highlander"),
    "Elezen": ("Wildwood", "Duskwight"),
    "Lalafell": ("Plainsfolk", "Dunesfolk"),
    "Miqo'te": ("Seeker of the Sun", "Keeper of the Moon"),
    "Roegadyn": ("Sea Wolf", "Hellsguard"),
    "Au Ra": ("Raen", "Xaela"),
    "Viera": ("Rava", "Veena"),
    "Hrothgar": ("Helions", "The Lost"),
}

NON_PLAYABLE_RACES = (
    "Amalj'aa", "Sylphs", "Kobolds", "Sahagin", "Ixal", "Goblin", "Qiqirn",
    "Moogle", "Vanu Vanu", "Gnath", "Ananta", "Kojin", "Namazu", "Lupin",
    "Tengu", "Garleans", "Bangaa", "Seeq", "Matanga", "Arkasodara", "Zun",
    "Mord", "Ondo", "Nu Mou", "Pixies", "Qitari", "Dwarves", "Loporrits",
    "Omicrons", "Dragons", "Ea", "Grebuloff", "Karellians", "Nibirun",
    "Gordhonites", "Class Twenty-fours", "Mandervillians", "PuPu's race (unnamed)",
    "Nagylvi", "Mamool Ja", "Pelupelu", "Moblins", "Yok Huy", "Hanuhanu",
    "Köttr", "Fylgjur", "Felynes",
)

# ENpcBase.BodyType: 3 selects the aged model, 4 the small model used by
# children and young teens. BodyType 1 is the ordinary adult model and carries
# no age fact. The game has no numeric age field.
BODY_TYPE_AGE = {
    3: "mature",
    4: "young_teen",
}

# BustOrTone1 is model-dependent. It tracks bust size on female models and
# muscle tone on male models for these races; other models use the same field
# for different customization parameters, so it is not interpreted there.
MUSCLE_TONE_RACES = ("Hyur", "Roegadyn", "Au Ra")

PITCH_VALUES = (
    "very_low", "low", "low_mid", "mid", "mid_high", "high", "very_high",
)
PITCH_INDEX = {value: index for index, value in enumerate(PITCH_VALUES)}

RACE_PITCH_SHIFT = {
    "Hyur": 0.0,
    "Elezen": 0.0,
    "Lalafell": 0.6,
    "Miqo'te": 0.2,
    "Roegadyn": -0.6,
    "Au Ra": -0.2,
    "Viera": 0.1,
    "Hrothgar": -0.5,
}

RACE_TIMBRE = {
    "Hyur": {"clear": 1.0, "warm": 0.8, "bright": 0.3},
    "Elezen": {"silvery": 1.0, "clear": 0.8, "bright": 0.4},
    "Lalafell": {"bright": 1.0, "mellow": 0.7, "clear": 0.4},
    "Miqo'te": {"warm": 1.0, "bright": 0.6, "husky": 0.4},
    "Roegadyn": {"rich": 1.0, "dark": 0.7, "booming": 0.4},
    "Au Ra": {"smooth": 1.0, "clear": 0.6, "dark": 0.3},
    "Viera": {"bright": 1.0, "clear": 0.7, "silvery": 0.3},
    "Hrothgar": {"gravelly": 1.0, "rich": 0.7, "dark": 0.4},
}

RACE_RESONANCE = {
    "Hyur": {"rounded": 1.0, "forward_resonant": 0.4},
    "Elezen": {"forward_resonant": 1.0, "rounded": 0.4},
    "Lalafell": {"head_resonant": 1.0, "compact": 0.5},
    "Miqo'te": {"forward_resonant": 1.0, "rounded": 0.4},
    "Roegadyn": {"deep_resonance": 1.0, "chest_resonant": 0.5},
    "Au Ra": {"chest_resonant": 0.8, "forward_resonant": 0.5},
    "Viera": {"rounded": 1.0, "forward_resonant": 0.4},
    "Hrothgar": {"deep_resonance": 1.0, "throaty": 0.5},
}

RACE_RANGE = {
    "Hyur": {},
    "Elezen": {"narrow_range": 0.4},
    "Lalafell": {"wide_range": 0.6},
    "Miqo'te": {"wide_range": 0.5},
    "Roegadyn": {"narrow_range": 0.6},
    "Au Ra": {},
    "Viera": {},
    "Hrothgar": {"narrow_range": 0.5},
}

AUDIO_FIELDS = (
    "presentation", "perceived_age", "pitch", "pitch_range",
    "timbre_primary", "resonance",
)
AUDIO_VALUES = {
    "presentation": {"masculine", "feminine"},
    "perceived_age": {"child", "young_teen", "older_teen", "young_adult", "adult",
                      "middle_aged", "mature", "elderly", "ageless", "ancient"},
    "pitch": set(PITCH_VALUES),
    "pitch_range": {"narrow_range", "wide_range"},
    "timbre_primary": {"clear", "warm", "bright", "dark", "rich", "mellow",
                       "nasal", "hollow", "metallic", "breathy", "husky",
                       "gravelly", "raspy", "hoarse", "smooth", "silvery",
                       "reedy", "booming", "whispery", "synthetic"},
    "resonance": {"chest_resonant", "forward_resonant", "head_resonant",
                  "rounded", "compact", "open", "hollow", "nasal", "throaty",
                  "deep_resonance", "light_resonance"},
}


def normalize_sex(value: Any) -> str | None:
    """Return male or female, or None when the game does not know."""
    sex = str(value or "").strip().casefold()
    if not sex or sex == "unknown":
        return None
    if sex not in SEX_VALUES:
        raise ValueError(f"game sex must be male, female, or unknown, got {value!r}")
    return sex


def normalize_race(value: Any) -> str | None:
    if value in (None, ""):
        return None
    race = str(value).strip()
    if race not in PLAYABLE_RACES:
        raise ValueError(f"playable race must be one of {PLAYABLE_RACES}, got {value!r}")
    return race


def normalize_clan(race: str | None, value: Any) -> str | None:
    if value in (None, ""):
        return None
    if race is None:
        raise ValueError(f"clan {value!r} has no playable race")
    clan = str(value).strip()
    if clan not in PLAYABLE_CLANS.get(race, ()):
        raise ValueError(f"clan must be one of {PLAYABLE_CLANS.get(race, ())}, got {value!r}")
    return clan


def normalize_non_playable_race(value: Any) -> str | None:
    if value in (None, ""):
        return None
    race = str(value).strip()
    if race not in NON_PLAYABLE_RACES:
        raise ValueError(f"non-playable race must be one of {NON_PLAYABLE_RACES}, got {value!r}")
    return race


def _percent(value: Any) -> int | None:
    """Return a 0-100 character-creator value, or None for an unset field."""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


def _height(value: Any) -> int | None:
    """Return the height slider 0-100; 255 is the export fallback."""
    number = _percent(value)
    return number if number is not None and number <= 100 else None


def _scale(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if number > 0 else None


def _decision(
    value: Any,
    *,
    rule: str | None,
    note: str | None = None,
    facts: dict[str, Any] | None = None,
    factors: list[dict[str, Any]] | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"value": value, "rule": rule}
    if facts is not None:
        record["facts"] = facts
    if factors:
        record["factors"] = factors
    if confidence is not None:
        record["confidence"] = round(min(0.95, max(0.0, confidence)), 2)
    if note:
        record["note"] = note
    return record


def _audio_or_unavailable(field: str, voice_evidence: dict | None) -> dict[str, Any] | None:
    """Return an audio-backed decision when one exists, else None."""
    if not isinstance(voice_evidence, dict):
        return None
    value = voice_evidence.get(field)
    if not isinstance(value, str) or value not in AUDIO_VALUES.get(field, set()):
        return None
    confidence = voice_evidence.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = None
    return _decision(
        value,
        rule="audio_evidence",
        note="Measured from recorded voice lines, not inferred from appearance.",
        confidence=confidence,
    )


def _weighted_pitch(factors: list[tuple[str, Any, float, float]]) -> tuple[str | None, list[dict], float]:
    score = float(PITCH_INDEX["mid"])
    total = 0.0
    used: list[dict[str, Any]] = []
    for name, value, weight, delta in factors:
        if value is None:
            continue
        score += weight * delta
        total += weight
        used.append({"name": name, "value": value, "weight": weight, "delta": delta})
    if not used:
        return None, [], 0.0
    index = min(max(round(score), 0), len(PITCH_VALUES) - 1)
    return PITCH_VALUES[index], used, total / 2.5


def _weighted_category(
    factors: list[tuple[str, Any, float, dict[str, float]]],
) -> tuple[str | None, list[dict], float]:
    scores: dict[str, float] = defaultdict(float)
    total = 0.0
    used: list[dict[str, Any]] = []
    for name, value, weight, preferences in factors:
        if value is None or not preferences:
            continue
        total += weight
        used.append({
            "name": name, "value": value, "weight": weight,
            "preferences": preferences,
        })
        for choice, strength in preferences.items():
            scores[choice] += weight * strength
    if not scores:
        return None, [], 0.0
    best, best_score = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0]
    return best, used, min(0.95, best_score / 2.0)


def _muscle_preferences(muscle: int | None) -> dict[str, float]:
    if muscle is None:
        return {}
    if muscle <= 25:
        return {"clear": 0.5, "bright": 0.2}
    if muscle <= 50:
        return {"warm": 0.3, "clear": 0.2}
    if muscle <= 75:
        return {"rich": 0.4, "warm": 0.2}
    return {"rich": 0.6, "dark": 0.3}


def _muscle_resonance(muscle: int | None) -> dict[str, float]:
    if muscle is None:
        return {}
    if muscle <= 25:
        return {"light_resonance": 0.6}
    if muscle <= 50:
        return {"rounded": 0.3}
    if muscle <= 75:
        return {"chest_resonant": 0.4}
    return {"deep_resonance": 0.7, "chest_resonant": 0.3}


def _height_timbre(height: int | None) -> dict[str, float]:
    if height is None:
        return {}
    if height <= 40:
        return {"bright": 0.2}
    if height >= 60:
        return {"dark": 0.3}
    return {}


def _height_resonance(height: int | None) -> dict[str, float]:
    if height is None:
        return {}
    if height <= 40:
        return {"light_resonance": 0.3}
    if height >= 60:
        return {"deep_resonance": 0.4}
    return {}


def cast_identity(identity: dict, *, npc_id: int | None = None) -> dict[str, dict[str, Any]]:
    """Return one decision per identity field.

    Facts are always recorded. Voice identity is inferred only when no recorded
    voice exists; otherwise it stays null for the audio pipeline to fill.
    """
    sex = normalize_sex(identity.get("sex"))
    sex_reported = sex
    race_reported = identity.get("race")
    race_value = race_reported
    non_playable_value = identity.get("non_playable_race")
    if race_value in NON_PLAYABLE_RACES:
        if non_playable_value and non_playable_value != race_value:
            raise ValueError(
                f"race {race_value!r} conflicts with non_playable_race {non_playable_value!r}"
            )
        non_playable_value = race_value
        race_value = None
    race = normalize_race(race_value)
    clan = normalize_clan(race, identity.get("clan"))
    non_playable_race = normalize_non_playable_race(non_playable_value)
    body_type = identity.get("body_type")
    # A sex byte is only trustworthy for a playable-race model. Non-playable
    # races, beasts, monsters, and zero-default rows may have no gender at all;
    # their default 0 byte must not become "male".
    if race is None or body_type == 0:
        sex = None
    raw_height = identity.get("height")
    height = _height(raw_height) if race else None
    scale = _scale(identity.get("scale"))
    raw_bust = _percent(identity.get("bust_or_tone_1"))
    raw_extra = _percent(identity.get("extra_feature_2_or_bust"))

    # BustOrTone1 is only interpreted as muscle on male models of the races
    # where that field is known to be muscle tone. On female models it is bust.
    muscle = raw_bust if sex == "male" and race in MUSCLE_TONE_RACES else None
    bust = raw_bust if sex == "female" else None

    voice_evidence = identity.get("voice_evidence")
    has_recorded_voice = (
        bool(identity.get("has_se_voice_reference"))
        or bool(identity.get("has_scd"))
        or bool(voice_evidence)
    )

    facts = {
        "sex": sex,
        "sex_reported": sex_reported,
        "race": race,
        "race_reported": race_reported,
        "clan": clan,
        "non_playable_race": non_playable_race,
        "body_type": body_type,
        "height": height,
        "height_raw": raw_height,
        "scale": scale,
        "bust_or_tone_1": raw_bust,
        "extra_feature_2_or_bust": raw_extra,
        "muscle_tone": muscle,
        "bust": bust,
        "jaw": identity.get("jaw"),
        "mouth": identity.get("mouth"),
        "face": identity.get("face"),
        "nose": identity.get("nose"),
        "eye_shape": identity.get("eye_shape"),
        "eyebrows": identity.get("eyebrows"),
        "hair_style": identity.get("hair_style"),
        "skin_color": identity.get("skin_color"),
        "has_recorded_voice": has_recorded_voice,
    }

    age = BODY_TYPE_AGE.get(body_type) if isinstance(body_type, int) else None
    age_decision = _decision(
        age,
        rule="body_type_age" if age else None,
        facts=facts,
        note=(
            f"Game BodyType {body_type} selects the aged or small model."
            if age else
            f"BodyType {body_type!r} does not establish a perceived age."
        ),
    )

    # Actual voice evidence has priority over every appearance inference.
    if has_recorded_voice:
        audio: dict[str, dict[str, Any]] = {}
        for field in AUDIO_FIELDS:
            measured = _audio_or_unavailable(field, voice_evidence if isinstance(voice_evidence, dict) else None)
            audio[field] = measured or _decision(
                None,
                rule="voiced_use_audio_evidence",
                facts=facts,
                note="NPC has recorded voice lines; derive this field from the audio, not appearance.",
            )
        return {
            "sex": _decision(sex, rule="sex_as_recorded", facts=facts),
            "presentation": audio["presentation"],
            "race": _decision(race, rule="game_race_as_recorded", facts=facts),
            "clan": _decision(clan, rule="game_clan_as_recorded", facts=facts),
            "non_playable_race": _decision(
                non_playable_race, rule="non_playable_race_as_recorded", facts=facts
            ),
            "perceived_age": audio["perceived_age"],
            "pitch": audio["pitch"],
            "pitch_range": audio["pitch_range"],
            "timbre_primary": audio["timbre_primary"],
            "timbre_secondary": _decision(None, rule=None, note="No approved secondary-timbre evidence."),
            "resonance": audio["resonance"],
            "articulation": _decision(None, rule=None, note="No approved articulation evidence."),
            "speaking_pace": _decision(None, rule=None, note="Speaking pace is style, not identity; no data."),
            "intonation": _decision(None, rule=None, note="Intonation is style/personality; no data."),
            "distinctive_traits": _decision(None, rule=None, note="No approved trait evidence."),
        }

    presentation = {"male": "masculine", "female": "feminine"}.get(sex)
    presentation_decision = _decision(
        presentation,
        rule="sex_to_voice_presentation" if presentation else None,
        facts=facts,
        factors=[{"name": "sex", "value": sex, "weight": 1.0}] if sex else None,
        confidence=0.5 if presentation else None,
        note="A statistical starting point from game sex; refine from audio when available.",
    )

    pitch_factors = []
    if sex:
        pitch_factors.append(("sex", sex, 1.0, -1.0 if sex == "male" else 1.0))
    if age:
        pitch_factors.append((
            "perceived_age", age, 0.8,
            1.0 if age == "young_teen" else -0.6,
        ))
    if race and RACE_PITCH_SHIFT.get(race, 0.0):
        pitch_factors.append(("race", race, 0.5, RACE_PITCH_SHIFT[race]))
    if height and (height <= 40 or height >= 60):
        pitch_factors.append((
            "height", height, 0.25,
            0.3 if height <= 40 else -0.3,
        ))
    pitch, pitch_used, pitch_confidence = _weighted_pitch(pitch_factors)
    pitch_decision = _decision(
        pitch,
        rule="weighted_inference" if pitch else None,
        facts=facts,
        factors=pitch_used or None,
        confidence=pitch_confidence if pitch else None,
        note=(
            "Weighted vote over sex, perceived age, race, and height. "
            "Muscle tone is intentionally excluded from pitch."
        ) if pitch else "No factors available; omit pitch.",
    )

    timbre_factors = [
        ("race", race, 1.0, RACE_TIMBRE.get(race, {}) if race else {}),
        ("muscle_tone", muscle, 0.7, _muscle_preferences(muscle)),
        ("height", height, 0.3, _height_timbre(height)),
        ("perceived_age", age, 0.4, {"bright": 0.3} if age == "young_teen" else {"gravelly": 0.3} if age == "mature" else {}),
    ]
    timbre, timbre_used, timbre_confidence = _weighted_category(timbre_factors)
    timbre_decision = _decision(
        timbre,
        rule="weighted_inference" if timbre else None,
        facts=facts,
        factors=timbre_used or None,
        confidence=timbre_confidence if timbre else None,
        note="Weighted vote over race, muscle tone, height, and perceived age." if timbre else "No factors available; omit timbre.",
    )

    resonance_factors = [
        ("race", race, 1.0, RACE_RESONANCE.get(race, {}) if race else {}),
        ("muscle_tone", muscle, 0.8, _muscle_resonance(muscle)),
        ("height", height, 0.4, _height_resonance(height)),
        ("perceived_age", age, 0.4, {"chest_resonant": 0.3} if age == "mature" else {"light_resonance": 0.3} if age == "young_teen" else {}),
    ]
    resonance, resonance_used, resonance_confidence = _weighted_category(resonance_factors)
    resonance_decision = _decision(
        resonance,
        rule="weighted_inference" if resonance else None,
        facts=facts,
        factors=resonance_used or None,
        confidence=resonance_confidence if resonance else None,
        note="Weighted vote over race, muscle tone, height, and perceived age." if resonance else "No factors available; omit resonance.",
    )

    range_factors = [
        ("perceived_age", age, 1.0, {"wide_range": 0.8} if age == "young_teen" else {"narrow_range": 0.6} if age == "mature" else {}),
        ("race", race, 0.5, RACE_RANGE.get(race, {}) if race else {}),
    ]
    pitch_range, range_used, range_confidence = _weighted_category(range_factors)
    range_decision = _decision(
        pitch_range,
        rule="weighted_inference" if pitch_range else None,
        facts=facts,
        factors=range_used or None,
        confidence=range_confidence if pitch_range else None,
        note="Weighted vote over perceived age and race." if pitch_range else "No factors available; omit range.",
    )

    return {
        "sex": _decision(sex, rule="sex_as_recorded", facts=facts),
        "presentation": presentation_decision,
        "race": _decision(race, rule="game_race_as_recorded", facts=facts),
        "clan": _decision(clan, rule="game_clan_as_recorded", facts=facts),
        "non_playable_race": _decision(
            non_playable_race, rule="non_playable_race_as_recorded", facts=facts
        ),
        "perceived_age": age_decision,
        "pitch": pitch_decision,
        "pitch_range": range_decision,
        "timbre_primary": timbre_decision,
        "timbre_secondary": _decision(None, rule=None, note="No approved secondary-timbre evidence."),
        "resonance": resonance_decision,
        "articulation": _decision(
            None, rule=None,
            note="Jaw and mouth are customization indices, not vocal-tract measurements; omit articulation.",
        ),
        "speaking_pace": _decision(
            None, rule=None,
            note="Speaking pace is style/personality, not a game identity fact; omit it.",
        ),
        "intonation": _decision(
            None, rule=None,
            note="Intonation is style/personality, not a game identity fact; omit it.",
        ),
        "distinctive_traits": _decision(None, rule=None, note="No approved trait evidence."),
    }
