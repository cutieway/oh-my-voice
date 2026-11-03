# Identity and style inventory

Status: field audit for schema 1.2, generated 2026-11-03.

> **Field review in progress (2026-11-03).** Casting records game facts first:
> `presentation`, `race`, `clan`, `non_playable_race`, `perceived_age`, then the
> acoustic fields. Unvoiced NPCs get a deterministic gender/child pitch plus
> weak morphology priors for vocal size/weight and optional timbre preferences.
> Voiced NPCs get null acoustics and must use verified audio evidence. The
> vocabulary sections describe allowed values and current rules.

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

`identity` has `additionalProperties: false` and 15 allowed fields, including
the legacy optional `accent`; no field is required. Casting emits 14 fields and
leaves optional descriptors null when no defensible evidence exists. Every
non-null identity field must have an `evidence` record and an
`identity_provenance` record.

Evidence levels are `explicit`, `supported_inference`, `creative_selection`,
`default`, and `unknown`; the first two require a source and quote.

Rendered order is fixed by `render.identity_v2`:

```text
{article} {head} voice with a {pitch} pitch and {article} {size_weight} quality.
It has {timbre descriptors}. Speech has {articulation}, {intonation}, {pace}.
{accent}{traits}Neutral conversational delivery for game dialogue.
```

Null optional fields are omitted. A missing Style dimension never suppresses
the other Style dimensions.

## 3. Field-by-field

### 3.1 Presentation

| Property | Value |
|---|---|
| Required | yes in the old schema; null for voiced or unknown sex |
| Values | `masculine`, `feminine`; null when sex is unknown, missing, or voiced |
| Render | current vocabulary values |
| Producer | `sex_to_voice_presentation` for unvoiced NPCs |
| Basis | game sex is male or female only when a playable-race model carries it; unknown, missing, non-playable, and zero-default rows are null. Any other value warns and normalizes to null while the raw byte stays in `facts` |

`presentation` is the derived voice presentation, not the raw sex fact. The raw
`sex` value remains in the decision's `facts` when the row is a playable race;
non-playable races and beasts keep sex null. This is a statistical starting
point from game metadata, not acoustic evidence; audio evidence overrides it.

### 3.1a Race (fact field, added 2026-11-03)

Casting records a playable race as-is: one of `Hyur`, `Elezen`, `Lalafell`,
`Miqo'te`, `Roegadyn`, `Au Ra`, `Viera`, `Hrothgar`, or null. A non-playable
race from the exporter is moved to `non_playable_race`; an unknown name warns,
normalizes to null, and keeps the raw value in `facts.race_reported`.

Coverage in `data/xivdata/npc-index.json`: 30,989 NPCs, of which 5,389 (17.4%)
have no race. Those rows also have `body_type = 0` and `height = 0` and report
`sex = male` — the ENpcBase zero defaults, not trustworthy facts. Casting
drops `sex` and `height` for `body_type = 0` rows and keeps the reported byte
only in `facts.sex_reported`. Non-playable races also keep sex null even when
their byte is 0/1.

### 3.1b Clan (fact field, added 2026-11-03)

`ENpcBase.Tribe` is recorded as-is when it belongs to the recorded race. A clan
without a race, or a clan from another race, warns and normalizes to null while
the raw value stays in `facts.clan_reported`.

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
| Values | `child`, `youthful`, `young_teen`, `older_teen`, `young_adult`, `adult`, `middle_aged`, `mature`, `elderly`, `ageless`, `ancient` |
| Producer | `body_type_elderly` / `body_type_childlike` / `body_type_standard_adult_convention` |
| Basis | `BodyType` 3 → `elderly` (game fact), 4 → `youthful` (game fact, shared child/young-teen model), 1 → `adult` only as a labelled project convention; otherwise null |

The exporter names BodyType 3 `Elderly` and 4 `Child`; 4 is a small model shared
by children and some young teens, so the prompt uses `youthful` rather than
asserting a precise age. Lalafell race never sets age or child pitch.

### 3.3 Pitch

| Property | Value |
|---|---|
| Required | no |
| Values | `very_low`, `low`, `low_mid`, `mid`, `mid_high`, `high`, `very_high` |
| Producer | `gender_base_pitch` for unvoiced NPCs; `body_type_child_pitch` for BodyType 4; `audio_evidence` when verified |
| Basis | male `low_mid`, female `mid_high`; BodyType 4 male `mid_high`, female `high` |
| Excluded | race, clan, height, muscle tone, scale and customization sliders never shift pitch |

