# Identity and style inventory

Status: field audit for schema 1.2, generated 2026-11-03.

> **Field review in progress (2026-11-03).** Casting records game facts first:
> `presentation`, `race`, `clan`, `non_playable_race`, `perceived_age`, then the
> acoustic fields. Unvoiced NPCs get weighted inference for pitch, range,
> timbre, and resonance. Voiced NPCs get null acoustics and must use audio
> evidence. The vocabulary sections describe allowed values and current rules.

Sources of truth: `blueprints/vocabulary.json`, `blueprints/schema.json`,
`tools/blueprint_pipeline/casting.py`, `tools/blueprint_pipeline/generator.py`,
`tools/blueprint_pipeline/classifier.py`. No value below is inferred from
truncated output; the vocabularies were dumped programmatically and the emitted
sets were brute-forced through `cast_identity()` (see Verification).

## 1. The two layers

| Layer | Stored in | Read by | Deterministic | Truth status |
|---|---|---|---|---|
| Acoustic identity | `identity` | `render_identity()` | yes, same NPC always same values | casting decision, not a recording fact |
| Game metadata | `character`, dossier `identity` | casting rules only | yes | hard game facts |
| Speech style | `speech_style` | `render_style()` | no | probabilistic text inference |
| Reference audio | `reference_pin` | voicebank | seed yes, sampling no | produced target voice |
| Dialogue audio | pack | Base model | sampling only | clones the reference |

`identity` never enters `render_style()`, and `speech_style` never enters
`render_identity()`. The two text clauses are concatenated once, for the
VoiceDesign reference only; cloned lines never see either instruction.

## 2. Identity schema

`identity` has `additionalProperties: false` and 12 allowed fields. Six are
required: `presentation`, `pitch`, `timbre_primary`, `articulation`,
`speaking_pace`, `intonation`.

Every present identity field must have an `evidence` record; accent included.
Evidence levels are `explicit`, `supported_inference`, `creative_selection`,
`default`, and `unknown`; the first two require a source and quote.

Rendered order is fixed by `render.identity_template`:

```text
{age} {presentation} voice with a {pitch} pitch, over a {pitch_range}.
It has a {timbre}{secondary}{resonance}. Speech is {articulation} with
{intonation} intonation, at a {pace} pace{accent}.{traits}
Neutral conversational delivery for game dialogue.
```

## 3. Field-by-field

### 3.1 Presentation

| Property | Value |
|---|---|
| Required | yes in the old schema; null for voiced or unknown sex |
| Values | `masculine`, `feminine`; null when sex is unknown, missing, or voiced |
| Render | current vocabulary values |
| Producer | `sex_to_voice_presentation` for unvoiced NPCs |
| Basis | game sex is male or female only when a playable-race model carries it; unknown, missing, non-playable, and zero-default rows are null. Any other value raises |

`presentation` is the derived voice presentation, not the raw sex fact. The raw
`sex` value remains in the decision's `facts` when the row is a playable race;
non-playable races and beasts keep sex null. This is a statistical starting
point from game metadata, not acoustic evidence; audio evidence overrides it.

### 3.1a Race (fact field, added 2026-11-03)

Casting records a playable race as-is: one of `Hyur`, `Elezen`, `Lalafell`,
`Miqo'te`, `Roegadyn`, `Au Ra`, `Viera`, `Hrothgar`, or null. A non-playable
race from the exporter is moved to `non_playable_race`; an unknown name still
raises.

Coverage in `data/xivdata/npc-index.json`: 30,989 NPCs, of which 5,389 (17.4%)
have no race. Those rows also have `body_type = 0` and `height = 0` and report
`sex = male` — the ENpcBase zero defaults, not trustworthy facts. Casting
drops `sex` and `height` for `body_type = 0` rows and keeps the reported byte
only in `facts.sex_reported`. Non-playable races also keep sex null even when
their byte is 0/1.

### 3.1b Clan (fact field, added 2026-11-03)

`ENpcBase.Tribe` is recorded as-is and must belong to the recorded race. A clan
without a race, or a clan from another race, raises.

| Race | Clans |
|---|---|
| Hyur | Midlander, Highlander |
| Elezen | Wildwood, Duskwight |
| Lalafell | Plainsfolk, Dunesfolk |
| Miqo'te | Seeker of the Sun, Keeper of the Moon |
| Roegadyn | Sea Wolf, Hellsguard |
| Au Ra | Raen, Xaela |
| Viera | Rava, Veena |
| Hrothgar | Helions, The Lost |

All 25,600 race-bearing NPC records have a matching clan. The 5,389 race-less
records have no clan either. Missing clan is null, not “unknown”.

### 3.1c Non-playable race (fact field, added 2026-11-03)

