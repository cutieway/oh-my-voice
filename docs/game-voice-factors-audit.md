# Voice-factor audit: what the current game data actually provides

Status: audited 2026-11-03 against the local Endwalker-era `sqpack` export.
Source: `tools/xivdata npc-index` over `ENpcResident`, `ENpcBase`, `Race`,
`Tribe`, `EmjVoiceNpc`.

This audit checks the proposed voice-factor model against fields that exist in
the current game export. Fields that are absent or ambiguous are not used as
facts, and the casting layer leaves them null.

## Coverage

| Measure | Count |
|---|---:|
| `ENpcBase` rows | 59,851 |
| Named `ENpcResident` rows exported | 30,989 |
| Has a playable race | 25,600 |
| Has no playable race | 5,389 |
| Has a race-matching clan | 25,600 |
| `BodyType = 1` (ordinary) | 22,464 |
| `BodyType = 3` (aged) | 433 |
| `BodyType = 4` (small/child model) | 2,703 |
| `BodyType = 0` (no model data) | 5,389 |
| Height `25-100` | 21,031 |
| Height `0` (bottom slider, also zero-default rows) | 8,449 |
| Height `255` (export fallback) | 1,509 |
| Scale `= 1.0` | 27,812 |
| Scale range | 0.1–8.0 |
| Speakers with SCD audio | 667 |
| NPCs linked to SCD audio | 300 |

The 5,389 race-less rows also have `body_type = 0`, `height = 0`, `scale = 1`,
and all zero appearance fields. They report `sex = male`, but that is the
ENpcBase zero default, not a trustworthy sex fact. Examples: delivery moogle,
Ultros, Menphina, Omega, golem, cactuar.

## Fields that exist and are usable

| Game field | Values | Coverage | Use |
|---|---|---:|---|
| `Gender` | male, female, unknown | 30,989 | Sex fact; unknown becomes null |
| `Race` | 8 playable races | 25,600 | Race fact; no derivation |
| `Tribe` | 2 clans per race | 25,600 | Clan fact; must match race |
| `BodyType` | 0, 1, 3, 4 | 30,989 | Only 3 and 4 bound perceived age |
| `Height` | 0, 25, 50, 75, 100, 255 | 30,989 | 0–100 is the slider; 255 is the export fallback |
| `Scale` | 0.1–8.0 | 30,989 | Model scale, not a humanoid height measurement |
| `BustOrTone1` | 0, 25, 50, 75, 100 | 30,989 | Bust on female models; muscle on male models of some races |
| `ExtraFeature2OrBust` | 0, 25, 50, 75, 100 | 30,989 | Model-dependent; not interpreted |
| `Jaw` | 0–3 | 30,989 | Customization index, not vocal-tract anatomy |
| `Mouth` | 0–2, 128–131 | 30,989 | Customization index, not articulation |
| `Face` | 0–48, 100+ | 30,989 | Customization index |
| `Nose`, `EyeShape`, `Eyebrows` | small indices | 30,989 | Appearance only |
| `HairStyle`, `SkinColor` | indices | 30,989 | Appearance only |
| `ModelChara`, `ModelBody` | model IDs | 30,989 | Not interpreted as physique |
| `NotRewriteHeight` | bool | 30,989 | Export flag, not a voice fact |

## Muscle tone: usable, but only on specific models

`BustOrTone1` is a dual-purpose field. Known examples from the export:

| NPC | Model | Value | Interpretation |
|---|---|---:|---|
| Raubahn (Hyur Highlander male) | male | 100 | muscular |
| Gerolt (Hyur Highlander male) | male | 100 | muscular |
| Hildibrand (Hyur Highlander male) | male | 25 | slim |
| Magnai (Au Ra Xaela male) | male | 100 | muscular |
| Sadu (Au Ra Xaela female) | female | 0 | bust value, not muscle |
| Cirina (Au Ra Xaela female) | female | 100 | bust value, not muscle |
| Merlwyb (Roegadyn Sea Wolf female) | female | 50 | bust value, not muscle |

Therefore `muscle_tone` is derived only when:

- sex is male, and
- race is `Hyur`, `Roegadyn`, or `Au Ra`.

For other male models the same byte may represent another customization
parameter. For all female models it is bust size and is never treated as
muscle. The raw `bust_or_tone_1` and `extra_feature_2_or_bust` values remain in
the facts for audit.

## Fields that do not exist as usable voice factors

