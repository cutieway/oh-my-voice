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
            "Schema": 1,
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


if __name__ == "__main__":
    unittest.main()