`NON_PLAYABLE_RACES` lists 47 entries from the wiki: Amalj'aa, Sylphs, Kobolds,
Sahagin, Ixal, Goblin, Qiqirn, Moogle, Vanu Vanu, Gnath, Ananta, Kojin, Namazu,
Lupin, Tengu, Garleans, Bangaa, Seeq, Matanga, Arkasodara, Zun, Mord, Ondo,
Nu Mou, Pixies, Qitari, Dwarves, Loporrits, Omicrons, Dragons, Ea, Grebuloff,
Karellians, Nibirun, Gordhonites, Class Twenty-fours, Mandervillians, PuPu's
race (unnamed), Nagylvi, Mamool Ja, Pelupelu, Moblins, Yok Huy, Hanuhanu, Köttr,
Fylgjur, Felynes.

It is a separate field, not a fallback for an unknown playable race. Casting
moves a known non-playable race from `race` into this field, so the exporter's
`race_name` works without a second lookup. The list is not exhaustive of
race-less NPCs: creatures, primals, constructs, and unnamed entities fall
outside both lists and keep both fields null.

### 3.2 Perceived age

| Property | Value |
|---|---|
| Required | no; null allowed and rendered by omission |
| Values and rendered bands | `child` 5–12, `young_teen` 13–15 (“young teenage”), `older_teen` 16–19 (“teenage”), `young_adult` 20–29, `adult` 30–39, `middle_aged` 40–54 (“middle-aged”), `mature` 55–69, `elderly` 70+, `ageless`, `ancient` (“ancient and timeless”) |
| Producer | `body_type_age`, otherwise null |
| Basis | game `BodyType`: 3 → mature, 4 → young_teen. Every other BodyType → null |

Casting emits mature, young_teen, or null. Measured against 12 NPCs with
documented ages, BodyType alone matched 2/11, and `BodyType 1 → adult` was wrong
for Merlwyb, Momodi, Hildibrand, Godbert, and Kan-E-Senna. `height` is exported
but not used for age.

### 3.3 Pitch

| Property | Value |
|---|---|
| Required | yes in the old schema; casting may return null |
| Values | `very_low`, `low`, `low_mid` (“low-mid”), `mid` (“middle”), `mid_high` (“mid-high”), `high`, `very_high` |
| Producer | `weighted_inference` for unvoiced NPCs; null for voiced NPCs or when no factor exists |
| Basis | Weighted vote over sex, perceived age, race, and height |
| Excluded | Muscle tone and scale do not affect pitch |

Sex contributes ±1 step, perceived age ±0.6–1.0, race −0.6–+0.6, and height
±0.3 at low weight. The result is rounded to the nearest of seven values. The
decision records every factor and its weight; there is no single
`sex_race_age_pitch_ladder` rule.

If the NPC has recorded voice lines, pitch is null with rule
`voiced_use_audio_evidence`. Appearance must not overrule the actor.

### 3.4 Pitch range

Casting uses a weighted vote over perceived age and race for unvoiced NPCs, and
returns null for voiced NPCs or when neither factor exists.

| Property | Value |
|---|---|
| Required | no |
| Values | `narrow_range` (“narrow pitch range”), `wide_range` (“wide pitch range”) |
| Producer | `age_race_range` |
| Basis | age if known: child/young_teen wide, mature/elderly narrow. Otherwise race: Roegadyn/Hrothgar/Elezen narrow, Lalafell/Miqo'te wide, all others a stable hash pick |

Casting emits both values.

### 3.5 Timbre primary

| Property | Value |
|---|---|
| Required | yes |
| Values | `clear`, `warm`, `bright`, `dark`, `rich`, `mellow`, `nasal`, `hollow`, `metallic`, `breathy`, `husky`, `gravelly`, `raspy`, `hoarse`, `smooth`, `silvery`, `reedy`, `booming`, `whispery`, `synthetic` |
| Render | literal |
| Producer | `race_timbre_family` |
| Basis | race selects a family; `sha256(canonical_id + ":timbre")` selects one member |

Race families:

| Race | Family |
|---|---|
| Hyur | clear, warm, bright |
| Elezen | silvery, bright, clear |
| Lalafell | bright, clear, mellow |
| Miqo'te | warm, bright, husky |
| Roegadyn | rich, dark, gravelly |
| Au Ra | smooth, dark, clear |
| Hrothgar | gravelly, rich, dark |
| Viera | bright, clear, warm |
| unknown / no metadata | clear, warm, mellow |

Casting emits clear, warm, bright, silvery, mellow, husky, rich, dark, gravelly,
and smooth: 10 of 20.

### 3.6 Timbre secondary

| Property | Value |
|---|---|
| Required | no |
| Values | same 20 values as `timbre_primary` |
| Cardinality | at most one |
| Constraint | must differ from the primary and from its family |
| Render | “with a hint of {value}” |
| Producer | none; casting never sets it |

### 3.7 Resonance

