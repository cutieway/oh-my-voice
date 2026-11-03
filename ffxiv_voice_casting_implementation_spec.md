# FFXIV synthetic voice casting — implementation specification

## Purpose

Refactor `tools/blueprint_pipeline/casting.py` so that an unvoiced FFXIV NPC receives a **deterministic synthetic voice identity** from verified game facts, with carefully labelled lore-informed creative priors. The independent JEV dialogue-classification pipeline supplies a stable character-level **Style** clause. `Identity + Style` are concatenated into one Qwen3-TTS VoiceDesign `instruct` to create a canonical reference recording. That recording is pinned and used with Qwen3-TTS Base ICL to synthesize the character's unvoiced lines.

This is a casting system, not an assertion that FFXIV lore gives us scientifically measurable voice anatomy. Keep provenance explicit:

- `game_fact`: copied/decoded from game data.
- `lore_fact`: supported by official FFXIV lore.
- `explicit_audio`: measured from a verified recording.
- `creative_selection`: a deliberate, deterministic synthetic-casting choice informed by game facts/lore. Never describe it as measured acoustics.
- `unknown`: no sufficiently defensible mapping; render nothing for that field.

The same NPC plus the same game-data version and casting-rule version must produce the same Identity prompt. JEV Style is separately versioned and can change when its dialogue sample or classifier changes.

---

## 1. Target architecture

Keep three internal layers, but render two prompt clauses:

1. **Facts** — immutable normalized game facts, including raw values and source identifiers.
2. **Identity casting** — deterministic rules transform facts into stable acoustic *design choices*.
3. **Style classification** — JEV classifies the character's recurring written-dialogue manner. This is a separate pipeline and is appended to the Identity sentence when generating the canonical VoiceDesign reference.

```python
facts = extract_and_normalize_game_facts(raw_npc)
identity = cast_voice_identity(facts, rules_version="2.0")
style = classify_character_style(dialogue_dossier, lore_context=style_context)

instruct = render_instruct(identity=identity, style=style)
reference = generate_voice_design(text=SHARED_REFERENCE_TEXT,
                                  instruct=instruct,
                                  seed=stable_seed(npc_id, instruct, rules_version))
pin_reference(reference.wav, reference.transcript, instruct, seed, model_version)

# Later: use the pinned reference WAV + exact transcript with Qwen3-TTS Base ICL.
```

**Do not make Identity disappear merely because an NPC has recorded dialogue.** The pipeline should decide separately whether an NPC needs a synthetic reference. If an NPC has an actual verified voice recording, that recording is the ground truth and normally should be used instead of synthetic casting. A mere SCD/container reference is not enough to prove that a particular NPC has recorded speech; require a verified speaker-to-asset relationship.

**Do not move this Style into per-line emotion prompting.** In this project, Style is a stable character-level continuation of Identity, used to shape the canonical reference voice. The actual text of each later line can still influence delivery naturally in Qwen Base.

---

## 2. Verified game-data extraction corrections

Use the actual export schema as the authority. GarlandTools' NPC extractor reads `ENpcBase.Gender`, `Race`, `Tribe`, `Height`, `BodyType`, and appearance fields. It only creates a normal appearance record when `Race` is a recognized nonzero race key. Its code treats `Gender == 0` as male and other valid model gender as female. Do not document this as `ENpcResident.Sex` unless the exact upstream data pipeline really uses that field.

### BodyType

The reference extractor maps:

| Raw value | Exporter classification | Casting treatment |
|---:|---|---|
| `1` | Default/standard model; omitted from the optional appearance JSON | `standard` model. It may be cast as an adult-sounding voice as a project convention, but it is not a chronological-age fact. |
| `3` | `Elderly` | Deterministically set perceived vocal age to `elderly`. Do not force a lower pitch. |
| `4` | `Child` | Deterministically set perceived vocal age to `child` or `youthful/childlike`; do not relabel every case `young_teen`. |
| other / absent | Unknown | `None`; preserve raw value. |

Important: the official data extraction names 3 `Elderly` and 4 `Child`. BodyType 4 is described in project comments as a small model used for children and young teens, so it does not reliably distinguish a child from a young teen. A `childlike`/`youthful` prompt token is safer than claiming a precise chronological age. BodyType 1 is the ordinary/default model, not proof of an NPC's exact lore age. Do not use Lalafell's childlike appearance to override this field; official lore explicitly says outsiders cannot reliably judge Lalafell age by appearance.

### Gender and race

- Rename the normalized fact to `model_gender` (or retain the legacy `sex` field with a documented source). Source it from the actual game-export field. For the GarlandTools-style extraction: `ENpcBase.Gender == 0` => `male`, the other valid playable model value => `female`.
- Only treat gender as known when the NPC has a recognized humanoid/playable-race model record. A zero/default byte for a unique creature or unsupported model is not evidence of male.
- `male` -> synthetic voice `presentation=masculine`; `female` -> `presentation=feminine`. This is a deterministic project mapping, not a measured property. Do not map missing/unknown gender to `neutral`; unknown is `None`. `neutral`/`androgynous` should be selected only by an explicit separate casting decision or audio evidence.
- Preserve the raw race value. Normalize known playable race values; do not throw merely because a later patch or an unrecognized NPC race appears. Unknown value => normalized `None`, raw value retained, validation warning recorded.
- Preserve the source field `Tribe` exactly. Prefer the internal name `tribe_or_subrace` instead of calling every value a biological clan. In particular, Hrothgar Helion/Lost are social-status categories, not biological clans.

