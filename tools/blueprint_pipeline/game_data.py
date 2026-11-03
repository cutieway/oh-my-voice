from __future__ import annotations

import pathlib
from typing import Any

from .cache import read_json


def load_game_index(path: pathlib.Path | None) -> dict[int, dict[str, Any]]:
    if path is None:
        return {}
    payload = read_json(path)
    rows = payload.get("npcs", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("game NPC index must contain an array named 'npcs'")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("npc_id", row.get("id")) is None:
            continue
        npc_id = int(row.get("npc_id", row.get("id")))
        result[npc_id] = {
            "name": row.get("name"),
            "race_id": row.get("race_id"),
            "race": row.get("race_name", row.get("race")),
            "tribe_id": row.get("tribe_id"),
            "clan": row.get("tribe_name", row.get("clan")),
            "sex": row.get("sex", row.get("gender_name", "unknown")),
            "body_type": row.get("body_type"),
            "height": row.get("height"),
            "scale": row.get("scale"),
            "bust_or_tone_1": row.get("bust_or_tone_1"),
            "extra_feature_2_or_bust": row.get("extra_feature_2_or_bust"),
            "jaw": row.get("jaw"),
            "mouth": row.get("mouth"),
            "face": row.get("face"),
            "nose": row.get("nose"),
            "eye_shape": row.get("eye_shape"),
            "eyebrows": row.get("eyebrows"),
            "hair_style": row.get("hair_style"),
            "skin_color": row.get("skin_color"),
            "model_chara": row.get("model_chara"),
            "model_body": row.get("model_body"),
            "not_rewrite_height": row.get("not_rewrite_height"),
        }
    return result


def game_metadata(npc_id: int | None, game_index: dict[int, dict]) -> dict:
    if npc_id is None:
        return {
            "race_id": None, "race": None, "tribe_id": None, "clan": None,
            "sex": "unknown", "body_type": None, "height": None, "scale": None,
            "bust_or_tone_1": None, "extra_feature_2_or_bust": None,
            "jaw": None, "mouth": None, "face": None, "nose": None,
            "eye_shape": None, "eyebrows": None, "hair_style": None,
            "skin_color": None, "model_chara": None, "model_body": None,
            "not_rewrite_height": None,
        }
    return game_index.get(
        npc_id,
        {
            "race_id": None, "race": None, "tribe_id": None, "clan": None,
            "sex": "unknown", "body_type": None, "height": None, "scale": None,
            "bust_or_tone_1": None, "extra_feature_2_or_bust": None,
            "jaw": None, "mouth": None, "face": None, "nose": None,
            "eye_shape": None, "eyebrows": None, "hair_style": None,
            "skin_color": None, "model_chara": None, "model_body": None,
            "not_rewrite_height": None,
        },
    )