| Property | Value |
|---|---|
| Required | no |
| Values | `chest_resonant` (“chest resonance”), `forward_resonant` (“forward resonance”), `head_resonant` (“head resonance”), `rounded` (“rounded resonance”), `compact` (“compact resonance”), `open` (“open resonance”), `hollow` (“hollow resonance”), `nasal` (“nasal resonance”), `throaty` (“throaty resonance”), `deep_resonance` (“deep resonance”), `light_resonance` (“light resonance”) |
| Producer | `age_race_resonance` |
| Basis | age if known: child/young_teen light, mature/elderly chest. Otherwise race: Roegadyn/Hrothgar deep, Au Ra chest, Elezen/Miqo'te forward, Lalafell head, Viera/Hyur rounded. Unknown race → rounded |

Casting emits chest_resonant, forward_resonant, head_resonant, rounded,
deep_resonance, and light_resonance: 6 of 11.

### 3.8 Articulation

| Property | Value |
|---|---|
| Required | yes |
| Values | `crisp`, `precise`, `soft`, `clipped`, `relaxed`, `rounded`, `sharp`, `slurred`, `breathy`, `delicate`, `heavy`, `drawling`, `staccato`, `fluid` |
| Render | literal |
| Producer | `race_or_stable_articulation` |
| Basis | race: Elezen precise, Roegadyn/Hrothgar heavy, Lalafell delicate, Au Ra crisp. All others: stable hash pick among relaxed, rounded, fluid |

Casting emits crisp, precise, delicate, heavy, relaxed, rounded, and fluid:
7 of 14. Acting values such as slurred, drawling, staccato, and sharp require a
manual override.

### 3.9 Speaking pace

| Property | Value |
|---|---|
| Required | yes |
| Values | `very_slow`, `slow`, `measured`, `moderate`, `brisk`, `fast`, `very_fast` |
| Render | literal |
| Producer | `age_pace` |
| Basis | age if known: child/young_teen/older_teen brisk, mature/elderly slow. Otherwise stable hash pick among measured, moderate |

Casting emits slow, measured, moderate, and brisk: 4 of 7.

### 3.10 Intonation

| Property | Value |
|---|---|
| Required | yes |
| Values | `flat`, `restrained`, `natural`, `expressive`, `melodic`, `dramatic`, `rising`, `falling`, `lilting`, `monotone` |
| Render | literal |
| Producer | `race_or_stable_intonation` |
| Basis | race: Lalafell/Miqo'te expressive, Elezen restrained, Roegadyn falling. All others: stable hash pick among natural, restrained, expressive |

Casting emits natural, restrained, expressive, and falling: 4 of 10.

### 3.11 Accent

| Property | Value |
|---|---|
| Required | no |
| Shape | object with exactly `profile` and `strength`, or both omitted |
| Profiles | `general_british_english` (“General British”), `received_pronunciation` (“Received Pronunciation”), `northern_english` (“northern English”), `scottish`, `irish`, `welsh`, `standard_american_english` (“General American”), `canadian`, `australian`, `new_zealand`, `south_african`, `international_english` (“neutral international English”) |
| Strengths | `subtle`, `light`, `moderate`, `strong`, `pronounced` |
| Producer | project default |
| Current value | `{profile: general_british_english, strength: light}`, evidence level `default` |

Qwen3-TTS has no accent mode; this is a soft prompt hint and must be auditioned.
Casting never derives an accent from race or nation.

### 3.12 Distinctive traits

| Property | Value |
|---|---|
| Required | no |
| Cardinality | 0–2 |
| Values | `slight_vocal_fry`, `light_vibrato`, `slight_tremor`, `audible_breath_support`, `dry_vocal_texture`, `rounded_vowels`, `elongated_vowels`, `tight_vocal_placement`, `airy_upper_register` |
| Render | “Notable vocal traits: …”; literal names |
| Producer | none; casting leaves the list empty |
| Constraints | no duplicates; no two traits in the same family; trait family may not match the primary timbre family |

## 4. Game facts outside `identity`

These drive the deterministic rules but are never rendered:

| Location | Fields | Use |
|---|---|---|
| `character` | `id`, `name`, `npc_id`, `race`, `clan`, `sex`, `source_url`, `applies_to` | identity gating, rules, audit |
| Dossier `identity` | `race_id`, `tribe_id`, `body_type`, `height` | casting inputs; `height` unused |
| Upstream gap | `inventory.py` backfills an unknown game sex from dialogue DB gender | byte 0 is ambiguous, so only gender 1 becomes female; 0 stays unknown. An index sex is used only when a playable race exists |
| Missing | occupation, title, nation | `character.role` stays null |

No race, clan, sex, or body word appears in the instruct.

## 5. Speech style

Style is a closed-label interpretation of the NPC's dialogue. Five fields are
asked and stored:

| Field | Values |
|---|---|
| `register` | `formal`, `neutral`, `conversational` |
| `vocabulary_complexity` | `plain`, `ordinary`, `educated`, `specialized`, `poetic` |
| `sentence_complexity` | `simple`, `mixed`, `complex` |
| `colloquialism` | `low`, `moderate`, `high` |
| `rhetorical_style` | `direct`, `measured`, `flowing`, `emphatic`, `fragmented`, `inquisitive` |

Not asked and rejected in schema 1.2: `diction_style`,
`contraction_frequency`, `speech_habits`.

### Current decision path

1. Fewer than 40 unique dialogue lines: every field becomes null with
   `method: insufficient_dialogue_volume`; the profile renders identity only.
2. Otherwise the classifier answers each field with a full distribution.
3. A choice is accepted only at `confidence ≥ 0.55` and selected probability
   `≥ 0.35`.
4. An accepted choice is stored with `method: <provider>_choice`.
5. A rejected choice falls back to the deterministic dialogue heuristic, stored
   with `method: deterministic_dialogue_analysis` and its low confidence.
6. `speech_style_analysis` records value, confidence, probabilities, method,
   provider, model id, dossier hash, and classification id per field.

The `rules` provider still exists and produces deterministic style answers for
offline runs. It is not the default, but it is a second profile source.

### JEV's non-style answers

JEV also answers three gates that do not write identity or style fields:
`identity_resolved` (noul), `voice_evidence_present` (noul), and
`individual_design` (score). They feed review status and archetype choice.

## 6. Hard-split audit

### Facts and weighted inference are separate

Casting emits game facts directly and marks every inferred field as a weighted
creative choice. A field with no approved factor is null.

| Identity field | Source | Current value |
|---|---|---|
| presentation | ENpcResident.Sex → masculine/feminine | weighted mapping, null for voiced |
| race | ENpcBase.Race | one of 8 playable races, null |
| clan | ENpcBase.Tribe | race-specific clan, null |
| non_playable_race | none wired yet | null |
| perceived_age | ENpcBase.BodyType | mature, young_teen, null |
| pitch | weighted sex/age/race/height | value or null |
| pitch_range | weighted age/race | value or null |
| timbre_primary | weighted race/muscle/height/age | value or null |
| timbre_secondary | none | null |
| resonance | weighted race/muscle/height/age | value or null |
| articulation | jaw/mouth are customization indices | null |
| speaking_pace | style/personality, not game data | null |
| intonation | style/personality, not game data | null |
| accent | no homeland/language data | null |
| distinctive_traits | none | null |

Muscle tone is used only for male Hyur, Roegadyn, and Au Ra, where
`BustOrTone1` is confirmed to track physique. On female models it is bust size
and is never treated as muscle. Height uses the 0–100 slider and ignores the
255 export fallback; it is only used when a playable race exists. Scale is
recorded but not used.

The three possible policies remain:

| Policy | Effect |
|---|---|
| Facts + weighted inference | Current casting: facts plus transparent creative inference for unvoiced NPCs |
| Audio-first | Voiced NPCs get null acoustics and are filled from SCD measurements |
| Strict fact-only | Null every acoustic field until measured |

### Style is not JEV-only today

Two paths currently write profile style without an accepted JEV answer: the
deterministic heuristic fallback, and the `rules` provider. To make style
“JEV choice or null”:

1. Remove the heuristic fallback; a rejected answer becomes null with the reason
   and confidence recorded.
2. Treat `RulesProvider` as benchmark-only, not a profile source.
3. Decide whether the three non-style JEV gates remain.

## 7. Verification

- Identity vocabularies dumped field by field from `vocabulary.json`; counts:
  presentation 6, age 10, pitch 7, range 2, timbre 20, secondary 20 shared,
  resonance 11, articulation 14, pace 7, intonation 10, accents 12 × 5
  strengths, traits 9.
- Casting smoke over sex ∈ {male, female, unknown, missing}, race ∈ {8 playable
  races, None}, clan ∈ {both clans per race, mismatched, missing, None},
  body_type ∈ {1, 3, 4, None}, height ∈ {0, 25, 50, 100, 255}, and recorded
  voice ∈ {true, false}. Facts are copied, unavailable fields are null,
  mismatched clan raises, and voiced identities skip appearance inference.
- Exporter smoke over `ENpcBase`: 30,989 named rows with race, clan, sex,
  body type, height, scale, `BustOrTone1`, `ExtraFeature2OrBust`, and appearance
  indices. See `docs/game-voice-factors-audit.md`.
- Confirmed schema constraints by reading `validate_profile()`: closed
  vocabularies, required fields, accent pair, timbre family conflict, trait
  family conflict, trait count, evidence presence. The required-field defaults
  still need removal so nulls are omitted instead of filled.