### BustOrTone1 / ExtraFeature2OrBust

The current code's muscle/bust interpretation is incomplete. The GarlandTools extractor handles these values as follows:

| Race/model | `BustOrTone1` | `ExtraFeature2OrBust` |
|---|---|---|
| Hyur, either model gender | Muscle tone | Bust only for female models |
| Roegadyn, either model gender | Muscle tone | Bust only for female models |
| Au Ra male | Muscle tone | Do not assume bust from this field |
| Au Ra female | Bust | Not muscle |
| Other female playable races | Bust | Not muscle |
| Other male playable races | Do not infer muscle from this field | Not bust |

Implement:

```python
muscle_tone = None
bust = None
if race in {"Hyur", "Roegadyn"}:
    muscle_tone = raw_bust_or_tone_1
    if model_gender == "female":
        bust = raw_extra_feature_2_or_bust
elif race == "Au Ra" and model_gender == "male":
    muscle_tone = raw_bust_or_tone_1
elif model_gender == "female":
    bust = raw_bust_or_tone_1
```

Do not infer muscle tone for other combinations without a validated model-specific field definition. Do not treat bust size as a voice factor.

### Remaining appearance fields

Keep these as verified appearance facts, but **do not currently map them to voice identity**:

- `face`, `jaw`, `eyebrows`, `nose`, `mouth`, `eye_shape`
- `hair_style`, `hair_color`, `skin_color`
- horns, ears, tail, face markings, extra features
- `bust`

The customization index for a jaw or mouth is not a measurement of vocal-tract length, oral-cavity shape, tongue position, or articulation. Face/hair/skin/eye customization is not an age or voice measurement. Do not use a face index or white hair to infer elderly voice automatically.

### Height and scale

- `height` is an appearance slider, not a universal physical height. A slider value of 60 on one race/gender is not necessarily the same height as 60 on another.
- Never use raw `height <= 40` or `height >= 60` as an absolute voice rule. If the exporter can provide validated race+gender minimum and maximum physical heights, convert the slider to a normalized within-model percentile first. Until then, preserve the raw slider but do not use it in casting.
- If normalized height is later used, it may make a *very weak* contribution to perceived vocal size/formant impression only. Do not use it to move pitch.
- `scale` should remain a raw fact only until the export schema confirms that it represents the character's intrinsic body size rather than object/model scaling. Do not use it in voice casting at present.

---

## 3. Acoustic reasoning: what physical lore can and cannot justify

Use a distinction between **perceived vocal size/weight** and actual vocal mechanics.

Human voice research finds that formant-based estimates of vocal-tract length predict height/weight better than fundamental frequency. In one meta-analysis of 39 samples, formant-based estimates explained up to about 10% of height variance, while F0 explained less than 2% within sex. This supports a cautious *perceived size / darker-vs-brighter spectral impression* prior from large vs small morphology; it does not support a strong race-to-pitch rule.

Therefore:

- Sex/gender presentation may set the base pitch family as a deliberate synthetic convention.
- Child body models can receive a higher/youthful pitch prior.
- Race/body size/musculature can weakly affect perceived vocal size, fullness, and possibly a broad timbral color.
- Do **not** infer a pitch range, vocal fry, breathiness, rasp, hoarseness, nasal quality, or chest/head/forward/throaty placement from body shape or race.
- Do **not** equate muscularity with vocal-fold mass or a lower pitch. Muscle tone is only a weak creative cue for perceived vocal weight/fullness.
- Elderly voice should not automatically mean low pitch. Research on aging voices finds more frequent perceptual roughness, breathiness, strain and instability, while F0 changes are variable and can rise in older age groups. Use the elderly label first; only add light aged texture as a low-strength creative cue if desired.

---

## 4. Replace the current race acoustic tables

### Remove / deprecate

Remove these as direct pitch/acoustic mappings:

- `RACE_PITCH_SHIFT`
- `RACE_RESONANCE`
- `RACE_RANGE`

The lore does not tell us that Elezen have forward resonance, Lalafell have head resonance, Roegadyn have chest resonance, or Hrothgar have throaty resonance. Those are specific vocal production/tract descriptors and are not established by game appearance data. Pitch range is also not inferable from race, clan, height, or dialogue text.

Replace `RACE_TIMBRE` with a more defensible morphology prior that first affects **perceived vocal size** and **vocal weight/fullness**, not a hard timbre label.

### New outputs

Add two optional identity dimensions to the vocabulary and renderer:

- `perceived_vocal_size`: `compact`, `average`, `large`
- `vocal_weight`: `light`, `balanced`, `full`

These are deliberate synthetic voice-casting descriptors, not measurements. Render them as natural language (e.g. “a compact, lighter-sounding voice” / “a larger-sounding, full-bodied voice”). If there is not enough evidence, omit them.

