from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
BLUEPRINTS = ROOT / "blueprints"
DEFAULT_DB = ROOT / "data" / "dialogue.db"
DEFAULT_OUT = ROOT / "data" / "blueprints"
DEFAULT_CACHE = DEFAULT_OUT / "cache"
DEFAULT_VOCABULARY = BLUEPRINTS / "vocabulary.json"
# Local by default: dialogue must not leave the machine unless the operator
# explicitly opts into a remote endpoint.
DEFAULT_JEV_ENDPOINT = "http://127.0.0.1:8081/v1/systemone"
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
    "min_dialogue_lines_for_individual": 50,
    "min_style_fields_for_individual": 2,
    "min_dialogue_lines_for_review": 5,
}

MAX_QUESTION_COUNT = 20
MAX_STATE_BYTES = 96 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_DIALOGUE_SAMPLE_CHARS = 12000
# Must stay at or above the strong-tier line floor so the sample can reach it.
MAX_DIALOGUE_SAMPLE_COUNT = 40
MAX_RETRIES = 4
HTTP_TIMEOUT_SECONDS = 30

# Version tags travel with every Style result and every pinned reference. Bump
# them when the taxonomy, classifier contract, prompt, or sampler changes.
STYLE_TAXONOMY_VERSION = "style-taxonomy-3.0"
STYLE_CLASSIFIER_VERSION = "jev-style-classifier-3.0"
STYLE_PROMPT_VERSION = "style-prompt-3.0"
DIALOGUE_SAMPLER_VERSION = "dialogue-sampler-3.0"
SPEECH_STYLE_SCHEMA_VERSION = "speech-style-3.0"
STYLE_EVIDENCE_POLICY_VERSION = "style-evidence-3.1"

STYLE_FIELDS = (
    "register",
    "lexical_complexity",
    "sentence_complexity",
    "colloquialism",
    "discourse_style",
)

# Status describes evidence sufficiency, never the classification itself.
# `mixed` is a value in every production taxonomy and always uses `supported`
# (or `provisional`) here; it is deliberately not a status.
STYLE_STATUSES = (
    "supported",
    "provisional",
    "insufficient_evidence",
    "unresolved",
    "rejected",
)

# Initial evidence floors. They are starting values from the v2 evaluation
# protocol, not universal laws; tune them only against development data.
STYLE_EVIDENCE_TIERS = {
    "minimum": {"lines": 8, "words": 100, "contexts": 1},
    "supported": {"lines": 20, "words": 300, "contexts": 2},
    "strong": {"lines": 40, "words": 600, "contexts": 3},
}

# A field may need more evidence than the global floor. These multipliers are
# deliberately conservative and configurable through vocabulary.json.
STYLE_FIELD_EVIDENCE_MULTIPLIERS = {
    "register": 1.0,
    "lexical_complexity": 1.0,
    "sentence_complexity": 1.0,
    "colloquialism": 1.0,
    "discourse_style": 1.25,
}
