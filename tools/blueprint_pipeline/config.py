from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
BLUEPRINTS = ROOT / "blueprints"
DEFAULT_DB = ROOT / "data" / "dialogue.db"
DEFAULT_OUT = ROOT / "data" / "blueprints"
DEFAULT_CACHE = DEFAULT_OUT / "cache"
DEFAULT_VOCABULARY = BLUEPRINTS / "vocabulary.json"
DEFAULT_WIKI_API = "https://ffxiv.consolegameswiki.com/mediawiki/api.php"
DEFAULT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_JEV_KEY_ENV = "TYPESAFE_API_KEY"
# TypeSafe's Jev 1.14.0 input price verified 2026-11-01. Override the CLI
# estimate when the provider changes its pricing; tokenization remains a rough
# preflight estimate and the response's usage is the recorded actual count.
DEFAULT_JEV_INPUT_USD_PER_MILLION_TOKENS = 0.042
DEFAULT_USER_AGENT = (
    "OhMyVoice/2.0 (FFXIV synthetic blueprint research; "
    "https://codeberg.org/cutieway/oh-my-voice)"
)
LANGUAGE = "en"

DEFAULT_THRESHOLDS = {
    "choice_confidence": 0.55,
    "choice_probability": 0.35,
    "noul_yes": 0.75,
    "noul_no": 0.25,
    "distinctiveness_score": 3.7,
    "min_dialogue_lines_for_individual": 50,
    "min_dialogue_lines_for_review": 5,
    # Mood is decided from dialogue alone, one chunk at a time. The floor is the
    # volume at which a baseline register can exist at all; below it no mood
    # question is asked and the pack stays neutral.
    "min_dialogue_lines_for_mood": 40,
    "mood_probability": 0.50,
    "mood_abstention_mass": 0.55,
    "mood_consistency_ratio": 0.6,
    "mood_consistency_margin": 0.05,
    "mood_dispersion_limit": 0.55,
    # Measured on Bespoke-Nimble-9B-v3: this tokenizer runs ~1.34 characters per
    # token, so 7k characters is ~5.2k tokens of dialogue. With the compact
    # aggregates, questions, and schema that lands near 7.5k of an 8k context,
    # leaving room for the typed answer.
    "mood_chunk_chars": 7000,
    "mood_chunk_overlap": 0.15,
    "mood_max_chunks": 8,
}

MAX_QUESTION_COUNT = 20
MAX_STATE_BYTES = 96 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_DIALOGUE_SAMPLE_CHARS = 6000
MAX_DIALOGUE_SAMPLE_COUNT = 24
MAX_RETRIES = 4
HTTP_TIMEOUT_SECONDS = 30