A derived timbre preference may be generated from these two dimensions:

- compact size -> weak `bright` preference
- large size -> weak `dark` preference
- full weight -> weak `rich` preference
- light weight -> weak `light/clear` preference, only if supported in the closed vocabulary

Do not automatically turn these preferences into `chest_resonant`, `head_resonant`, `forward_resonant`, or `throaty`.

### Suggested initial race morphology priors

The numeric values below are **starting creative-prior strengths**, not empirical probabilities, confidence, or canonical lore values. Keep them small, document the supporting lore, and tune by blinded listening tests of generated reference clips.

Use two normalized axes:

- `size`: perceived vocal size / formant-scale impression (`-1` compact to `+1` large)
- `weight`: perceived vocal weight/fullness (`-1` light to `+1` full)

```python
RACE_MORPHOLOGY_PRIOR = {
    "Hyur":      {"size": 0.00, "weight": 0.00},  # official lore: average height/build
    "Elezen":    {"size": 0.15, "weight": 0.00},  # tall, long-limbed, slender
    "Lalafell":  {"size": -0.30, "weight": 0.00}, # diminutive; rotund does not equal vocal mass
    "Miqo'te":   {"size": 0.00, "weight": 0.00},  # ears/tail/territoriality are not vocal anatomy
    "Roegadyn":  {"size": 0.55, "weight": 0.55},  # explicitly massive and muscular
    "Au Ra":     {"size": 0.00, "weight": 0.00},  # apply sex-specific dimorphism below
    "Viera":     {"size": 0.15, "weight": 0.00},  # tall and slender
    "Hrothgar":  {"size": 0.15, "weight": 0.10},  # shared baseline; use explicit sex-specific build below
}

CLAN_MORPHOLOGY_PRIOR = {
    ("Hyur", "Highlander"): {"size": 0.10, "weight": 0.15},
    # Midlander is baseline. No other clan currently has a sufficiently direct
    # body-size distinction to justify a separate acoustic prior.
}

RACE_GENDER_MORPHOLOGY_PRIOR = {
    ("Au Ra", "male"):   {"size": 0.20, "weight": 0.20},
    ("Au Ra", "female"): {"size": -0.05, "weight": 0.00},
    ("Hrothgar", "male"):   {"size": 0.30, "weight": 0.35},
    ("Hrothgar", "female"): {"size": 0.10, "weight": 0.15},
}
```

Why these are deliberately conservative:

- Hyur are officially average in height and build; use as baseline.
- Highlanders are explicitly larger and more muscular than Midlanders; give only a small increment over the Hyur baseline.
- Elezen are tall, long-limbed and slender; a small perceived-size prior is more appropriate than a deep/dark/old voice stereotype.
- Lalafell are diminutive and rotund. Use compact perceived size, but **never infer child age or child pitch from race**; official lore explicitly warns that their apparent age is hard for non-Lalafell to judge.
- Miqo'te's visible ears/tail and solitary/territorial social tendencies do not justify a timbre or resonance mapping.
- Roegadyn have the clearest physical evidence for a larger/full-bodied perceived voice. This justifies the strongest weight/size prior, but not an automatic low pitch, chest-resonance technique, growl, or rasp.
- Au Ra are explicitly described as having a large male/female body-mass disparity. Use a modest sex-specific size/fullness difference; their horns improve hearing/spatial recognition, not voice production.
- Viera are tall and slender; small size prior only. Rava vs Veena does not justify an acoustic distinction.
- Hrothgar males are officially described as burly and lionlike; female Hrothgar are officially described as lithe and muscular. Use a modest sex-specific size/fullness choice, not automatic animalistic growl/throaty resonance.

These values should be stored with `evidence_type="creative_selection"`, source URL, rationale, and rule version. They should never be presented as game facts.

### Individual modifiers

1. **Height:** do not apply until normalized within the exact race+gender model range. Then add at most `±0.05` to the `size` score. No pitch change.
2. **Muscle tone:** only for validated model combinations above. Convert the slider to `[-1, +1]` around 50, then add at most `±0.08` to `weight`. Do not use it for pitch or resonance.
3. **BodyType 4 / child:** set perceived age to child/youthful; increase the synthetic pitch prior. Do not let race/height override it.
4. **BodyType 3 / elderly:** set perceived age to elderly; do not force pitch up or down. Optionally allow a very weak `slightly_rough`/`slightly_breathy` creative preference, but omit by default because older voice outcomes vary considerably.
5. **BodyType 1 / standard:** set `perceived_age="adult"` only as a project casting convention for the standard adult model. Retain metadata stating this is an appearance-model mapping, not chronological lore age.

Suggested conversion thresholds (initial tuning values, not research measurements):

```python
def size_label(score):
    if score <= -0.25: return "compact"
    if score >= 0.25: return "large"
    return "average"

def weight_label(score):
    if score <= -0.25: return "light"
    if score >= 0.25: return "full"
    return "balanced"
```

