# Style taxonomy v3 migration

The profile envelope stays `schema_version: "1.2"`; the Style construct changes
are versioned separately as `style-taxonomy-3.0` and travel in
`speech_style_analysis.taxonomy_version` and every pinned reference. Benchmark
v2 under `data/blueprints/evaluation/benchmark_v2/` is frozen and must not be
rebuilt with v3 labels.

## Field renames

| v2 field | v3 field | Migration |
|---|---|---|
| `speech_style.vocabulary_complexity` | `speech_style.lexical_complexity` | Rename. Map `educated` -> `advanced`, keep `plain` / `ordinary` / `advanced` / `mixed`. Drop `specialized` and `poetic` from this field. |
| `speech_style.rhetorical_style` | `speech_style.discourse_style` | Rename. Map `measured` -> `explanatory`, `flowing` -> `explanatory`, `emphatic` -> `exhortative`, `fragmented` -> `mixed` unless a human confirms a single discourse move; keep `direct` / `inquisitive` / `mixed`. |

Historical v2 labels in `docs/jev-eval-16npc-2026-11-02.*` and
`docs/style-ab-2026-11-02.md` are records of that benchmark and are not
rewritten.

## Construct changes

- `register` measures formality only. Contractions and slang belong to
  `colloquialism`; do not lower `register` because of them.
- `lexical_complexity` measures word difficulty only. Technical/domain-specific,
  poetic/figurative, archaic, dialectal, and formulaic choices move to the
  audit-only `lexical_features` evidence, not to the ordinal scale.
- `sentence_complexity` measures clause structure only, not character count or
  spoken speed.
- `colloquialism` measures informal grammar, slang, reductions, and casual
  markers only. A blunt direct line is not colloquial unless it uses informal
  wording.
- `discourse_style` replaces the overlapping rhetoric candidates:
  `direct`, `explanatory`, `inquisitive`, `persuasive`, `exhortative`,
  `reflective`, `narrative`, `mixed`. It remains experimental until the human
  pilot clears its agreement gate.

## Audit-only evidence

`speech_style_analysis.experimental.lexical_features` and
`.experimental.verbal_habits` collect technical/poetic/archaic/dialectal/
formulaic observations and candidate mannerisms. They are never written to
`profile.speech_style`, never rendered into the VoiceDesign instruction, and do
not affect evidence tiers.
