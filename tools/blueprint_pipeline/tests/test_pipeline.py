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
from contextlib import redirect_stdout
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
from blueprint_pipeline.config import (
    DEFAULT_THRESHOLDS,
    MAX_DIALOGUE_SAMPLE_COUNT,
    STYLE_EVIDENCE_TIERS,
)
from blueprint_pipeline.dossier import build_dossiers
from blueprint_pipeline.dialogue import (
    _length_bucket as length_bucket,
    analyze_dialogue,
    build_dialogue_sample_pools,
    evidence_sufficiency,
    sample_manifest_hash,
    select_representative_samples,
    select_targeted_samples,
    split_speaker_label,
)
from blueprint_pipeline.casting import cast_identity, normalize_sex
from blueprint_pipeline.generator import build_individual_profile, validate_generated_profile
from blueprint_pipeline.inventory import build_inventory
from blueprint_pipeline.audit import render_style_audit
from blueprint_pipeline.audition import audit_entries, build_audition_plans
from blueprint_pipeline.reporting import review_report
from blueprint_pipeline.jev import (
    EVIDENCE_SOURCE,
    JevConfigurationError,
    JevProvider,
    JevResponseError,
    apply_evidence,
    build_questions,
    compact_state,
    evidence_question_specs,
    request_fingerprint,
    run_evidence_pass,
    select_evidence,
    validate_response,
)
from blueprint_pipeline.cli import main
from blueprint_pipeline.review import (
    ReviewError,
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


def response_for(questions: dict, sample_ids: list[str] | None = None) -> dict:
    answers = {}
    evidence = list(sample_ids or ["L1", "L2", "L3"])
    for question_id, question in questions.items():
        if question["type"] == "choice":
            options = list(question["criteria"])
            choice = options[0]
            answers[question_id] = {
                "type": "choice",
                "choice": choice,
                "status": "supported",
                "confidence": 0.99,
                "model_self_reported_confidence": 0.99,
                "supporting_line_ids": evidence[:3],
                "counterexample_line_ids": [],
                "rationale": "The pattern recurs across the cited lines.",
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
        self.assertNotIn("individual_design", normalized["answers"])
        self.assertNotIn("identity_resolved", normalized["answers"])
        self.assertEqual(normalized["answers"]["speech_style.register"]["choice"], "formal")
        self.assertEqual(normalized["answers"]["speech_style.register"]["supporting_line_ids"], ["L1", "L2", "L3"])
        captured = []
        def opener(request, timeout):
            captured.append(request)
            return FakeResponse(payload)
        with patch.dict(os.environ, {"UNIT_TYPESAFE_KEY": "secret-test-key"}):
            provider = JevProvider(
                endpoint="https://jev.test/v1/systemone",
                api_key_env="UNIT_TYPESAFE_KEY",
                opener=opener,
                sleeper=lambda _: None,
                allow_remote=True,
            )
            result = provider.classify({
                "identity": {"name": "Test"},
                "statistics": {"dialogue_count": 1, "unique_line_count": 1},
                "dialogue_samples": [{"id": "L1", "text": "The gate opens at dawn."}],
            }, self.questions)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer secret-test-key")
        request_body = json.loads(captured[0].data)
        self.assertEqual(set(request_body), {"model", "state", "questions"})
        state = request_body["state"]
        self.assertNotIn("identity", state)
        self.assertNotIn("appearance", state)
        self.assertNotIn("voice_evidence", state)
        self.assertTrue(all("signals" not in sample for sample in state["dialogue_samples"]))
        self.assertEqual([sample["id"] for sample in state["dialogue_samples"]], ["L1"])
        self.assertNotIn("identity_resolved", request_body["questions"])
        self.assertNotIn("voice_evidence_present", request_body["questions"])
        self.assertNotIn("individual_design", request_body["questions"])
        self.assertEqual(
            set(request_body["questions"]),
            {f"speech_style.{field}" for field in (
                "register", "lexical_complexity", "sentence_complexity",
                "colloquialism", "discourse_style",
            )},
        )
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

    def test_remote_requires_opt_in_and_key_but_local_does_not_need_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(JevConfigurationError, "disabled by default"):
                JevProvider(
                    endpoint="https://api.example.test/v1/systemone",
                    api_key_env="UNIT_TYPESAFE_MISSING",
                ).classify({}, self.questions)
            with self.assertRaisesRegex(JevConfigurationError, "API key missing"):
                JevProvider(
                    endpoint="https://api.example.test/v1/systemone",
                    api_key_env="UNIT_TYPESAFE_MISSING",
                    allow_remote=True,
                ).classify({}, self.questions)
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

    def test_local_endpoint_runs_without_network_or_remote_opt_in(self):
        payload = response_for(self.questions)
        captured = []

        def opener(request, timeout):
            captured.append(request)
            return FakeResponse(payload)

        with patch.dict(os.environ, {}, clear=True):
            provider = JevProvider(
                endpoint="http://127.0.0.1:8081/v1/systemone",
                api_key_env="UNIT_MISSING_KEY",
                opener=opener,
                sleeper=lambda _: None,
            )
            result = provider.classify({
                "identity": {"canonical_id": "npc_local", "name": "Local"},
                "statistics": {"dialogue_count": 1, "unique_line_count": 1},
                "dialogue_samples": [{"id": "L1", "text": "The gate opens at dawn."}],
            }, self.questions)
        self.assertEqual(result["model"], "jev-1.14.0")
        self.assertEqual(len(captured), 1)
        self.assertIsNone(captured[0].get_header("Authorization"))
        self.assertEqual(provider.endpoint_class, "local")

    def test_identity_bearing_state_is_rejected_before_transport(self):
        def opener(request, timeout):
            raise AssertionError("identity-bearing state must not reach the network")

        provider = JevProvider(
            endpoint="http://127.0.0.1:8081/v1/systemone",
            opener=opener,
            sleeper=lambda _: None,
        )
        for state in (
            {"identity": {"name": "Secret"}},
            {"dialogue_samples": [{"id": "L1", "text": "Hi.", "source": "quest"}]},
            {"dialogue_coverage": {"source_distribution": {"quest": 1}}},
        ):
            with self.subTest(state=state):
                with self.assertRaisesRegex(JevConfigurationError, "must not include"):
                    provider.classify_state(state, self.questions)

    def test_non_style_question_path_is_rejected(self):
        vocabulary = json.loads(json.dumps(self.vocab))
        vocabulary["classification"]["questions"]["identity.presentation"] = {
            "type": "choice",
            "instructions": "Identity is not a Style question.",
            "values": "speech_style.register",
        }
        with self.assertRaisesRegex(ValueError, "Style questions"):
            build_questions(vocabulary)

    def test_verified_voice_is_rejected_before_provider_call(self):
        class NeverCall(RulesProvider):
            def classify(self, dossier, questions):
                raise AssertionError("provider must not see a verified voiced identity")
        dossier = {"identity": {"canonical_id": "npc_20", "npc_id": 20, "resolution_status": "eligible", "has_recorded_voice": True}}
        result = classify_dossier(dossier, NeverCall(self.vocab), self.vocab)
        self.assertEqual(result["status"], "needs_review")

    def test_raw_scd_reference_is_not_proof_of_recorded_voice(self):
        dossier = {
            "input_hash": "input",
            "identity": {
                "canonical_id": "npc_21", "npc_id": 21, "resolution_status": "eligible",
                "has_se_voice_reference": True, "has_recorded_voice": False,
                "identity_ambiguous": False,
            },
            "statistics": {"dialogue_count": 100},
            "dialogue_analysis": {"interpretation": {"register": "formal"}},
        }
        result = classify_dossier(dossier, RulesProvider(self.vocab), self.vocab)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("unverified SE voice reference", " ".join(result["review_reasons"]))
        self.assertNotEqual(result["profile_allocation"]["decision"], "official_audio")


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

    def test_audition_pin_stores_reference_design_provenance(self):
        profile = copy.deepcopy(self.archetypes[0])
        profile["speech_style"] = {"register": "formal"}
        profile["speech_style_analysis"] = {
            "taxonomy_version": "taxonomy-test",
            "classifier_version": "classifier-test",
            "prompt_version": "prompt-test",
            "sampler_version": "sampler-test",
            "corpus_hash": "corpus-test",
            "sample_manifest_hash": "manifest-test",
            "fields": {"register": {"value": "formal", "status": "supported"}},
        }
        plan = blueprint.build_plan(profile, self.vocab)
        self.assertEqual(plan["taxonomy_version"], "taxonomy-test")
        self.assertEqual(plan["corpus_hash"], "corpus-test")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "reference.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0\0" * 8000 * 10)
            pinned = mark_auditioned(
                profile,
                reference_file=wav,
                design_seed=plan["seed"],
                by="reviewer",
                reference_transcript=plan["reference"]["text"],
                design_model=plan["design_model"],
                identity_prompt_hash=plan["identity_prompt_hash"],
                style_profile_hash=plan["style_profile_hash"],
                combined_instruct_hash=plan["combined_instruct_hash"],
                casting_rules_version=plan["casting_rules_version"],
                renderer_version=plan["renderer_version"],
                instruct=plan["instruct"],
                taxonomy_version=plan["taxonomy_version"],
                classifier_version=plan["classifier_version"],
                prompt_version=plan["prompt_version"],
                sampler_version=plan["sampler_version"],
                corpus_hash=plan["corpus_hash"],
                sample_manifest_hash=plan["sample_manifest_hash"],
            )
            pin = pinned["reference_pin"]
            self.assertEqual(pin["instruct"], plan["instruct"])
            self.assertEqual(pin["taxonomy_version"], "taxonomy-test")
            self.assertEqual(pin["classifier_version"], "classifier-test")
            self.assertEqual(pin["prompt_version"], "prompt-test")
            self.assertEqual(pin["sampler_version"], "sampler-test")
            self.assertEqual(pin["corpus_hash"], "corpus-test")
            self.assertEqual(pin["sample_manifest_hash"], "manifest-test")
            self.assertEqual(blueprint.validate_profile(pinned, self.vocab), [])


class StyleAuditTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()
        self.profile = json.loads(
            (ROOT / "blueprints" / "archetypes" / "archetype_masculine_clear_mid.json")
            .read_text(encoding="utf-8")
        )
        self.profile["speech_style"] = {
            "register": "formal",
            "lexical_complexity": "advanced",
            "sentence_complexity": "complex",
            "colloquialism": "low",
            "discourse_style": "explanatory",
        }
        self.dossier = {
            "identity": {
                "canonical_id": "npc_77", "npc_id": 77, "name": "Audit Subject",
                "race": "Hyur", "clan": "Midlander", "sex": "male",
            },
            "dialogue_samples": [
                {"text": "First local line.", "source": "quest", "sheet": "Q/1",
                 "voice_line_id": "line-a"},
            ],
        }
        self.classification = {
            "canonical_id": "npc_77",
            "provider": "jev",
            "model_id": "jev-test",
            "status": "needs_review",
            "configuration": {
                "state_hash": "state-hash", "questions_hash": "questions-hash",
                "guardrails_hash": "guardrails-hash", "vocabulary_hash": "vocab-hash",
            },
            "speech_style": {},
            "speech_style_analysis": {
                "model_id": "jev-test",
                "model_revision": "rev-1",
                "endpoint": "http://127.0.0.1:8081/v1/systemone",
                "endpoint_class": "local",
                "taxonomy_version": "style-taxonomy-3.0",
                "classifier_version": "jev-style-classifier-3.0",
                "prompt_version": "style-prompt-3.0",
                "sampler_version": "dialogue-sampler-3.0",
                "evidence_policy_version": "style-evidence-3.0",
                "request_fingerprint": "request-fp",
                "sample_manifest_hash": "manifest-hash",
                "budget_reduction": {"removed": 0},
                "evidence": {"sampled_lines": 1, "sampled_words": 3, "contexts_covered": 1},
                "representative_sample": [
                    {"id": "LAAAAAAAAAAAAAAA", "text": "First local line."},
                ],
                "dimensions": {
                    "register": {
                        "value": "formal", "status": "supported", "evidence_tier": "supported",
                        "supporting_line_ids": ["LAAAAAAAAAAAAAAA"],
                        "counterexample_line_ids": [],
                        "patterns": [],
                        "rationale": "Conventional wording recurs.",
                        "citation_balance": 1.0,
                        "diagnostics": {
                            "raw_probabilities": {"formal": 0.8, "neutral": 0.2},
                            "raw_confidence": 0.8,
                            "model_self_reported_confidence": None,
                        },
                        "evidence": {
                            "sampled_lines": 1, "sampled_words": 3,
                            "context_count": 1, "field_multiplier": 1.0,
                            "thresholds": {"minimum": {"lines": 8, "words": 100, "contexts": 1}},
                        },
                    },
                },
                "experimental": {
                    "lexical_features": {"contraction_frequency": "low"},
                    "note": "audit only",
                },
            },
        }

    def test_review_report_reuses_production_plan_and_exact_sample(self):
        report = review_report(
            self.dossier, self.classification, self.profile, blueprint, self.vocab,
        )
        plan = blueprint.build_plan(self.profile, self.vocab)
        self.assertEqual(report["combined_instruction"], plan["instruct"])
        self.assertEqual(report["identity_instruction"], plan["identity_prompt"])
        self.assertEqual(report["style_instruction"], plan["style_prompt"])
        self.assertEqual(report["combined_instruct_hash"], plan["combined_instruct_hash"])
        self.assertEqual(report["design_seed"], plan["seed"])
        self.assertEqual(report["reference_text"], plan["reference"]["text"])
        self.assertEqual(report["jev_sample"][0]["id"], "LAAAAAAAAAAAAAAA")
        self.assertNotIn("rendered_neutral_instruction", report)

    def test_report_distinguishes_diagnostics_from_evidence_status(self):
        html_report = render_style_audit(
            review_report(
                self.dossier, self.classification, self.profile, blueprint, self.vocab,
            )
        )
        self.assertIn("application status", html_report)
        self.assertIn("evidence tier", html_report)
        self.assertIn("model diagnostics", html_report)
        self.assertIn("raw probability distribution", html_report)
        self.assertIn("model self-reported confidence", html_report)
        self.assertIn("not supplied", html_report)
        self.assertIn("Audit-only features", html_report)
        self.assertIn("Non-production", html_report)
        self.assertIn("LAAAAAAAAAAAAAAA", html_report)
        # The exact sample, not a reconstruction from the dossier, is shown.
        self.assertNotIn("First local line.", html_report.replace(
            "First local line.", "", 1
        ))
        for heading in ("Identity", "Style", "Combined"):
            self.assertIn(f"<h3>{heading}</h3>", html_report)

    def test_missing_sample_is_reported_not_reconstructed(self):
        self.classification["speech_style_analysis"].pop("representative_sample")
        report = review_report(
            self.dossier, self.classification, self.profile, blueprint, self.vocab,
        )
        self.assertEqual(report["jev_sample"], [])
        self.assertIn("does not reconstruct", report["jev_sample_status"])
        html_report = render_style_audit(report)
        self.assertIn("does not reconstruct", html_report)

    def test_audition_plans_share_seed_and_reference(self):
        plans = build_audition_plans(self.profile, self.vocab, seeds=3, seed_step=100)
        identity = [row for row in plans if row["condition"] == "identity_only"]
        styled = [row for row in plans if row["condition"] == "identity_style"]
        self.assertEqual(len(identity), 3)
        self.assertEqual([row["seed"] for row in identity], [row["seed"] for row in styled])
        self.assertEqual(identity[0]["seed"], blueprint.build_plan(self.profile, self.vocab)["seed"])
        for left, right in zip(identity, styled):
            self.assertEqual(left["reference"], right["reference"])
            self.assertEqual(left["design_model"], right["design_model"])
            self.assertEqual(left["instruct"], blueprint.render_identity(self.profile, self.vocab))
            self.assertEqual(right["instruct"], blueprint.build_plan(self.profile, self.vocab)["instruct"])
        self.assertEqual(set(blueprint.STYLE_FIELDS), {
            "register", "lexical_complexity", "sentence_complexity",
            "colloquialism", "discourse_style",
        })

    def test_mark_auditioned_records_condition_and_reference_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "identity_style.seed-7.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0\0" * 8000 * 10)
            pinned = mark_auditioned(
                self.profile,
                reference_file=wav,
                design_seed=7,
                by="listener",
                condition="identity_style",
                reference_file_path=str(wav),
            )
        pin = pinned["reference_pin"]
        self.assertEqual(pin["condition"], "identity_style")
        self.assertEqual(pin["reference_file"], str(wav))
        self.assertEqual(pin["design_seed"], 7)
        self.assertEqual(len(pin["reference_sha256"]), 64)
        self.assertEqual(pinned["lifecycle"]["status"], "auditioned")
        with self.assertRaises(ReviewError):
            mark_auditioned(
                self.profile, reference_file=wav, design_seed=7, by="listener",
                condition="identity_plus_mood",
            )

    def _pin_condition(self, condition: str) -> tuple[dict, dict]:
        vocabulary_path = ROOT / "blueprints" / "vocabulary.json"
        with tempfile.TemporaryDirectory() as tmp:
            # A styling clause is required for the two conditions to differ, so
            # the fixture carries real Style values rather than the bare archetype.
            profile = copy.deepcopy(self.profile)
            profile_path = Path(tmp) / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            plan = blueprint.build_plan(profile, self.vocab)
            self.assertNotEqual(plan["identity_prompt"], plan["instruct"])
            wav = Path(tmp) / "seed-5.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0\0" * 8000 * 9)
            out = Path(tmp) / "out"
            with redirect_stdout(io.StringIO()):
                main([
                    "review", "audition", str(profile_path),
                    "--out", str(out), "--by", "listener",
                    "--reference", str(wav), "--condition", condition,
                    "--vocabulary", str(vocabulary_path),
                ])
            snapshots = sorted((out / "profiles").glob("*.json"))
            self.assertEqual(len(snapshots), 1)
            pin = json.loads(snapshots[0].read_text())["reference_pin"]
        return pin, plan

    def test_pin_records_the_instruction_that_produced_the_wav(self):
        identity_pin, plan = self._pin_condition("identity_only")
        self.assertEqual(identity_pin["condition"], "identity_only")
        self.assertEqual(identity_pin["instruct"], plan["identity_prompt"])
        self.assertEqual(identity_pin["combined_instruct_hash"], plan["identity_prompt_hash"])

        style_pin, _ = self._pin_condition("identity_style")
        self.assertEqual(style_pin["condition"], "identity_style")
        self.assertEqual(style_pin["instruct"], plan["instruct"])
        self.assertEqual(style_pin["combined_instruct_hash"], plan["combined_instruct_hash"])
        self.assertEqual(identity_pin["reference_sha256"], style_pin["reference_sha256"])

    def test_audit_links_generated_reference_takes(self):
        report = {
            "voices": [
                {
                    "profile_id": "npc_77", "condition": "identity_only",
                    "takes": [{
                        "condition": "identity_only",
                        "ref": "voices/npc_77/identity_only/refs/seed-1.wav",
                        "seed": 1, "ref_seconds": 10.0, "ref_sha256": "a" * 64,
                        "instruct": "Identity only.", "ref_in_range": True,
                    }],
                },
                {
                    "profile_id": "npc_77", "condition": "identity_style",
                    "takes": [{
                        "condition": "identity_style",
                        "ref": "voices/npc_77/identity_style/refs/seed-1.wav",
                        "seed": 1, "ref_seconds": 11.0, "ref_sha256": "b" * 64,
                        "instruct": "Identity plus style.", "ref_in_range": True,
                    }],
                },
            ],
        }
        entries = audit_entries(report)
        self.assertEqual([entry["condition"] for entry in entries],
                         ["identity_only", "identity_style"])
        self.assertEqual(entries[0]["sha256"], "a" * 64)
        self.assertTrue(all(entry["in_range"] for entry in entries))
        html_report = render_style_audit(
            review_report(
                self.dossier, self.classification, self.profile, blueprint, self.vocab,
            ),
            entries,
        )
        self.assertIn("<audio controls", html_report)
        self.assertIn("identity_style", html_report)
        self.assertIn("Identity plus style.", html_report)