### 3.4 Pitch range

| Property | Value |
|---|---|
| Required | no |
| Values | `narrow_range`, `wide_range` |
| Producer | `audio_evidence` only |
| Basis | null for every unvoiced NPC; requires an actual audio measurement |

### 3.5 Perceived vocal size and vocal weight

| Property | Value |
|---|---|
| Required | no |
| Values | size `compact`, `average`, `large`; weight `light`, `balanced`, `full` |
| Producer | `race_morphology_prior` |
| Basis | weak documented race/clan/gender morphology priors, plus a weak muscle-tone weight cue where `BustOrTone1` is confirmed muscle |
| Provenance | `prior_score`, `casting_strength`, input `facts`, `factors`, `rationale`, `sources`, `rule_version` |

These are creative casting scores, not probabilities or measured acoustics.
Timbre preferences are optional and derive only from these two axes; resonance
placement is never forced from race.

### 3.6 Appearance facts

`face`, `jaw`, `mouth`, `nose`, `eye_shape`, `eyebrows`, `hair_style`,
`skin_color`, `bust_or_tone_1`, `extra_feature_2_or_bust` and `scale` are
appearance facts only. `BustOrTone1` is muscle for Hyur/Roegadyn and male Au Ra;
it is bust for female models, with `ExtraFeature2OrBust` used for Hyur/Roegadyn
female bust. No customization index is treated as vocal anatomy.

## 6. Hard-split audit

### Facts and creative priors are separate

Casting emits game facts directly and marks every inferred field as a creative
choice with `prior_score` / `casting_strength`. A field with no approved factor
is null.

| Identity field | Source | Current value |
|---|---|---|
| presentation | `ENpcBase.Gender` → masculine/feminine | deterministic convention, null when gender is not a valid playable-race fact |
| race | `ENpcBase.Race` | one of 8 playable races, null with warning |
| clan | `ENpcBase.Tribe` | race-specific clan, null with warning |
| non_playable_race | `ENpcBase.Race` known non-playable list | value, null |
| perceived_age | `ENpcBase.BodyType` | adult convention / elderly / youthful, null |
| perceived_vocal_size | morphology priors | compact / average / large, null |
| vocal_weight | morphology + valid muscle cue | light / balanced / full, null |
| pitch | gender baseline + BodyType 4 | value, null |
| pitch_range | audio only | null unless measured |
| timbre_primary, timbre_secondary | size/weight preference | value, null |
| timbre_preferences | size/weight preference | up to two values, null |
| resonance | audio only | null unless measured |
| articulation | audio only | null unless measured |
| speaking_pace | audio only | null unless measured |
| intonation | audio only | null unless measured |
| distinctive_traits | explicit approved evidence | null |

`BustOrTone1` is muscle for Hyur and Roegadyn (either gender) and for male Au
Ra; it is bust for female Au Ra and for other female models. Hyur/Roegadyn
female bust uses `ExtraFeature2OrBust`. No muscle cue affects pitch or
resonance. Height uses the 0–100 slider and ignores the 255 export fallback; it
is preserved but not used. Scale is recorded but not used.

The three possible policies remain:

| Policy | Effect |
|---|---|
| Facts + creative priors | Current casting: facts plus transparent, versioned creative priors for unvoiced NPCs |
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
  body_type ∈ {1, 3, 4, 9, None}, height ∈ {0, 25, 50, 100, 255}, unknown
  race/clan names, and recorded voice ∈ {unverified, verified}. Facts are
  copied, unavailable fields are null, unknown/mismatched values warn while
  retaining the raw fact, and only verified audio evidence skips appearance
  inference.
- Exporter smoke over `ENpcBase`: 30,989 named rows with race, clan, sex,
  body type, height, scale, `BustOrTone1`, `ExtraFeature2OrBust`, and appearance
  indices. See `docs/game-voice-factors-audit.md`.
- Confirmed schema constraints by reading `validate_profile()`: closed
  vocabularies, optional/null identity fields, accent pair, timbre family
  conflict, trait family conflict, trait count, and evidence presence.
