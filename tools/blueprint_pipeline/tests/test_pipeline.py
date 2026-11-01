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
from blueprint_pipeline.casting import cast_identity
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
from blueprint_pipeline.mood import (
    aggregate_mood,
    build_mood_chunks,
    plan_chunk_count,
)
from blueprint_pipeline.review import (
    apply_manual_changes,
    approve_profile,
    mark_auditioned,
    preserve_manual_profile,
)
from blueprint_pipeline.wiki import MediaWikiProvider, parse_infobox, plain_text


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

    def test_npc_infobox_template_and_wikitext_prose_without_extracts(self):
        # ffxiv.consolegameswiki.com has no TextExtracts extension: `extract` is
        # empty for every page, and the infobox template is `{{NPC infobox`.
        fields = parse_infobox(
            "{{NPC infobox\n| name = Hildibrand\n| race = Hyur\n| clan = Midlander\n"
            "| occupation = Inspector\n| description = Known for his calm demeanour.\n}}"
        )
        self.assertEqual(fields["race"], "Hyur")
        self.assertEqual(fields["occupation"], "Inspector")
        self.assertEqual(parse_infobox("{{Infobox character\n| race = Lalafell\n}}")["race"], "Lalafell")

        wikitext = (
            "{{NPC infobox\n| name = Test NPC\n| description = Known for her calm demeanour under fire.\n}}\n"
            "__NOTOC__\n"
            "'''Test NPC''' is a [[Hyur]] of the [[Goldsmith's Guild]].\n"
            "{| class=\"wikitable\"\n! Locations\n|}\n==Dialogue==\n[[File:Test full.jpeg|thumb]]\n"
        )
        self.assertNotIn("NOTOC", plain_text(wikitext))
        self.assertNotIn("File:", plain_text(wikitext))

        def opener(request, timeout):
            return FakeResponse({"query": {"pages": [{
                "pageid": 3, "title": "Test NPC", "fullurl": "https://wiki.test/wiki/Test_NPC",
                "extract": "", "pageprops": {},
                "revisions": [{"revid": 5, "timestamp": "2026-10-20T00:00:00Z",
                               "slots": {"main": {"content": wikitext}}}],
            }]}})
        with tempfile.TemporaryDirectory() as tmp:
            provider = MediaWikiProvider("https://wiki.test/api.php", cache_dir=Path(tmp), opener=opener, sleeper=lambda _: None)
            result = provider.resolve_page({"identity": {"name": "Test NPC"}})
            self.assertEqual(result["status"], "resolved")
            self.assertIn("Goldsmith's Guild", result["extract"])
            enriched = provider.enrich_dossier({"identity": {"canonical_id": "npc_3", "npc_id": 3, "name": "Test NPC"}})
            self.assertIn("calm demeanour", enriched["lore_quotes"][0]["quote"])
            self.assertIn("Goldsmith's Guild", enriched["wiki_page"]["plain_text_excerpt"])

    def test_template_disambiguation_is_not_resolved_as_identity(self):
        pages = {
            "Gerolt": {
                "pageid": 10, "title": "Gerolt", "fullurl": "https://wiki.test/wiki/Gerolt",
                "extract": "", "pageprops": {"notoc": ""},
                "revisions": [{"revid": 1, "slots": {"main": {"content": (
                    "{{disambig}}\n[[Gerolt]] appears in several expansions.\n"
                    "* [[Gerolt (ARR)]]\n* [[Gerolt (Eureka Anemos)]]\n"
                )}}}],
            },
            "Gerolt (ARR)": {
                "pageid": 11, "title": "Gerolt (ARR)", "fullurl": "https://wiki.test/wiki/Gerolt_(ARR)",
                "extract": "", "pageprops": {},
                "revisions": [{"revid": 2, "slots": {"main": {"content": (
                    "{{NPC infobox\n| name = Gerolt (ARR)\n}}\nGerolt runs the relic forge in Revenant's Toll."
                )}}}],
            },
            "Gerolt (Eureka Anemos)": {
                "pageid": 12, "title": "Gerolt (Eureka Anemos)", "fullurl": "https://wiki.test/wiki/Gerolt_(Eureka_Anemos)",
                "extract": "", "pageprops": {},
                "revisions": [{"revid": 3, "slots": {"main": {"content": (
                    "{{NPC infobox\n| name = Gerolt (Eureka Anemos)\n}}\nGerolt handles Anemos relic stages."
                )}}}],
            },
        }

        def opener(request, timeout):
            query = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(request.full_url.split("?", 1)[1]))
            title = query.get("titles") or ""
            page = pages.get(title) or {"title": title, "missing": True}
            return FakeResponse({"query": {"pages": [page]}})

        with tempfile.TemporaryDirectory() as tmp:
            provider = MediaWikiProvider("https://wiki.test/api.php", cache_dir=Path(tmp), opener=opener, sleeper=lambda _: None)
            ambiguous = provider.resolve_page({"identity": {"name": "Gerolt", "aliases": []}})
            self.assertEqual(ambiguous["status"], "ambiguous")

            located = provider.resolve_page({"identity": {
                "name": "Gerolt", "aliases": [], "location": "Eureka Anemos",
            }})
            self.assertEqual(located["status"], "resolved")
            self.assertEqual(located["title"], "Gerolt (Eureka Anemos)")
            self.assertEqual(located["reason"], "disambiguation_link")

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

    def test_composed_profile_keeps_acoustics_and_validated_style_only(self):
        from blueprint_pipeline.compose import compose_archetype_style_profile
        archetype = json.loads((Path(__file__).resolve().parents[3] / "blueprints" / "archetypes" / "archetype_masculine_warm_low_mid.json").read_text())
        dossier = {"identity": {"canonical_id": "npc_1", "name": "Test", "npc_id": 1, "sex": "male"}}
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
        # No BodyType in this dossier, so the ladder must not claim an age step.
        self.assertIn("sex_race_pitch_ladder", profile["evidence"]["pitch"]["note"])
        self.assertEqual(profile["evidence"]["perceived_age"]["level"], "default")
        self.assertEqual(set(profile["speech_style"]), {"register", "sentence_complexity"})
        self.assertNotIn("diction_style", profile["speech_style"])
        self.assertEqual(profile["speech_style_analysis"]["fields"]["register"]["confidence"], 0.9)
        self.assertEqual(profile["lifecycle"]["status"], "draft")

    def test_style_clause_renders_only_when_requested(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import blueprint as renderer
        vocabulary = renderer.load_vocabulary(Path(__file__).resolve().parents[3] / "blueprints" / "vocabulary.json")
        profile = json.loads((Path(__file__).resolve().parents[3] / "blueprints" / "archetypes" / "archetype_masculine_clear_mid.json").read_text())
        profile["speech_style"] = {
            "register": "formal",
            "vocabulary_complexity": "specialized",
            "sentence_complexity": "complex",
            "colloquialism": "low",
            "rhetorical_style": "measured",
        }
        plain = renderer.build_plan(profile, vocabulary)["variants"]["neutral"]["instruct"]
        styled = renderer.build_plan(profile, vocabulary, with_style=True)["variants"]["neutral"]["instruct"]
        self.assertNotIn("wording is", plain)
        self.assertIn("formal in register", styled)
        self.assertTrue(styled.startswith(plain.rstrip(".")), "style clause must append, never rewrite identity")

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


class WikiLoreQuoteTests(unittest.TestCase):
    def test_disposition_sentences_become_mood_quotes(self):
        provider = MediaWikiProvider("https://wiki.test/api.php", cache_dir=Path(tempfile.mkdtemp()))
        page = {
            "status": "resolved",
            "title": "Test NPC",
            "url": "https://wiki.test/wiki/Test_NPC",
            "revision_id": 42,
            "reason": "title match",
            "extract": (
                "She is known for her calm demeanour even under fire. "
                "Her voice is low and quiet. "
                "She sells fish in the market. "
                "The townsfolk say her temper is short after a bad catch."
            ),
            "wikitext": "{{Infobox NPC|Race=Hyur|Gender=Female}}",
        }
        provider.resolve_page = lambda dossier: page
        dossier = {"identity": {"canonical_id": "npc_5", "npc_id": 5, "name": "Test NPC", "race": "Hyur"}}
        result = provider.enrich_dossier(dossier)
        quotes = [record["quote"] for record in result["lore_quotes"]]
        self.assertEqual(len(quotes), 2)
        self.assertIn("calm demeanour", quotes[0])
        self.assertIn("temper", quotes[1])
        self.assertTrue(all(record["field"] == "mood" for record in result["lore_quotes"]))
        self.assertTrue(all(record["source"] == page["url"] for record in result["lore_quotes"]))
        # Voice quotes stay in their own field; the acoustic and mood audits do not mix.
        self.assertTrue(all(record["field"] == "voice_description" for record in result["voice_evidence"]))
        state = compact_state(result)
        self.assertEqual(len(state["lore_quotes"]), 2)
        self.assertEqual(state["lore_quotes"][0]["field"], "mood")

    def test_wiki_lore_quotes_reach_the_model_request_body(self):
        # A quote is only evidence if it is actually sent: assert on the wire
        # body the Jev provider posts, not on the intermediate state dict.
        bodies = []

        def opener(request, timeout):
            bodies.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse({
                "model": "stub-1",
                "answers": {"mood.preset": {
                    "type": "choice",
                    "choice": "none",
                    "probabilities": {"none": 0.9, "calm": 0.1},
                    "confidence": 0.9,
                }},
            })

        provider = JevProvider(
            model="stub-1",
            endpoint="http://127.0.0.1:18081/v1/systemone",
            opener=opener,
            sleeper=lambda _: None,
        )
        dossier = {
            "identity": {"canonical_id": "npc_9", "npc_id": 9, "name": "Test NPC"},
            "statistics": {"unique_line_count": 200},
            "dialogue_samples": [],
            "lore_quotes": [{
                "field": "mood",
                "source": "wiki_lore",
                "quote": "She is known for her calm demeanour even under fire.",
            }],
        }
        provider.classify(dossier, {"mood.preset": {
            "type": "choice",
            "criteria": ["none", "calm"],
        }})
        self.assertEqual(len(bodies), 1)
        self.assertIn(
            "She is known for her calm demeanour even under fire.",
            json.dumps(bodies[0]["state"], ensure_ascii=False),
        )


class MoodAndVolumeGateTests(unittest.TestCase):
    """Mood is an archon decision with three independent gates; style has a floor."""

    def setUp(self):
        self.vocab = blueprint.load_vocabulary()
        self.questions = build_questions(self.vocab)

    def _provider(self, *, preset: str, probability: float, confidence: float, justified: float):
        class StubMood(RulesProvider):
            name = "stub"
            model_id = "stub-1"

            def classify(self, dossier, questions):
                result = super().classify(dossier, questions)
                options = list(questions["mood.preset"]["criteria"])
                result["answers"]["mood.preset"] = {
                    "type": "choice",
                    "choice": preset,
                    "confidence": confidence,
                    "probabilities": {
                        key: probability if key == preset
                        else (1.0 - probability) / (len(options) - 1)
                        for key in options
                    },
                }
                result["answers"]["mood.justified"] = {"type": "noul", "noul": justified}
                return result

        return StubMood(self.vocab)

    def _dossier(self, lines: int, *, name: str = "Test") -> dict:
        return {
            "input_hash": "hash",
            "identity": {
                "canonical_id": "npc_10", "npc_id": 10, "name": name,
                "resolution_status": "eligible", "has_se_voice_reference": False,
                "identity_ambiguous": False, "sex": "male",
            },
            "statistics": {"dialogue_count": lines, "unique_line_count": lines},
            "dialogue_analysis": {
                "measurements": {"line_count": lines},
                "interpretation": {
                    "register": "formal", "vocabulary_complexity": "educated",
                    "sentence_complexity": "complex", "colloquialism": "low",
                    "rhetorical_style": "measured", "speech_habits": ["none_observed"],
                },
            },
        }

    def test_mood_options_are_presets_plus_none_only(self):
        options = list(self.questions["mood.preset"]["criteria"])
        self.assertEqual(options[0], "none")
        self.assertEqual(
            set(options[1:]), set(self.vocab["moods"]["allowed_presets"])
        )
        self.assertEqual(len(options), len(self.vocab["moods"]["allowed_presets"]) + 1)
        for option in options:
            self.assertTrue(self.questions["mood.preset"]["criteria"][option].strip())

    def test_strong_mood_on_a_rich_character_is_accepted(self):
        provider = self._provider(preset="angry", probability=0.9, confidence=0.9, justified=0.95)
        result = classify_dossier(self._dossier(400), provider, self.vocab)
        self.assertEqual(result["pack_archon"], "angry")
        self.assertTrue(result["mood"]["accepted"])
        self.assertEqual(result["mood"]["blocked_by"], [])
        self.assertEqual(result["mood"]["gate"]["line_count"], 400)

    def test_low_volume_blocks_a_mood_that_the_model_would_accept(self):
        provider = self._provider(preset="angry", probability=0.9, confidence=0.9, justified=0.95)
        floor = self.vocab["classification"]["thresholds"]["min_dialogue_lines_for_mood"]
        result = classify_dossier(self._dossier(floor - 1), provider, self.vocab)
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertEqual(result["mood"]["preset"], "none")
        self.assertTrue(any("below the mood floor" in reason for reason in result["mood"]["blocked_by"]))

    def test_justification_and_confidence_gates_block_a_mood(self):
        unjustified = self._provider(preset="calm", probability=0.9, confidence=0.9, justified=0.10)
        result = classify_dossier(self._dossier(400), unjustified, self.vocab)
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertTrue(any("justified" in reason for reason in result["mood"]["blocked_by"]))

        weak = self._provider(preset="calm", probability=0.40, confidence=0.60, justified=0.95)
        result = classify_dossier(self._dossier(400), weak, self.vocab)
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertTrue(any("thresholds" in reason for reason in result["mood"]["blocked_by"]))

    def test_none_answer_keeps_neutral_even_with_volume_and_justification(self):
        provider = self._provider(preset="none", probability=0.97, confidence=0.97, justified=0.99)
        result = classify_dossier(self._dossier(400), provider, self.vocab)
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertTrue(any("none/neutral" in reason for reason in result["mood"]["blocked_by"]))

    def test_sparse_character_records_no_style_but_keeps_the_estimate(self):
        floor = self.vocab["classification"]["thresholds"]["min_dialogue_lines_for_style"]
        result = classify_dossier(self._dossier(floor - 1), RulesProvider(self.vocab), self.vocab)
        register = result["speech_style"]["register"]
        self.assertIsNone(register["value"])
        self.assertEqual(register["method"], "insufficient_dialogue_volume")
        self.assertEqual(register["deterministic_estimate"], "formal")
        self.assertTrue(any("style floor" in reason for reason in result["review_reasons"]))

        rich = classify_dossier(self._dossier(floor + 1), RulesProvider(self.vocab), self.vocab)
        self.assertEqual(rich["speech_style"]["register"]["value"], "formal")
        self.assertEqual(rich["speech_style"]["register"]["method"], "rules_choice")


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
            "lore_quotes": [{"quote": "Known for her calm demeanour.", "source": "wiki", "field": "mood"}],
        }
        state = compact_state(dossier)
        self.assertIn("dialogue_measurements", state)
        self.assertNotIn("interpretation", json.dumps(state))
        self.assertEqual(state["dialogue_measurements"]["signal_line_rates"]["dialect_lines"], 0.4)
        self.assertEqual(state["dialogue_coverage"]["sampled_lines"], 1)
        self.assertEqual(round(state["dialogue_coverage"]["coverage_of_unique_lines"], 3), 0.001)
        self.assertIn("signal-weighted subset", state["dialogue_coverage"]["warning"])
        self.assertEqual(state["lore_quotes"][0]["field"], "mood")
        self.assertEqual(state["dialogue_samples"][0]["signals"], ["question"])
        self.assertTrue(any("archon" in rule for rule in state["guardrails"]))

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


