# xivdata — cutscene SCD extraction + NPC attribution

Standalone Lumina reader. It does **not** hook the game and does **not** need
Dalamud running; it only needs the installed game's `sqpack` directory and the
Lumina assemblies that ship with Dalamud.

## Why this works

`cut_scene/*` Excel sheets contain rows like:

```
TEXT_VOICEMAN_05400_000010_ALPHINAUD  ->  Well, my friends─now that we have...
```

The key is the join:

1. `TEXT_` -> `vo_`, drop the final `_SPEAKER` token:
   `vo_voiceman_05400_000010`.
2. `VoiceScdPaths` expands that base to
   `cut/{ffxiv,ex1..ex5}/sound/voicem/voiceman_05400/vo_voiceman_05400_000010_{m,_,f}_en.scd`.
3. The `SPEAKER` token is normalized (lowercase, spaces/apostrophes/hyphens
   removed) and matched exactly against `ENpcResident.Singular` and its
   first-name token. The result gives the NPC row ID, race ID, and gender.
4. The first SCD candidate that exists and is at least 1 KiB is the real voice
   clip. Stub SCDs are skipped.

The CSV keeps both the game's `speaker_token` and the resolved `npc_id/name`, so
unresolved tokens (e.g. `originalsphene`, `venas`, `mysteryvoice`) are still
identifiable and can be aliased later.

## Build

The project references the Lumina assemblies from Dalamud:

```
DalamudDir=~/.xlcore/dalamud/Hooks/dev dotnet build
```

Requires .NET 10. On this machine both are in the NixOS home packages
(`dotnet-sdk_10`, `vgmstream`).

## Run

```bash
# from the repo root; output stays under the gitignored data/ root
dotnet run --project tools/xivdata -- --out data/xivdata/scan

# one character, extract SCD + WAV
dotnet run --project tools/xivdata -- --out data/xivdata/sphene \
  --speaker sphene --extract --vgmstream "$(command -v vgmstream-cli)"

# pilot
dotnet run --project tools/xivdata -- --out data/xivdata/pilot --limit 20 --extract
```

`--sqpack` defaults to `~/Public/xlcore/ffxiv/game/sqpack`.
`--lang` supports `en`, `de`, `fr`, `ja`; the language is also used in the SCD
filename and the sheet text.

## Output

```
dialogue.csv
scd/<NPC name>/<audio base>.scd
wav/<NPC name>/<audio base>.wav
```

`dialogue.csv` columns:

```
sheet,row_id,text_key,audio_base,speaker_token,npc_id,npc_name,
race_id,gender,scd_path,scd_bytes,text
```

## Measured on game version 2026.10.16

- 98 `cut_scene/*` sheets
- 25,598 rows with a real SCD
- 21,918 resolved to an `ENpcResident` row (85.6%)
- 3,680 unresolved tokens, 1,163 unparsed keys, 380 keys with no SCD
  - all 380 missing-SCD keys are `voiceman`; this scan has zero `manfst`
    rows, and the `manfst` branch matches Echokraut's
    `VoiceScdPaths.Build` verbatim, so it is not hiding a systematic miss
- 4.5 GiB of SCD payload

Example per-character totals: Alphinaud 1,631; Y'shtola 1,244; Alisaie 1,159;
Wuk Lamat 1,071; Sphene 255 (25.3 min after decoding).

## SCD decoding

FFmpeg's `scd` demuxer rejects FFXIV's SCD version 50331648. Use
`vgmstream-cli`:

```bash
vgmstream-cli -o out.wav in.scd
```

It reports 44,099 Hz mono Microsoft 4-bit ADPCM for the sampled file; resample
to 24 kHz for Qwen3-TTS training.
