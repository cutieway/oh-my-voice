"""Deterministic synthetic voice casting from verified FFXIV game facts.

The model keeps four provenance classes explicit:

1. **Facts** copied from the game export. The current exporter reads
   ``ENpcBase.Gender``, ``ENpcBase.Race``, ``ENpcBase.Tribe``,
   ``ENpcBase.BodyType``, appearance sliders and ``ENpcBase.Scale``. A raw value
   is always preserved; an unrecognized value becomes a normalized ``None`` plus
   a warning instead of a fatal error.
2. **Creative casting priors** derived from documented race/clan morphology.
   These are labelled ``creative_selection`` and carry a ``prior_score`` /
   ``casting_strength``, never a fake confidence.
3. **Audio evidence** measured from a verified recording. It can fill acoustic
   fields, but it does not delete game facts.
4. **Unknown** fields stay ``None`` and the renderer omits them.

The game data does not measure a person's voice. Race and body morphology are
used only for a weak perceived-size / vocal-weight impression. Pitch follows the
model gender convention and the BodyType 4 child override; it is never shifted
by race, height or muscle tone. Pitch range, resonance, articulation, pace and
intonation are not inferable from static game data and stay null unless a
verified audio measurement supplies them.
"""

from __future__ import annotations

import math
from typing import Any

# ENpcBase.Gender as exported by tools/xivdata/NpcIndexCommand.cs:
# 0 => male, 1 => female, every other byte => unknown. `unknown` and missing
# data both normalize to None; there is no neutral gender fact.
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

RULES_VERSION = "2.0"

# ENpcBase.BodyType. The reference extractor names 3 `Elderly` and 4 `Child`;
# 4 is a small model shared by children and some young teens, so the project
# renders it as `youthful` rather than asserting a precise chronological age.
# 1 is the ordinary/default model and becomes `adult` only as a project casting
# convention. The game has no numeric age field.
BODY_TYPE_AGE = {
    1: "adult",
    3: "elderly",
    4: "youthful",
}
BODY_TYPE_EVIDENCE = {
    1: "creative_selection",
    3: "game_fact",
    4: "game_fact",
}
BODY_TYPE_RULE = {
    1: "body_type_standard_adult_convention",
    3: "body_type_elderly",
    4: "body_type_childlike",
}

# A deliberate synthetic pitch convention, not a measured acoustic property.
BASE_PITCH_BY_GENDER = {
    "male": "low_mid",
    "female": "mid_high",
}
CHILD_PITCH_BY_GENDER = {
    "male": "mid_high",
    "female": "high",
}

PITCH_VALUES = (
    "very_low", "low", "low_mid", "mid", "mid_high", "high", "very_high",
)

# Starting creative-prior strengths, not probabilities or measured confidence.
# `size` runs -1 (compact) to +1 (large); `weight` runs -1 (light) to +1 (full).
RACE_MORPHOLOGY_PRIOR = {
    "Hyur": {"size": 0.00, "weight": 0.00},
    "Elezen": {"size": 0.15, "weight": 0.00},
    "Lalafell": {"size": -0.30, "weight": 0.00},
    "Miqo'te": {"size": 0.00, "weight": 0.00},
    "Roegadyn": {"size": 0.55, "weight": 0.55},
    "Au Ra": {"size": 0.00, "weight": 0.00},
    "Viera": {"size": 0.15, "weight": 0.00},
    "Hrothgar": {"size": 0.15, "weight": 0.10},
}

CLAN_MORPHOLOGY_PRIOR = {
    ("Hyur", "Highlander"): {"size": 0.10, "weight": 0.15},
}

RACE_GENDER_MORPHOLOGY_PRIOR = {
    ("Au Ra", "male"): {"size": 0.20, "weight": 0.20},
    ("Au Ra", "female"): {"size": -0.05, "weight": 0.00},
    ("Hrothgar", "male"): {"size": 0.30, "weight": 0.35},
    ("Hrothgar", "female"): {"size": 0.10, "weight": 0.15},
}

