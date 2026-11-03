from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Evidence:
    source_type: str
    source: str
    revision_id: str | int | None
    quote: str
    field: str
    level: str


@dataclass
class Candidate:
    canonical_id: str
    npc_id: int | None
    name: str
    aliases: list[str]
    normalized_names: list[str]
    identity_status: str
    has_se_reference: bool
    identity_ambiguous: bool
    dialogue_count: int = 0
    unique_line_count: int = 0
    source_counts: dict[str, int] = field(default_factory=dict)
    source_line_ids: dict[str, int] = field(default_factory=dict)
    expansion_counts: dict[str, int] = field(default_factory=dict)
    race_id: int | None = None
    race: str | None = None
    tribe_id: int | None = None
    clan: str | None = None
    sex: str = "unknown"
    body_type: int | None = None
    height: int | None = None
    scale: float | None = None
    bust_or_tone_1: int | None = None
    extra_feature_2_or_bust: int | None = None
    jaw: int | None = None
    mouth: int | None = None
    face: int | None = None
    nose: int | None = None
    eye_shape: int | None = None
    eyebrows: int | None = None
    hair_style: int | None = None
    skin_color: int | None = None
    model_chara: int | None = None
    model_body: int | None = None
    not_rewrite_height: bool | None = None
    review_reasons: list[str] = field(default_factory=list)


@dataclass
class Dossier:
    identity: dict[str, Any]
    statistics: dict[str, Any]
    dialogue_analysis: dict[str, Any]
    dialogue_samples: list[dict[str, Any]]
    voice_evidence: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    schema_version: str = "1.0"
    input_hash: str | None = None


def to_dict(value: Any) -> Any:
    return asdict(value) if hasattr(value, "__dataclass_fields__") else value
