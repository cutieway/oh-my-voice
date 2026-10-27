import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_generator():
    spec = importlib.util.spec_from_file_location("omv_generate", HERE / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.gen = load_generator()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.vdir = self.out / "voicelines"
        self.vdir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, **overrides):
        value = {
            "Schema": 2,
            "Language": "en",
            "Sanitizer": "v1:" + "a" * 64,
            "Backend": "qwen3tts",
            "Model": {"Id": "model", "Revision": "revision"},
            "Generation": {"Seed": None},
            "Encoder": {"Codec": "opus"},
            "Voices": {},
            "Npcs": {},
            "IgnoredSpeakers": [],
            "VoiceEmbeddings": {},
            "Voicelines": {},
        }
        value.update(overrides)
        return value

    def test_merge_carries_lines_and_metadata(self):
        (self.vdir / "old.ogg").write_bytes(b"old")
        previous = self.manifest(
            Voices={"old": {"IsGeneric": False}},
            Npcs={"Old": {"NpcId": "1", "VoiceId": "old"}},
            IgnoredSpeakers=["ignored"],
            VoiceEmbeddings={"old": "sha"},
            Voicelines={"old": {"Bytes": 3, "VoiceId": "old"}},
        )
        current = self.manifest(
            Voices={"new": {"IsGeneric": False}},
            Npcs={"New": {"NpcId": "2", "VoiceId": "new"}},
            IgnoredSpeakers=["new-ignored"],
            VoiceEmbeddings={"new": "sha2"},
            Voicelines={"new": {"Bytes": 3}},
        )

        added, missing = self.gen.merge_previous_manifest(current, previous, self.vdir)

        self.assertEqual((added, missing), (1, 0))
        self.assertEqual(set(current["Voicelines"]), {"old", "new"})
        self.assertEqual(current["Voices"]["old"], {"IsGeneric": False})
        self.assertEqual(current["Npcs"]["Old"]["NpcId"], "1")
        self.assertEqual(current["IgnoredSpeakers"], ["ignored", "new-ignored"])
        self.assertEqual(current["VoiceEmbeddings"], {"old": "sha", "new": "sha2"})

    def test_missing_audio_is_not_carried(self):
        previous = self.manifest(Voicelines={"gone": {"Bytes": 3}})
        current = self.manifest(Voicelines={"new": {"Bytes": 3}})

        added, missing = self.gen.merge_previous_manifest(current, previous, self.vdir)

        self.assertEqual((added, missing), (0, 1))
        self.assertNotIn("gone", current["Voicelines"])

    def test_variant_audio_is_carried_before_root_exists(self):
        (self.vdir / "var.he.ogg").write_bytes(b"he")
        (self.vdir / "var.she.ogg").write_bytes(b"she")
        previous = self.manifest(Voicelines={"var": {
            "Bytes": 0,
            "DurationSeconds": 0,
            "GenerationFingerprint": "",
            "Variants": {
                "she": {"File": "var.she.ogg", "Bytes": 3},
                "he": {"File": "var.he.ogg", "Bytes": 2},
            },
        }})
        current = self.manifest(Voicelines={"new": {"Bytes": 3}})

        added, missing = self.gen.merge_previous_manifest(current, previous, self.vdir)

        self.assertEqual((added, missing), (1, 0))
        self.assertIn("var", current["Voicelines"])
        self.assertEqual(current["Voicelines"]["var"]["Bytes"], 0)
        self.assertEqual(set(current["Voicelines"]["var"]["Variants"]),
                         {"she", "he"})

    def test_incompatible_settings_are_rejected(self):
        previous = self.manifest()
        current = self.manifest(Model={"Id": "other", "Revision": "revision"})

        error = self.gen.manifest_merge_error(previous, current)

        self.assertIn("Model differs", error)

    def test_corrupt_manifest_raises(self):
        (self.out / "manifest.json").write_text("{", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.gen.read_previous_manifest(self.out)

    def test_manifest_checkpoint_is_atomic(self):
        value = self.manifest(Voicelines={"line": {"Bytes": 3}})

        self.gen.write_manifest(self.out, value)

        self.assertEqual(json.loads((self.out / "manifest.json").read_text()), value)
        self.assertFalse((self.out / "manifest.json.tmp").exists())

    def test_reused_line_keeps_previous_entry_without_probe(self):
        previous = {"Bytes": 3, "GenerationFingerprint": "fp",
                    "Sentence": "hello"}
        manifest = self.manifest()
        action = {"kind": "skip", "previous": previous,
                  "target": self.vdir / "line.ogg", "generation_fp": "fp"}

        self.gen.store_reused_line(manifest, {"id": "line"}, action)

        self.assertIs(manifest["Voicelines"]["line"], previous)

    def test_reused_line_does_not_overwrite_merged_entry(self):
        merged = {"Bytes": 9, "GenerationFingerprint": "fp"}
        manifest = self.manifest(Voicelines={"line": merged})
        action = {"kind": "skip", "previous": {"Bytes": 3},
                  "target": self.vdir / "line.ogg", "generation_fp": "fp"}

        self.gen.store_reused_line(manifest, {"id": "line"}, action)

        self.assertIs(manifest["Voicelines"]["line"], merged)

    def test_missing_entries_do_not_count_as_loss(self):
        # 5 previous, 2 already broken, 3 carried: repair, not loss.
        self.assertEqual(self.gen.merge_line_loss(5, 2, 3), 0)
        # 4 previous, none broken, only 3 after merge: real loss.
        self.assertEqual(self.gen.merge_line_loss(4, 0, 3), 1)


class SpeechVariantTests(unittest.TestCase):
    def setUp(self):
        self.gen = load_generator()

    def test_vocative_is_removed(self):
        self.assertEqual(self.gen.speech_variants(
            "_FIRSTNAME_ - do you see the object?"),
            {"base": "Do you see the object?"})
        self.assertEqual(self.gen.speech_variants(
            "Greetings, _FIRSTNAME_. I thought it fitting."),
            {"base": "Greetings. I thought it fitting."})

    def test_name_only_is_skipped(self):
        self.assertEqual(self.gen.speech_variants("_FIRSTNAME_!?"), {})
        self.assertEqual(self.gen.speech_variants(
            "_FIRSTNAME_! _FIRSTNAME_! Are you all right?"),
            {"base": "Are you all right?"})

    def test_third_person_gets_they_she_he(self):
        variants = self.gen.speech_variants("_FIRSTNAME_ and I shall return.")
        self.assertEqual(variants["they"], "They and I shall return.")
        self.assertEqual(variants["she"], "She and I shall return.")
        self.assertEqual(variants["he"], "He and I shall return.")

    def test_singular_they_verb_agreement(self):
        cases = {
            "_FIRSTNAME_ is a hero.": "They are a hero.",
            "_FIRSTNAME_ has learned much.": "They have learned much.",
            "_FIRSTNAME_ was here.": "They were here.",
            "_FIRSTNAME_ does not know.": "They do not know.",
            "_FIRSTNAME_ makes the rounds.": "They make the rounds.",
            "_FIRSTNAME_ returns at dawn.": "They return at dawn.",
            "_FIRSTNAME_ knows the way.": "They know the way.",
        }
        for sentence, expected in cases.items():
            self.assertEqual(self.gen.speech_variants(sentence)["they"],
                             expected, sentence)

    def test_they_question_agreement(self):
        cases = {
            "Does _FIRSTNAME_ not feel it?": "Do they not feel it?",
            "What has _FIRSTNAME_ learned?": "What have they learned?",
            "Is _FIRSTNAME_ here?": "Are they here?",
            "Has _FIRSTNAME_ finished?": "Have they finished?",
        }
        for sentence, expected in cases.items():
            self.assertEqual(self.gen.speech_variants(sentence)["they"],
                             expected, sentence)

    def test_predicate_nominative_stays_object(self):
        cases = {
            "It is _FIRSTNAME_, is it not?": "It is them, is it not?",
            "This is _FIRSTNAME_.": "This is them.",
            "Well, if it isn't _FIRSTNAME_!": "Well, if it isn't them!",
        }
        for sentence, expected in cases.items():
            self.assertEqual(self.gen.speech_variants(sentence)["they"],
                             expected, sentence)

    def test_possessive_and_band(self):
        self.assertEqual(
            self.gen.speech_variants("with _FIRSTNAME_'s arrival")["they"],
            "With their arrival")
        variants = self.gen.speech_variants(
            "_FIRSTNAME_ and band will break through.")
        self.assertEqual(variants["they"],
                         "They and their band will break through.")
        self.assertEqual(variants["she"],
                         "She and her band will break through.")
        self.assertEqual(variants["he"],
                         "He and his band will break through.")

    def test_list_final_name_is_an_object(self):
        variants = self.gen.speech_variants(
            "Go to Alisaie, G'raha, and _FIRSTNAME_.")
        self.assertEqual(variants["they"], "Go to Alisaie, G'raha, and them.")
        self.assertEqual(variants["she"], "Go to Alisaie, G'raha, and her.")
        self.assertEqual(variants["he"], "Go to Alisaie, G'raha, and him.")

    def test_list_with_middle_name(self):
        variants = self.gen.speech_variants(
            "Nashu, _FIRSTNAME_, and I will investigate.")
        self.assertEqual(variants["they"],
                         "Nashu, they, and I will investigate.")
        self.assertEqual(variants["she"],
                         "Nashu, she, and I will investigate.")
        variants = self.gen.speech_variants(
            "With Nashu, _FIRSTNAME_, and Estinien, we will win.")
        self.assertEqual(variants["they"],
                         "With Nashu, them, and Estinien, we will win.")

    def test_coordination_case_follows_phrase_role(self):
        # The whole coordination takes the noun phrase's case: the source
        # text's own "and I" must not force a subject reading.
        cases = {
            "You and _FIRSTNAME_ can delve into the nooks and crannies.":
                ("You and they can delve into the nooks and crannies.",
                 "You and she can delve into the nooks and crannies.",
                 "You and he can delve into the nooks and crannies."),
            "Understood. If you and _FIRSTNAME_ want to concentrate on "
            "True Vue, the rest of us will see to the other areas.":
                ("Understood. If you and they want to concentrate on True "
                 "Vue, the rest of us will see to the other areas.",
                 "Understood. If you and she want to concentrate on True "
                 "Vue, the rest of us will see to the other areas.",
                 "Understood. If you and he want to concentrate on True "
                 "Vue, the rest of us will see to the other areas."),
            "I'm back! And I've brought _FIRSTNAME_ and Raha with me.":
                ("I'm back! And I've brought them and Raha with me.",
                 "I'm back! And I've brought her and Raha with me.",
                 "I'm back! And I've brought him and Raha with me."),
            "...while _FIRSTNAME_ and Krile question the residents.":
                ("While they and Krile question the residents.",
                 "While she and Krile question the residents.",
                 "While he and Krile question the residents."),
            "An acquaintance of _FIRSTNAME_ and Alphinaud, are you?":
                ("An acquaintance of them and Alphinaud, are you?",
                 "An acquaintance of her and Alphinaud, are you?",
                 "An acquaintance of him and Alphinaud, are you?"),
            "Is that _FIRSTNAME_ and Tataru I spy?":
                ("Is that them and Tataru I spy?",
                 "Is that her and Tataru I spy?",
                 "Is that him and Tataru I spy?"),
            "You are a better shot than _FIRSTNAME_ and Estinien.":
                ("You are a better shot than them and Estinien.",
                 "You are a better shot than her and Estinien.",
                 "You are a better shot than him and Estinien."),
            # A preposition in the previous sentence must not govern the
            # coordination, and an embedded clause keeps its subject.
            "I was thinking of you. _FIRSTNAME_ and Estinien, come here!":
                ("I was thinking of you. They and Estinien, come here!",
                 "I was thinking of you. She and Estinien, come here!",
                 "I was thinking of you. He and Estinien, come here!"),
            "I think _FIRSTNAME_ and Raha together can do it.":
                ("I think they and Raha together can do it.",
                 "I think she and Raha together can do it.",
                 "I think he and Raha together can do it."),
        }
        for sentence, (they, she, he) in cases.items():
            variants = self.gen.speech_variants(sentence)
            self.assertEqual(variants["they"], they, sentence)
            self.assertEqual(variants["she"], she, sentence)
            self.assertEqual(variants["he"], he, sentence)

    def test_real_three_person_lists(self):
        cases = {
            "To review: Lyse, _FIRSTNAME_, and I will meet with Gyodo as planned.":
                ("To review: Lyse, they, and I will meet with Gyodo as planned.",
                 "To review: Lyse, she, and I will meet with Gyodo as planned.",
                 "To review: Lyse, he, and I will meet with Gyodo as planned."),
            "Which is how Alisaie, _FIRSTNAME_, and I came to find you.":
                ("Which is how Alisaie, they, and I came to find you.",
                 "Which is how Alisaie, she, and I came to find you.",
                 "Which is how Alisaie, he, and I came to find you."),
            "I wouldn't put it past them, so mayhap it would be best if Lyse, "
            "_FIRSTNAME_, and I joined you.":
                ("I wouldn't put it past them, so mayhap it would be best if "
                 "Lyse, they, and I joined you.",
                 "I wouldn't put it past them, so mayhap it would be best if "
                 "Lyse, she, and I joined you.",
                 "I wouldn't put it past them, so mayhap it would be best if "
                 "Lyse, he, and I joined you."),
            "Meanwhile, Wuk Lamat, _FIRSTNAME_, and myself will handle this half.":
                ("Meanwhile, Wuk Lamat, they, and myself will handle this half.",
                 "Meanwhile, Wuk Lamat, she, and myself will handle this half.",
                 "Meanwhile, Wuk Lamat, he, and myself will handle this half."),
        }
        for sentence, (they, she, he) in cases.items():
            variants = self.gen.speech_variants(sentence)
            self.assertEqual(variants["they"], they)
            self.assertEqual(variants["she"], she)
            self.assertEqual(variants["he"], he)

    def test_vocative_before_independent_and_clause(self):
        self.assertEqual(self.gen.speech_variants(
            "But fail me, _FIRSTNAME_, and I may be forced to reconsider."),
            {"base": "But fail me, and I may be forced to reconsider."})
        self.assertEqual(self.gen.speech_variants(
            "You have become a symbol of hope to all the people of Eorzea, "
            "_FIRSTNAME_, and I believe this to be no coincidence."),
            {"base": "You have become a symbol of hope to all the people of "
                     "Eorzea, and I believe this to be no coincidence."})

    def test_politeness_and_object_pronoun(self):
        self.assertEqual(self.gen.speech_variants(
            "My dear _FIRSTNAME_, there is no way I could repay you."),
            {"base": "My dear, there is no way I could repay you."})
        variants = self.gen.speech_variants(
            "It's been forever since me and _FIRSTNAME_ last performed.")
        self.assertEqual(variants["they"],
                         "It's been forever since me and them last performed.")

    def test_mid_sentence_conjunction_vocative(self):
        self.assertEqual(self.gen.speech_variants(
            "A doppelganger might fool them, but _FIRSTNAME_? Nay."),
            {"base": "A doppelganger might fool them, but you? Nay."})

    def test_speech_units_use_pronoun_filenames(self):
        variants = {"id": "abc", "variants": {
            "they": "They", "she": "She", "he": "He"}}
        self.assertEqual(self.gen.speech_units(variants), [
            ("they", "abc.ogg", "They"),
            ("she", "abc.she.ogg", "She"),
            ("he", "abc.he.ogg", "He"),
        ])
        base = {"id": "abc", "speech_text": "Do you see it?"}
        self.assertEqual(self.gen.speech_units(base), [
            ("base", "abc.ogg", "Do you see it?"),
        ])

    def test_planned_speech_keeps_all_pronoun_variants(self):
        planned = self.gen.planned_speech("_FIRSTNAME_ and I shall return.")
        self.assertEqual(planned["variants"], {
            "they": "They and I shall return.",
            "she": "She and I shall return.",
            "he": "He and I shall return.",
        })
        self.assertNotIn("speech_text", planned)
        self.assertEqual(
            self.gen.planned_speech("Greetings, _FIRSTNAME_."),
            {"speech_text": "Greetings."})
        self.assertIsNone(self.gen.planned_speech("_FIRSTNAME_!?"))

    def test_speaker_label_is_not_spoken(self):
        self.assertEqual(
            self.gen.planned_speech("(-Krile-)Worked, did it?"),
            {"speech_text": "Worked, did it?"})
        self.assertEqual(
            self.gen.planned_speech(
                "(-Krile-)... Well, at least it wasn't smashed."),
            {"speech_text": "... Well, at least it wasn't smashed."})

    def test_speaker_label_keeps_pronoun_variants(self):
        planned = self.gen.planned_speech(
            "(-Narrator-)Then _FIRSTNAME_ and I shall go.")
        self.assertEqual(planned["variants"], {
            "they": "Then they and I shall go.",
            "she": "Then she and I shall go.",
            "he": "Then he and I shall go.",
        })

    def test_label_only_line_is_skipped(self):
        self.assertIsNone(
            self.gen.planned_speech("(-Bakool Ja Ja the Mighty-)"))


class DurationCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.gen = load_generator()

    def test_slowest_pilot_line_fits(self):
        # 9 words, 2.2 wps -> 5.09 s base; the slowest audition was 7.45 s.
        text = ("(-Alisaie-)Head northwest. There's a passage leading "
                "deeper within. Hurry!")
        self.assertEqual(len(text.split()), 9)
        self.assertGreaterEqual(self.gen.duration_limit(text, 2.2), 7.4465)
        self.assertGreaterEqual(
            self.gen.auto_max_new_tokens(text, 2.2), int(7.4465 * 12))


if __name__ == "__main__":
    unittest.main()