class EvidencePassTests(unittest.TestCase):
    """Citations come from a separate, explicitly recorded model pass."""

    IDs = [f"L{index:03d}" for index in range(6)]

    def setUp(self):
        self.vocab = blueprint.load_vocabulary()
        self.questions = build_questions(self.vocab)

    def _answers(self, **register) -> dict:
        answer = {
            "type": "choice",
            "choice": "formal",
            "supporting_line_ids": [],
            "counterexample_line_ids": [],
            "patterns": [],
            "rationale": "",
            "diagnostics": {"raw_probabilities": {"formal": 0.7, "neutral": 0.3}},
        }
        answer.update(register)
        return {"speech_style.register": answer}

    def _line_response(self, probabilities: dict[str, float]) -> dict:
        choice = max(probabilities, key=probabilities.get)
        return {
            "type": "choice",
            "choice": choice,
            "diagnostics": {
                "raw_probabilities": probabilities,
                "raw_confidence": probabilities[choice],
                "model_self_reported_confidence": None,
            },
        }

    def test_specs_cover_each_proposed_value_and_skip_abstentions(self):
        answers = self._answers()
        answers["speech_style.colloquialism"] = {
            "type": "choice", "choice": None, "diagnostics": {"raw_probabilities": {}},
        }
        specs = evidence_question_specs(answers, self.questions, self.IDs)
        self.assertEqual(
            set(specs),
            {"evidence.register.support", "evidence.register.counter"},
        )
        self.assertEqual(list(specs["evidence.register.support"]["criteria"]), self.IDs)
        for spec in specs.values():
            self.assertEqual(spec["type"], "choice")
            self.assertIn("formal", spec["instructions"])
        self.assertEqual(evidence_question_specs(answers, self.questions, []), {})

    def test_mixed_value_asks_one_question_per_candidate_pattern(self):
        answers = {
            "speech_style.register": {
                "type": "choice",
                "choice": "mixed",
                "diagnostics": {"raw_probabilities": {
                    "formal": 0.5, "conversational": 0.4, "neutral": 0.1, "mixed": 0.0,
                }},
            },
        }
        specs = evidence_question_specs(answers, self.questions, self.IDs)
        self.assertEqual(
            set(specs),
            {
                "evidence.register.pattern.formal",
                "evidence.register.pattern.conversational",
                "evidence.register.pattern.neutral",
            },
        )
        self.assertNotIn("evidence.register.support", specs)

    def test_selection_ranks_only_above_flat_probability(self):
        probabilities = {
            "L000": 0.55, "L001": 0.2, "L002": 0.1,
            "L003": 0.05, "L004": 0.05, "L005": 0.05,
        }
        selected = select_evidence(
            self._answers(),
            self.questions,
            {
                "evidence.register.support": self._line_response(probabilities),
                "evidence.register.counter": self._line_response(
                    {"L005": 0.5, "L004": 0.3, "L003": 0.2},
                ),
            },
        )
        citation = selected["fields"]["register"]
        self.assertEqual(citation["supporting_line_ids"], ["L000", "L001", "L002"])
        self.assertEqual(citation["counterexample_line_ids"], ["L005", "L004"])
        self.assertAlmostEqual(citation["citation_balance"], 0.6)
        self.assertEqual(selected["failures"], {})

    def test_selection_never_duplicates_a_line_across_sides(self):
        selected = select_evidence(
            self._answers(),
            self.questions,
            {
                "evidence.register.support": self._line_response({"L000": 0.6, "L001": 0.4}),
                "evidence.register.counter": self._line_response({"L000": 0.7, "L002": 0.3}),
            },
        )
        citation = selected["fields"]["register"]
        self.assertEqual(citation["supporting_line_ids"], ["L000", "L001"])
        self.assertEqual(citation["counterexample_line_ids"], ["L002"])

    def test_applied_citations_are_marked_as_model_derived(self):
        answers = self._answers()
        evidence = select_evidence(
            answers,
            self.questions,
            {
                "evidence.register.support": self._line_response(
                    {"L000": 0.5, "L001": 0.3, "L002": 0.2},
                ),
                "evidence.register.counter": self._line_response({"L003": 0.9, "L004": 0.1}),
            },
        )
        evidence["source"] = EVIDENCE_SOURCE
        evidence["version"] = "jev-evidence-pass-1.0"
        applied = apply_evidence(answers, evidence)
        answer = applied["speech_style.register"]
        self.assertEqual(answer["supporting_line_ids"], ["L000", "L001", "L002"])
        self.assertEqual(answer["diagnostics"]["evidence_source"], EVIDENCE_SOURCE)
        self.assertEqual(
            answer["diagnostics"]["evidence_questions"]["evidence.register.support"]["top_lines"][0],
            {"line_id": "L000", "probability": 0.5},
        )
        # The value answer itself is untouched apart from the citations.
        self.assertEqual(answers["speech_style.register"]["supporting_line_ids"], [])

    def test_pass_splits_a_failing_chunk_instead_of_losing_all_citations(self):
        calls = []

        class ChunkedProvider:
            name = "jev"
            model_id = "jev-test"

            def classify_state(self, state, questions):
                calls.append(sorted(questions))
                if len(questions) > 1:
                    raise JevResponseError("request exceeds the available context size")
                question_id = next(iter(questions))
                return {
                    "model": "jev-test",
                    "answers": {question_id: self_answer()},
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                }

        def self_answer():
            return {
                "type": "choice",
                "choice": "L000",
                "diagnostics": {
                    "raw_probabilities": {line_id: (1.0 if line_id == "L000" else 0.0)
                                          for line_id in self.IDs},
                    "raw_confidence": 1.0,
                    "model_self_reported_confidence": None,
                },
            }

        result = run_evidence_pass(
            ChunkedProvider(), {"dialogue_samples": []}, self._answers(), self.questions, self.IDs,
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["failures"], {})
        self.assertEqual(
            result["fields"]["register"]["supporting_line_ids"], ["L000"],
        )
        self.assertEqual(result["usage"]["input_tokens"], 20)

    def test_provider_without_request_state_records_a_failure(self):
        class OfflineProvider:
            name = "rules"
            model_id = "rules-3.0"

            def classify(self, dossier, questions):
                raise AssertionError("evidence pass must not fall back to classify")

        result = run_evidence_pass(
            OfflineProvider(), {"dialogue_samples": []}, self._answers(), self.questions, self.IDs,
        )
        self.assertEqual(
            set(result["failures"]),
            {"evidence.register.support", "evidence.register.counter"},
        )
        self.assertEqual(result["fields"], {})


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

    def test_jev_benchmark_runner_uses_identity_blind_state(self):
        root = Path(tempfile.mkdtemp())
        samples = [
            {"id": f"L{index:02d}", "text": "The gate opens at dawn and the road stays clear today.",
             "source": "quest", "sheet": "s"}
            for index in range(10)
        ]
        dossier = {
            "identity": {"canonical_id": "npc_1", "race": "Hyur", "sex": "female"},
            "statistics": {"dialogue_count": 10, "unique_line_count": 10},
            "dialogue_samples": samples,
        }
        dossier_path = root / "dossiers" / "B1.json"
        dossier_path.parent.mkdir(parents=True)
        dossier_path.write_text(json.dumps(dossier))
        (root / "manifest.json").write_text(json.dumps({
            "public_dossiers": [{
                "benchmark_id": "B1",
                "dossier_path": str(dossier_path),
                "input_hash": "h",
                "sample_line_ids": ["L00", "L01"],
            }],
            "model": {},
        }))
        captured = {}

        class FakeProvider:
            def __init__(self, **kwargs):
                pass

            def classify_state(self, state, questions):
                captured["state"] = state
                return response_for(questions, ["L00", "L01", "L02"])

        with patch.object(evaluation, "JevProvider", FakeProvider), patch(
            "sys.stdout", new=io.StringIO()
        ):
            report = evaluation.run_jev(root=root, limit=1)
        self.assertEqual(report["failure_count"], 0)
        self.assertNotIn("identity", captured["state"])
        self.assertNotIn("appearance", captured["state"])
        prediction = json.loads((root / "predictions" / "jev" / "B1.json").read_text())
        self.assertEqual(prediction["answers"]["speech_style.register"]["choice"], "formal")
        self.assertEqual(len(prediction["sample_line_ids"]), 10)

    def test_benchmark_rejects_citations_absent_from_the_request(self):
        answers = {
            "speech_style.register": {
                "type": "choice",
                "choice": "formal",
                "status": "supported",
                "supporting_line_ids": ["L00", "missing"],
                "counterexample_line_ids": [],
            },
            "speech_style.colloquialism": {
                "type": "choice",
                "choice": "mixed",
                "status": "supported",
                "supporting_line_ids": ["L00", "L01"],
                "counterexample_line_ids": [],
            },
        }
        evaluation._reject_invalid_jev_citations(
            answers, {"L00", "L01", "L02"},
        )
        self.assertIsNone(answers["speech_style.register"]["choice"])
        self.assertEqual(answers["speech_style.register"]["status"], "rejected")
        self.assertIsNone(answers["speech_style.colloquialism"]["choice"])
        self.assertEqual(answers["speech_style.colloquialism"]["status"], "rejected")
        self.assertEqual(
            evaluation._prediction_label({
                "choice": None, "status": "insufficient_evidence",
            }),
            "insufficient_evidence",
        )

    def test_benchmark_mixed_requires_two_patterns_across_contexts(self):
        answer = {
            "type": "choice",
            "choice": "mixed",
            "status": "supported",
            "supporting_line_ids": [],
            "counterexample_line_ids": [],
            "patterns": [
                {"label": "formal", "line_ids": ["L00", "L01", "L02"]},
                {"label": "conversational", "line_ids": ["L03", "L04", "L05"]},
            ],
        }
        same_context = {line_id: "quest::a" for line_id in ["L00", "L01", "L02", "L03", "L04", "L05"]}
        rejected = {"speech_style.register": dict(answer)}
        evaluation._reject_invalid_jev_citations(
            rejected, set(same_context), sample_contexts=same_context,
        )
        self.assertIsNone(rejected["speech_style.register"]["choice"])
        self.assertEqual(rejected["speech_style.register"]["status"], "rejected")

        split_context = {
            "L00": "quest::a", "L01": "quest::a", "L02": "quest::a",
            "L03": "quest::b", "L04": "quest::b", "L05": "quest::b",
        }
        accepted = {"speech_style.register": dict(answer)}
        evaluation._reject_invalid_jev_citations(
            accepted, set(split_context), sample_contexts=split_context,
        )
        self.assertEqual(accepted["speech_style.register"]["choice"], "mixed")
        self.assertEqual(accepted["speech_style.register"]["status"], "supported")

    def test_text_only_state_withholds_measurements(self):
        dossier = {"benchmark_id": "B1", "dialogue_samples": [
            {"id": "B1-L01", "text": "The gate opens at dawn.", "source": "secret-source", "sheet": "secret-sheet"},
            {"id": "B1-L02", "text": "We will be ready.", "source": "secret-source", "sheet": "secret-sheet"},
        ]}
        assisted = evaluation.build_speech_style_state(dossier, condition="assisted")
        text_only = evaluation.build_speech_style_state(dossier, condition="text_only")
        self.assertIn("measurements", assisted)
        self.assertNotIn("measurements", text_only)
        self.assertEqual(assisted["dialogue_samples"], text_only["dialogue_samples"])
        self.assertNotIn("secret-source", json.dumps(text_only))
        self.assertNotIn("secret-sheet", json.dumps(text_only))
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
        order = evaluation.ORDINAL_FIELD_LABELS["lexical_complexity"]
        metrics = evaluation._ordinal_metrics(["plain", "advanced", "mixed", "insufficient_evidence"], ["ordinary", "advanced", "mixed", "plain"], order)
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
            "lexical_complexity": "advanced",
            "sentence_complexity": "complex",
            "colloquialism": "low",
            "discourse_style": "explanatory",
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

    def test_completed_adjudication_scores_new_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = {
                "B1": {
                    "sample_count": 2,
                    "dialogue_samples": [
                        {"id": "B1-L01", "text": "The gate opens at dawn."},
                        {"id": "B1-L02", "text": "We will be ready."},
                    ],
                },
                "B2": {
                    "sample_count": 2,
                    "dialogue_samples": [
                        {"id": "B2-L01", "text": "The road is long."},
                        {"id": "B2-L02", "text": "Keep moving."},
                    ],
                },
            }
            (root / "dossiers").mkdir()
            public, selection = [], []
            for benchmark_id, item in items.items():
                path = root / "dossiers" / f"{benchmark_id}.json"
                path.write_text(json.dumps({"benchmark_id": benchmark_id, "dialogue_samples": item["dialogue_samples"]}))
                public.append({"benchmark_id": benchmark_id, "dossier_path": str(path)})
                selection.append({
                    "benchmark_id": benchmark_id,
                    "split": "locked_test" if benchmark_id == "B1" else "development",
                    "stratum": "5-20",
                    "source_dossier_path": str(path),
                })
            (root / "manifest.json").write_text(json.dumps({
                "public_dossiers": public,
                "private_selection": selection,
                "annotation_scopes": {"full": {"item_ids": ["B1", "B2"]}},
            }))
            (root / "annotations").mkdir()
            for name in ("annotator_a", "annotator_b"):
                (root / "annotations" / f"{name}.json").write_text(json.dumps(self._form(items)))
            (root / "adjudicated").mkdir()
            (root / "adjudicated" / "gold.json").write_text(json.dumps({
                "items": [
                    {
                        "benchmark_id": benchmark_id,
                        "adjudicated_labels": {
                            field: next(iter(options)) for field, options in evaluation.FIELDS.items()
                        },
                    }
                    for benchmark_id in items
                ],
            }))
            prediction_dir = root / "predictions" / "rules_full"
            prediction_dir.mkdir(parents=True)
            (prediction_dir / "B1.json").write_text(json.dumps({
                "answers": {
                    field: {"choice": next(iter(options))}
                    for field, options in evaluation.FIELDS.items()
                },
            }))
            report = evaluation.evaluate_benchmark(root=root, scope="full")
            self.assertEqual(report["annotation_status"], "complete_and_adjudicated")
            metrics = report["gold_metrics"]["rules_full"]["fields"]["register"]
            self.assertIn("selective_accuracy", metrics)
            self.assertIn("cohen_kappa", metrics)
            self.assertIn("mixed_detection", metrics)
            self.assertEqual(metrics["selective_accuracy"]["n"], 1)

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
        self.assertNotIn("dialogue_measurements", state)
        self.assertNotIn("interpretation", json.dumps(state))
        self.assertNotIn("identity", state)
        self.assertNotIn("voice_evidence", state)
        self.assertEqual(state["dialogue_coverage"]["sampled_lines"], 1)
        self.assertEqual(round(state["dialogue_coverage"]["coverage_of_unique_lines"], 3), 0.001)
        self.assertEqual(state["dialogue_coverage"]["sampled_source_count"], 1)
        self.assertEqual(state["dialogue_coverage"]["sampled_context_count"], 1)
        self.assertIn("deterministic subset", state["dialogue_coverage"]["warning"])
        serialized = json.dumps(state)
        self.assertNotIn("quest", serialized)
        self.assertNotIn("sheet", serialized)
        self.assertNotIn("sources", state["evidence_sufficiency"])
        self.assertNotIn("signals", state["dialogue_samples"][0])
        self.assertRegex(state["dialogue_samples"][0]["id"], r"^L[0-9A-F]{16}$")
        self.assertEqual(state["dialogue_samples"], compact_state(dossier)["dialogue_samples"])
        text_only = compact_state({
            "identity": {"canonical_id": "npc_text_only"},
            "statistics": {"dialogue_count": 2, "unique_line_count": 2},
            "dialogue_samples": [
                {"text": "First distinct line."},
                {"text": "Second distinct line."},
            ],
        })
        ids = [sample["id"] for sample in text_only["dialogue_samples"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("lore_quotes", state)
        self.assertNotIn("mood", " ".join(state["guardrails"]).lower())
        assisted = compact_state(dossier, include_measurements=True)
        self.assertEqual(assisted["dialogue_measurements"]["signal_line_rates"]["dialect_lines"], 0.4)

    def test_request_state_never_carries_source_or_sheet_names(self):
        dossier = {
            "identity": {"canonical_id": "npc_secret"},
            "statistics": {"dialogue_count": 3, "unique_line_count": 3},
            "dialogue_samples": [
                {"text": "Hello there.", "source": "msq_7_1_spoiler", "sheet": "secret_sheet"},
            ],
        }
        state = compact_state(dossier)
        serialized = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("msq_7_1_spoiler", serialized)
        self.assertNotIn("secret_sheet", serialized)
        self.assertEqual(set(state["dialogue_samples"][0]), {"id", "text"})

    def test_request_fingerprint_tracks_state_endpoint_and_model_revision(self):
        state = {"dialogue_samples": [{"id": "L1", "text": "Hello."}]}
        questions = build_questions(self.vocab)
        base = request_fingerprint(
            provider="jev", endpoint="http://127.0.0.1:8081/v1/systemone",
            model="jev-latest", state=state, questions=questions,
            vocabulary=self.vocab,
        )
        changed_state = {"dialogue_samples": [{"id": "L1", "text": "Hello!"}]}
        self.assertNotEqual(base, request_fingerprint(
            provider="jev", endpoint="http://127.0.0.1:8081/v1/systemone",
            model="jev-latest", state=changed_state, questions=questions,
            vocabulary=self.vocab,
        ))
        self.assertNotEqual(base, request_fingerprint(
            provider="jev", endpoint="http://127.0.0.1:8082/v1/systemone",
            model="jev-latest", state=state, questions=questions,
            vocabulary=self.vocab,
        ))
        self.assertNotEqual(base, request_fingerprint(
            provider="jev", endpoint="http://127.0.0.1:8081/v1/systemone",
            model="jev-latest", state=state, questions=questions,
            vocabulary=self.vocab, resolved_model_revision="rev-2",
        ))
        changed_questions = json.loads(json.dumps(questions))
        changed_questions["speech_style.register"]["instructions"] += " Changed."
        self.assertNotEqual(base, request_fingerprint(
            provider="jev", endpoint="http://127.0.0.1:8081/v1/systemone",
            model="jev-latest", state=state, questions=changed_questions,
            vocabulary=self.vocab,
        ))
        changed_vocabulary = json.loads(json.dumps(self.vocab))
        changed_vocabulary["speech_style"]["dimensions"]["register"]["values"]["formal"]["render"] = "very formal"
        self.assertNotEqual(base, request_fingerprint(
            provider="jev", endpoint="http://127.0.0.1:8081/v1/systemone",
            model="jev-latest", state=state, questions=questions,
            vocabulary=changed_vocabulary,
        ))
        self.assertNotEqual(base, request_fingerprint(
            provider="jev", endpoint="http://127.0.0.1:8081/v1/systemone",
            model="jev-latest", state=state, questions=questions,
            vocabulary=self.vocab, guardrails=("Changed guardrail.",),
        ))
        for name in (
            "STYLE_TAXONOMY_VERSION",
            "STYLE_CLASSIFIER_VERSION",
            "STYLE_PROMPT_VERSION",
            "SPEECH_STYLE_SCHEMA_VERSION",
            "DIALOGUE_SAMPLER_VERSION",
            "STYLE_EVIDENCE_POLICY_VERSION",
        ):
            with self.subTest(name=name):
                with patch(f"blueprint_pipeline.jev.{name}", "changed-version"):
                    self.assertNotEqual(base, request_fingerprint(
                        provider="jev", endpoint="http://127.0.0.1:8081/v1/systemone",
                        model="jev-latest", state=state, questions=questions,
                        vocabulary=self.vocab,
                    ))
        self.assertEqual(
            request_fingerprint(
                provider="jev",
                endpoint="http://user:secret@127.0.0.1:8081/v1/systemone?token=abc",
                model="jev-latest", state=state, questions=questions,
                vocabulary=self.vocab,
            ),
            base,
        )

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
        self.assertTrue(state["budget_reduction"]["applied"])
        self.assertEqual(state["budget_reduction"]["original_samples"], 20)
        self.assertEqual(state["budget_reduction"]["final_samples"], len(state["dialogue_samples"]))
        self.assertEqual(
            state["sample_manifest_hash"],
            sample_manifest_hash("npc_2", state["dialogue_samples"]),
        )
        self.assertEqual(
            state["dialogue_coverage"]["sampled_words"],
            sum(len(sample["text"].split()) for sample in state["dialogue_samples"]),
        )
        self.assertEqual(
            state["dialogue_coverage"]["sampled_lines"], len(state["dialogue_samples"])
        )

    def test_representative_sampler_can_fill_the_strong_line_floor(self):
        lines = [
            {
                "voice_line_id": str(index),
                "text": " ".join(f"w{index}_{part}" for part in range(12)),
            }
            for index in range(MAX_DIALOGUE_SAMPLE_COUNT + 20)
        ]
        samples = select_representative_samples("npc", lines)
        self.assertEqual(len(samples), MAX_DIALOGUE_SAMPLE_COUNT)

    def test_strong_tier_is_reachable_and_contexts_are_the_diversity_gate(self):
        self.assertGreaterEqual(
            MAX_DIALOGUE_SAMPLE_COUNT, STYLE_EVIDENCE_TIERS["strong"]["lines"],
        )
        strong_samples = [
            {
                "text": "word " * 15,
                "source": f"source-{index % 3}",
                "sheet": f"sheet-{index % 3}",
            }
            for index in range(40)
        ]
        strong = evidence_sufficiency(
            {"unique_line_count": 40}, strong_samples,
        )
        self.assertEqual(strong["tier"], "strong")
        self.assertEqual(strong["source_count"], 3)
        self.assertEqual(strong["context_count"], 3)

        one_source_two_sheets = [
            {
                "text": "word " * 15,
                "source": "one-source",
                "sheet": f"sheet-{index % 2}",
            }
            for index in range(20)
        ]
        supported = evidence_sufficiency(
            {"unique_line_count": 20}, one_source_two_sheets,
        )
        self.assertEqual(supported["tier"], "supported")
        self.assertEqual(supported["source_count"], 1)
        self.assertEqual(supported["context_count"], 2)

        two_sources_one_sheet = [
            {
                "text": "word " * 15,
                "source": f"source-{index % 2}",
                "sheet": "one-sheet",
            }
            for index in range(20)
        ]
        supported_by_source = evidence_sufficiency(
            {"unique_line_count": 20}, two_sources_one_sheet,
        )
        # A source/sheet pair is the independent context. Two sources with
        # the same sheet name are still two independent contexts.
        self.assertEqual(supported_by_source["tier"], "supported")
        self.assertEqual(supported_by_source["source_count"], 2)
        self.assertEqual(supported_by_source["context_count"], 2)

    def test_unknown_and_repeated_context_names_do_not_inflate_diversity(self):
        unknown = [
            {"text": "word " * 15, "source": "unknown", "sheet": "unknown"},
            {"text": "word " * 15, "source": "", "sheet": "n/a"},
            {"text": "word " * 15},
        ]
        tier = evidence_sufficiency({"unique_line_count": 3}, unknown)
        self.assertEqual(tier["context_count"], 0)
        self.assertEqual(tier["source_count"], 0)

        repeated_exports = [
            {"text": "word " * 15, "source": "Quest", "sheet": "Sheet_A"},
            {"text": "word " * 15, "source": "quest", "sheet": "sheet_a"},
            {"text": "word " * 15, "source": "QUEST", "sheet": "SHEET_A"},
        ]
        tier = evidence_sufficiency({"unique_line_count": 3}, repeated_exports)
        self.assertEqual(tier["context_count"], 1)
        self.assertEqual(tier["source_count"], 1)

    def test_budget_trim_preserves_length_and_context_strata(self):
        samples = [
            {
                "id": f"S{index:02d}",
                "text": "word " * 100,
                "source": "large-source",
                "sheet": "common-sheet",
                "length_bucket": "short",
            }
            for index in range(10)
        ]
        samples.append({
            "id": "L00",
            "text": "word " * 100,
            "source": "small-source",
            "sheet": "rare-sheet",
            "length_bucket": "long",
        })
        dossier = {
            "identity": {"canonical_id": "npc_3"},
            "statistics": {"dialogue_count": 11, "unique_line_count": 11},
            "dialogue_samples": samples,
        }
        full = compact_state(dossier, max_bytes=10**9)
        max_bytes = int(len(json.dumps(full, ensure_ascii=False).encode("utf-8")) * 0.6)
        trimmed = compact_state(dossier, max_bytes=max_bytes)
        self.assertLess(len(trimmed["dialogue_samples"]), 11)
        reduction = trimmed["budget_reduction"]
        self.assertTrue(reduction["applied"])
        self.assertEqual(reduction["strategy"], "balanced_length_and_context")
        self.assertEqual(reduction["lost_context_count"], 0)
        self.assertEqual(reduction["lost_length_buckets"], [])
        self.assertEqual(trimmed["dialogue_coverage"]["sampled_source_count"], 2)
        self.assertEqual(trimmed["dialogue_coverage"]["sampled_context_count"], 2)
        self.assertIn("short", reduction["length_buckets_retained"])
        self.assertIn("long", reduction["length_buckets_retained"])

    def test_budget_trim_reports_unavoidable_coverage_loss(self):
        samples = [
            {
                "id": "A00",
                "text": "word " * 100,
                "source": "source-a",
                "sheet": "sheet-a",
                "length_bucket": "short",
            },
            {
                "id": "B00",
                "text": "word " * 100,
                "source": "source-b",
                "sheet": "sheet-b",
                "length_bucket": "long",
            },
        ]
        dossier = {
            "identity": {"canonical_id": "npc_4"},
            "statistics": {"dialogue_count": 2, "unique_line_count": 2},
            "dialogue_samples": samples,
        }
        one = compact_state(
            {**dossier, "dialogue_samples": samples[:1]}, max_bytes=10**9,
        )
        one_size = len(json.dumps(one, ensure_ascii=False).encode("utf-8"))
        trimmed = compact_state(dossier, max_bytes=one_size + 250)
        self.assertEqual(len(trimmed["dialogue_samples"]), 1)
        reduction = trimmed["budget_reduction"]
        self.assertEqual(reduction["reason"], "max_state_bytes")
        self.assertEqual(reduction["lost_source_count"], 1)
        self.assertEqual(reduction["lost_context_count"], 1)
        self.assertEqual(len(reduction["lost_length_buckets"]), 1)

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

    def test_targeted_pool_finds_habits_without_biasing_representative_pool(self):
        lines = [
            {"voice_line_id": f"g{i}", "text": f"The road is long and the sky is grey number {i}.", "source": "quest", "sheet": "s"}
            for i in range(12)
        ] + [
            {"voice_line_id": "d1", "text": "Aye, I've not seen yer like here before, an' that's the truth of it.", "source": "quest", "sheet": "s"},
        ]
        representative = select_representative_samples("npc_4", lines, max_count=3, vocabulary=self.vocab)
        targeted = select_targeted_samples("npc_4", lines, max_count=3, vocabulary=self.vocab)
        self.assertEqual(targeted[0]["voice_line_id"], "d1")
        self.assertTrue(any(signal.startswith("dialect:") for signal in targeted[0]["signals"]))
        self.assertTrue(all("signals" not in sample for sample in representative))

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
            "lexical_complexity": "advanced",
            "sentence_complexity": "complex",
            "colloquialism": "low",
            "discourse_style": "explanatory",
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


class StyleDecisionContractTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()

    def _dossier(self, count: int = 30) -> dict:
        samples = [
            {
                "id": f"L{index:03d}",
                "text": "The gate opens at dawn and the road stays clear today.",
                "source": "quest" if index % 2 else "custom",
                "sheet": f"sheet-{index % 3}",
            }
            for index in range(count)
        ]
        return {
            "input_hash": "dossier-hash",
            "identity": {
                "canonical_id": "npc_30", "npc_id": 30, "name": "Test",
                "resolution_status": "eligible", "identity_ambiguous": False,
                "has_recorded_voice": False, "has_se_voice_reference": False,
            },
            "statistics": {"dialogue_count": 100, "unique_line_count": 100},
            "dialogue_analysis": {
                "measurements": {"word_count": count * 12, "raw_line_count": count},
                "interpretation": {
                    "register": "formal",
                    "lexical_complexity": "advanced",
                    "sentence_complexity": "complex",
                    "colloquialism": "low",
                    "discourse_style": "explanatory",
                },
            },
            "dialogue_samples": samples,
        }

    class CitedRules(RulesProvider):
        def classify(self, dossier, questions):
            result = super().classify(dossier, questions)
            ids = [sample["id"] for sample in dossier["dialogue_samples"]]
            for answer in result["answers"].values():
                answer["supporting_line_ids"] = ids[:2]
                answer["status"] = "supported"
            return result

    class CitedJev(CitedRules):
        name = "jev"
        model_id = "jev-test"

    def test_supported_and_provisional_fields_are_separated_without_defaults(self):
        dossier = self._dossier()
        result = classify_dossier(dossier, self.CitedRules(self.vocab), self.vocab)
        self.assertEqual(result["status"], "ready_for_audition")
        self.assertEqual(result["profile_allocation"]["decision"], "individual_design")
        self.assertEqual(result["speech_style"]["register"]["status"], "supported")
        self.assertEqual(result["speech_style"]["discourse_style"]["status"], "provisional")
        self.assertEqual(result["speech_style"]["discourse_style"]["value"], "explanatory")
        self.assertIn("discourse_style", result["profile_allocation"]["provisional_style_fields"])

        casting = cast_identity(dossier["identity"])
        profile = build_individual_profile(dossier, result, casting, self.vocab)
        self.assertEqual(
            set(profile["speech_style"]),
            {"register", "lexical_complexity", "sentence_complexity", "colloquialism"},
        )
        self.assertNotIn("discourse_style", profile["speech_style"])
        self.assertEqual(
            profile["speech_style_analysis"]["fields"]["discourse_style"]["status"],
            "provisional",
        )
        style = blueprint.render_style(profile, self.vocab)
        self.assertIn("formal in register", style)
        self.assertNotIn("discourse", style)

    def test_recorded_sample_uses_request_ids_when_dossier_rows_have_no_id(self):
        dossier = self._dossier()
        for sample in dossier["dialogue_samples"]:
            sample.pop("id")
        state = compact_state(dossier, vocabulary=self.vocab)
        result = classify_dossier(
            dossier, RulesProvider(self.vocab), self.vocab, request_state=state,
        )
        recorded = result["speech_style_analysis"]["representative_sample"]
        # Exact ordered copy of what the model received, not a re-derived set.
        self.assertEqual(recorded, state["dialogue_samples"])
        self.assertEqual(
            [row["text"] for row in recorded],
            [row["text"] for row in state["dialogue_samples"]],
        )

    def test_trimmed_request_state_downgrades_field_evidence(self):
        dossier = self._dossier(count=40)
        full = classify_dossier(dossier, RulesProvider(self.vocab), self.vocab)
        self.assertIn(
            full["speech_style_analysis"]["evidence"]["field_tiers"]["register"],
            {"supported", "strong"},
        )
        trimmed_state = compact_state(dossier, max_bytes=3000, vocabulary=self.vocab)
        trimmed = classify_dossier(
            dossier,
            RulesProvider(self.vocab),
            self.vocab,
            request_state=trimmed_state,
        )
        analysis = trimmed["speech_style_analysis"]["evidence"]
        self.assertLess(analysis["sampled_lines"], 40)
        self.assertNotEqual(analysis["field_tiers"]["register"], "strong")
        self.assertLessEqual(
            len(trimmed_state["dialogue_samples"]),
            full["speech_style_analysis"]["evidence"]["sampled_lines"],
        )

    def test_rules_provider_without_rule_result_returns_null(self):
        dossier = {
            "input_hash": "dossier-hash",
            "identity": {
                "canonical_id": "npc_31", "npc_id": 31, "name": "Test",
                "resolution_status": "eligible", "identity_ambiguous": False,
                "has_recorded_voice": False, "has_se_voice_reference": False,
            },
            "statistics": {"dialogue_count": 30, "unique_line_count": 30},
            "dialogue_analysis": {"interpretation": {}},
            "dialogue_samples": self._dossier()["dialogue_samples"],
        }
        result = classify_dossier(dossier, RulesProvider(self.vocab), self.vocab)
        for field, decision in result["speech_style"].items():
            self.assertIsNone(decision["value"], field)
            self.assertIn(decision["status"], {"unresolved", "insufficient_evidence", "rejected"}, field)
            self.assertEqual(decision["method"], "rules_baseline")

    def test_mixed_survives_aggregation_and_rendering(self):
        class MixedJev(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                ids = [sample["id"] for sample in dossier["dialogue_samples"]]
                result["answers"]["speech_style.register"].update(
                    choice="mixed", status="supported",
                    supporting_line_ids=[],
                    counterexample_line_ids=[],
                    patterns=[
                        {"label": "formal", "line_ids": ids[0:3]},
                        {"label": "conversational", "line_ids": ids[3:6]},
                    ],
                )
                return result

        dossier = self._dossier()
        result = classify_dossier(dossier, MixedJev(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "supported")
        self.assertEqual(result["speech_style"]["register"]["value"], "mixed")
        casting = cast_identity(dossier["identity"])
        profile = build_individual_profile(dossier, result, casting, self.vocab)
        self.assertEqual(profile["speech_style"]["register"], "mixed")
        self.assertIn("mixed in register", blueprint.render_style(profile, self.vocab))

        class AmbiguousStatus(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"].update(
                    choice="formal", status="mixed",
                    supporting_line_ids=[sample["id"] for sample in dossier["dialogue_samples"]][:2],
                )
                return result

        result = classify_dossier(dossier, AmbiguousStatus(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")
        self.assertIsNone(result["speech_style"]["register"]["value"])
        self.assertIn("at least 3 cited lines", result["speech_style"]["register"]["rationale"])

    def test_model_reported_status_never_controls_acceptance(self):
        class SelfDoubt(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                ids = [sample["id"] for sample in dossier["dialogue_samples"]]
                result["answers"]["speech_style.register"].update(
                    choice="formal",
                    status="insufficient_evidence",
                    supporting_line_ids=ids[:3],
                )
                return result

        result = classify_dossier(self._dossier(), SelfDoubt(self.vocab), self.vocab)
        decision = result["speech_style"]["register"]
        self.assertEqual(decision["status"], "supported")
        self.assertEqual(decision["value"], "formal")
        self.assertEqual(decision["model_reported_status"], "insufficient_evidence")

    def test_legacy_mixed_status_promotes_only_a_mixed_value(self):
        dossier = self._dossier()
        casting = cast_identity(dossier["identity"])
        ambiguous = {
            "schema_version": "2.0",
            "speech_style": {
                "register": {"value": "formal", "status": "mixed"},
                "colloquialism": {"value": "mixed", "status": "mixed"},
            },
        }
        profile = build_individual_profile(dossier, ambiguous, casting, self.vocab)
        self.assertNotIn("register", profile["speech_style"])
        self.assertEqual(profile["speech_style"]["colloquialism"], "mixed")
        self.assertNotIn("register", profile["speech_style_analysis"]["fields"])
        self.assertEqual(
            profile["speech_style_analysis"]["fields"]["colloquialism"]["status"],
            "supported",
        )

        analyzed = {
            "schema_version": "2.0",
            "speech_style": {
                "register": {"value": "formal", "status": "mixed"},
                "colloquialism": {"value": "mixed", "status": "mixed"},
            },
            "speech_style_analysis": {
                "dimensions": {
                    "register": {"value": "formal", "status": "mixed"},
                    "colloquialism": {"value": "mixed", "status": "mixed"},
                },
            },
        }
        profile = build_individual_profile(dossier, analyzed, casting, self.vocab)
        self.assertNotIn("register", profile["speech_style"])
        self.assertEqual(profile["speech_style"]["colloquialism"], "mixed")
        self.assertEqual(
            profile["speech_style_analysis"]["fields"]["register"]["status"],
            "unresolved",
        )
        self.assertEqual(
            profile["speech_style_analysis"]["fields"]["register"]["value"],
            "formal",
        )
        self.assertEqual(
            profile["speech_style_analysis"]["fields"]["colloquialism"]["status"],
            "supported",
        )

    def test_jev_citations_are_checked_against_budget_trimmed_request(self):
        samples = [
            {
                "id": f"L{index:03d}",
                "text": "word " * 100,
                "source": "quest" if index % 2 else "custom",
                "sheet": f"sheet-{index % 3}",
            }
            for index in range(300)
        ]
        dossier = self._dossier(count=300)
        dossier["dialogue_samples"] = samples
        dossier["statistics"] = {"dialogue_count": 300, "unique_line_count": 300}

        class DroppedCitation(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                sent = {
                    sample["id"]
                    for sample in compact_state(dossier, vocabulary=self.vocabulary)["dialogue_samples"]
                }
                dropped = [
                    sample["id"]
                    for sample in dossier["dialogue_samples"]
                    if sample["id"] not in sent
                ][:2]
                result["answers"]["speech_style.register"].update(
                    supporting_line_ids=dropped,
                )
                return result

        result = classify_dossier(dossier, DroppedCitation(self.vocab), self.vocab)
        decision = result["speech_style"]["register"]
        self.assertEqual(decision["status"], "rejected")
        self.assertIn("absent from the request", decision["rationale"])
        sent_ids = {
            sample["id"] for sample in compact_state(dossier, vocabulary=self.vocab)["dialogue_samples"]
        }
        kept = [sample for sample in samples if sample["id"] in sent_ids]
        self.assertLess(len(kept), len(samples))
        self.assertEqual(
            result["speech_style_analysis"]["evidence"]["sampled_lines"], len(kept),
        )
        self.assertEqual(
            result["speech_style_analysis"]["sample_manifest_hash"],
            sample_manifest_hash("npc_30", kept),
        )
        reduction = result["speech_style_analysis"]["budget_reduction"]
        self.assertTrue(reduction["applied"])
        self.assertEqual(reduction["final_samples"], len(kept))

    def test_current_classification_without_status_is_not_promoted(self):
        dossier = self._dossier()
        result = classify_dossier(dossier, self.CitedRules(self.vocab), self.vocab)
        del result["speech_style"]["register"]["status"]
        casting = cast_identity(dossier["identity"])
        profile = build_individual_profile(dossier, result, casting, self.vocab)
        self.assertNotIn("register", profile["speech_style"])

    def test_insufficient_sample_yields_null_fields_not_first_options(self):
        dossier = self._dossier(count=3)
        result = classify_dossier(dossier, self.CitedRules(self.vocab), self.vocab)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["profile_allocation"]["decision"], "shared_archetype")
        for field, decision in result["speech_style"].items():
            self.assertIsNone(decision["value"], field)
            self.assertEqual(decision["status"], "insufficient_evidence", field)
        casting = cast_identity(dossier["identity"])
        profile = build_individual_profile(dossier, result, casting, self.vocab)
        self.assertIsNone(profile["speech_style"])
        self.assertEqual(blueprint.render_style(profile, self.vocab), "")
        plan = blueprint.build_plan(profile, self.vocab)
        self.assertNotIn("The wording is", plan["instruct"])

    def test_invalid_citation_and_mixed_gates_are_rejected(self):
        class BadCitation(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"]["supporting_line_ids"] = ["not-in-request"]
                return result

        dossier = self._dossier()
        result = classify_dossier(dossier, BadCitation(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")
        self.assertIsNone(result["speech_style"]["register"]["value"])
        self.assertNotIn("rules_baseline_estimate", result["speech_style"]["register"])

        ids = [sample["id"] for sample in dossier["dialogue_samples"]]

        class MixedWithoutPatterns(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"].update(
                    choice="mixed", status="supported",
                    patterns=[],
                )
                return result

        result = classify_dossier(dossier, MixedWithoutPatterns(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")
        self.assertIsNone(result["speech_style"]["register"]["value"])

        class MixedSinglePattern(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"].update(
                    choice="mixed", status="supported",
                    patterns=[{"label": "formal", "line_ids": ids[:3]}],
                )
                return result

        result = classify_dossier(dossier, MixedSinglePattern(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")

        class MixedPatternTooShort(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"].update(
                    choice="mixed", status="supported",
                    patterns=[
                        {"label": "formal", "line_ids": ids[:2]},
                        {"label": "conversational", "line_ids": ids[2:5]},
                    ],
                )
                return result

        result = classify_dossier(dossier, MixedPatternTooShort(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")
        self.assertIn("at least 3 cited lines", result["speech_style"]["register"]["rationale"])

        class MixedForgedPattern(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"].update(
                    choice="mixed", status="supported",
                    patterns=[
                        {"label": "formal", "line_ids": ids[:3]},
                        {"label": "conversational", "line_ids": ["not-in-request", ids[3], ids[4]]},
                    ],
                )
                return result

        result = classify_dossier(dossier, MixedForgedPattern(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")
        self.assertIn("absent from the request", result["speech_style"]["register"]["rationale"])

        class MixedDuplicatePattern(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"].update(
                    choice="mixed", status="supported",
                    patterns=[
                        {"label": "formal", "line_ids": ids[:3]},
                        {"label": "conversational", "line_ids": ids[2:5]},
                    ],
                )
                return result

        result = classify_dossier(dossier, MixedDuplicatePattern(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")
        self.assertIn("more than one mixed pattern", result["speech_style"]["register"]["rationale"])

        class MixedOneContext(self.CitedJev):
            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                result["answers"]["speech_style.register"].update(
                    choice="mixed", status="supported",
                    supporting_line_ids=[],
                    counterexample_line_ids=[],
                    patterns=[
                        {"label": "formal", "line_ids": ids[:3]},
                        {"label": "conversational", "line_ids": ids[3:6]},
                    ],
                )
                return result

        one_context = self._dossier()
        for sample in one_context["dialogue_samples"]:
            sample["source"] = "quest"
            sample["sheet"] = "one"
        result = classify_dossier(one_context, MixedOneContext(self.vocab), self.vocab)
        self.assertEqual(result["speech_style"]["register"]["status"], "rejected")
        self.assertIn("two independent contexts", result["speech_style"]["register"]["rationale"])

    def test_jev_abstention_is_preserved_and_invalid_status_is_rejected(self):
        questions = build_questions(self.vocab)
        payload = response_for(questions)
        payload["answers"]["speech_style.register"] = {
            "type": "choice",
            "choice": None,
            "status": "insufficient_evidence",
            "probabilities": {},
        }
        normalized = validate_response(payload, questions)
        self.assertIsNone(normalized["answers"]["speech_style.register"]["choice"])
        self.assertEqual(
            normalized["answers"]["speech_style.register"]["model_reported_status"],
            "insufficient_evidence",
        )

        payload["answers"]["speech_style.register"]["status"] = "guessed"
        with self.assertRaisesRegex(JevResponseError, "invalid status"):
            validate_response(payload, questions)

        payload["answers"]["speech_style.register"] = {
            "type": "choice",
            "choice": "formal",
            "status": "mixed",
            "probabilities": {key: float(key == "formal") for key in questions["speech_style.register"]["criteria"]},
            "supporting_line_ids": ["L1", "L2"],
        }
        with self.assertRaisesRegex(JevResponseError, "invalid status"):
            validate_response(payload, questions)

        payload["answers"]["speech_style.register"] = {
            "type": "choice",
            "choice": "formal",
            "probabilities": {key: float(key == "formal") for key in questions["speech_style.register"]["criteria"]},
            "supporting_line_ids": ["L1", "L2"],
        }
        normalized = validate_response(payload, questions)
        self.assertIsNone(
            normalized["answers"]["speech_style.register"]["model_reported_status"]
        )
        self.assertEqual(normalized["answers"]["speech_style.register"]["choice"], "formal")

        payload["answers"]["speech_style.register"] = {
            "type": "choice",
            "choice": "formal",
            "status": "supported",
            "probabilities": {key: float(key == "formal") for key in questions["speech_style.register"]["criteria"]},
            "supporting_line_ids": ["L1", "L1"],
        }
        with self.assertRaisesRegex(JevResponseError, "duplicate"):
            validate_response(payload, questions)

    def test_style_hash_is_stable_across_audit_timestamps_but_tracks_style(self):
        dossier = self._dossier()
        result = classify_dossier(dossier, self.CitedRules(self.vocab), self.vocab)
        casting = cast_identity(dossier["identity"])
        profile = build_individual_profile(dossier, result, casting, self.vocab)
        before = blueprint.prompt_hashes(profile, self.vocab)
        profile["classification"]["timestamp"] = "different-audit-time"
        after_timestamp = blueprint.prompt_hashes(profile, self.vocab)
        self.assertEqual(before["style_profile_hash"], after_timestamp["style_profile_hash"])
        profile["speech_style"]["register"] = "conversational"
        after_style = blueprint.prompt_hashes(profile, self.vocab)
        self.assertEqual(before["identity_prompt"], after_style["identity_prompt"])
        self.assertEqual(before["identity_prompt_hash"], after_style["identity_prompt_hash"])
        self.assertNotEqual(before["style_profile_hash"], after_style["style_profile_hash"])
        self.assertNotEqual(before["combined_instruct_hash"], after_style["combined_instruct_hash"])

    def test_identity_renderer_has_no_unconditional_neutral_tail(self):
        profile = json.loads(
            (ROOT / "blueprints" / "archetypes" / "archetype_masculine_clear_mid.json").read_text()
        )
        rendered = blueprint.render_identity(profile, self.vocab)
        self.assertNotIn("Neutral conversational delivery", rendered)

    def test_sample_pools_and_duplicate_accounting_are_explicit(self):
        lines = [
            {"dialog_id": "a", "voice_line_id": "a", "text": "Aye, I've not seen yer like here before.", "source": "quest", "sheet": "A"},
            {"dialog_id": "a", "voice_line_id": "a", "text": "Aye, I've not seen yer like here before.", "source": "quest", "sheet": "A"},
            {"dialog_id": "b", "voice_line_id": "b", "text": "The road is long and the sky is grey.", "source": "quest", "sheet": "A"},
        ]
        pools = build_dialogue_sample_pools("npc_40", lines, vocabulary=self.vocab)
        self.assertEqual(pools["representative"]["count"], len(pools["representative_samples"]))
        self.assertEqual(pools["targeted"]["count"], len(pools["targeted_samples"]))
        self.assertIn("targeted_signal_counts", pools)
        self.assertIn("targeted evidence never enters", pools["note"])
        analysis = analyze_dialogue(lines)
        self.assertGreaterEqual(analysis["measurements"]["duplicate"]["exact_duplicate_rows"], 1)

    def test_representative_sample_is_stable_under_input_reordering(self):
        lines = [
            {"voice_line_id": f"v{index}", "text": text, "source": source, "sheet": sheet}
            for index, (text, source, sheet) in enumerate([
                ("Aye, the road is long.", "quest", "A"),
                ("The gate opens at dawn.", "quest", "B"),
                ("We will be ready before the bells.", "custom", "C"),
                ("Hm. Another traveller.", "quest", "A"),
            ])
        ]
        first = select_representative_samples("npc_41", lines, max_count=4)
        repeated = select_representative_samples("npc_41", lines, max_count=4)
        reversed_input = select_representative_samples("npc_41", list(reversed(lines)), max_count=4)
        with_vocabulary = select_representative_samples(
            "npc_41", lines, max_count=4, vocabulary=self.vocab,
        )
        self.assertEqual([s["id"] for s in first], [s["id"] for s in repeated])
        self.assertEqual([s["id"] for s in first], [s["id"] for s in reversed_input])
        self.assertEqual(first, with_vocabulary)
