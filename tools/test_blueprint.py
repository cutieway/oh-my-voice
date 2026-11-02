"""Blueprint validation rules: what an agentic voice-design pass may not do.

These exercise the real vocabulary and the archived/archetype example
blueprints. blueprints/characters/ is empty until a curated character exists, so
the fixture falls back to an archetype profile.
"""
import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHARACTERS = ROOT / "blueprints" / "characters"
ARCHIVE = ROOT / "blueprints" / "archive" / "characters"


def load_blueprint():
    spec = importlib.util.spec_from_file_location("omv_blueprint", ROOT / "tools" / "blueprint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


blueprint = load_blueprint()


ARCHETYPES = ROOT / "blueprints" / "archetypes"


def _fixture_path(name):
    for root in (CHARACTERS, ARCHIVE, ARCHETYPES):
        path = root / f"{name}.json"
        if path.is_file():
            return path
    raise FileNotFoundError(f"no blueprint fixture named {name!r}")


def profile(name="gerolt"):
    return json.loads(_fixture_path(name).read_text(encoding="utf-8"))


class ShippedBlueprintTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()

    def test_every_available_blueprint_is_valid(self):
        paths = sorted(CHARACTERS.glob("*.json")) + sorted(ARCHIVE.glob("*.json"))
        for path in paths:
            with self.subTest(path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(blueprint.validate_profile(data, self.vocab, path.stem), [])
                self.assertEqual(data["character"]["id"], path.stem)

    def test_identity_prompt_is_in_the_target_length(self):
        low, high = self.vocab["render"]["identity_word_target"]
        for path in sorted(CHARACTERS.glob("*.json")) + sorted(ARCHIVE.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            words = len(blueprint.render_identity(data, self.vocab).split())
            with self.subTest(path.stem):
                self.assertTrue(low <= words <= high, words)

    def test_instruct_without_style_is_the_identity_sentence(self):
        data = profile("archetype_masculine_clear_mid")
        self.assertEqual(
            blueprint.render_instruct(data, self.vocab),
            blueprint.render_identity(data, self.vocab),
        )

    def test_style_clause_is_appended_by_default(self):
        data = profile("archetype_masculine_clear_mid")
        data["speech_style"] = {
            "register": "formal",
            "vocabulary_complexity": "educated",
            "sentence_complexity": "complex",
            "colloquialism": "low",
            "rhetorical_style": "measured",
        }
        data["speech_style_analysis"] = None
        instruct = blueprint.render_instruct(data, self.vocab)
        identity = blueprint.render_identity(data, self.vocab)
        self.assertTrue(instruct.startswith(identity + " "))
        self.assertIn("formal in register", instruct)
        self.assertIn("rhetorical shape", instruct)

    def test_render_is_deterministic(self):
        data = profile()
        self.assertEqual(
            blueprint.render_identity(data, self.vocab),
            blueprint.render_identity(data, self.vocab),
        )


class VocabularyRuleTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()

    def assert_rejected(self, message_part, data=None):
        data = data or profile()
        errors = blueprint.validate_profile(data, self.vocab, "test")
        self.assertTrue(
            any(message_part in error for error in errors),
            f"expected {message_part!r} in {errors}",
        )

    def test_unknown_value_is_rejected(self):
        data = profile()
        data["identity"]["timbre_primary"] = "velvet"
        self.assert_rejected("not in the vocabulary", data)

    def test_overlapping_roughness_is_rejected(self):
        data = profile()
        data["identity"]["timbre_primary"] = "gravelly"
        data["identity"]["timbre_secondary"] = "husky"
        self.assert_rejected("shares the 'rough' family", data)

    def test_secondary_from_another_family_is_allowed(self):
        data = profile()
        data["identity"]["timbre_primary"] = "gravelly"
        data["identity"]["timbre_secondary"] = "breathy"
        data["evidence"]["timbre_secondary"] = {"level": "creative_selection"}
        self.assertEqual(blueprint.validate_profile(data, self.vocab, "test"), [])

    def test_trait_repeating_a_family_is_rejected(self):
        data = profile()
        data["identity"]["distinctive_traits"] = ["slight_vocal_fry"]
        data["evidence"]["distinctive_traits"] = {"level": "creative_selection"}
        self.assert_rejected("repeats the timbre family", data)

    def test_more_than_two_traits_is_rejected(self):
        data = profile()
        data["identity"]["distinctive_traits"] = [
            "light_vibrato",
            "rounded_vowels",
            "tight_vocal_placement",
        ]
        self.assert_rejected("at most 2 values", data)

    def test_missing_evidence_is_rejected(self):
        data = profile()
        del data["evidence"]["speaking_pace"]
        self.assert_rejected("evidence.speaking_pace is missing", data)

    def test_explicit_evidence_needs_a_quote(self):
        data = profile()
        data["evidence"]["presentation"] = {"level": "explicit", "source": "https://example.test"}
        self.assert_rejected("needs a quote", data)

    def test_extreme_value_needs_source_evidence(self):
        data = profile()
        data["identity"]["speaking_pace"] = "very_fast"
        self.assert_rejected("needs explicit or supported_inference evidence", data)

    def test_half_an_accent_is_rejected(self):
        data = profile()
        data["identity"]["accent"] = {"profile": "scottish"}
        self.assert_rejected("needs exactly profile and strength", data)

    def test_emotion_word_in_identity_is_rejected(self):
        vocab = copy.deepcopy(self.vocab)
        vocab["identity"]["timbre_primary"]["values"]["warm"]["render"] = "cheerful warm"
        data = profile("archetype_masculine_warm_low_mid")
        errors = blueprint.validate_profile(data, vocab, "test")
        self.assertTrue(any("emotion words" in error for error in errors), errors)

    def test_removed_mood_layer_is_rejected_in_schema_1_2(self):
        data = profile("archetype_masculine_clear_mid")
        data["moods"] = {"calm": {}}
        data["pack_archon"] = "calm"
        errors = blueprint.validate_profile(data, self.vocab, "test")
        self.assertTrue(any("removed in schema 1.2" in error for error in errors), errors)

    def test_unvalidated_style_fields_are_rejected_in_schema_1_2(self):
        data = profile("archetype_masculine_clear_mid")
        data["speech_style"] = {"register": "formal", "diction_style": "plain"}
        errors = blueprint.validate_profile(data, self.vocab, "test")
        self.assertTrue(any("diction_style is not a schema 1.2 field" in error for error in errors), errors)

    def test_lore_age_is_rejected_in_schema_1_2(self):
        data = profile("archetype_masculine_clear_mid")
        data["character"]["lore_age"] = 30
        errors = blueprint.validate_profile(data, self.vocab, "test")
        self.assertTrue(any("lore_age was removed" in error for error in errors), errors)

    def test_unknown_field_is_not_silently_ignored(self):
        data = profile()
        data["identity"]["sibilance"] = "harsh"
        self.assert_rejected("identity.sibilance = 'harsh' is not in the vocabulary", data)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()

    def test_seed_is_stable_and_distinct_per_voice(self):
        self.assertEqual(
            blueprint.seed_for("gerolt"),
            blueprint.seed_for("gerolt"),
        )
        self.assertNotEqual(
            blueprint.seed_for("gerolt"),
            blueprint.seed_for("godbert"),
        )

    def test_plan_carries_the_shared_reference_script(self):
        data = profile()
        plan = blueprint.build_plan(data, self.vocab)
        self.assertEqual(plan["reference"]["text"], self.vocab["reference"]["text"])
        self.assertEqual(
            plan["instruct"],
            blueprint.render_instruct(data, self.vocab),
        )
        self.assertNotIn("variants", plan)


class CollisionTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()

    def test_two_identical_identities_collide(self):
        a = profile("gerolt")
        b = copy.deepcopy(a)
        b["character"]["id"] = "gerolt_twin"
        collisions = blueprint.find_collisions([a, b], self.vocab)
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0][1], ["gerolt", "gerolt_twin"])

    def test_different_identities_do_not_collide(self):
        profiles = [profile(name) for name in ("gerolt", "godbert", "momodi")]
        self.assertEqual(blueprint.find_collisions(profiles, self.vocab), [])


if __name__ == "__main__":
    unittest.main()