class MoodProfileTests(unittest.TestCase):
    def setUp(self):
        self.vocab = blueprint.load_vocabulary()
        self.dossier = {
            "input_hash": "hash",
            "identity": {
                "canonical_id": "npc_10", "npc_id": 10, "name": "Test",
                "race": "Hyur", "clan": "Midlander", "sex": "male",
                "resolution_status": "eligible", "speaker_ids": [],
            },
            "statistics": {"dialogue_count": 400, "unique_line_count": 400},
            "dialogue_analysis": {"measurements": {}},
            "dialogue_samples": [],
        }

    def _classification(self, archon: str) -> dict:
        accepted = archon != "neutral"
        return {
            "canonical_id": "npc_10",
            "provider": "jev",
            "model_id": "nimble",
            "speech_style": {
                "register": {"value": "formal", "method": "jev_choice", "confidence": 0.8},
            },
            "answers": {"speech_style.register": {"type": "choice", "confidence": 0.8}},
            "mood": {
                "preset": archon if accepted else "none",
                "archon": archon,
                "accepted": accepted,
                "gate": {
                    "confidence": 0.9,
                    "selected_probability": 0.88,
                    "line_count": 400,
                    "chunks": 3,
                    "agreeing_chunks": 3,
                    "dispersion": 0.04,
                },
                "blocked_by": [] if accepted else ["answer was none/neutral: no consistent default register"],
            },
        }

    def test_accepted_mood_becomes_a_declared_archon_with_creative_evidence(self):
        profile = build_individual_profile(
            self.dossier, self._classification("calm"), cast_identity(self.dossier["identity"]), self.vocab,
        )
        self.assertEqual(profile["pack_archon"], "calm")
        self.assertEqual(profile["moods"], {"calm": {}})
        self.assertEqual(profile["evidence"]["pack_archon"]["level"], "creative_selection")
        note = profile["evidence"]["pack_archon"]["note"]
        self.assertIn("calm", note)
        self.assertIn("3 of 3 dialogue chunks agreed", note)
        self.assertEqual(validate_generated_profile(profile, self.vocab), [])

    def test_default_mood_packs_neutral_with_default_evidence(self):
        profile = build_individual_profile(
            self.dossier, self._classification("neutral"), cast_identity(self.dossier["identity"]), self.vocab,
        )
        self.assertEqual(profile["pack_archon"], "neutral")
        self.assertEqual(profile["moods"], {})
        self.assertEqual(profile["evidence"]["pack_archon"]["level"], "default")
        note = profile["evidence"]["pack_archon"]["note"]
        self.assertIn("none/neutral", note)
        self.assertIn("3 of 3 dialogue chunks agreed", note)
        self.assertEqual(validate_generated_profile(profile, self.vocab), [])