RACE_MORPHOLOGY_RATIONALE = {
    "Hyur": "Official lore describes Hyur as average in height and build; use as the baseline.",
    "Elezen": "Official lore describes Elezen as tall, long-limbed and slender; a small perceived-size prior only.",
    "Lalafell": "Official lore describes Lalafell as diminutive and rotund; compact perceived size, never child age or pitch.",
    "Miqo'te": "Visible ears and tails are not vocal-tract evidence; no acoustic modifier.",
    "Roegadyn": "Official lore describes Roegadyn as massive and muscular; the strongest size/weight prior, not low pitch or chest resonance.",
    "Au Ra": "Official lore describes a pronounced male/female body-mass disparity; apply the sex-specific prior below.",
    "Viera": "Official lore describes Viera as tall and slender; a small perceived-size prior only.",
    "Hrothgar": "Official lore describes male Hrothgar as burly and female Hrothgar as lithe and muscular; a modest sex-specific prior, not growling resonance.",
}
CLAN_MORPHOLOGY_RATIONALE = {
    ("Hyur", "Highlander"): "Highlanders are officially larger and more muscular than Midlanders; a small increment over the Hyur baseline.",
}
RACE_GENDER_MORPHOLOGY_RATIONALE = {
    ("Au Ra", "male"): "Au Ra males are officially larger-bodied than Au Ra females; a modest perceived-size/weight increment.",
    ("Au Ra", "female"): "Au Ra females are officially smaller-bodied than Au Ra males; a very small compact-size adjustment.",
    ("Hrothgar", "male"): "Male Hrothgar are officially burly; a moderate perceived-size/weight increment.",
    ("Hrothgar", "female"): "Female Hrothgar are officially lithe and muscular; a small perceived-size/weight increment.",
}

RACE_SOURCES = {
    "Hyur": "https://na.finalfantasyxiv.com/a_realm_reborn/world/races",
    "Elezen": "https://na.finalfantasyxiv.com/a_realm_reborn/world/races",
    "Lalafell": "https://na.finalfantasyxiv.com/a_realm_reborn/world/races",
    "Miqo'te": "https://na.finalfantasyxiv.com/a_realm_reborn/world/races",
    "Roegadyn": "https://eu.finalfantasyxiv.com/a_realm_reborn/sp/world/races/roegadyn/",
    "Au Ra": "https://eu.finalfantasyxiv.com/heavensward/sp/story/newrace/",
    "Viera": "https://na.finalfantasy.com/en/titles/finalfantasy14/",
    "Hrothgar": "https://na.finalfantasyxiv.com/dawntrail/system/",
}
HROTHGAR_SOCIAL_CLANS = ("Helions", "The Lost")

TIMBRE_ORDER = (
    "clear", "warm", "bright", "dark", "rich", "mellow", "smooth",
    "silvery", "nasal", "hollow", "metallic", "breathy", "husky",
    "gravelly", "raspy", "hoarse", "reedy", "booming", "whispery",
    "synthetic",
)

AUDIO_FIELDS = (
    "presentation", "perceived_age", "perceived_vocal_size", "vocal_weight",
    "pitch", "pitch_range", "timbre_primary", "timbre_secondary", "resonance",
)
AUDIO_VALUES = {
    "presentation": {"masculine", "feminine", "androgynous", "neutral"},
    "perceived_age": {"child", "youthful", "young_teen", "older_teen",
                      "young_adult", "adult", "middle_aged", "mature",
                      "elderly", "ageless", "ancient"},
    "perceived_vocal_size": {"compact", "average", "large"},
    "vocal_weight": {"light", "balanced", "full"},
    "pitch": set(PITCH_VALUES),
    "pitch_range": {"narrow_range", "wide_range"},
    "timbre_primary": set(TIMBRE_ORDER),
    "timbre_secondary": set(TIMBRE_ORDER),
    "resonance": {"chest_resonant", "forward_resonant", "head_resonant",
                  "rounded", "compact", "open", "hollow", "nasal", "throaty",
                  "deep_resonance", "light_resonance"},
}


def _warn(warnings: list[dict[str, Any]], field: str, raw: Any, reason: str) -> None:
    warnings.append({"field": field, "raw": raw, "reason": reason})


def normalize_sex(value: Any, warnings: list[dict[str, Any]] | None = None) -> str | None:
    """Return male or female, or None when the game does not know.

    Unrecognized model values are preserved by the caller as raw data and
    normalized to None; they are not a fatal error.
    """
    sex = str(value or "").strip().casefold()
    if not sex or sex == "unknown":
        return None
    if sex not in SEX_VALUES:
        if warnings is not None:
            _warn(warnings, "sex", value, "unrecognized ENpcBase.Gender value; normalized to null")
        return None
    return sex