| Proposed factor | Current data | Decision |
|---|---|---|
| Numeric age | no field | `BodyType` 3/4 only; null otherwise |
| Homeland / region | no NPC homeland field | null |
| Native language | no NPC language field | null |
| Occupation | not in `ENpcBase` | null in casting; dialogue/style may infer separately |
| Personality | not in `ENpcBase` | null in casting |
| Overall body proportions | only model IDs / scale | not interpreted |
| Jaw / face / mouth anatomy | customization indices | not used for articulation |
| General NPC voice ID | `EmjVoiceNpc` has only 15 emote rows | actual SCD audio is the voice evidence |
| Recorded voice sample | 667 speakers / 300 NPCs have SCD | audio evidence overrides casting |

`EmjVoiceNpc` is not a general NPC voice table. It contains 15 named emote-voice
entries (Alphinaud, Alisaie, Thancred, Urianger, …). The useful voice evidence is
the SCD/WAV corpus in `data/game-voices/` and the speaker `has_scd` flag.

## Mapping the proposed output fields to the current schema

The proposal names more voice-identity fields than the current vocabulary has.
They map onto existing fields without adding dead keys:

| Proposed field | Current field | Status |
|---|---|---|
| Gender / voice presentation | `presentation` | masculine/feminine for unvoiced; null for voiced |
| Perceived age | `perceived_age` | BodyType 3/4 only |
| Pitch | `pitch` | weighted inference |
| Vocal register | `pitch` | the register band is the pitch value |
| Timbre | `timbre_primary` | weighted inference |
| Vocal texture | `timbre_primary` | husky/gravelly/smooth/etc. |
| Resonance | `resonance` | weighted inference |
| Vocal fullness | `resonance` | deep/chest/light bands |
| Accent | `accent` | null: no homeland/language data |
| Articulation | `articulation` | null: jaw/mouth are indices, not anatomy |
| Voice personality | `intonation` / `speaking_pace` | null: style/personality, not game identity |

Adding separate `vocal_register`, `vocal_texture`, `vocal_fullness`, and
`voice_personality` fields would duplicate these values and require a second
vocabulary and renderer path. Revisit only if the TTS model needs a distinct
control for one of them.

## Casting policy derived from the audit

1. **Facts are copied, never inferred**: sex, race, clan, non-playable race,
   body type, normalized height, scale, raw appearance fields. Sex is only a
   fact for a playable-race model; non-playable races, beasts, and monsters may
   have no gender, and their default byte is null.
2. **Voiced NPCs skip appearance inference entirely.** If the identity has a
   recorded voice reference or audio evidence, every voice-identity field is
   null with rule `voiced_use_audio_evidence`. The audio pipeline fills them.
3. **Unvoiced NPCs use weighted inference**, not single hard rules:
   - presentation: playable-race sex → masculine/feminine, with a low confidence;
   - perceived age: only `BodyType` 3 → mature, 4 → young_teen;
   - pitch: weighted vote over sex, perceived age, race, height;
   - pitch range: weighted vote over age and race;
   - timbre: weighted vote over race, muscle tone, height, perceived age;
   - resonance: weighted vote over race, muscle tone, height, perceived age.
4. **Muscle tone never affects pitch.** It contributes to timbre and resonance
   only, matching the perceptual evidence in the proposal.
5. **No articulation, pace, intonation, accent, or personality inference**:
   those need audio or dialogue/style evidence, not physical metadata.
6. **Null means omit.** A missing field is not replaced by an adult, mid, or
   neutral default in casting.

## Remaining risks

- `BodyType 4` is used by children and young teenagers. Mapping it to
  `young_teen` is conservative but still a model-level guess.
- `BodyType 3` is the aged model; `mature` is chosen over `elderly` because the
  model does not establish a numeric age.
- Height `50` is both the average slider value and the default for many NPCs.
  A height of 50 is therefore weak evidence and carries low weight.
- Height `255` is the export fallback, not a tall character. Zero-default rows
  also carry height `0`; casting only uses height when a playable race exists.
- Scale is a model transform (0.1–8.0), not a body-size measurement; it is
  recorded but not used in the weighted inference.
- `BustOrTone1` semantics are model-dependent. The race/sex gate is deliberately
  conservative; expanding it requires a model-level source or audition data.
- The 5,389 zero-default rows must not be read as male adults. Their sex, race,
  age, and appearance fields are absent.
- `has_se_voice_reference` is the current dossier flag; the SCD speaker table is
  the fuller source. Voiced coverage should be re-derived from `speakers.has_scd`
  when profiles are regenerated.

## Verification

```bash
dotnet build tools/xivdata/xivdata.csproj
dotnet run --project tools/xivdata/xivdata.csproj -- npc-index > /tmp/npc-index.json
python3 -m unittest tools.blueprint_pipeline.tests.test_pipeline
```

The export is deterministic for a fixed game build; the audit counts are from
the 2026-11-03 export. Re-run the export after a game patch before trusting the
counts.