def mood_answers(questions: dict, spec: dict) -> dict:
    """One chunk's typed mood answer, shaped like a validated provider response."""
    options = list(questions["mood.preset"]["criteria"])
    distribution = spec.get("distribution")
    if distribution is None:
        probability = float(spec["probability"])
        distribution = {
            key: probability if key == spec["preset"] else (1.0 - probability) / (len(options) - 1)
            for key in options
        }
    return {
        "mood.preset": {
            "type": "choice",
            "choice": spec["preset"],
            "probabilities": distribution,
            "confidence": float(spec["confidence"]),
        },
        "mood.justified": {"type": "noul", "noul": float(spec["justified"])},
    }


class ScriptedChunkProvider(ClassifierProvider):
    """Stands in for the local model: one scripted answer per chunk call."""

    name = "stub"
    model_id = "stub-1"
    supports_chunks = True

    def __init__(self, vocabulary: dict, script: list[dict]):
        self.rules = RulesProvider(vocabulary)
        self.script = script
        self.asked: list[list[str]] = []
        self.chunk_calls = 0

    def classify(self, dossier, questions):
        self.asked.append(sorted(questions))
        return self.rules.classify(dossier, questions)

    def classify_chunk(self, dossier, questions, chunk):
        self.asked.append(sorted(questions))
        spec = self.script[min(self.chunk_calls, len(self.script) - 1)]
        self.chunk_calls += 1
        return {
            "model": self.model_id,
            "answers": mood_answers(questions, spec),
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }


def synthetic_lines(count: int) -> list[dict]:
    """Dialogue that fills all three length buckets (6 / 17 / 34 words)."""
    shapes = (
        "Jus' the two of us, then.",
        "Ye may not believe it, but the ore speaks when the forge is quiet enough to hear it.",
        "When the furnace roars and the hammer rings true, a smith learns more about the metal "
        "than any scholar could teach him in a lifetime of reading, and that is the whole of it.",
    )
    return [
        {
            "text": shapes[index % len(shapes)],
            "source": "quest",
            "sheet": f"sheet_{index // 25}",
            "signals": ["dialect:jus"] if index % 3 == 0 else [],
        }
        for index in range(count)
    ]


class ChunkedMoodTests(unittest.TestCase):
    """Mood comes from in-game dialogue only: chunks in, one aggregate out."""

    def setUp(self):
        self.vocab = blueprint.load_vocabulary()
        self.questions = build_questions(self.vocab)
        self.policy = {**DEFAULT_THRESHOLDS, **self.vocab["classification"]["thresholds"]}
        # Small windows keep the fixtures readable; the production value is
        # exercised by the chunk planner test below.
        self.small = {"mood_chunk_chars": 400, "mood_chunk_overlap": 0.1}

    def _dossier(self, lines: int, pool: list[dict] | None = None) -> dict:
        pool = pool if pool is not None else synthetic_lines(lines)
        return {
            "input_hash": "hash",
            "identity": {
                "canonical_id": "npc_10", "npc_id": 10, "name": "Test",
                "resolution_status": "eligible", "has_se_voice_reference": False,
                "identity_ambiguous": False, "sex": "male",
            },
            "statistics": {"dialogue_count": lines, "unique_line_count": lines},
            "dialogue_analysis": {
                "measurements": {"line_count": lines, "signal_line_rates": {"dialect_lines": 0.3}},
                "interpretation": {
                    "register": "formal", "vocabulary_complexity": "educated",
                    "sentence_complexity": "complex", "colloquialism": "low",
                    "rhetorical_style": "measured", "speech_habits": ["none_observed"],
                },
            },
            "dialogue_samples": pool[:24],
            "dialogue_pool": pool,
        }

    def test_below_the_floor_no_mood_question_is_asked(self):
        floor = int(self.policy["min_dialogue_lines_for_mood"])
        provider = ScriptedChunkProvider(
            self.vocab,
            [{"preset": "angry", "probability": 0.95, "confidence": 0.95, "justified": 0.95}],
        )
        result = classify_dossier(self._dossier(floor - 1), provider, self.vocab, thresholds=self.small)
        self.assertEqual(provider.chunk_calls, 0)
        self.assertTrue(all(not any(q.startswith("mood.") for q in asked) for asked in provider.asked))
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertEqual(result["mood"]["blocked_by"], ["insufficient_volume"])
        self.assertEqual(result["mood"]["method"], "default")

    def test_dossier_without_a_pool_is_refused_not_single_chunked(self):
        # A dossier built before the mood pool carries only the 24-line style
        # sample. Judging that as the corpus is the single-scene guess chunking
        # exists to prevent, so the refusal has to be explicit.
        dossier = self._dossier(400)
        dossier.pop("dialogue_pool")
        provider = ScriptedChunkProvider(
            self.vocab,
            [{"preset": "angry", "probability": 0.95, "confidence": 0.95, "justified": 0.95}],
        )
        result = classify_dossier(dossier, provider, self.vocab, thresholds=self.small)
        self.assertEqual(provider.chunk_calls, 0)
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertEqual(result["mood"]["blocked_by"], ["insufficient_pool"])
        self.assertTrue(any("re-run `dossiers`" in reason for reason in result["review_reasons"]))

    def test_consistent_chunks_accept_the_preset(self):
        provider = ScriptedChunkProvider(
            self.vocab,
            [{"preset": "angry", "probability": 0.80, "confidence": 0.85, "justified": 0.90}],
        )
        result = classify_dossier(self._dossier(200), provider, self.vocab, thresholds=self.small)
        self.assertGreaterEqual(provider.chunk_calls, 3)
        self.assertEqual(result["pack_archon"], "angry")
        self.assertTrue(result["mood"]["accepted"])
        self.assertEqual(result["mood"]["blocked_by"], [])
        self.assertEqual(result["mood"]["method"], "stub_chunks")
        self.assertEqual(result["mood"]["gate"]["chunks"], provider.chunk_calls)
        self.assertEqual(result["mood"]["gate"]["agreeing_chunks"], provider.chunk_calls)
        self.assertEqual(len(result["mood"]["chunks"]), provider.chunk_calls)
        self.assertEqual(result["mood"]["chunking"]["chunks"], provider.chunk_calls)
        self.assertAlmostEqual(
            sum(result["mood"]["aggregate"]["mean_probabilities"].values()), 1.0, places=2
        )

    def test_one_loud_chunk_does_not_decide(self):
        provider = ScriptedChunkProvider(
            self.vocab,
            [
                {"preset": "angry", "probability": 0.95, "confidence": 0.95, "justified": 0.95},
                {"preset": "calm", "probability": 0.95, "confidence": 0.95, "justified": 0.95},
                {"preset": "happy", "probability": 0.95, "confidence": 0.95, "justified": 0.95},
            ],
        )
        result = classify_dossier(self._dossier(200), provider, self.vocab, thresholds=self.small)
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertIn("low_consistency", result["mood"]["blocked_by"])
        self.assertLess(result["mood"]["gate"]["agreeing_chunks"], result["mood"]["gate"]["required_chunks"])

    def test_flat_distributions_are_rejected_by_dispersion(self):
        options = list(self.questions["mood.preset"]["criteria"])
        flat = {key: 0.8 / (len(options) - 1) for key in options}
        flat["angry"] = 0.2
        provider = ScriptedChunkProvider(
            self.vocab,
            [{"preset": "angry", "confidence": 0.9, "justified": 0.9, "distribution": flat}],
        )
        result = classify_dossier(self._dossier(200), provider, self.vocab, thresholds=self.small)
        self.assertEqual(result["pack_archon"], "neutral")
        self.assertIn("high_dispersion", result["mood"]["blocked_by"])
        self.assertIn("low_probability", result["mood"]["blocked_by"])

    def test_every_rejection_names_a_gate(self):
        cases = {
            "mixed": [
                {"preset": "angry", "probability": 0.95, "confidence": 0.95, "justified": 0.95},
                {"preset": "calm", "probability": 0.95, "confidence": 0.95, "justified": 0.95},
                {"preset": "happy", "probability": 0.95, "confidence": 0.95, "justified": 0.95},
            ],
            "unjustified": [{"preset": "angry", "probability": 0.9, "confidence": 0.9, "justified": 0.1}],
            "unconfident": [{"preset": "angry", "probability": 0.9, "confidence": 0.5, "justified": 0.9}],
            "none": [{"preset": "none", "probability": 0.97, "confidence": 0.97, "justified": 0.97}],
        }
        for name, script in cases.items():
            with self.subTest(name):
                provider = ScriptedChunkProvider(self.vocab, script)
                result = classify_dossier(
                    self._dossier(200), provider, self.vocab, thresholds=self.small
                )
                self.assertEqual(result["pack_archon"], "neutral")
                self.assertTrue(result["mood"]["blocked_by"], name)
                self.assertFalse(result["mood"]["accepted"], name)

    def test_chunk_planner_scales_with_volume(self):
        self.assertEqual(plan_chunk_count(8, self.policy), 0)
        self.assertEqual(plan_chunk_count(39, self.policy), 0)
        self.assertEqual(plan_chunk_count(40, self.policy), 1)
        self.assertEqual(plan_chunk_count(119, self.policy), 2)
        self.assertEqual(plan_chunk_count(300, self.policy), 4)
        self.assertEqual(plan_chunk_count(466, self.policy), 4)
        self.assertEqual(plan_chunk_count(1133, self.policy), 6)
        self.assertEqual(plan_chunk_count(1552, self.policy), 7)
        self.assertEqual(plan_chunk_count(5000, self.policy), 8)

    def test_gerolt_scale_volume_produces_many_stratified_chunks(self):
        pool = synthetic_lines(1600)
        count = plan_chunk_count(1552, self.policy)
        self.assertGreaterEqual(count, 5)
        chunks = build_mood_chunks(pool, count, self.policy)
        self.assertEqual(len(chunks), count)
        for chunk in chunks:
            self.assertLessEqual(chunk["chars"], self.policy["mood_chunk_chars"])
            buckets = {length_bucket(str(line["text"])) for line in chunk["lines"]}
            self.assertEqual(buckets, {"short", "medium", "long"}, chunk["index"])
        # Consecutive chunks overlap, so a register at a window edge is seen twice.
        first, second = chunks[0]["lines"], chunks[1]["lines"]
        self.assertTrue({id(line) for line in first} & {id(line) for line in second})

    def test_chunk_state_withholds_lore_and_keeps_global_aggregates(self):
        dossier = self._dossier(200)
        dossier["lore_quotes"] = [{"quote": "Known for her calm demeanour.", "source": "wiki", "field": "mood"}]
        dossier["voice_evidence"] = [{"field": "pitch", "quote": "a deep voice"}]
        samples = synthetic_lines(5)
        chunk_state = compact_state(dossier, samples=samples, chunk={"index": 1, "count": 4, "chars": 400})
        self.assertEqual(chunk_state["lore_quotes"], [])
        self.assertNotIn("voice_evidence", chunk_state)
        self.assertEqual(chunk_state["dialogue_chunk"]["index"], 1)
        self.assertEqual(chunk_state["dialogue_chunk"]["count"], 4)
        # A chunk carries the compact aggregates instead of the raw measurements:
        # the same signal, a fraction of the tokens.
        self.assertNotIn("dialogue_measurements", chunk_state)
        self.assertEqual(
            chunk_state["dialogue_aggregates"]["unique_lines"],
            dossier["dialogue_analysis"]["measurements"]["line_count"],
        )
        self.assertIn("signal_line_rates", chunk_state["dialogue_aggregates"])
        self.assertEqual(len(chunk_state["dialogue_samples"]), 5)
        self.assertTrue(any("dialogue lines" in line for line in chunk_state["guardrails"]))
        # The non-chunk state still carries lore for the identity and style path.
        full_state = compact_state(dossier)
        self.assertEqual(len(full_state["lore_quotes"]), 1)
        self.assertIn("voice_evidence", full_state)

    def test_aggregate_reports_per_chunk_evidence(self):
        options = list(self.questions["mood.preset"]["criteria"])
        answers = [
            mood_answers(self.questions, {"preset": "calm", "probability": 0.7, "confidence": 0.8, "justified": 0.9}),
            mood_answers(self.questions, {"preset": "calm", "probability": 0.8, "confidence": 0.9, "justified": 0.85}),
        ]
        mood = aggregate_mood(answers, self.policy, line_count=200, provider="stub", chunk_lines=[40, 40])
        self.assertTrue(mood["accepted"])
        self.assertEqual(mood["archon"], "calm")
        self.assertEqual(mood["aggregate"]["agreeing_chunks"], 2)
        self.assertEqual([item["chunk"] for item in mood["chunks"]], [0, 1])
        self.assertEqual(set(mood["probabilities"]), set(options))
        self.assertAlmostEqual(sum(mood["probabilities"].values()), 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
