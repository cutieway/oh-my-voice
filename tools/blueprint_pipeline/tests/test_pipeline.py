from __future__ import annotations

import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

import blueprint
from blueprint_pipeline.archetypes import (
    load_archetypes,
    resolve_effective_profile,
    select_archetype,
    write_assignment_snapshot,
    load_assignment_snapshot,
)
from blueprint_pipeline import evaluation
from blueprint_pipeline.cache import object_hash, read_json
from blueprint_pipeline.classifier import ClassifierProvider, RulesProvider, classify_dossier
from blueprint_pipeline.config import DEFAULT_THRESHOLDS
from blueprint_pipeline.dossier import build_dossiers
from blueprint_pipeline.dialogue import (
    _length_bucket as length_bucket,
    analyze_dialogue,
    select_representative_samples,
    split_speaker_label,
)
from blueprint_pipeline.casting import cast_identity, normalize_sex
from blueprint_pipeline.generator import build_individual_profile, validate_generated_profile
from blueprint_pipeline.inventory import build_inventory
from blueprint_pipeline.jev import (
    JevConfigurationError,
    JevProvider,
    JevResponseError,
    build_questions,
    compact_state,
    validate_response,
)
from blueprint_pipeline.review import (
    apply_manual_changes,
    approve_profile,
    mark_auditioned,
    preserve_manual_profile,
)


