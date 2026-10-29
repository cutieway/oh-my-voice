"""Resume semantics: what the queue view considers work.

The schema and views come from `xivdata migrate`, so these tests exercise the
real SQL rather than a copy. They need `dotnet`; without it they are skipped.
"""
import importlib.util
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
NOW = "2026-10-29T00:00:00Z"


def load_roster():
    spec = importlib.util.spec_from_file_location("omv_roster", ROOT / "tools" / "roster.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QueueViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("dotnet") is None:
            raise unittest.SkipTest("dotnet not available")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.db = cls.dir / "dialogue.db"
        cls.db.touch()
        xvectors = cls.dir / "xvectors"
        xvectors.mkdir()
        subprocess.run(
            ["dotnet", "run", "--project", str(ROOT / "tools" / "xivdata"), "--",
             "migrate", "--db", str(cls.db), "--xvectors", str(xvectors)],
            cwd=ROOT, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.connection = sqlite3.connect(self.db)
        self.connection.executescript(
            "DELETE FROM dialogs; DELETE FROM speakers; DELETE FROM dialog_speakers;")
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def add_speaker(self, voice, has_scd=1):
        self.connection.execute(
            "INSERT INTO speakers (voice_id, name, npc_key, normalized_name, "
            "speaker_match, has_scd, unvoiced_lines, dialogue_lines, updated_at) "
            "VALUES (?, ?, ?, ?, 'id', ?, 1, 1, ?)",
            (voice, voice.title(), voice, voice, has_scd, NOW))

    def add_line(self, line_id, voice, status="pending", stale=0, has_scd=0,
                 kind="dialogue"):
        row_id = self.connection.execute(
            "SELECT COUNT(*) FROM dialogs").fetchone()[0] + 1
        self.connection.execute(
            "INSERT INTO dialogs (id, lang, source, sheet, row_id, speaker_id, "
            "speaker_match, text_raw, text_macro, text_clean, text_hash, "
            "voice_line_id, text_kind, has_scd, audio_status, audio_stale, "
            "created_at, updated_at) VALUES (?, 'en', 'quest', 'sheet', ?, ?, "
            "'id', 'text', 'text', 'text', 'hash', ?, ?, ?, ?, ?, ?, ?)",
            (f"row-{row_id}", row_id, voice, line_id, kind, has_scd, status,
             stale, NOW, NOW))

    def queued(self, voice=None):
        if voice is None:
            row = self.connection.execute("SELECT COUNT(*) FROM v_generation_queue").fetchone()
        else:
            row = self.connection.execute(
                "SELECT queued FROM v_speaker_workload WHERE voice_id = ?", (voice,)).fetchone()
            return row[0] if row else 0
        return row[0]

    def test_pending_line_is_queued(self):
        self.add_speaker("alpha")
        self.add_line("line-a", "alpha")
        self.assertEqual(self.queued(), 1)
        self.assertEqual(self.queued("alpha"), 1)

    def test_generated_line_leaves_the_queue(self):
        self.add_speaker("alpha")
        self.add_line("line-a", "alpha", status="generated")
        self.assertEqual(self.queued("alpha"), 0)

    def test_stale_generated_line_returns_to_the_queue(self):
        self.add_speaker("alpha")
        self.add_line("line-a", "alpha", status="generated", stale=1)
        self.assertEqual(self.queued("alpha"), 1)

    def test_game_voiced_duplicate_is_never_queued(self):
        self.add_speaker("alpha")
        self.add_line("line-a", "alpha")
        self.add_line("line-a", "alpha", has_scd=1)
        self.assertEqual(self.queued("alpha"), 0)

    def test_pending_copy_wins_over_generated_sibling(self):
        self.add_speaker("alpha")
        # Same line id, two rows: the old text was rendered, the new text is
        # pending. The queue must expose the pending copy, not the generated one.
        self.add_line("line-a", "alpha", status="generated")
        self.add_line("line-a", "alpha", status="pending")
        self.assertEqual(self.queued("alpha"), 1)
        sentence, status = self.connection.execute(
            "SELECT sentence, audio_status FROM v_generation_queue").fetchone()
        self.assertEqual(status, "pending")

    def test_skipped_and_non_dialogue_lines_are_not_queued(self):
        self.add_speaker("alpha")
        self.add_line("skipped", "alpha", status="skipped")
        self.add_line("journal", "alpha", kind="journal")
        self.assertEqual(self.queued("alpha"), 0)

    def test_speaker_without_game_audio_has_no_queue(self):
        self.add_speaker("beta", has_scd=0)
        self.add_line("line-b", "beta")
        self.assertEqual(self.queued("beta"), 0)


class RunOrderTests(unittest.TestCase):
    def setUp(self):
        self.roster = load_roster()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "dialogue.db"
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE speakers (
              voice_id TEXT PRIMARY KEY, npc_id INTEGER, name TEXT, npc_key TEXT,
              has_scd INTEGER, voiced_lines INTEGER, unvoiced_lines INTEGER,
              dialogue_lines INTEGER, generated_lines INTEGER, xvector_path TEXT
            );
            CREATE TABLE v_speaker_workload (
              voice_id TEXT, npc_key TEXT, name TEXT, has_scd INTEGER,
              xvector_path TEXT, queued INTEGER
            );
            INSERT INTO speakers VALUES
              ('big', 1, 'Big', 'big', 1, 10, 30, 40, 0, NULL),
              ('small', 2, 'Small', 'small', 1, 5, 4, 9, 0, NULL),
              ('done', 3, 'Done', 'done', 1, 5, 5, 10, 5, NULL),
              ('nox', 4, 'No X', 'nox', 1, 5, 7, 12, 0, NULL),
              ('moved', 5, 'Moved', 'moved', 1, 5, 3, 8, 0, '/gone/moved.npy');
            INSERT INTO v_speaker_workload VALUES
              ('big', 'big', 'Big', 1, NULL, 30),
              ('small', 'small', 'Small', 1, NULL, 4),
              ('done', 'done', 'Done', 1, NULL, 0),
              ('nox', 'nox', 'No X', 1, NULL, 7),
              ('moved', 'moved', 'Moved', 1, '/gone/moved.npy', 3);
            """)
        connection.commit()
        connection.close()
        for name in ("big", "small", "done", "moved"):
            (self.dir / f"{name}.npy").write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def run_next(self):
        argv = ["roster.py", "--db", str(self.db), "--xvector-dir", str(self.dir), "next"]
        lines = []
        with mock.patch.object(self.roster.sys, "argv", argv), \
                mock.patch("sys.stdout", new_callable=lambda: type(
                    "Sink", (), {"write": lambda _self, text: lines.append(text)})()):
            self.assertEqual(self.roster.main(), 0)
        return "".join(lines).strip().splitlines()

    def test_order_is_largest_queue_first(self):
        self.assertEqual(self.run_next(),
                         ["big\tBig\t30", "small\tSmall\t4", "moved\tMoved\t3"])

    def test_stored_xvector_path_falls_back_to_the_directory(self):
        # 'moved' stores /gone/moved.npy but the file sits in --xvector-dir.
        self.assertIn("moved", [line.split("\t")[0] for line in self.run_next()])

    def test_finished_and_vectorless_speakers_are_skipped(self):
        voices = [line.split("\t")[0] for line in self.run_next()]
        self.assertNotIn("done", voices)
        self.assertNotIn("nox", voices)


if __name__ == "__main__":
    unittest.main()