def normalize_race(value: Any, warnings: list[dict[str, Any]] | None = None) -> str | None:
    if value in (None, ""):
        return None
    race = str(value).strip()
    if race not in PLAYABLE_RACES:
        if warnings is not None:
            _warn(warnings, "race", value, "unrecognized playable race; raw value retained, normalized to null")
        return None
    return race


def normalize_clan(
    race: str | None,
    value: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> str | None:
    if value in (None, ""):
        return None
    clan = str(value).strip()
    if race is None:
        if warnings is not None:
            _warn(warnings, "clan", value, "clan has no recognized playable race; normalized to null")
        return None
    if clan not in PLAYABLE_CLANS.get(race, ()):
        if warnings is not None:
            _warn(warnings, "clan", value, f"clan is not valid for {race}; raw value retained, normalized to null")
        return None
    return clan


def normalize_non_playable_race(
    value: Any,
    warnings: list[dict[str, Any]] | None = None,
) -> str | None:
    if value in (None, ""):
        return None
    race = str(value).strip()
    if race not in NON_PLAYABLE_RACES:
        if warnings is not None:
            _warn(warnings, "non_playable_race", value, "unrecognized non-playable race; raw value retained, normalized to null")
        return None
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
    return round(number, 3) if math.isfinite(number) and number > 0 else None


def _body_type(value: Any, warnings: list[dict[str, Any]]) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        _warn(warnings, "body_type", value, "non-numeric BodyType; raw value retained, normalized to null")
        return None
    if number in BODY_TYPE_AGE:
        return number
    _warn(warnings, "body_type", value, "unsupported BodyType; raw value retained, normalized to null")
    return None


def _decision(
    value: Any,
    *,
    rule: str | None,
    evidence_type: str,
    facts: dict[str, Any] | None = None,
    factors: list[dict[str, Any]] | None = None,
    prior_score: float | None = None,
    casting_strength: float | None = None,
    rationale: str | None = None,
    sources: list[str] | None = None,
    source: str | None = None,
    quote: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "value": value,
        "rule": rule,
        "evidence_type": evidence_type,
        "rule_version": RULES_VERSION,
    }
    if facts is not None:
        record["facts"] = facts
    if factors:
        record["factors"] = factors
    if prior_score is not None:
        record["prior_score"] = round(min(1.0, max(0.0, prior_score)), 3)
    if casting_strength is not None:
        record["casting_strength"] = round(min(1.0, max(0.0, casting_strength)), 3)
    if rationale:
        record["rationale"] = rationale
    if sources:
        record["sources"] = list(dict.fromkeys(sources))
    if source:
        record["source"] = source
    if quote:
        record["quote"] = quote
    if note:
        record["note"] = note
    return record


def _fact_facts(facts: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: facts.get(key) for key in keys}


def _verified_voice_evidence(identity: dict) -> dict | None:
    """Return evidence only when it proves a speaker-to-audio relationship.

    An SCD/container path alone is a raw game fact, not proof that this NPC has
    recorded lines. Callers must pass either ``has_se_voice_reference`` or a
    voice-evidence record that is explicitly verified (or carries a source plus
    at least one measured field).
    """
    evidence = identity.get("voice_evidence")
    if not isinstance(evidence, dict):
        return None
    if evidence.get("verified") is True:
        return evidence
    if evidence.get("source") and any(evidence.get(field) for field in AUDIO_FIELDS):
        return evidence
    return None


def _audio_or_unavailable(field: str, voice_evidence: dict | None) -> dict[str, Any] | None:
    """Return an audio-backed decision when one exists, else None."""
    if not isinstance(voice_evidence, dict):
        return None
    value = voice_evidence.get(field)
    if not isinstance(value, str) or value not in AUDIO_VALUES.get(field, set()):
        return None
    return _decision(
        value,
        rule="audio_evidence",
        evidence_type="explicit_audio",
        source=str(voice_evidence.get("source") or "audio:voice_evidence"),
        quote=f"{field}={value}",
        rationale="Measured from a verified recorded voice, not inferred from appearance.",
        note="Audio evidence wins over synthetic casting for this field.",
    )


def _size_label(score: float) -> str:
    if score <= -0.25:
        return "compact"
    if score >= 0.25:
        return "large"
    return "average"


def _weight_label(score: float) -> str:
    if score <= -0.25:
        return "light"
    if score >= 0.25:
        return "full"
    return "balanced"


def _morphology_scores(
    *,
    race: str | None,
    clan: str | None,
    model_gender: str | None,
    muscle_tone: int | None,
) -> tuple[float, float, list[dict[str, Any]]]:
    """Combine documented morphology priors into size/weight scores.

    Height is deliberately not applied: the export gives a per-model slider,
    not a physical height, so it cannot be normalized without validated
    race+gender ranges. Muscle tone is a weak weight cue only for the model
    combinations where BustOrTone1 is known to mean muscle tone.
    """
    size = 0.0
    weight = 0.0
    factors: list[dict[str, Any]] = []

    def add(name: str, value: Any, prior: dict[str, float], rationale: str, sources: list[str]) -> None:
        nonlocal size, weight
        size += float(prior.get("size", 0.0))
        weight += float(prior.get("weight", 0.0))
        factors.append({
            "name": name,
            "value": value,
            "size_delta": round(float(prior.get("size", 0.0)), 3),
            "weight_delta": round(float(prior.get("weight", 0.0)), 3),
            "rationale": rationale,
            "sources": sources,
        })

    if race in RACE_MORPHOLOGY_PRIOR:
        add(
            "race_morphology_prior", race, RACE_MORPHOLOGY_PRIOR[race],
            RACE_MORPHOLOGY_RATIONALE.get(race, "Documented race morphology prior."),
            [RACE_SOURCES[race]],
        )
    clan_prior = CLAN_MORPHOLOGY_PRIOR.get((race, clan))
    if clan_prior is not None:
        add(
            "clan_morphology_prior", clan, clan_prior,
            CLAN_MORPHOLOGY_RATIONALE.get((race, clan), "Documented clan morphology prior."),
            [RACE_SOURCES.get(race or "", "")],
        )
    gender_prior = RACE_GENDER_MORPHOLOGY_PRIOR.get((race, model_gender))
    if gender_prior is not None:
        add(
            "race_gender_morphology_prior", model_gender, gender_prior,
            RACE_GENDER_MORPHOLOGY_RATIONALE.get((race, model_gender), "Documented race/gender morphology prior."),
            [RACE_SOURCES.get(race or "", "")],
        )
    if muscle_tone is not None:
        delta = round(((muscle_tone - 50) / 50.0) * 0.08, 3)
        weight += delta
        factors.append({
            "name": "muscle_tone_prior",
            "value": muscle_tone,
            "size_delta": 0.0,
            "weight_delta": delta,
            "rationale": "Muscle tone is a weak creative cue for perceived vocal fullness, not vocal-fold mass or pitch.",
            "sources": [RACE_SOURCES.get(race or "", "")],
        })
    return (
        min(1.0, max(-1.0, size)),
        min(1.0, max(-1.0, weight)),
        factors,
    )


def _timbre_preferences(size_label: str, weight_label: str) -> list[str]:
    scores: dict[str, float] = {}
    if size_label == "compact":
        scores["bright"] = scores.get("bright", 0.0) + 0.2
    elif size_label == "large":
        scores["dark"] = scores.get("dark", 0.0) + 0.2
    if weight_label == "full":
        scores["rich"] = scores.get("rich", 0.0) + 0.2
    elif weight_label == "light":
        scores["clear"] = scores.get("clear", 0.0) + 0.2
    ordered = sorted(scores.items(), key=lambda item: (-item[1], TIMBRE_ORDER.index(item[0])))
    return [choice for choice, _ in ordered[:2]]


def cast_identity(identity: dict, *, npc_id: int | None = None) -> dict[str, Any]:
    """Return facts, one decision per identity field, and validation warnings.

    Facts are always recorded. Synthetic acoustic identity is inferred only when
    no verified recorded voice exists; otherwise acoustic fields stay null for
    the audio pipeline while the game facts remain intact.
    """
    warnings: list[dict[str, Any]] = []
    sex_reported = identity.get("sex")
    sex = normalize_sex(sex_reported, warnings)

    race_reported = identity.get("race")
    non_playable_reported = identity.get("non_playable_race")
    race_value = race_reported
    if race_value in NON_PLAYABLE_RACES:
        if non_playable_reported and non_playable_reported != race_value:
            raise ValueError(
                f"race {race_value!r} conflicts with non_playable_race {non_playable_reported!r}"
            )
        non_playable_reported = race_value
        race_value = None
    race = normalize_race(race_value, warnings)
    clan_reported = identity.get("clan")
    clan = normalize_clan(race, clan_reported, warnings)
    non_playable_race = normalize_non_playable_race(non_playable_reported, warnings)

    body_type_reported = identity.get("body_type")
    body_type = _body_type(body_type_reported, warnings)

    # A gender byte is only trustworthy for a recognized playable-race model.
    # Non-playable races, beasts, monsters, and zero-default rows may have no
    # gender at all; their default byte must not become "male".
    gender_ignored_reason = None
    if sex is not None and race is None:
        gender_ignored_reason = "no recognized playable-race model"
        sex = None
    elif sex is not None and body_type_reported == 0:
        gender_ignored_reason = "zero/default BodyType row"
        sex = None

    raw_height = identity.get("height")
    height = _height(raw_height) if race else None
    # BodyType 0 is the exporter's zero-default row: its height 0 is absent
    # model data, not the bottom of the slider. Keep the raw byte, drop the
    # normalized height. A real BodyType 1/3/4 model may legitimately use 0.
    if body_type_reported == 0:
        height = None
    raw_scale = identity.get("scale")
    scale = _scale(raw_scale)
    raw_bust = _percent(identity.get("bust_or_tone_1"))
    raw_extra = _percent(identity.get("extra_feature_2_or_bust"))

    # BustOrTone1 / ExtraFeature2OrBust per the reference extractor:
    # - Hyur and Roegadyn, either gender: BustOrTone1 is muscle tone; female
    #   models use ExtraFeature2OrBust for bust.
    # - Au Ra male: BustOrTone1 is muscle tone.
    # - Au Ra female: BustOrTone1 is bust.
    # - Other female playable races: BustOrTone1 is bust.
    # - Other male playable races: neither field is interpreted.
    muscle_tone = None
    bust = None
    if race in {"Hyur", "Roegadyn"}:
        muscle_tone = raw_bust
        if sex == "female":
            bust = raw_extra
    elif race == "Au Ra" and sex == "male":
        muscle_tone = raw_bust
    elif sex == "female":
        bust = raw_bust

    voice_evidence = _verified_voice_evidence(identity)
    # An SCD/container reference or a `has_se_voice_reference` flag is a raw
    # game fact, not proof that this NPC's own lines are recorded. Only verified
    # speaker-to-audio evidence may route acoustic identity away from casting.
    has_recorded_voice = voice_evidence is not None

    facts = {
        "model_gender": sex,
        "model_gender_reported": sex_reported,
        "gender_ignored_reason": gender_ignored_reason,
        "race": race,
        "race_reported": race_reported,
        "clan": clan,
        "clan_reported": clan_reported,
        "non_playable_race": non_playable_race,
        "non_playable_race_reported": non_playable_reported,
        "body_type": body_type,
        "body_type_reported": body_type_reported,
        "height": height,
        "height_raw": raw_height,
        "scale": scale,
        "scale_raw": raw_scale,
        "bust_or_tone_1": raw_bust,
        "extra_feature_2_or_bust": raw_extra,
        "muscle_tone": muscle_tone,
        "bust": bust,
        "jaw": identity.get("jaw"),
        "mouth": identity.get("mouth"),
        "face": identity.get("face"),
        "nose": identity.get("nose"),
        "eye_shape": identity.get("eye_shape"),
        "eyebrows": identity.get("eyebrows"),
        "hair_style": identity.get("hair_style"),
        "skin_color": identity.get("skin_color"),
        "has_scd": bool(identity.get("has_scd")),
        "has_se_voice_reference": bool(identity.get("has_se_voice_reference")),
        "voice_evidence_verified": voice_evidence is not None,
        "has_recorded_voice": has_recorded_voice,
    }
    if race == "Hrothgar" and clan in HROTHGAR_SOCIAL_CLANS:
        facts["clan_semantics"] = "social_status"

    model_gender_decision = _decision(
        sex,
        rule="enpcbase_gender_as_recorded",
        evidence_type="game_fact",
        facts=_fact_facts(facts, "model_gender", "model_gender_reported", "race", "body_type_reported"),
        rationale="Model gender is copied from ENpcBase.Gender through the export's 0=male, 1=female mapping.",
        source=f"game:ENpcBase/{npc_id}" if npc_id is not None else "game:ENpcBase",
        quote=f"Gender={sex_reported!r}",
        note=(
            f"Model gender ignored: {gender_ignored_reason}."
            if gender_ignored_reason else
            "Unknown or missing gender stays null; it is not mapped to neutral."
        ),
    )
    race_decision = _decision(
        race,
        rule="game_race_as_recorded",
        evidence_type="game_fact",
        facts=_fact_facts(facts, "race", "race_reported"),
        rationale="Playable race is copied from the game export.",
        source=f"game:ENpcBase/{npc_id}" if npc_id is not None else "game:ENpcBase",
        quote=f"Race={race_reported!r}",
    )
    clan_decision = _decision(
        clan,
        rule="game_tribe_as_recorded",
        evidence_type="game_fact",
        facts=_fact_facts(facts, "clan", "clan_reported", "clan_semantics"),
        rationale="Tribe/subrace is copied exactly; Hrothgar Helion/Lost are social-status categories, not biological clans.",
        source=f"game:ENpcBase/{npc_id}" if npc_id is not None else "game:ENpcBase",
        quote=f"Tribe={clan_reported!r}",
    )
    non_playable_decision = _decision(
        non_playable_race,
        rule="non_playable_race_as_recorded",
        evidence_type="game_fact",
        facts=_fact_facts(facts, "non_playable_race", "non_playable_race_reported"),
        rationale="Non-playable race is preserved as a raw game fact and has no default acoustic mapping.",
        source=f"game:ENpcBase/{npc_id}" if npc_id is not None else "game:ENpcBase",
        quote=f"Race={non_playable_reported!r}",
    )

    age = BODY_TYPE_AGE.get(body_type) if body_type is not None else None
    age_decision = _decision(
        age,
        rule=BODY_TYPE_RULE.get(body_type) if age else None,
        evidence_type=BODY_TYPE_EVIDENCE.get(body_type, "unknown") if age else "unknown",
        facts=_fact_facts(facts, "body_type", "body_type_reported"),
        rationale=(
            "BodyType 1 is the ordinary/default model; adult-appearing is a project casting convention, not chronological age."
            if body_type == 1 else
            "The reference extractor names BodyType 3 Elderly; set perceived age without forcing a lower pitch."
            if body_type == 3 else
            "BodyType 4 is the small model shared by children and some young teens; render it as youthful/childlike, not a precise age."
            if body_type == 4 else
            "No supported BodyType age mapping."
        ),
        source=f"game:ENpcBase/{npc_id}" if npc_id is not None and body_type in {3, 4} else None,
        quote=f"BodyType={body_type_reported!r}" if body_type in {3, 4} else None,
        note=(
            "BodyType 1 has no chronological-age fact; Lalafell age is never inferred from race."
            if body_type == 1 else None
        ),
    )

    # Actual verified voice evidence has priority over appearance inference for
    # acoustic fields. Facts above are never removed.
    if has_recorded_voice:
        acoustic: dict[str, dict[str, Any]] = {}
        for field in AUDIO_FIELDS:
            measured = _audio_or_unavailable(field, voice_evidence)
            acoustic[field] = measured or _decision(
                None,
                rule="voiced_use_audio_evidence",
                evidence_type="unknown",
                facts=_fact_facts(facts, "has_recorded_voice", "has_se_voice_reference", "has_scd"),
                note="NPC has a verified recorded voice; derive this acoustic field from the audio, not appearance.",
            )
        timbre_preferences = [acoustic["timbre_primary"]["value"]] if acoustic["timbre_primary"]["value"] else None
        return {
            "facts": facts,
            "warnings": warnings,
            "model_gender": model_gender_decision,
            "presentation": acoustic["presentation"],
            "race": race_decision,
            "clan": clan_decision,
            "non_playable_race": non_playable_decision,
            "perceived_age": acoustic["perceived_age"],
            "perceived_vocal_size": acoustic["perceived_vocal_size"],
            "vocal_weight": acoustic["vocal_weight"],
            "pitch": acoustic["pitch"],
            "pitch_range": acoustic["pitch_range"],
            "timbre_primary": acoustic["timbre_primary"],
            "timbre_secondary": acoustic["timbre_secondary"],
            "timbre_preferences": _decision(
                timbre_preferences,
                rule="audio_evidence" if timbre_preferences else None,
                evidence_type="explicit_audio" if timbre_preferences else "unknown",
                facts=_fact_facts(facts, "has_recorded_voice"),
                source=str(voice_evidence.get("source") or "audio:voice_evidence") if timbre_preferences else None,
                quote="timbre_primary" if timbre_preferences else None,
                rationale="Timbre preference copied from verified audio evidence." if timbre_preferences else None,
            ),
            "resonance": acoustic["resonance"],
            "articulation": _decision(
                None, rule=None, evidence_type="unknown",
                note="Jaw and mouth are customization indices, not vocal-tract measurements; articulation requires audio evidence.",
            ),
            "speaking_pace": _decision(
                None, rule=None, evidence_type="unknown",
                note="Speaking pace requires audio measurement; text cannot measure actual speed.",
            ),
            "intonation": _decision(
                None, rule=None, evidence_type="unknown",
                note="Intonation requires audio measurement; text cannot measure F0 contours.",
            ),
            "distinctive_traits": _decision(
                None, rule=None, evidence_type="unknown",
                note="No approved explicit trait evidence.",
            ),
        }

    presentation = {"male": "masculine", "female": "feminine"}.get(sex)
    presentation_decision = _decision(
        presentation,
        rule="sex_to_voice_presentation" if presentation else None,
        evidence_type="creative_selection" if presentation else "unknown",
        facts=_fact_facts(facts, "model_gender", "model_gender_reported"),
        rationale=(
            "Deterministic project mapping from valid model gender to synthetic vocal presentation; it is not a measured property."
            if presentation else
            "Unknown model gender is not mapped to neutral; presentation stays null."
        ),
    )

    # Pitch: gender baseline plus the BodyType 4 child override only.
    if body_type == 4:
        pitch = CHILD_PITCH_BY_GENDER.get(sex, "mid_high")
        pitch_rule = "body_type_child_pitch"
        pitch_rationale = "BodyType 4 selects the child/youthful pitch prior; race, height and muscle tone do not shift pitch."
    elif sex in BASE_PITCH_BY_GENDER:
        pitch = BASE_PITCH_BY_GENDER[sex]
        pitch_rule = "gender_base_pitch"
        pitch_rationale = "A deliberate synthetic pitch convention from model gender; it is not a measured acoustic pitch."
    else:
        pitch = None
        pitch_rule = None
        pitch_rationale = "No valid model gender or child-model override; pitch stays null."
    pitch_decision = _decision(
        pitch,
        rule=pitch_rule,
        evidence_type="creative_selection" if pitch else "unknown",
        facts=_fact_facts(facts, "model_gender", "body_type", "body_type_reported"),
        rationale=pitch_rationale,
    )

    size_score, weight_score, morphology_factors = _morphology_scores(
        race=race,
        clan=clan,
        model_gender=sex,
        muscle_tone=muscle_tone,
    )
    size = _size_label(size_score) if race else None
    weight = _weight_label(weight_score) if race else None
    size_strength = sum(abs(factor["size_delta"]) for factor in morphology_factors)
    weight_strength = sum(abs(factor["weight_delta"]) for factor in morphology_factors)
    morphology_facts = _fact_facts(
        facts, "race", "clan", "model_gender", "body_type", "muscle_tone", "height_raw",
    )
    morphology_sources = [
        source for factor in morphology_factors for source in factor.get("sources", []) if source
    ]
    size_decision = _decision(
        size,
        rule="race_morphology_prior" if size else None,
        evidence_type="creative_selection" if size else "unknown",
        facts=morphology_facts,
        factors=morphology_factors or None,
        prior_score=abs(size_score) if size else None,
        casting_strength=size_strength if size else None,
        rationale=(
            "Perceived vocal size is a creative prior from documented race/clan morphology; "
            "it is not a measured formant scale, pitch, or official voice fact."
            if size else
            "No recognized race morphology prior; perceived vocal size stays null."
        ),
        sources=morphology_sources or None,
        note=(
            "Height is preserved as a raw slider but not used until validated race+gender ranges exist."
            if size else None
        ),
    )
    weight_decision = _decision(
        weight,
        rule="race_morphology_prior" if weight else None,
        evidence_type="creative_selection" if weight else "unknown",
        facts=morphology_facts,
        factors=morphology_factors or None,
        prior_score=abs(weight_score) if weight else None,
        casting_strength=weight_strength if weight else None,
        rationale=(
            "Perceived vocal weight is a creative prior from documented morphology and, where valid, "
            "a weak muscle-tone cue. It is not vocal-fold mass, loudness, or a measured acoustic property."
            if weight else
            "No recognized race morphology prior; vocal weight stays null."
        ),
        sources=morphology_sources or None,
        note=(
            "Muscle tone is never used for pitch or resonance."
            if muscle_tone is not None else None
        ),
    )

    preferences = _timbre_preferences(size, weight) if size and weight else []
    timbre_primary = preferences[0] if preferences else None
    timbre_secondary = preferences[1] if len(preferences) > 1 else None
    timbre_preferences_decision = _decision(
        preferences or None,
        rule="size_weight_timbre_preference" if preferences else None,
        evidence_type="creative_selection" if preferences else "unknown",
        facts=morphology_facts,
        prior_score=0.2 if preferences else None,
        casting_strength=0.2 * len(preferences) if preferences else None,
        rationale=(
            "Optional timbre preferences derived weakly from perceived vocal size/weight; "
            "they are not forced race timbres or resonance placements."
            if preferences else
            "No size/weight preference strong enough to suggest a timbre; omit it."
        ),
        sources=morphology_sources or None,
    )
    timbre_primary_decision = _decision(
        timbre_primary,
        rule="size_weight_timbre_preference" if timbre_primary else None,
        evidence_type="creative_selection" if timbre_primary else "unknown",
        facts=morphology_facts,
        prior_score=0.2 if timbre_primary else None,
        casting_strength=0.2 if timbre_primary else None,
        rationale="Optional timbre preference derived from perceived vocal size/weight." if timbre_primary else "No supported timbre preference; omit timbre.",
        sources=morphology_sources or None,
    )
    timbre_secondary_decision = _decision(
        timbre_secondary,
        rule="size_weight_timbre_preference" if timbre_secondary else None,
        evidence_type="creative_selection" if timbre_secondary else "unknown",
        facts=morphology_facts,
        prior_score=0.2 if timbre_secondary else None,
        casting_strength=0.2 if timbre_secondary else None,
        rationale="Second optional timbre preference from perceived vocal size/weight." if timbre_secondary else "No approved secondary-timbre evidence.",
        sources=morphology_sources or None,
    )

    return {
        "facts": facts,
        "warnings": warnings,
        "model_gender": model_gender_decision,
        "presentation": presentation_decision,
        "race": race_decision,
        "clan": clan_decision,
        "non_playable_race": non_playable_decision,
        "perceived_age": age_decision,
        "perceived_vocal_size": size_decision,
        "vocal_weight": weight_decision,
        "pitch": pitch_decision,
        "pitch_range": _decision(
            None,
            rule=None,
            evidence_type="unknown",
            facts=_fact_facts(facts, "race", "clan", "model_gender", "height_raw"),
            note="Pitch range requires an actual audio measurement; it is not inferable from race, clan, height, or text.",
        ),
        "timbre_primary": timbre_primary_decision,
        "timbre_secondary": timbre_secondary_decision,
        "timbre_preferences": timbre_preferences_decision,
        "resonance": _decision(
            None,
            rule=None,
            evidence_type="unknown",
            note="Resonance is a vocal-production descriptor; race and body shape do not establish chest/head/forward/throaty placement.",
        ),
        "articulation": _decision(
            None,
            rule=None,
            evidence_type="unknown",
            note="Jaw and mouth are customization indices, not vocal-tract measurements; articulation requires audio evidence.",
        ),
        "speaking_pace": _decision(
            None,
            rule=None,
            evidence_type="unknown",
            note="Speaking pace is not inferable from static game facts or transcripts; it requires audio measurement.",
        ),
        "intonation": _decision(
            None,
            rule=None,
            evidence_type="unknown",
            note="Intonation is not inferable from physical game facts or ordinary text; it requires audio evidence.",
        ),
        "distinctive_traits": _decision(
            None,
            rule=None,
            evidence_type="unknown",
            note="No approved explicit lore trait or audio evidence.",
        ),
    }