def make_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE speakers (
            voice_id TEXT PRIMARY KEY, npc_id INTEGER, name TEXT, npc_key TEXT,
            normalized_name TEXT, race_id INTEGER, gender INTEGER, speaker_match TEXT,
            has_scd INTEGER, voiced_lines INTEGER, unvoiced_lines INTEGER, dialogue_lines INTEGER
        );
        CREATE TABLE dialogs (
            id INTEGER PRIMARY KEY, lang TEXT, speaker_id TEXT, npc_id INTEGER,
            source TEXT, sheet TEXT, row_id INTEGER, speaker_name TEXT, npc_key TEXT,
            speaker_match TEXT, text_clean TEXT, text_kind TEXT, has_scd INTEGER,
            voice_line_id TEXT
        );
        CREATE TABLE dialog_speakers (
            dialog_id INTEGER, speaker_id TEXT, npc_id INTEGER, match_source TEXT, is_primary INTEGER
        );
    """)
    speakers = [
        ("example_a", 10, "Example A", "examplea", "examplea", 1, 0, "direct", 0, 0, 3, 3),
        ("example_alias", 10, "Example Alias", "examplealias", "examplealias", 1, 0, "token", 0, 0, 3, 3),
        ("voiced_20", 20, "Voice Twin", "voicetwin", "voicetwin", 1, 0, "direct", 1, 2, 1, 3),
        ("unvoiced_20", 20, "Voice Twin Alias", "voicetwinalias", "voicetwinalias", 1, 0, "token", 0, 0, 1, 1),
        ("voiced_30", 30, "Name Collision", "namecollision", "namecollision", 1, 0, "direct", 1, 1, 0, 1),
        ("unvoiced_31", 31, "Name Collision", "namecollision", "namecollision", 1, 0, "token", 0, 0, 1, 1),
        ("unresolved", None, "Unknown Speaker", "unknownspeaker", "unknownspeaker", None, None, "unresolved", 0, 0, 1, 1),
    ]
    connection.executemany("INSERT INTO speakers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", speakers)
    rows = [
        (1, "en", "example_a", 10, "quest", "Quest/A", 1, "Example A", "examplea", "direct", "A short greeting.", "dialogue", 0, "line-a"),
        (2, "en", "example_alias", 10, "custom", "Quest/B", 2, "Example Alias", "examplealias", "token", "A longer answer with a few useful words.", "dialogue", 0, "line-b"),
        (3, "en", "unvoiced_20", 20, "quest", "Quest/C", 3, "Voice Twin Alias", "voicetwinalias", "token", "This has no line recording, but the speaker has another SE clip.", "dialogue", 0, "line-c"),
        (4, "en", "voiced_20", 20, "quest", "Quest/D", 4, "Voice Twin", "voicetwin", "direct", "Existing actor missing line.", "dialogue", 0, "line-d"),
        (5, "en", "unvoiced_31", 31, "quest", "Quest/E", 5, "Name Collision", "namecollision", "token", "Name collision dialogue.", "dialogue", 0, "line-e"),
        (6, "en", "unresolved", None, "quest", "Quest/F", 6, "Unknown Speaker", "unknownspeaker", "unresolved", "Unresolved words.", "narration", 0, "line-f"),
        (7, "en", "example_a", 10, "quest", "Quest/G", 7, "Example A", "examplea", "direct", "Shared line from two linked labels.", "dialogue", 0, "line-g"),
        (8, "en", "example_a", 10, "quest", "Quest/G", 8, "Example A", "examplea", "direct", "A journal row is not dialogue.", "journal", 0, "line-h"),
    ]
    connection.executemany("INSERT INTO dialogs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.executemany(
        "INSERT INTO dialog_speakers VALUES (?,?,?,?,?)",
        [(7, "example_a", 10, "direct", 1), (7, "example_alias", 10, "token", 0)],
    )
    connection.commit()
    connection.close()


def response_for(questions: dict) -> dict:
    answers = {}
    for question_id, question in questions.items():
        if question["type"] == "choice":
            options = list(question["criteria"])
            choice = options[0]
            answers[question_id] = {
                "type": "choice",
                "choice": choice,
                "confidence": 0.99,
                "probabilities": {key: float(key == choice) for key in options},
            }
        elif question["type"] == "score":
            score = min(3, len(question["criteria"]) - 1)
            answers[question_id] = {
                "type": "score",
                "score": score,
                "confidence": 0.95,
                "probabilities": {str(i): float(i == score) for i in range(len(question["criteria"]))},
            }
        else:
            answers[question_id] = {"type": "noul", "noul": 0.99}
    return {
        "model": "jev-1.14.0",
        "answers": answers,
        "usage": {"input_tokens": 1234, "output_tokens": 234},
    }


class FakeResponse(io.BytesIO):
    def __init__(self, payload: dict, *, status: int = 200, headers: dict | None = None):
        super().__init__(json.dumps(payload).encode())
        self.status = status
        self.headers = headers or {}


class InventoryAndDossierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "dialogue.db"
        make_database(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_gate_aliases_voiced_overlap_name_collision_and_unresolved(self):
        inventory = build_inventory(self.db)
        by_id = {item["canonical_id"]: item for item in inventory["candidates"]}
        self.assertEqual(by_id["npc_10"]["identity_status"], "eligible")
        self.assertEqual(by_id["npc_10"]["speaker_ids"], ["example_a", "example_alias"])
        self.assertEqual(by_id["npc_10"]["dialogue_count"], 3)
        self.assertEqual(by_id["npc_20"]["identity_status"], "review")
        self.assertTrue(by_id["npc_20"]["has_se_reference"])
        self.assertEqual(by_id["npc_31"]["identity_status"], "review")
        self.assertTrue(any("normalized name" in reason for reason in by_id["npc_31"]["review_reasons"]))
        unresolved = by_id["unresolved_unresolved"]
        self.assertEqual(unresolved["identity_status"], "review_unresolved")
        self.assertIsNone(unresolved["npc_id"])
        self.assertNotIn("npc_30", by_id)

    def test_unknown_sex_fallback_does_not_treat_zero_as_male(self):
        inventory = build_inventory(self.db)
        by_id = {item["canonical_id"]: item for item in inventory["candidates"]}
        self.assertEqual(by_id["npc_10"]["sex"], "unknown")
        index = build_inventory(self.db, game_index={10: {"race": None, "sex": "male"}})
        indexed = {item["canonical_id"]: item for item in index["candidates"]}
        self.assertEqual(indexed["npc_10"]["sex"], "unknown")
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE speakers SET gender = 1 WHERE voice_id = 'example_a'")
        connection.commit()
        connection.close()
        inventory = build_inventory(self.db)
        by_id = {item["canonical_id"]: item for item in inventory["candidates"]}
        self.assertEqual(by_id["npc_10"]["sex"], "female")

    def test_dossier_deduplicates_shared_association_and_excludes_non_dialogue(self):
        dossiers = build_dossiers(self.db, out=self.root / "cache", only={"npc_10"})
        self.assertEqual(len(dossiers), 1)
        dossier = read_json(Path(dossiers[0]["path"]))
        self.assertEqual(dossier["statistics"]["dialogue_count"], 3)
        self.assertEqual(dossier["statistics"]["unique_line_count"], 3)
        self.assertEqual(len(dossier["dialogue_samples"]), 3)
        self.assertTrue(all(sample["text_kind"] == "dialogue" for sample in dossier["dialogue_samples"]))

    def test_representative_samples_and_measurements_are_reproducible(self):
        rows = [
            {"dialog_id": "a", "voice_line_id": "a", "text": "Short question?", "source": "quest", "sheet": "A"},
            {"dialog_id": "b", "voice_line_id": "b", "text": "Before the morning bells arrive, please follow the marked path and keep your companions nearby.", "source": "custom", "sheet": "B"},
            {"dialog_id": "c", "voice_line_id": "c", "text": "We do not know.", "source": "quest", "sheet": "A"},
        ]
        first = select_representative_samples("npc_10", rows, max_count=3)
        second = select_representative_samples("npc_10", rows, max_count=3)
        self.assertEqual(first, second)
        analysis = analyze_dialogue(rows)
        self.assertEqual(analysis, analyze_dialogue(rows))
        self.assertEqual(analysis["measurements"]["line_count"], 3)
        self.assertGreater(analysis["measurements"]["unique_word_count"], 0)
        self.assertGreaterEqual(analysis["measurements"]["question_count"], 1)
        self.assertEqual(analyze_dialogue([])["measurements"]["word_count"], 0)


class JevTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()
        self.questions = build_questions(self.vocab)

    def test_choice_score_noul_distributions_and_request_shape(self):
        payload = response_for(self.questions)
        normalized = validate_response(payload, self.questions)
        self.assertEqual(normalized["answers"]["individual_design"]["probabilities"]["3"], 1.0)
        self.assertEqual(normalized["answers"]["identity_resolved"]["noul"], 0.99)
        captured = []
        def opener(request, timeout):
            captured.append(request)
            return FakeResponse(payload)
        with patch.dict(os.environ, {"UNIT_TYPESAFE_KEY": "secret-test-key"}):
            provider = JevProvider(endpoint="https://jev.test/v1/systemone", api_key_env="UNIT_TYPESAFE_KEY", opener=opener, sleeper=lambda _: None)
            result = provider.classify({"identity": {"name": "Test"}}, self.questions)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer secret-test-key")
        request_body = json.loads(captured[0].data)
        self.assertEqual(set(request_body), {"model", "state", "questions"})
        self.assertEqual(result["model"], "jev-1.14.0")
        self.assertEqual(result["usage"]["input_tokens"], 1234)
        self.assertNotIn("secret-test-key", json.dumps(result))

    def test_rate_limit_retry_honors_retry_after(self):
        payload = response_for(self.questions)
        calls = []
        delays = []
        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                return FakeResponse({}, status=429, headers={"Retry-After": "0"})
            return FakeResponse(payload)
        with patch.dict(os.environ, {"UNIT_TYPESAFE_KEY": "secret-test-key"}):
            provider = JevProvider(api_key_env="UNIT_TYPESAFE_KEY", retries=1, opener=opener, sleeper=delays.append)
            result = provider.classify({}, self.questions)
        self.assertEqual(result["model"], "jev-1.14.0")
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [0.0])

    def test_missing_key_invalid_option_and_low_confidence_route(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(JevConfigurationError):
                JevProvider(api_key_env="UNIT_TYPESAFE_MISSING").classify({}, self.questions)
        payload = response_for(self.questions)
        payload["answers"]["speech_style.register"]["choice"] = "not-a-vocabulary-value"
        with self.assertRaises(JevResponseError):
            validate_response(payload, self.questions)

        class UncertainRules(RulesProvider):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                for answer in result["answers"].values():
                    if answer["type"] in {"choice", "score"}:
                        answer["confidence"] = 0.1
                return result

        dossier = {
            "input_hash": "input",
            "identity": {"canonical_id": "npc_10", "npc_id": 10, "resolution_status": "eligible", "has_se_voice_reference": False, "identity_ambiguous": False, "sex": "female"},
            "statistics": {"dialogue_count": 100},
            "dialogue_analysis": {"interpretation": {"register": "formal"}},
        }
        result = classify_dossier(dossier, UncertainRules(self.vocab), self.vocab)
        self.assertEqual(result["status"], "needs_review")
        # Acoustics are cast from game data, never asked of the model.
        self.assertNotIn("acoustic_casting", result)
        self.assertFalse([key for key in result["questions"] if key.startswith("identity.")])

    def test_voiced_identity_is_rejected_before_provider_call(self):
        class NeverCall(RulesProvider):
            def classify(self, dossier, questions):
                raise AssertionError("provider must not see a voiced identity")
        dossier = {"identity": {"canonical_id": "npc_20", "npc_id": 20, "resolution_status": "eligible", "has_se_voice_reference": True}}
        result = classify_dossier(dossier, NeverCall(self.vocab), self.vocab)
        self.assertEqual(result["status"], "needs_review")


class BlueprintAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()
        self.archetypes = load_archetypes()

    def test_archetypes_and_generated_individual_profiles_validate(self):
        for profile in self.archetypes:
            self.assertEqual(blueprint.validate_profile(profile, self.vocab), [])
        self.assertEqual(blueprint.find_collisions(self.archetypes, self.vocab), [])
        dossier = {
            "identity": {"canonical_id": "npc_10", "npc_id": 10, "name": "Example A", "speaker_ids": ["example_a"], "sex": "unknown"},
            "input_hash": "dossier-hash",
            "statistics": {"dialogue_count": 200},
            "dialogue_analysis": {"interpretation": {"register": "formal", "speech_habits": ["none_observed"]}},
        }
        classification = {
            "provider": "rules", "model_id": "rules-1.0", "dossier_hash": "dossier-hash",
            "configuration_hash": "config-hash", "timestamp": "2026-11-01T00:00:00Z",
            "answers": {},
            "speech_style": {"register": {"value": "formal"}},
        }
        casting = cast_identity(dossier["identity"])
        self.assertEqual(validate_generated_profile(profile, self.vocab), [])
        self.assertEqual(profile["lifecycle"]["status"], "draft")
        self.assertTrue(all(field in profile["evidence"] for field, value in profile["identity"].items() if value not in (None, [])))
        self.assertEqual(blueprint.render_identity(profile, self.vocab).split()[0], "A")

    def test_archetype_resolution_and_human_review_are_immutable_and_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            selected = select_archetype({"canonical_id": "npc_99", "sex": "female"}, self.archetypes)
            assignment = {
                "canonical_id": "npc_99", "npc_id": 99, "speaker_ids": ["speaker99"],
                "profile_id": selected["profile_id"], "assignment_status": "draft",
            }
            registry_path = write_assignment_snapshot([assignment], out)
            registry = load_assignment_snapshot(registry_path)
            effective = resolve_effective_profile("speaker99", registry, profile_roots=(ROOT / "blueprints" / "archetypes",))
            self.assertEqual(effective["profile"]["character"]["id"], selected["profile_id"])
            self.assertFalse(effective["ready_for_generation"])

            original = copy.deepcopy(effective["profile"])
            wav = out / "reference.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0\0" * 8000 * 10)
            auditioned = mark_auditioned(original, reference_file=wav, design_seed=101, by="reviewer")
            approved = approve_profile(auditioned, by="reviewer")
            self.assertEqual(blueprint.validate_profile(approved, self.vocab), [])
            self.assertTrue(approved["lifecycle"]["approval"]["reference_sha256"])
            changed = apply_manual_changes(approved, {"identity.pitch": "mid_high"}, by="editor")
            self.assertEqual(changed["lifecycle"]["status"], "ready_for_audition")
            self.assertIsNone(changed["lifecycle"]["approval"])
            self.assertEqual(blueprint.validate_profile(changed, self.vocab), [])
            self.assertEqual(preserve_manual_profile(changed, original), changed)


class SpeechStyleBenchmarkProtocolTests(unittest.TestCase):
    def _items(self, sample_count: int = 12) -> dict:
        samples = [{"id": f"B1-L{index:02d}", "text": f"Line {index} of the sample."} for index in range(1, sample_count + 1)]
        return {"B1": {"sample_count": len(samples), "dialogue_samples": samples}}

    def _form(self, items: dict, scope: str = "full") -> dict:
        payload_items = []
        for benchmark_id, item in items.items():
            payload_items.append({
                "benchmark_id": benchmark_id,
                "sample_count": item["sample_count"],
                "overall_evidence_sufficiency": "sufficient",
                "annotator_notes": "",
                "dialogue_samples": item["dialogue_samples"],
                "fields": {
                    field: {
                        "label": next(iter(options)),
                        "evidence_sufficient": "yes",
                        "evidence_line_ids": [item["dialogue_samples"][0]["id"]],
                        "notes": "",
                    }
                    for field, options in evaluation.FIELDS.items()
                },
            })
        return {
            "schema_version": "2.0",
            "benchmark": evaluation.BENCHMARK_VERSION,
            "protocol": evaluation.PROTOCOL_VERSION,
            "scope": scope,
            "instructions": "test",
            "items": payload_items,
        }

    def test_text_only_state_withholds_measurements(self):
        dossier = {"benchmark_id": "B1", "dialogue_samples": [
            {"id": "B1-L01", "text": "The gate opens at dawn."},
            {"id": "B1-L02", "text": "We will be ready."},
        ]}
        assisted = evaluation.build_speech_style_state(dossier, condition="assisted")
        text_only = evaluation.build_speech_style_state(dossier, condition="text_only")
        self.assertIn("measurements", assisted)
        self.assertNotIn("measurements", text_only)
        self.assertEqual(assisted["dialogue_samples"], text_only["dialogue_samples"])
        with self.assertRaises(ValueError):
            evaluation.build_speech_style_state(dossier, condition="audio")

    def test_form_validation_requires_citations_and_binds_sufficiency(self):
        items = self._items()
        evaluation._validate_annotation_form(self._form(items), items, label="a", scope="full")

        uncited = self._form(items)
        uncited["items"][0]["fields"]["register"]["evidence_line_ids"] = []
        with self.assertRaisesRegex(ValueError, "at least one supporting line"):
            evaluation._validate_annotation_form(uncited, items, label="a", scope="full")

        mixed_one_cite = self._form(items)
        mixed_one_cite["items"][0]["fields"]["register"].update(label="mixed")
        with self.assertRaisesRegex(ValueError, "mixed requires at least two"):
            evaluation._validate_annotation_form(mixed_one_cite, items, label="a", scope="full")

        unknown_line = self._form(items)
        unknown_line["items"][0]["fields"]["register"]["evidence_line_ids"] = ["B1-L99"]
        with self.assertRaisesRegex(ValueError, "not in this item"):
            evaluation._validate_annotation_form(unknown_line, items, label="a", scope="full")

        unbound = self._form(items)
        unbound["items"][0]["fields"]["register"].update(label="insufficient_evidence", evidence_line_ids=[])
        with self.assertRaisesRegex(ValueError, "requires evidence_sufficient='no'"):
            evaluation._validate_annotation_form(unbound, items, label="a", scope="full")

        inverse = self._form(items)
        inverse["items"][0]["fields"]["register"]["evidence_sufficient"] = "no"
        with self.assertRaisesRegex(ValueError, "requires label insufficient_evidence"):
            evaluation._validate_annotation_form(inverse, items, label="a", scope="full")

        wrong_scope = self._form(items, scope="pilot")
        with self.assertRaisesRegex(ValueError, "does not match the requested"):
            evaluation._validate_annotation_form(wrong_scope, items, label="a", scope="full")

    def test_pilot_gate_requires_every_field_to_pass(self):
        agreement = {
            "register": {"exact_agreement": 0.80, "cohen_kappa": 0.61},
            "colloquialism": {"exact_agreement": 0.80, "ordinal_weighted_cohen_kappa": 0.59},
        }
        gate = evaluation._pilot_gate(agreement)
        self.assertEqual(gate["status"], "revise_taxonomy")
        self.assertTrue(gate["fields"]["register"]["passed"])
        self.assertFalse(gate["fields"]["colloquialism"]["passed"])
        self.assertEqual(gate["fields"]["colloquialism"]["kappa_statistic"], "ordinal_weighted_cohen_kappa")

    def test_off_scale_vocabulary_labels_are_excluded_from_ordinal_metrics(self):
        order = evaluation.ORDINAL_FIELD_LABELS["vocabulary_complexity"]
        metrics = evaluation._ordinal_metrics(["plain", "advanced", "poetic", "specialized"], ["ordinary", "advanced", "poetic", "plain"], order)
        self.assertEqual(metrics["n"], 2)
        self.assertEqual(metrics["excluded_count"], 2)
        self.assertAlmostEqual(metrics["mean_absolute_label_distance"], 0.5)

    def test_sex_is_a_two_value_model_fact_with_null_fallback(self):
        cases = (
            ("male", "male", "masculine"),
            ("female", "female", "feminine"),
            ("unknown", None, None),
            ("", None, None),
        )
        for raw, sex, presentation in cases:
            with self.subTest(raw=raw):
                decisions = cast_identity(
                    {"canonical_id": "npc_1", "sex": raw, "race": "Hyur", "body_type": 1}
                )
                self.assertEqual(normalize_sex(raw), sex)
                self.assertEqual(decisions["model_gender"]["value"], sex)
                self.assertEqual(decisions["presentation"]["value"], presentation)
        self.assertIsNone(normalize_sex("androgynous"))
        self.assertIsNone(normalize_sex("neutral"))

    def test_zero_default_body_type_has_no_sex_fact(self):
        decisions = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Hyur", "body_type": 0, "height": 0,
        })
        self.assertIsNone(decisions["model_gender"]["value"])
        self.assertEqual(decisions["facts"]["model_gender_reported"], "male")
        self.assertEqual(decisions["facts"]["gender_ignored_reason"], "zero/default BodyType row")
        self.assertIsNone(decisions["facts"]["height"])
        self.assertEqual(decisions["facts"]["height_raw"], 0)

    def test_race_and_clan_facts_are_as_recorded(self):
        decisions = cast_identity(
            {"canonical_id": "npc_1", "sex": "male", "race": "Au Ra", "clan": "Raen"}
        )
        self.assertEqual(decisions["race"]["value"], "Au Ra")
        self.assertEqual(decisions["clan"]["value"], "Raen")
        self.assertIsNone(decisions["non_playable_race"]["value"])
        monster = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Amalj'aa",
        })
        self.assertIsNone(monster["model_gender"]["value"])
        self.assertIsNone(monster["race"]["value"])
        self.assertEqual(monster["non_playable_race"]["value"], "Amalj'aa")
        mismatch = cast_identity(
            {"canonical_id": "npc_1", "sex": "male", "race": "Hyur", "clan": "Raen"}
        )
        self.assertIsNone(mismatch["clan"]["value"])
        self.assertTrue(any(row["field"] == "clan" for row in mismatch["warnings"]))
        orphan = cast_identity({"canonical_id": "npc_1", "sex": "male", "clan": "Midlander"})
        self.assertIsNone(orphan["clan"]["value"])
        self.assertTrue(any(row["field"] == "clan" for row in orphan["warnings"]))

    def test_unavailable_identity_fields_are_null_not_defaulted(self):
        empty = cast_identity({"canonical_id": "npc_1"})
        for field in (
            "presentation", "perceived_age", "perceived_vocal_size", "vocal_weight",
            "pitch", "pitch_range", "timbre_primary", "timbre_secondary",
            "timbre_preferences", "resonance", "articulation", "speaking_pace",
            "intonation", "distinctive_traits",
        ):
            with self.subTest(field=field):
                self.assertIsNone(empty[field]["value"])

        male = cast_identity(
            {"canonical_id": "npc_1", "sex": "male", "race": "Hyur", "body_type": 1}
        )
        self.assertEqual(male["presentation"]["value"], "masculine")
        self.assertEqual(male["pitch"]["value"], "low_mid")
        self.assertIsNone(male["timbre_primary"]["value"])
        self.assertIsNone(male["resonance"]["value"])
        self.assertEqual(male["perceived_vocal_size"]["value"], "average")
        self.assertEqual(male["vocal_weight"]["value"], "balanced")

        aged = cast_identity(
            {"canonical_id": "npc_1", "sex": "male", "race": "Hyur", "body_type": 3}
        )
        self.assertEqual(aged["perceived_age"]["value"], "elderly")

        voiced = cast_identity(
            {"canonical_id": "npc_1", "sex": "female", "race": "Hyur",
             "has_se_voice_reference": True,
             "voice_evidence": {"verified": True, "source": "audio:test"}}
        )
        for field in ("presentation", "perceived_age", "pitch", "pitch_range",
                      "timbre_primary", "resonance"):
            with self.subTest(field=field, voiced=True):
                self.assertIsNone(voiced[field]["value"])
                self.assertEqual(voiced[field]["rule"], "voiced_use_audio_evidence")

    def test_muscle_tone_and_bust_follow_the_export_field_semantics(self):
        male_hyur = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Hyur",
            "body_type": 1, "bust_or_tone_1": 100,
        })
        self.assertEqual(male_hyur["facts"]["muscle_tone"], 100)
        self.assertIsNone(male_hyur["facts"]["bust"])
        female_hyur = cast_identity({
            "canonical_id": "npc_1", "sex": "female", "race": "Hyur",
            "body_type": 1, "bust_or_tone_1": 100, "extra_feature_2_or_bust": 40,
        })
        self.assertEqual(female_hyur["facts"]["muscle_tone"], 100)
        self.assertEqual(female_hyur["facts"]["bust"], 40)
        male_elezen = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Elezen",
            "body_type": 1, "bust_or_tone_1": 100,
        })
        self.assertIsNone(male_elezen["facts"]["muscle_tone"])
        self.assertIsNone(male_elezen["facts"]["bust"])

    def test_height_fallback_is_not_a_fact_and_never_moves_pitch(self):
        decisions = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Hyur", "height": 255,
        })
        self.assertIsNone(decisions["facts"]["height"])
        self.assertEqual(decisions["pitch"]["value"], "low_mid")
        short = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Hyur", "height": 0,
        })
        tall = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Hyur", "height": 100,
        })
        self.assertEqual(short["facts"]["height"], 0)
        self.assertEqual(short["pitch"]["value"], tall["pitch"]["value"])

    def test_audio_evidence_overrides_appearance(self):
        decisions = cast_identity({
            "canonical_id": "npc_1", "sex": "male", "race": "Roegadyn",
            "voice_evidence": {"pitch": "high", "verified": True, "source": "audio:test"},
        })
        self.assertEqual(decisions["pitch"]["value"], "high")
        self.assertEqual(decisions["pitch"]["rule"], "audio_evidence")
        self.assertIsNone(decisions["timbre_primary"]["value"])

    def test_composed_profile_keeps_acoustics_and_validated_style_only(self):
        from blueprint_pipeline.compose import compose_archetype_style_profile
        archetype = json.loads((Path(__file__).resolve().parents[3] / "blueprints" / "archetypes" / "archetype_masculine_warm_low_mid.json").read_text())
        dossier = {"identity": {"canonical_id": "npc_1", "name": "Test", "npc_id": 1, "sex": "male", "race": "Hyur"}}
        classification = {
            "canonical_id": "npc_1",
            "provider": "jev",
            "model_id": "local",
            "status": "needs_review",
            "speech_style": {
                "register": {"value": "conversational", "confidence": 0.9, "probabilities": {"conversational": 0.9}},
                "sentence_complexity": {"value": "simple", "confidence": 0.6},
                "diction_style": {"value": "plain", "confidence": 0.99},
            },
        }
        vocabulary = json.loads((Path(__file__).resolve().parents[3] / "blueprints" / "vocabulary.json").read_text())
        profile = compose_archetype_style_profile(archetype, classification, dossier, vocabulary)
        self.assertEqual(profile["profile_kind"], "character")
        self.assertEqual(profile["character"]["id"], "npc_1")
        # Acoustics come from casting rules, not from the shared archetype.
        self.assertEqual(profile["identity"]["presentation"], "masculine")
        self.assertEqual(profile["identity"]["pitch"], "low_mid")
        self.assertEqual(profile["evidence"]["pitch"]["level"], "creative_selection")
        self.assertIn("gender_base_pitch", profile["evidence"]["pitch"]["note"])
        self.assertEqual(profile["identity_provenance"]["pitch"]["rule"], "gender_base_pitch")
        self.assertEqual(profile["identity_provenance"]["pitch"]["rule_version"], "2.0")
        self.assertIsNone(profile["identity"]["perceived_age"])
        self.assertNotIn("perceived_age", profile["evidence"])
        self.assertEqual(set(profile["speech_style"]), {"register", "sentence_complexity"})
        self.assertNotIn("diction_style", profile["speech_style"])
        self.assertEqual(profile["speech_style_analysis"]["fields"]["register"]["confidence"], 0.9)
        self.assertEqual(profile["lifecycle"]["status"], "draft")

    def test_speech_style_is_appended_to_the_instruct_by_default(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import blueprint as renderer
        vocabulary = renderer.load_vocabulary(Path(__file__).resolve().parents[3] / "blueprints" / "vocabulary.json")
        profile = json.loads((Path(__file__).resolve().parents[3] / "blueprints" / "archetypes" / "archetype_masculine_clear_mid.json").read_text())
        plain = renderer.build_plan(profile, vocabulary)["instruct"]
        profile["speech_style"] = {
            "register": "formal",
            "vocabulary_complexity": "specialized",
            "sentence_complexity": "complex",
            "colloquialism": "low",
            "rhetorical_style": "measured",
        }
        styled = renderer.build_plan(profile, vocabulary)["instruct"]
        self.assertIn("formal in register", styled)
        self.assertTrue(styled.startswith(plain + " "))

    def test_share_packet_carries_form_guide_and_readme(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            form_root = root / "annotations" / "pilot"
            form_root.mkdir(parents=True)
            items = self._items()
            for name in ("annotator_a", "annotator_b"):
                (form_root / f"{name}.json").write_text(json.dumps(self._form(items, scope="pilot")), encoding="utf-8")
            (root / "annotations" / "annotation_guide.md").write_text("# guide\n", encoding="utf-8")
            result = evaluation.build_share_packets(root=root, scope="pilot")
            for name in ("annotator_a", "annotator_b"):
                packet = Path(result["packets"][name]["path"])
                with zipfile.ZipFile(packet) as archive:
                    self.assertEqual(sorted(archive.namelist()), ["README.md", "annotation_form.json", "annotation_guide.md"])
                    self.assertEqual(
                        json.loads(archive.read("annotation_form.json")),
                        json.loads((form_root / f"{name}.json").read_text()),
                    )
                    self.assertIn("Do not edit `benchmark_id`", archive.read("README.md").decode())
                _, _, parsed, is_zip = evaluation._read_annotation_source(packet)
                self.assertTrue(is_zip)
                self.assertEqual(parsed["scope"], "pilot")

    def test_adjudication_requires_a_reason_per_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = self._items()
            (root / "dossiers").mkdir()
            public, selection = [], []
            for benchmark_id, item in items.items():
                path = root / "dossiers" / f"{benchmark_id}.json"
                path.write_text(json.dumps({"benchmark_id": benchmark_id, "dialogue_samples": item["dialogue_samples"]}), encoding="utf-8")
                public.append({"benchmark_id": benchmark_id, "dossier_path": str(path)})
                selection.append({"benchmark_id": benchmark_id, "split": "development", "stratum": "5-20"})
            (root / "manifest.json").write_text(json.dumps({
                "public_dossiers": public,
                "private_selection": selection,
                "annotation_scopes": {"full": {"item_ids": [benchmark_id for benchmark_id in items]}},
            }), encoding="utf-8")
            run_dir = root / "annotations" / "submissions" / "run-1"
            run_dir.mkdir(parents=True)
            for name, seed in (("annotator_a", 1), ("annotator_b", 2)):
                (run_dir / f"{name}.json").write_text(json.dumps(self._form(items)), encoding="utf-8")
            (run_dir / "manifest.json").write_text(json.dumps({
                "benchmark": evaluation.BENCHMARK_VERSION,
                "protocol": evaluation.PROTOCOL_VERSION,
                "scope": "full",
                "annotation_run": "run-1",
                "imported_epoch_ns": seed,
                "imported_utc": "2026-11-01T00:00:00Z",
            }), encoding="utf-8")
            form_path = evaluation.prepare_adjudication_form(root=root, annotation_run="run-1")["adjudication_form_path"]
            payload = json.loads(Path(form_path).read_text())
            item = payload["items"][0]
            item["adjudicated_labels"] = {field: next(iter(options)) for field, options in evaluation.FIELDS.items()}
            item["adjudication_reasons"] = {field: "" for field in evaluation.FIELDS}
            payload["status"] = "complete_manual_adjudication"
            submission = root / "submission.json"
            submission.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "a reason is required"):
                evaluation.import_adjudication(root=root, source=submission, annotation_run="run-1")
            item["adjudication_reasons"] = {field: "annotator_agreement" for field in evaluation.FIELDS}
            submission.write_text(json.dumps(payload), encoding="utf-8")
            result = evaluation.import_adjudication(root=root, source=submission, annotation_run="run-1")
            self.assertEqual(result["adjudicated_field_count"], len(evaluation.FIELDS))

    def test_expected_items_follow_manifest_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dossiers").mkdir()
            public = []
            selection = []
            for benchmark_id in ("B1", "B2"):
                samples = [{"id": f"{benchmark_id}-L01", "text": "Line."}]
                dossier = {"benchmark_id": benchmark_id, "dialogue_samples": samples}
                path = root / "dossiers" / f"{benchmark_id}.json"
                path.write_text(json.dumps(dossier), encoding="utf-8")
                public.append({"benchmark_id": benchmark_id, "dossier_path": str(path)})
                selection.append({"benchmark_id": benchmark_id, "split": "development", "stratum": "5-20"})
            (root / "manifest.json").write_text(json.dumps({
                "public_dossiers": public,
                "private_selection": selection,
                "annotation_scopes": {"pilot": {"item_ids": ["B2"]}, "full": {"item_ids": ["B1", "B2"]}},
            }), encoding="utf-8")
            self.assertEqual(sorted(evaluation._expected_annotation_items(root, "pilot")[0]), ["B2"])
            self.assertEqual(sorted(evaluation._expected_annotation_items(root, "full")[0]), ["B1", "B2"])


class CompactStateAndSampleTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()

    def test_state_carries_measurements_coverage_and_no_rules_labels(self):
        dossier = {
            "identity": {"canonical_id": "npc_1", "npc_id": 1, "name": "N"},
            "statistics": {"dialogue_count": 900, "unique_line_count": 800,
                           "source_distribution": {"quest": {"rows": 900, "unique_line_ids": 800}}},
            "dialogue_analysis": {
                "measurements": {"line_count": 800, "signal_line_rates": {"dialect_lines": 0.4}},
                "interpretation": {"register": "formal"},
            },
            "dialogue_samples": [{"text": "Hello.", "source": "quest", "sheet": "s", "signals": ["question"]}],
        }
        state = compact_state(dossier)
        self.assertIn("dialogue_measurements", state)
        self.assertNotIn("interpretation", json.dumps(state))
        self.assertEqual(state["dialogue_measurements"]["signal_line_rates"]["dialect_lines"], 0.4)
        self.assertEqual(state["dialogue_coverage"]["sampled_lines"], 1)
        self.assertEqual(round(state["dialogue_coverage"]["coverage_of_unique_lines"], 3), 0.001)
        self.assertIn("signal-weighted subset", state["dialogue_coverage"]["warning"])
        self.assertEqual(state["dialogue_samples"][0]["signals"], ["question"])
        self.assertEqual(state["voice_evidence"], [])
        self.assertNotIn("lore_quotes", state)
        self.assertNotIn("mood", " ".join(state["guardrails"]).lower())

    def test_state_drops_samples_before_it_drops_evidence(self):
        dossier = {
            "identity": {"canonical_id": "npc_2"},
            "statistics": {"dialogue_count": 50, "unique_line_count": 50},
            "dialogue_analysis": {"measurements": {"line_count": 50}},
            "dialogue_samples": [{"text": "x" * 400, "source": "quest"} for _ in range(20)],
        }
        state = compact_state(dossier, max_bytes=4000)
        self.assertLess(len(state["dialogue_samples"]), 20)
        self.assertEqual(state["dialogue_coverage"]["sampled_lines"], len(state["dialogue_samples"]))

    def test_speaker_labels_are_stage_directions_not_dialogue(self):
        self.assertEqual(
            split_speaker_label("(-Zombibrand-)Urrrgggh... Hurrrggghhh...!"),
            ("Urrrgggh... Hurrrggghhh...!", "Zombibrand"),
        )
        self.assertEqual(split_speaker_label("Plain line."), ("Plain line.", None))

        labelled = [{"voice_line_id": "1", "text": "(-Fuath-)Calm yourself, friend."}]
        plain = [{"voice_line_id": "1", "text": "Calm yourself, friend."}]
        self.assertEqual(
            analyze_dialogue(labelled, self.vocab)["measurements"]["word_count"],
            analyze_dialogue(plain, self.vocab)["measurements"]["word_count"],
        )
        sample = select_representative_samples("npc_3", labelled, vocabulary=self.vocab)[0]
        self.assertEqual(sample["text"], "Calm yourself, friend.")
        self.assertEqual(sample["speaker_label"], "Fuath")

    def test_sample_selection_prefers_stylistically_distinctive_lines(self):
        lines = [
            {"voice_line_id": f"g{i}", "text": f"The road is long and the sky is grey number {i}.", "source": "quest", "sheet": "s"}
            for i in range(12)
        ] + [
            {"voice_line_id": "d1", "text": "Aye, I've not seen yer like here before, an' that's the truth of it.", "source": "quest", "sheet": "s"},
        ]
        samples = select_representative_samples("npc_4", lines, max_count=3, vocabulary=self.vocab)
        self.assertEqual(samples[0]["voice_line_id"], "d1")
        self.assertTrue(any(signal.startswith("dialect:") for signal in samples[0]["signals"]))

    def test_many_sheets_do_not_starve_medium_and_long_buckets(self):
        # Gerolt's shape: 200+ quest sheets, 181 short / 953 medium / 947 long
        # lines. Taking one line per sheet inside a bucket filled all 24 slots
        # with short lines before the loop ever reached `medium`.
        import random

        pool = [f"word{index}" for index in range(400)]
        lines = []
        for sheet in range(60):
            rng = random.Random(sheet)
            lines.append({
                "voice_line_id": f"s{sheet}",
                "text": "Hm. " + " ".join(rng.sample(pool, 2)),
                "source": "quest", "sheet": f"quest/{sheet:03d}",
            })
            lines.append({
                "voice_line_id": f"m{sheet}",
                "text": " ".join(rng.sample(pool, 14)),
                "source": "quest", "sheet": f"quest/{sheet:03d}",
            })
            lines.append({
                "voice_line_id": f"l{sheet}",
                "text": " ".join(rng.sample(pool, 30)),
                "source": "quest", "sheet": f"quest/{sheet:03d}",
            })
        samples = select_representative_samples("npc_5", lines, max_count=24, vocabulary=self.vocab)
        buckets = [length_bucket(sample["text"]) for sample in samples]
        self.assertEqual(len(samples), 24)
        self.assertGreaterEqual(buckets.count("medium"), 7)
        self.assertGreaterEqual(buckets.count("long"), 7)
        self.assertLessEqual(buckets.count("short"), 9)

    def test_whole_word_matching_does_not_count_ye_inside_yes(self):
        lines = [{"voice_line_id": f"l{i}", "text": "Yes, there were many of them."} for i in range(10)]
        rates = analyze_dialogue(lines, self.vocab)["measurements"]["signal_line_rates"]
        self.assertEqual(rates["dialect_lines"], 0.0)


class VoiceCastingSpecTests(unittest.TestCase):
    """Spec-mandated casting, provenance and prompt-hash behaviour."""

    def setUp(self):
        self.vocab = blueprint.load_vocabulary()

    @staticmethod
    def cast(**identity):
        identity.setdefault("canonical_id", "npc_1")
        return cast_identity(identity)

    def test_body_type_1_is_adult_appearing_convention(self):
        decisions = self.cast(sex="male", race="Hyur", body_type=1)
        age = decisions["perceived_age"]
        self.assertEqual(age["value"], "adult")
        self.assertEqual(age["evidence_type"], "creative_selection")
        self.assertEqual(age["rule"], "body_type_standard_adult_convention")
        self.assertEqual(decisions["pitch"]["value"], "low_mid")

    def test_body_type_3_is_elderly_without_pitch_shift(self):
        decisions = self.cast(sex="male", race="Hyur", body_type=3)
        self.assertEqual(decisions["perceived_age"]["value"], "elderly")
        self.assertEqual(decisions["perceived_age"]["evidence_type"], "game_fact")
        self.assertEqual(decisions["pitch"]["value"], "low_mid")

    def test_body_type_4_is_youthful_with_higher_pitch(self):
        male = self.cast(sex="male", race="Hyur", body_type=4)
        female = self.cast(sex="female", race="Hyur", body_type=4)
        self.assertEqual(male["perceived_age"]["value"], "youthful")
        self.assertNotEqual(male["perceived_age"]["value"], "young_teen")
        self.assertEqual(male["pitch"]["value"], "mid_high")
        self.assertEqual(female["pitch"]["value"], "high")

    def test_lalafell_body_type_1_stays_adult_and_never_child_pitch(self):
        decisions = self.cast(sex="female", race="Lalafell", body_type=1)
        self.assertEqual(decisions["perceived_age"]["value"], "adult")
        self.assertEqual(decisions["pitch"]["value"], "mid_high")
        no_model = self.cast(sex="female", race="Lalafell")
        self.assertIsNone(no_model["perceived_age"]["value"])

    def test_elezen_lifespan_lore_never_changes_perceived_age(self):
        young = self.cast(sex="male", race="Elezen", body_type=1)
        older = self.cast(sex="male", race="Elezen", body_type=3)
        self.assertEqual(young["perceived_age"]["value"], "adult")
        self.assertEqual(older["perceived_age"]["value"], "elderly")

    def test_female_hyur_and_roegadyn_split_muscle_from_bust(self):
        for race in ("Hyur", "Roegadyn"):
            with self.subTest(race=race):
                decisions = self.cast(
                    sex="female", race=race, body_type=1,
                    bust_or_tone_1=35, extra_feature_2_or_bust=82,
                )
                self.assertEqual(decisions["facts"]["muscle_tone"], 35)
                self.assertEqual(decisions["facts"]["bust"], 82)

    def test_au_ra_muscle_and_bust_follow_model_gender(self):
        male = self.cast(sex="male", race="Au Ra", clan="Xaela", bust_or_tone_1=90)
        female = self.cast(sex="female", race="Au Ra", clan="Raen", bust_or_tone_1=90)
        self.assertEqual(male["facts"]["muscle_tone"], 90)
        self.assertIsNone(male["facts"]["bust"])
        self.assertIsNone(female["facts"]["muscle_tone"])
        self.assertEqual(female["facts"]["bust"], 90)

    def test_other_races_do_not_gain_unmapped_muscle(self):
        for race, sex in (("Miqo'te", "male"), ("Elezen", "male"), ("Viera", "male")):
            with self.subTest(race=race, sex=sex):
                decisions = self.cast(sex=sex, race=race, bust_or_tone_1=100)
                self.assertIsNone(decisions["facts"]["muscle_tone"])
                self.assertIsNone(decisions["facts"]["bust"])
        female_miqote = self.cast(sex="female", race="Miqo'te", bust_or_tone_1=60)
        self.assertEqual(female_miqote["facts"]["bust"], 60)
        self.assertIsNone(female_miqote["facts"]["muscle_tone"])

    def test_highlander_adds_a_small_morphology_modifier(self):
        midlander = self.cast(sex="male", race="Hyur", clan="Midlander")
        highlander = self.cast(sex="male", race="Hyur", clan="Highlander")
        self.assertEqual(highlander["pitch"]["value"], midlander["pitch"]["value"])
        self.assertGreater(
            highlander["perceived_vocal_size"]["prior_score"],
            midlander["perceived_vocal_size"]["prior_score"],
        )
        self.assertGreater(
            highlander["vocal_weight"]["prior_score"],
            midlander["vocal_weight"]["prior_score"],
        )

    def test_roegadyn_gets_strongest_prior_without_forced_stereotypes(self):
        roegadyn = self.cast(sex="male", race="Roegadyn", clan="Sea Wolf")
        lalafell = self.cast(sex="male", race="Lalafell", clan="Plainsfolk")
        self.assertGreater(
            roegadyn["perceived_vocal_size"]["prior_score"],
            lalafell["perceived_vocal_size"]["prior_score"],
        )
        self.assertEqual(roegadyn["pitch"]["value"], "low_mid")
        self.assertIsNone(roegadyn["resonance"]["value"])
        self.assertNotIn(roegadyn["timbre_primary"]["value"], {"booming", "raspy"})

    def test_viera_and_miqote_clans_have_no_acoustic_difference(self):
        for race, clans in (
            ("Viera", ("Rava", "Veena")),
            ("Miqo'te", ("Seeker of the Sun", "Keeper of the Moon")),
        ):
            first, second = (
                self.cast(sex="female", race=race, clan=clan) for clan in clans
            )
            for field in (
                "presentation", "perceived_vocal_size", "vocal_weight", "pitch",
                "pitch_range", "timbre_primary", "timbre_secondary", "resonance",
            ):
                with self.subTest(race=race, field=field):
                    self.assertEqual(first[field]["value"], second[field]["value"])

    def test_hrothgar_social_labels_do_not_change_morphology(self):
        helion = self.cast(sex="male", race="Hrothgar", clan="Helions")
        lost = self.cast(sex="male", race="Hrothgar", clan="The Lost")
        self.assertEqual(helion["facts"]["clan_semantics"], "social_status")
        self.assertEqual(lost["facts"]["clan_semantics"], "social_status")
        for field in ("perceived_vocal_size", "vocal_weight", "pitch"):
            self.assertEqual(helion[field]["value"], lost[field]["value"])

    def test_unknown_race_clan_and_body_type_warn_and_keep_raw_values(self):
        decisions = self.cast(
            sex="male", race="Newrace", clan="Newclan", body_type=9,
        )
        self.assertIsNone(decisions["race"]["value"])
        self.assertIsNone(decisions["clan"]["value"])
        self.assertIsNone(decisions["facts"]["body_type"])
        self.assertEqual(decisions["facts"]["race_reported"], "Newrace")
        self.assertEqual(decisions["facts"]["clan_reported"], "Newclan")
        self.assertEqual(decisions["facts"]["body_type_reported"], 9)
        warned = {row["field"] for row in decisions["warnings"]}
        self.assertEqual(warned, {"race", "clan", "body_type"})
        self.assertIsNone(decisions["perceived_age"]["value"])
        self.assertIsNone(decisions["pitch"]["value"])

    def test_zero_height_and_muscle_are_values_not_missing(self):
        decisions = self.cast(
            sex="male", race="Hyur", body_type=1, height=0, bust_or_tone_1=0,
        )
        self.assertEqual(decisions["facts"]["height"], 0)
        self.assertEqual(decisions["facts"]["muscle_tone"], 0)
        self.assertIsNotNone(decisions["vocal_weight"]["value"])
        factor = next(
            factor for factor in decisions["vocal_weight"]["factors"]
            if factor["name"] == "muscle_tone_prior"
        )
        self.assertEqual(factor["value"], 0)
        self.assertLess(factor["weight_delta"], 0)

    def test_height_never_changes_pitch(self):
        short = self.cast(sex="male", race="Hyur", body_type=1, height=0)
        tall = self.cast(sex="male", race="Hyur", body_type=1, height=100)
        self.assertEqual(short["pitch"]["value"], tall["pitch"]["value"])

    def test_no_race_sets_pitch_range(self):
        for race in ("Hyur", "Elezen", "Lalafell", "Miqo'te", "Roegadyn", "Au Ra", "Viera", "Hrothgar"):
            with self.subTest(race=race):
                self.assertIsNone(self.cast(sex="male", race=race)["pitch_range"]["value"])

    def test_morphology_fields_carry_prior_score_strength_and_sources(self):
        decisions = self.cast(sex="male", race="Roegadyn", clan="Sea Wolf", bust_or_tone_1=82)
        for field in ("perceived_vocal_size", "vocal_weight"):
            record = decisions[field]
            self.assertEqual(record["evidence_type"], "creative_selection")
            self.assertEqual(record["rule_version"], "2.0")
            self.assertIn("prior_score", record)
            self.assertIn("casting_strength", record)
            self.assertTrue(record["sources"])
            self.assertNotIn("confidence", record)
        self.assertEqual(decisions["vocal_weight"]["value"], "full")
        self.assertEqual(decisions["perceived_vocal_size"]["value"], "large")
        self.assertIn("muscle_tone", decisions["vocal_weight"]["facts"])

    def test_unverified_container_reference_does_not_route_to_audio(self):
        decisions = self.cast(
            sex="female", race="Hyur", body_type=1, has_se_voice_reference=True,
        )
        self.assertFalse(decisions["facts"]["has_recorded_voice"])
        self.assertEqual(decisions["pitch"]["value"], "mid_high")

    def test_voiced_npc_keeps_game_facts_while_acoustics_route_to_audio(self):
        decisions = self.cast(
            sex="female", race="Hyur", clan="Midlander", body_type=3,
            has_se_voice_reference=True,
            voice_evidence={"verified": True, "source": "audio:test", "pitch": "high"},
        )
        self.assertTrue(decisions["facts"]["has_recorded_voice"])
        self.assertEqual(decisions["facts"]["race"], "Hyur")
        self.assertEqual(decisions["facts"]["clan"], "Midlander")
        self.assertEqual(decisions["facts"]["body_type"], 3)
        self.assertEqual(decisions["pitch"]["value"], "high")
        self.assertEqual(decisions["pitch"]["evidence_type"], "explicit_audio")
        self.assertIsNone(decisions["perceived_age"]["value"])

    def test_style_change_does_not_change_identity_hash(self):
        profile = self._profile()
        before = blueprint.prompt_hashes(profile, self.vocab)
        profile["speech_style"] = {
            "register": "formal",
            "vocabulary_complexity": "specialized",
            "sentence_complexity": "complex",
            "colloquialism": "low",
            "rhetorical_style": "measured",
        }
        after = blueprint.prompt_hashes(profile, self.vocab)
        self.assertEqual(before["identity_prompt"], after["identity_prompt"])
        self.assertNotEqual(before["style_prompt"], after["style_prompt"])
        self.assertNotEqual(before["combined_instruct_hash"], after["combined_instruct_hash"])

    def test_casting_rule_version_changes_identity_hash_and_seed(self):
        profile = self._profile()
        before = blueprint.build_plan(profile, self.vocab)
        profile["casting"]["rules_version"] = "2.1-test"
        after = blueprint.build_plan(profile, self.vocab)
        self.assertNotEqual(before["identity_prompt_hash"], after["identity_prompt_hash"])
        self.assertNotEqual(before["seed"], after["seed"])
        self.assertNotEqual(before["combined_instruct_hash"], after["combined_instruct_hash"])

    def test_reference_audio_hash_changes_when_wav_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a.wav"
            second = root / "b.wav"
            self._write_wav(first, 220.0)
            self._write_wav(second, 440.0)
            profile = self._profile()
            pin_a = mark_auditioned(
                profile, reference_file=first, design_seed=7, by="tester",
                reference_transcript="same words",
            )["reference_pin"]
            pin_b = mark_auditioned(
                profile, reference_file=second, design_seed=7, by="tester",
                reference_transcript="same words",
            )["reference_pin"]
            self.assertEqual(pin_a["design_seed"], pin_b["design_seed"])
            self.assertNotEqual(pin_a["reference_sha256"], pin_b["reference_sha256"])

    def test_partial_style_still_renders_available_dimensions(self):
        profile = self._profile()
        profile["speech_style"] = {"register": "formal"}
        style = blueprint.render_style(profile, self.vocab)
        self.assertIn("formal in register", style)

    def _profile(self):
        classification = {
            "provider": "rules",
            "model_id": "rules-1.0",
            "dossier_hash": "d",
            "configuration_hash": "c",
            "timestamp": "t",
            "answers": {},
            "speech_style": {"register": {"value": "conversational", "confidence": 0.9}},
        }
        dossier = {"identity": {
            "canonical_id": "npc_1", "npc_id": 1, "name": "Test",
            "sex": "male", "race": "Roegadyn", "clan": "Sea Wolf",
            "body_type": 1, "bust_or_tone_1": 82, "speaker_ids": [],
        }}
        casting = cast_identity(dossier["identity"])
        return build_individual_profile(dossier, classification, casting, self.vocab)

    @staticmethod
    def _write_wav(path: Path, frequency: float) -> None:
        import math
        frames = bytearray()
        for index in range(16000 * 9):
            sample = int(16000 * math.sin(2 * math.pi * frequency * index / 16000))
            frames += sample.to_bytes(2, "little", signed=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(bytes(frames))
