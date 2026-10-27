import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load_linker():
    spec = importlib.util.spec_from_file_location("omv_link_audio", HERE / "link_audio.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LinkAudioTests(unittest.TestCase):
    def setUp(self):
        self.linker = load_linker()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pack = self.root / "voicelines"
        self.pack.mkdir()
        self.db = self.root / "dialogue.db"

    def tearDown(self):
        self.tmp.cleanup()

    def write_manifest(self, lines, schema=2):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"Schema": schema, "Voicelines": lines}),
                        encoding="utf-8")
        return path

    def run_linker(self, manifest):
        argv = [
            "link_audio.py", "--db", str(self.db), "--manifest", str(manifest),
            "--repo-root", str(self.root),
        ]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(self.linker.main(), 0)
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(
                "SELECT voice_line_id, pronoun, audio_path, bytes "
                "FROM audio_variants ORDER BY voice_line_id, pronoun"
            ).fetchall()
        finally:
            connection.close()

    def test_schema_two_links_root_and_variants(self):
        (self.pack / "abc.ogg").write_bytes(b"they")
        (self.pack / "abc.she.ogg").write_bytes(b"she!")
        (self.pack / "abc.he.ogg").write_bytes(b"he!!")
        (self.pack / "def.ogg").write_bytes(b"neutral")
        manifest = self.write_manifest({
            "abc": {
                "Bytes": 4,
                "DurationSeconds": 1.5,
                "GenerationFingerprint": "fp",
                "Variants": {
                    "she": {"File": "abc.she.ogg", "Bytes": 4,
                            "DurationSeconds": 1.5,
                            "GenerationFingerprint": "fp"},
                    "he": {"File": "abc.he.ogg", "Bytes": 4,
                           "DurationSeconds": 1.5,
                           "GenerationFingerprint": "fp"},
                },
            },
            "def": {"Bytes": 7, "DurationSeconds": 2.0,
                    "GenerationFingerprint": "fp2"},
        })

        rows = self.run_linker(manifest)

        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("abc", "he"), ("abc", "she"), ("abc", "they"),
                          ("def", "none")])
        self.assertEqual(rows[0][2], "voicelines/abc.he.ogg")
        self.assertEqual(rows[2][2], "voicelines/abc.ogg")
        self.assertEqual(rows[0][3], 4)

    def test_schema_one_pack_is_skipped(self):
        (self.pack / "abc.ogg").write_bytes(b"male")
        manifest = self.write_manifest({
            "abc": {
                "Bytes": 4,
                "DurationSeconds": 1.5,
                "GenerationFingerprint": "fp",
            },
        }, schema=1)

        self.assertEqual(self.run_linker(manifest), [])

    def test_existing_gender_table_is_migrated(self):
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE audio_variants (
              voice_line_id TEXT NOT NULL,
              gender TEXT NOT NULL CHECK (gender IN ('none','male','female')),
              audio_path TEXT NOT NULL,
              bytes INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL,
              generation_profile TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (voice_line_id, gender)
            );
            CREATE INDEX idx_audio_variants_path ON audio_variants(audio_path);
            INSERT INTO audio_variants VALUES
              ('old', 'male', 'voicelines/old.ogg', 4, 1000, 'fp', 'now');
            """)
        connection.commit()
        connection.close()

        manifest = self.write_manifest({
            "new": {"Bytes": 4, "DurationSeconds": 1.0,
                    "GenerationFingerprint": "fp"},
        })
        (self.pack / "new.ogg").write_bytes(b"new!")

        rows = self.run_linker(manifest)

        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("new", "none"), ("old", "he")])
        connection = sqlite3.connect(self.db)
        try:
            legacy = connection.execute(
                "SELECT gender, audio_path FROM audio_variants_schema1"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(legacy, [("male", "voicelines/old.ogg")])

    def test_missing_audio_is_not_linked(self):
        manifest = self.write_manifest({
            "gone": {"Bytes": 4, "DurationSeconds": 1.0,
                     "GenerationFingerprint": "fp"},
        })

        self.assertEqual(self.run_linker(manifest), [])


if __name__ == "__main__":
    unittest.main()