Do not return a fake `confidence` derived from the score. Call it `prior_score` or `casting_strength`. Confidence requires calibration against a labelled voice dataset.

---

## 5. Gender and pitch rules

`model_gender` is a hard game fact when the model record makes it valid. The voice presentation is a deterministic casting convention:

```python
PRESENTATION_BY_GENDER = {
    "male": "masculine",
    "female": "feminine",
}
```

Unknown gender remains `None`, not `neutral`.

For synthetic pitch, use a simple broad sex-presentation baseline and a child-model override. Treat these as project defaults (`creative_selection`), not claims about any particular NPC's actual acoustic pitch:

```python
BASE_PITCH_BY_GENDER = {
    "male": "low_mid",
    "female": "mid_high",
}
CHILD_PITCH_BY_GENDER = {
    "male": "mid_high",
    "female": "high",
}
```

BodyType 4 selects the child override. BodyType 3 (elderly) and BodyType 1 (standard/adult-appearing) use the gender baseline unless actual audio or an explicit character-specific source supports something else. Remove all race-based pitch shifts and all height/muscle-to-pitch shifts.

`pitch_range` should be `None` unless actual audio measurement supports it. A written dialogue classifier cannot determine acoustic pitch range.

---

## 6. Race- and clan-specific lore review

Use this as the complete initial lore mapping for the eight playable races. The race/clan lore may inform identity casting only through documented morphology; cultural facts belong in a **weak JEV context prior**, not a hard acoustic mapping.

| Race / subrace | Verified lore | Identity-casting decision | Optional JEV context prior (weak; dialogue must confirm) |
|---|---|---|---|
| **Hyur** | Average height/build; highly diverse ancestral homelands, languages and traditions; no unified cultural identity. | Baseline `size=0`, `weight=0`. No race timbre, resonance, pitch or range modifier. | No race-wide style prior. Do not assume a standard Hyur accent/register. |
| **Midlander Hyur** | Broadly diverse origins; spread across Eorzea and varied lives. | Baseline. | None. Actual homeland, role and dialogue matter more. |
| **Highlander Hyur** | Noticeably larger and more muscular than Midlanders; historical Gyr Abania/Ala Mhigo association. | Small increase in perceived size/weight. Do not assign low pitch. | Do not automatically assign an Ala Mhigan accent or aggressive style. Use only the individual's dialogue/background. |
| **Elezen** | Tall, long-limbed, slender; longer lifespan than Hyur; historical pride/honor. | Small perceived-size increment; no age, pitch, dark/bright timbre, or resonance stereotype. | Background may support formal/precise wording for some characters, but clan and individual dialogue decide. |
| **Wildwood Elezen** | Forest origins; many moved to city-states; reputation for law/order, haughtiness and argumentativeness. | No separate acoustic prior. | Very weak prior toward formal/ordered/direct or argumentative rhetoric; JEV must confirm and can override. |
| **Duskwight Elezen** | Cave-dwelling, reclusive, often avoid city-states. | No separate acoustic prior. | Very weak reserved/guarded context only where the character is actually portrayed that way. Do not turn “reclusive” into whispery/quiet vocal mechanics. |
| **Lalafell** | Diminutive and rotund; childlike countenance makes apparent age difficult to judge. | Compact perceived-size prior only. Never infer child age/pitch from being Lalafell. | No race-wide childlike, cute, high-pitched, or childish style prior. |
| **Plainsfolk Lalafell** | Many farmers/fishers/naval workers; described as relaxed and carefree. | No separate acoustic prior. | Weak relaxed/conversational style context. JEV dialogue confirms. |
| **Dunesfolk Lalafell** | Thanalan/Ul'dah; commerce-focused, known for turning ideas into action. | No separate acoustic prior. | Weak businesslike/pragmatic vocabulary context for relevant NPCs; don't force this on every Dunesfolk. |
| **Miqo'te** | Projecting ears and tails; many solitary due to territoriality; males described as especially shy of contact. | No race timbre/resonance/pitch/range mapping. Ears/tails are not vocal-tract evidence. | A weak reserved/socially cautious prior may apply to an individual whose dialogue supports it. Do not infer shyness from sex alone. |
| **Seekers of the Sun** | Diurnal, Sun-oriented; some live in Limsa/Sagolii. | No acoustic modifier. | No day/sun-derived vocal tone or pitch. |
| **Keepers of the Moon** | Nocturnal, Moon-oriented; forest hunting history and Gridanian tensions. | No acoustic modifier. | No dark/soft/whispery voice merely because they are nocturnal. |
| **Roegadyn** | Massive, muscular frames; maritime ancestry; culturally competitive, loyal and compassionate. | Strongest large/full perceived-size and weight prior. Do not automatically set low pitch, chest resonance, growl, rasp or booming delivery. | “Competitive”, “loyal”, “compassionate” are personality/social descriptions, not universal speech style. Use individual dialogue. |
| **Sea Wolves** | Northern maritime ancestry; historical pirates; now often sailors/seamen; use old Roegadyn-language names. | Same physical voice prior as Roegadyn; no Sea Wolf-specific resonance/accent. | Nautical terms only when present in dialogue/occupation. Do not infer a sailor accent from clan. |
| **Hellsguard** | Abalathia; tradition of mercenary trade; commonly sellswords/bodyguards. | Same physical voice prior as Roegadyn. | For a mercenary/bodyguard character, dialogue may support direct/terse/commanding rhetoric. This is role evidence, not a Hellsguard acoustic feature. |
| **Au Ra** | Horns aid hearing/spatial recognition; males and females have a pronounced body-mass disparity; native to Othard. | Modest sex-specific perceived-size/weight prior (male larger). Horns do not imply resonance or better articulation. | Raen/Xaela culture can be weak context only; do not infer aggression, serenity, or pitch directly. |
| **Raen Au Ra** | White-scale creation myth; tranquil/solitary cultural description; settled in eastern Othard valleys. | No separate acoustic modifier. | Weak measured/reserved context where supported by the NPC's own dialogue. |
| **Xaela Au Ra** | Black-scale creation myth; free-roaming close-knit tribes on the western steppe, hunting/gathering/warring. | No separate acoustic modifier. | Do not automatically infer aggressive, loud, or confrontational speech from tribal conflict. Dialogue and role must confirm. |
| **Viera** | Tall, slender; long ears; Green Word limits contact with outsiders; traditional forest-protector culture; matriarchal communities. | Small perceived-size increment from tall/slender morphology only. No ear-based resonance, no automatic accent, no pitch/range. | For a traditionally isolated character, weak reserved/formal context may be useful; actual upbringing and dialogue should decide. Do not apply to every Viera, especially diaspora/city-raised NPCs. |
| **Rava / Veena Viera** | Differ by homelands/communities; both belong to Viera and share Green Word concepts. | No separate acoustic mapping. | No evidence for a distinct Rava vs Veena accent or speech pattern. |
| **Hrothgar** | Male Hrothgar officially described as burly and lionlike; sharp claws/fangs; initially had difficulty communicating with commonfolk until mastering the common tongue. Female Hrothgar officially described as lithe and muscular. | Moderate large/full prior; make gender-specific morphology adjustments. Do not automatically assign growl, roar, gravelly timbre, throaty resonance or animal sounds. | Common-tongue acquisition may be relevant to an individual with a specific language-learning history, but race alone does not establish non-native English or a Slavic accent. |
| **Helions / The Lost** | Official Square Enix community representative clarifies these are social-status categories imposed by outsiders, not biological clans with separate physical traits. | No physical/acoustic modifier. Keep raw field, but classify its semantic type as social status. | Potential weak status context (Helion service to queen; Lost after loss of queen) only for an actual NPC whose dialogue demonstrates deference/independence. Do not hard-map it. |

