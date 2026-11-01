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
from blueprint_pipeline.cache import object_hash, read_json
from blueprint_pipeline.classifier import RulesProvider, classify_dossier
from blueprint_pipeline.dossier import build_dossiers
from blueprint_pipeline.dialogue import analyze_dialogue, select_representative_samples
from blueprint_pipeline.generator import build_individual_profile, validate_generated_profile
from blueprint_pipeline.inventory import build_inventory
from blueprint_pipeline.jev import (
    JevConfigurationError,
    JevProvider,
    JevResponseError,
    build_questions,
    validate_response,
)
from blueprint_pipeline.review import (
    apply_manual_changes,
    approve_profile,
    mark_auditioned,
    preserve_manual_profile,
)
from blueprint_pipeline.wiki import MediaWikiProvider, parse_infobox


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


class WikiTests(unittest.TestCase):
    def test_nested_infobox_and_empty_infobox(self):
        fields = parse_infobox("{{Infobox character\n| race = [[Lalafell|Dunesfolk]]\n| alias = {{small|The Wanderer}}\n| age = 35<ref>citation</ref>\n}}")
        self.assertEqual(fields["race"], "Dunesfolk")
        self.assertEqual(fields["alias"], "The Wanderer")
        self.assertEqual(parse_infobox("No infobox here"), {})
        self.assertEqual(parse_infobox("{{Infobox character}}"), {})

    def test_user_agent_resolution_provenance_and_etag_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"query": {"pages": [{
                "pageid": 7,
                "title": "Example A",
                "fullurl": "https://wiki.test/wiki/Example_A",
                "extract": "Example A is a calm, soft-spoken scholar.",
                "pageprops": {},
                "revisions": [{"revid": 41, "timestamp": "2026-10-20T00:00:00Z", "slots": {"main": {"content": "{{Infobox character\n| race = Lalafell\n| occupation = Scholar\n}}"}}}],
            }]}}
            calls = []
            def opener(request, timeout):
                calls.append(request)
                self.assertTrue(request.get_header("User-agent"))
                return FakeResponse(payload, headers={"ETag": '"r1"'})
            provider = MediaWikiProvider("https://wiki.test/api.php", cache_dir=Path(tmp), opener=opener, sleeper=lambda _: None)
            dossier = {"identity": {"name": "Example A", "aliases": [], "race": "Lalafell", "clan": "Dunesfolk", "npc_id": 10}}
            enriched = provider.enrich_dossier(dossier)
            self.assertEqual(enriched["wiki_page"]["revision_id"], 41)
            self.assertEqual(enriched["identity"]["occupation"], "Scholar")
            self.assertIn("occupation: Scholar", [item["quote"] for item in enriched["lore_evidence"]])
            self.assertEqual(enriched["voice_evidence"][0]["quote"], "Example A is a calm, soft-spoken scholar.")
            self.assertEqual(len(calls), 1)

            def not_modified(request, timeout):
                self.assertEqual(request.get_header("If-none-match"), '"r1"')
                raise HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO())
            cached = MediaWikiProvider("https://wiki.test/api.php", cache_dir=Path(tmp), opener=not_modified, sleeper=lambda _: None)
            self.assertEqual(cached._lookup_title("Example A")["pageid"], 7)

    def test_ambiguous_aliases_need_context_or_review_and_missing_user_agent_is_rejected(self):
        def opener(request, timeout):
            query = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(request.full_url.split("?", 1)[1]))
            title = query.get("titles")
            extract = "" if title == "Shared" else "Dunesfolk in Limsa Lominsa"
            return FakeResponse({"query": {"pages": [{
                "pageid": 1 if title == "Shared" else 2,
                "title": title,
                "extract": extract,
                "pageprops": {"disambiguation": ""} if title == "Shared" else {},
                "revisions": [],
            }]}})
        with tempfile.TemporaryDirectory() as tmp:
            provider = MediaWikiProvider("https://wiki.test/api.php", cache_dir=Path(tmp), opener=opener, sleeper=lambda _: None)
            result = provider.resolve_page({"identity": {
                "name": "Shared", "aliases": ["Shared Alias"], "race": "Dunesfolk", "location": "Limsa Lominsa"
            }})
            self.assertEqual(result["status"], "resolved")
            self.assertEqual(result["title"], "Shared Alias")
            with self.assertRaises(ValueError):
                MediaWikiProvider(cache_dir=Path(tmp), user_agent="")

    def test_rate_limit_retry_honors_retry_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            delays = []
            payload = {"query": {"pages": [{"pageid": 2, "title": "Retry", "extract": "", "revisions": []}]}}
            def opener(request, timeout):
                calls.append(request)
                if len(calls) == 1:
                    raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, io.BytesIO())
                return FakeResponse(payload)
            provider = MediaWikiProvider(
                "https://wiki.test/api.php", cache_dir=Path(tmp), retries=1,
                requests_per_second=1000, opener=opener, sleeper=delays.append,
            )
            self.assertEqual(provider._lookup_title("Retry")["pageid"], 2)
            self.assertEqual(len(calls), 2)
            self.assertAlmostEqual(delays[0], 0.0)


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
        payload["answers"]["identity.pitch"]["choice"] = "not-a-vocabulary-value"
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
        self.assertNotIn("presentation", result["acoustic_casting"])
        confident = classify_dossier(dossier, RulesProvider(self.vocab), self.vocab)
        self.assertEqual(confident["acoustic_casting"]["presentation"]["value"], "neutral")

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
            "acoustic_casting": {
                "presentation": {"value": "neutral"}, "pitch": {"value": "mid"},
                "timbre_primary": {"value": "clear"}, "articulation": {"value": "relaxed"},
                "speaking_pace": {"value": "moderate"}, "intonation": {"value": "natural"},
            },
            "speech_style": {"register": {"value": "formal"}},
        }
        profile = build_individual_profile(dossier, classification, self.archetypes[0], self.vocab)
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


if __name__ == "__main__":
    unittest.main()
