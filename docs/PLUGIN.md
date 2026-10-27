# Plugin internals

How the Dalamud plugin turns what a character just said into a pack lookup, a
clean miss log, or audio. This page covers the runtime only.

- Building, playtesting, and repo layout: [DEVELOPMENT.md](DEVELOPMENT.md)
- Making voice audio: [QWEN3-TTS.md](QWEN3-TTS.md)
- The dialogue database behind a pack: [../tools/xivdata/README.md](../tools/xivdata/README.md)
- Speaker embeddings: [../tools/xvector/README.md](../tools/xvector/README.md)

## Capture

Two providers, both read-only:

| Provider | How | Manifests as |
| --- | --- | --- |
| `Talk` | Polled every framework update; change-detected on speaker + sentence | The speaker text node, or `Narrator` when empty |
| `TalkSubtitle` | `PostDraw` on the cutscene subtitle addon | `Narrator` |

A line is dispatched once per visible change. The subtitle frame-guard resets
when the subtitle addon hides, so a later byte-identical line still dispatches;
the dispatcher's time-window dedupe removes true duplicates.

Phase 1 is EN only and targets ARR MSQ dialogue. No other capture providers are
wired in, and there is no dead feature flag for them.

## Line identity

`sentence` is the sanitized runtime text from `OhMyVoice.Common.Sanitizer`
(version 1). The lookup key is:

```text
id = md5(lang:voiceId:npcId:sentence)
```

`lang` is fixed to `en`. `voiceId`/`npcId` come from the loaded manifest's
`Npcs` entry keyed by the cleaned speaker name; an unknown speaker falls back to
`npcId = speaker` and `voiceId = Unknown`, which normally produces a silent
miss.

The pack manifest records a sanitizer pin (`v1:<sha256>`). On load, a pin
mismatch or a failed sanitizer self-check disables the pack with a warning
instead of silently missing every line.

## Dispatch pipeline

1. Ignore everything when the plugin is disabled.
2. Read the live player forename/surname and clean the speaker/sentence.
3. Resolve the NPC entry and compute the line ID.
4. Drop a repeat of the same ID inside `SubtitleDedupeSeconds` (default 1 s).
5. Cancel any older pending line, wait `VoWaitMs` (default 100 ms) for real VO.
6. If real VO was detected within the last second, stay silent.
7. Resolve the pack file (see below). No file → record a `miss`; file →
   record a `hit`, stop the previous line, and play.

Only one line is ever in flight. A newly dispatched line supersedes a pending
wait; a hit stops the previous line before playing.

## Real-VO suppression

`VoSuppression` hooks `SoundManager.PlaySound` and
`PlayCutsceneVoSound` and only **detects** real voice paths; it never mutes or
alters game audio. Lowercase paths matching `sound/voice/`, `vo_man`,
`vo_voiceman`, or `sound/voicem/voiceman` mark real VO as active.

That timestamp is used twice: the dispatcher waits briefly before starting a
pack line, and it stops an already-playing pack line when real VO begins
(`StopOnVo`). The wait and the stop are independent settings.

## Playback

- Opus Ogg is decoded in managed code with Concentus; output is a 48 kHz
  stereo float mix through NAudio `WaveOut` (winmm). No native audio DLL ships
  with the plugin.
- The whole line is decoded to memory before playback starts.
- Volume is clamped to 0.01–2.0 (1–200% in the UI).
- The active line is stopped on logout and when a new line plays.

## Pack layout

The plugin reads its pack from the directory selected in settings. The path is
entered manually and must already exist when confirmed; there is no default pack
location.

```text
<selected directory>/
  manifest.json
  voicelines/{id}.ogg          # they/them root
  voicelines/{id}.she.ogg      # optional she/her variant
  voicelines/{id}.he.ogg       # optional he/him variant
  voicelines_override/{id}.ogg # hand-made audio wins
```

`events.jsonl` remains in the Dalamud config directory, not the selected pack
directory.

Resolution order for one line:

1. The matching `voicelines_override/{id}.{pronoun}.ogg` for a she/he line.
2. The matching `voicelines/{id}.{pronoun}.ogg` variant.
3. `voicelines_override/{id}.ogg`, then `voicelines/{id}.ogg` as the
   they/them rendering.

A she/he request whose variant is missing falls back to the neutral root, never
to the other gender. Lines without player-name pronouns have one root file and
ignore the selected pronoun.

The plugin requires manifest schema 2. Older manifests are disabled with a
warning instead of guessing which file is neutral.

A file must match the manifest's recorded byte size unless the manifest has no
size. Missing audio is a silent miss, never an error popup.

The manifest contract the plugin deserializes lives in
`OhMyVoice.Common/PackTypes.cs`:

- `Schema`, `Language`, `Sanitizer`, `Backend`, `Encoder`
- `Voices`, `Npcs`, `IgnoredSpeakers`
- `Voicelines[id]` with `Sentence`, root byte size, duration, and optional
  `Variants.she` / `Variants.he`

The generator also writes `Model`, `Generation`, and `VoiceEmbeddings` at the
top level. Those are informational extras for regeneration; the plugin ignores
them.

Encoded audio is Ogg-Opus, 48 kHz mono, 32 kbps constrained VBR. The reader does
not reject another bitrate: re-encoding means a new pack generation, not a
corrupt pack.

## Event log

`events.jsonl` sits beside the manifest. One JSON row per result and line ID:

- `hit` proves the line is covered.
- `miss` is the generation worklist and includes the sanitized sentence,
  speaker, source, zone, game version, and NPC metadata read from the running
  game (name, NPC id, race, sex, expansion).

Rows are deduped by `Result + Id` across sessions. On load, the log is
compacted into a temp file and swapped in atomically; unreadable rows are
dropped. A legacy `misses.jsonl` is migrated to `events.jsonl` once.

Misses are only logged. Nothing is synthesized live and no network request is
made.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `TermsAccepted` | off | Must be explicitly accepted on first run before runtime hooks start |
| `PackDirectory` | empty | User-selected existing folder containing the pack |
| `Enabled` | on | Master switch; off captures and plays nothing |
| `Volume` | 100 | Pack playback volume, 1–200% |
| `PlayerPronouns` | Auto | Auto uses character sex; or force They/them, She/her, He/him |
| `VoWaitMs` | 100 | Delay before starting, to let real VO win (0–500 ms) |
| `StopOnVo` | on | Stop a playing pack line when real VO starts |
| `DebugLogging` | off | Verbose `/xllog` output |
| `SubtitleDedupeSeconds` | 1.0 | Repeat window for the same line ID; config-file only |

The status window (`/omv`) shows enabled/playing state and has **Reload pack**
and **Settings** buttons. Settings save immediately. On first run, a mandatory
usage agreement is shown before either window can open; plugin capture and
playback remain disabled until it is accepted. The pack download button is a
disabled placeholder until a distribution URL is available.