### Non-playable races and beast tribes

Do not apply the eight playable-race morphology table to every non-playable species. Keep `non_playable_race` as a raw/normalized fact and add species-specific entries only when official lore documents physical morphology or a distinctive language/speech convention.

Most non-playable race entries should have no acoustic mapping by default. Do not turn “beast tribe” into “animalistic voice.” Exceptions may be added to the **Style/context** catalogue when the game/language team explicitly establishes a speech convention. For example, the official FFXIV localization interview discusses Sahagin `sh/psh` sentence openings/elongated `sh`, Namazu's inserted `pe` and choppy/excitable phrasing, and Moogle `kupo`. These are linguistic mannerisms, not timbre/resonance. They belong in a style/verbal-habit mapping and must not be generalized to all dialogue by a classifier without textual evidence.

---

## 7. Field-by-field decision table for current `casting.py`

| Current input field | Keep as fact? | Identity use | JEV Style use | Decision |
|---|---:|---|---|---|
| `sex` / `model_gender` | Yes | Hard mapping to masculine/feminine presentation; base pitch family is a deterministic creative default | No | Keep; correct source/documentation and preserve unknown as null |
| `race` | Yes | Only morphology-derived `perceived_vocal_size` / `vocal_weight` priors from §4; no direct race-to-pitch/resonance/range | Optional weak lore context; never substitute for dialogue | Keep, raw + normalized |
| `clan` / `tribe` | Yes | Only Highlander physical modifier; no other current clan acoustic mapping | Weak context for selected clan descriptions; dialogue wins | Rename/annotate semantics; Hrothgar Helion/Lost are social status |
| `non_playable_race` | Yes | No default mapping; explicit per-species extension only | Some species-specific verbal habits if documented and visible in dialogue | Keep raw; do not error on unknown future value |
| `body_type` | Yes | Primary perceived-age mapping; child model affects pitch upward; elderly label but no forced pitch shift | No | Correct `3=elderly`, `4=child`, `1=standard/adult-appearing` |
| `height` | Yes | No effect until normalized within race+gender range; then tiny size prior only | No | Keep raw; remove current raw threshold logic |
| `scale` | Yes, if export-defined | No current mapping | No | Preserve only until semantics validated |
| `bust_or_tone_1` | Yes | Muscle tone only for validated combinations; otherwise bust/raw appearance | No | Correct race/sex extraction as §2 |
| `extra_feature_2_or_bust` | Yes | Female Hyur/Roegadyn bust; no voice effect | No | Preserve and decode correctly |
| `jaw` | Yes | No mapping; index is not a tract measurement | No | Preserve only |
| `mouth` | Yes | No mapping; index is not an articulation measurement | No | Preserve only |
| `face` | Yes | No mapping; index is not age evidence | No | Preserve only |
| `nose` | Yes | No mapping | No | Preserve only |
| `eye_shape` | Yes | No mapping | No | Preserve only |
| `eyebrows` | Yes | No mapping | No | Preserve only |
| `hair_style` / `hair_color` | Yes | No automatic age or voice mapping | No | Preserve only |
| `skin_color` | Yes | No mapping | No | Preserve only |
| `has_se_voice_reference` | Yes, when verified | Routes NPC to actual recorded voice rather than synthetic casting | No | Require verified NPC-to-recording relationship |
| `has_scd` | Raw fact only | Not sufficient on its own to establish NPC voiced status | No | Do not use as the sole voice-evidence gate |
| `voice_evidence` | Yes, provenance required | Actual measured audio can override/validate identity for an already voiced NPC | Audio delivery/style analysis belongs in audio pipeline, not transcript JEV | Keep separate from game facts |

