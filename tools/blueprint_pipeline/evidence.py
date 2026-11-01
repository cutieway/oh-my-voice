from __future__ import annotations

from typing import Any

SOURCE_LEVELS = {"explicit", "supported_inference"}
ALL_LEVELS = SOURCE_LEVELS | {"creative_selection", "default", "unknown"}


def make_evidence(
    field: str,
    level: str,
    *,
    source_type: str | None = None,
    source: str | None = None,
    revision_id: str | int | None = None,
    quote: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if level not in ALL_LEVELS:
        raise ValueError(f"unknown evidence level: {level}")
    if level in SOURCE_LEVELS and (not source or not quote):
        raise ValueError(f"{level} evidence for {field} requires source and quote")
    record = {"level": level, "field": field}
    if source_type:
        record["source_type"] = source_type
    if source:
        record["source"] = source
    if revision_id is not None:
        record["revision_id"] = revision_id
    if quote:
        record["quote"] = quote
    if note:
        record["note"] = note
    return record


def source_evidence(
    field: str,
    source_type: str,
    source: str,
    revision_id: str | int | None,
    quote: str,
    *,
    level: str = "explicit",
) -> dict[str, Any]:
    return make_evidence(
        field, level, source_type=source_type, source=source,
        revision_id=revision_id, quote=quote,
    )


def creative_evidence(field: str, note: str, *, source: str | None = None) -> dict[str, Any]:
    return make_evidence(field, "creative_selection", note=note, source=source)
