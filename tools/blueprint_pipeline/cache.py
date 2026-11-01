from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def safe_component(value: str) -> str:
    cooked = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._")
    return cooked or "unknown"


def write_immutable_json(path: pathlib.Path, value: Any) -> pathlib.Path:
    """Create a snapshot without replacing an existing data artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    encoded = payload.encode("utf-8")
    candidate = path
    if candidate.exists():
        try:
            if candidate.read_bytes() == encoded:
                return candidate
        except OSError:
            pass
        suffix = hashlib.sha256(encoded).hexdigest()[:12]
        candidate = path.with_name(f"{path.stem}.{suffix}{path.suffix}")
        serial = 1
        while candidate.exists():
            try:
                if candidate.read_bytes() == encoded:
                    return candidate
            except OSError:
                pass
            candidate = path.with_name(f"{path.stem}.{suffix}.{serial}{path.suffix}")
            serial += 1

    with candidate.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
    return candidate


def write_content_addressed(directory: pathlib.Path, stem: str, value: Any) -> pathlib.Path:
    digest = object_hash(value)
    path = directory / f"{safe_component(stem)}.{digest[:16]}.json"
    return write_immutable_json(path, value)


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_files(directory: pathlib.Path, pattern: str = "*.json") -> list[pathlib.Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob(pattern), key=lambda p: (p.stat().st_mtime_ns, p.name))


def latest_by_created(paths: list[pathlib.Path], key: str | None = None) -> dict[str, Any] | None:
    records = []
    for path in paths:
        try:
            value = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if key is not None and value.get(key) != key:
            continue
        records.append((str(value.get("created_at", "")), path.name, value))
    return max(records, default=(None, None, None))[-1]