### Current output fields

| Current output | Recommendation |
|---|---|
| `sex` | Rename `model_gender` in facts; do not include as acoustic prompt field unless useful for audit |
| `presentation` | Keep: deterministic mapping from valid model gender; unknown stays null |
| `race`, `clan`, `non_playable_race` | Keep in facts/audit, but do not render as literal voice descriptors unless the prompt's creative profile explicitly needs them |
| `perceived_age` | Keep; from BodyType mapping, with source/evidence metadata |
| `pitch` | Keep; base gender + BodyType child rule only. No race/height/muscle shift |
| `pitch_range` | Remove from deterministic casting; null unless actual audio measurement |
| `timbre_primary` / `timbre_secondary` | Keep optional; derive only from size/weight creative preferences or audio evidence. Allow more than one descriptor; do not force a single race stereotype |
| `resonance` | Remove current race-specific mappings. Keep null unless actual recording or a very explicitly documented fictional voice property supports it |
| `articulation` | Do not derive from jaw/mouth indices. Text JEV may describe `diction_style`, but that belongs to Style, not anatomical articulation |
| `speaking_pace` | Do not infer from static game facts or transcripts. Leave null in Identity. Audio measurement could populate a separate acoustic profile; JEV text can classify rhetorical rhythm, not actual speed |
| `intonation` | Do not infer from physical game facts or ordinary text. Leave null unless audio evidence; JEV can infer rhetorical form but not actual F0 contours |
| `distinctive_traits` | Only explicit lore describing an audible/nonhuman trait or actual audio evidence. Otherwise null |

---

## 8. Clan/lore context for JEV Style

The official FFXIV localization team says character speech is considered in relation to background, archetype and personality, including word choice (fancy vs plain), contractions, truncations, verbal mannerisms, cultural words, hesitation, directness and long-windedness. Another localization-team account says tone of voice is matched to physique, social standing, culture and related context. This supports using lore as **context for interpreting dialogue**, not as a replacement for dialogue evidence.

Implement a separate, small `LORE_STYLE_CONTEXT` table, not an acoustic table. These values should be low-priority hints to JEV or to the analysis dossier:

```python
LORE_STYLE_CONTEXT = {
    ("Elezen", "Wildwood"): ["law/order associations", "reputation for haughtiness/argumentativeness"],
    ("Elezen", "Duskwight"): ["reclusive, avoids city-states"],
    ("Lalafell", "Plainsfolk"): ["relaxed, carefree demeanor"],
    ("Lalafell", "Dunesfolk"): ["commerce-oriented, action-oriented business culture"],
    ("Miqo'te", "Seekers of the Sun"): ["diurnal culture; no speech-style assertion"],
    ("Miqo'te", "Keepers of the Moon"): ["nocturnal/hunting culture; no speech-style assertion"],
    ("Roegadyn", "Sea Wolves"): ["maritime/sailing vocabulary only if present in the NPC's dialogue"],
    ("Roegadyn", "Hellsguard"): ["mercenary/bodyguard tradition; consider role-specific directness only if dialogue supports it"],
    ("Au Ra", "Raen"): ["tranquil/solitary cultural description"],
    ("Au Ra", "Xaela"): ["nomadic, close-knit tribal context; do not assume aggression"],
    ("Hrothgar", "Helions"): ["historical service to a queen; possible social-deference context only for relevant character"],
    ("Hrothgar", "The Lost"): ["social category after loss of queen; do not treat as biological clan"],
}
```

For Hyur Midlander/Highlander and Viera Rava/Veena, default to no clan-specific language-style prior unless the NPC's own background/dialogue provides evidence. For Miqo'te, day/night culture is not an accent. For Hrothgar, an official note that names resemble old Slavic names is a naming convention, **not** evidence of a Slavic spoken accent.

JEV should receive:

- a balanced, representative sample of the NPC's dialogue;
- dialogue count and source/scene coverage;
- known in-game identity/background facts as optional context;
- the low-priority lore context above;
- instructions to classify only repeated character-level written patterns.

