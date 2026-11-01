from __future__ import annotations

from .casting import cast_identity
from .generator import build_individual_profile


def compose_archetype_style_profile(
    archetype: dict,
    classification: dict,
    dossier: dict,
    vocabulary: dict,
) -> dict:
    """Compose a character profile: acoustics cast from game data, style classified.

    Acoustics are not inferable from dialogue text, so they come from the
    deterministic casting rules over game facts; wording is measured from the
    NPC's own lines. Both stay separate in the profile so a style decision can
    never pass as acoustic evidence.

    `archetype` is kept in the signature for the CLI fallback path, which still
    routes generic NPCs to a shared archetype profile; it is not used here.
    """
    del archetype
    return build_individual_profile(
        dossier, classification, cast_identity(dossier.get("identity") or {}), vocabulary,
    )