If the dialogue evidence is strong and contradicts the lore prior, dialogue wins. If dialogue volume is insufficient, leave unsupported fields null rather than fabricating a style. Do not use lore to force a style label.

### JEV may classify (text-derived Style)

Keep or refine these existing fields:

- `register`: formal / neutral / conversational
- `vocabulary_complexity`: plain / ordinary / educated / specialized / poetic
- `sentence_complexity`: simple / mixed / complex
- `colloquialism`: low / moderate / high
- `diction_style`: precise / relaxed / plain / ornate (written-word choice, not phonetic articulation)
- `rhetorical_style`: measured / direct / flowing / fragmented / inquisitive / emphatic / etc.
- `contraction_frequency`
- repeated `speech_habits` / verbal tics when demonstrable

Add confidence/probability per field, sample count, representative excerpts, classifier/model version, and aggregation metadata. Aggregate across representative lines; avoid letting one emotional cutscene dominate the character profile.

### JEV must not classify from text alone

Do not ask a text-only JEV classifier to infer:

- pitch, pitch range, register in the vocal/acoustic sense;
- timbre, resonance, breathiness, rasp, hoarseness, vocal fry;
- actual speaking speed, loudness, pauses, voice quality;
- accent or dialect solely from race/homeland/name;
- BodyType, age facts, gender facts, race/clan facts.

Those are respectively hard game facts, creative identity casting, or audio-measured properties. Text punctuation can suggest rhetoric, not acoustic intonation or timing.

---

## 9. Scoring and provenance implementation requirements

1. Replace the current one-winner `_weighted_category()` for identity casting. It currently breaks ties alphabetically and returns `best_score / 2` as if it were confidence. That is not calibrated confidence.
2. Return multiple supported preferences where appropriate, e.g. `timbre_preferences=["rich", "dark"]`, not only one winner.
3. Rename `confidence` to `prior_score` / `support_score` for hand-authored creative rules. Only call it confidence after evaluation against a labelled reference-audio set.
4. Store every contribution:

```json
{
  "value": "full",
  "source": "creative_selection",
  "rule": "race_morphology_prior",
  "prior_score": 0.55,
  "facts": {"race": "Roegadyn", "model_gender": "male", "muscle_tone": 82},
  "rationale": "Official lore describes Roegadyn as massive and muscular; this is a perceived vocal-weight design choice, not measured acoustics.",
  "sources": ["https://eu.finalfantasyxiv.com/a_realm_reborn/sp/world/races/roegadyn/"],
  "rule_version": "2.0"
}
```

5. A score is a prompt preference strength, not probability that a Roegadyn really sounds a certain way.
6. No evidence => `None`, and renderer omits that adjective. Avoid arbitrary baseline `clear`, `warm`, or `rounded` labels merely to fill a field.
7. Do not use `if height:` or `if muscle:` because zero is a valid slider value; use `is not None`.
8. Make normalization forward-compatible: unknown race/clan/body enum values should be preserved as raw data and result in a validation warning/normalized null, not a fatal error, unless the source data is internally contradictory.
9. Validate finite numbers for `scale` (`math.isfinite`) and preserve raw values if the normalized value is invalid.
10. Remove `npc_id` from the function signature if unused, or use it in provenance/output.
11. Keep audio evidence routing separate from deterministic synthetic casting. Do not erase Identity just because a container reference exists.
12. Make the renderer tolerate partial/nullable fields. Current `render_identity()` indexes mandatory fields such as `presentation`, `pitch`, `timbre_primary`, `articulation`, `intonation`, and `speaking_pace`; it must be updated if these are intentionally nullable or moved to other layers.
13. `render_style()` should render available JEV fields rather than suppressing the entire Style clause because one field is missing. Keep a minimum-evidence threshold, but use the supported fields when some dimensions are unknown.
14. Maintain separate keys:
    - `identity_prompt_hash` (facts + casting rules + renderer version)
    - `style_profile_hash` (dialogue dossier + JEV model/config + style vocabulary)
    - `combined_instruct_hash` (both clauses + reference script + VoiceDesign model/version)
    - `reference_audio_sha256` (the pinned actual reference audio)

---

## 10. Test requirements

Add unit tests for at least these cases:

1. BodyType 1 => standard/adult-appearing, not exact chronological age.
2. BodyType 3 => elderly; no automatic downward pitch shift.
3. BodyType 4 => child/youthful; higher pitch prior; not `young_teen` as a fact.
4. Lalafell BodyType 1 remains adult-appearing; Lalafell race alone never sets child age/high pitch.
5. Elezen age/lifespan lore never changes perceived age.
6. Female Hyur and female Roegadyn get muscle from `BustOrTone1` and bust from `ExtraFeature2OrBust`.
7. Male Au Ra gets muscle from `BustOrTone1`; female Au Ra gets bust from `BustOrTone1`.
8. Other race/sex combinations do not get muscle unless an explicit mapping is added.
9. Highlander gets a small morphology modifier over Midlander; no separate pitch rule.
10. Roegadyn gets the strongest size/weight prior, but no forced `low`, `chest_resonant`, `booming`, or `raspy` output.
11. Viera Rava and Veena have no automatic acoustic difference.
12. Miqo'te Seeker/Keeper have no automatic acoustic difference.
13. Hrothgar Helion/Lost are preserved as social categories and do not change morphology priors.
14. Unknown race/body-type values do not crash; normalized field becomes null and raw value is retained.
15. `height=0` and `muscle_tone=0` are valid values, not missing.
16. No height-to-pitch rule.
17. No race-to-pitch-range rule.
18. JEV can change Style without changing Identity hash.
19. Changing identity casting rules changes Identity hash and reference seed/hash.
20. Changing the actual reference WAV changes the audio hash even if the seed is unchanged.
21. Presence of an unverified SCD/container reference does not set `has_recorded_voice=True`.
22. A voiced NPC's original recording selection is handled by voice-evidence routing, not by deleting its game facts.

---

## 11. Research basis and sources

### Game extraction / technical mapping

- GarlandTools NPC extractor (`ENpcBase.Gender`, BodyType mapping, appearance field handling): https://raw.githubusercontent.com/ufx/GarlandTools/master/Garland.Data/Modules/NPCs.cs
- Project casting implementation: https://github.com/cutieway/oh-my-voice/blob/main/tools/blueprint_pipeline/casting.py
- Project voice vocabulary and evidence policy: https://github.com/cutieway/oh-my-voice/blob/main/blueprints/vocabulary.json
- Project blueprint renderer: https://github.com/cutieway/oh-my-voice/blob/main/tools/blueprint.py

### Official FFXIV race and clan lore

- Hyur / Midlander / Highlander: https://na.finalfantasyxiv.com/a_realm_reborn/world/races
- Elezen / Wildwood / Duskwight: https://na.finalfantasyxiv.com/a_realm_reborn/world/races
- Lalafell / Plainsfolk / Dunesfolk: https://na.finalfantasyxiv.com/a_realm_reborn/world/races
- Miqo'te / Seekers / Keepers: https://na.finalfantasyxiv.com/a_realm_reborn/world/races
- Roegadyn / Sea Wolves / Hellsguard: https://eu.finalfantasyxiv.com/a_realm_reborn/sp/world/races/roegadyn/
- Au Ra / Raen / Xaela: https://eu.finalfantasyxiv.com/heavensward/sp/story/newrace/
- Viera / Hrothgar: https://na.finalfantasy.com/en/titles/finalfantasy14/
- Female Hrothgar: https://na.finalfantasyxiv.com/dawntrail/system/
- Hrothgar Helion/Lost distinction (Square Enix community representative): https://forum.square-enix.com/ffxiv/threads/398566-Hrothgar-Naming-Conventions

### Official FFXIV localization guidance (useful for Style/JEV)

- FFXIV Localization Team Interview, Part 2 (2021): https://eu.finalfantasy.com/topics/266
  - The English lore/translation representative explains that they examine where a character is from and their archetype, then consider how a person of similar background/personality would speak: word choice, contractions, truncation, verbal mannerisms/cultural terms, hesitation, directness and long-windedness.
  - The German translator says they match the character's tone of voice to physique, social standing, culture and related context to develop a fitting speech pattern.
  - The same interview describes tribe-specific verbal patterns for Sahagin, Namazu and Moogles. These are language/style cues, not acoustic resonance.

### Human voice science (for conservative morphology priors)

- Pisanski et al., “Vocal indicators of body size in men and women: a meta-analysis” (39 samples, five continents): https://www.sciencedirect.com/science/article/pii/S0003347214002632
  - Formant-based vocal-tract-length estimates predict human body size better than F0; F0 explained less than 2% of height variance within sex. Supports only a weak perceived-size/formant-color prior, not a strong size-to-pitch mapping.
- Rojas et al., “How Does Our Voice Change as We Age?” systematic review/meta-analysis: https://pubmed.ncbi.nlm.nih.gov/32083980/
  - Older voices are more often perceived as rougher, breathier, strained and unstable, while F0 findings vary. Do not force elderly = low pitch.
- Pisanski et al., “Volitional exaggeration of body size through fundamental and formant frequency modulation in humans”: https://pmc.ncbi.nlm.nih.gov/articles/PMC5043380/
  - People can intentionally lower F0 and formants to sound larger; this is about perceived vocal-size signalling, not proof that a character's build determines their natural pitch.

---

## Final acceptance criterion

For any unvoiced NPC, the system should produce:

1. A reproducible, deterministic Identity prompt from verified game facts plus versioned, explicitly labelled creative morphology priors.
2. An independent, confidence-bearing, character-level Style prompt from JEV's analysis of representative dialogue, optionally informed—but never dictated—by low-strength official lore context.
3. A canonical VoiceDesign reference generated from the concatenated Identity + Style instruction, saved with its exact seed, prompt, shared transcript, model/version and audio hash.
4. A reusable Base ICL voice clone prompt created from the pinned reference WAV and exact transcript.

The system must never claim that a lore-inspired creative voice choice is an original in-game acoustic fact.
