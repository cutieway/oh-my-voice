using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Data.Sqlite;

namespace XivData;

public sealed record SpeakerRow(
    string VoiceId,
    uint? NpcId,
    string Name,
    string NpcKey,
    string NormalizedName,
    uint RaceId,
    byte? Gender,
    string Match);

public sealed record SpeakerRef(
    string VoiceId,
    uint? NpcId,
    string Match,
    bool Primary);

public sealed record DialogRow(
    string Id,
    string Lang,
    string Source,
    string Sheet,
    uint RowId,
    int LineIndex,
    string? TextKey,
    string? AudioBase,
    string? SpeakerId,
    uint? NpcId,
    string? SpeakerName,
    string NpcKey,
    string SpeakerMatch,
    string TextRaw,
    string TextMacro,
    string TextClean,
    string TextHash,
    string? VoiceLineId,
    string TextKind,
    bool HasScd,
    string? ScdPath,
    int? ScdBytes,
    string? ScdLang,
    string AudioStatus,
    DateTimeOffset Now);

/// <summary>
/// Offline dialogue store. This is the build database: harvest, review, edit,
/// and generation state. Runtime packs are still exported from it; the plugin
/// never reads this file.
///
/// WAL + busy_timeout keep the DB editable while the game is running.
/// </summary>
public sealed class DialogueDb : IDisposable
{
    public const int SchemaVersion = 1;

    private readonly SqliteConnection connection;
    private readonly SqliteCommand upsertSpeaker;
    private readonly SqliteCommand upsertDialog;
    private readonly SqliteCommand upsertDialogSpeaker;
    private readonly SqliteCommand upsertRun;
    private readonly SqliteCommand finishRun;
    private SqliteTransaction? batch;

    public string Path { get; }
    public bool FtsAvailable { get; }

    private DialogueDb(string path, SqliteConnection connection, bool ftsAvailable)
    {
        Path = path;
        this.connection = connection;
        FtsAvailable = ftsAvailable;
        upsertSpeaker = Prepare(SpeakerSql);
        upsertDialog = Prepare(DialogSql);
        upsertDialogSpeaker = Prepare(DialogSpeakerSql);
        upsertRun = Prepare(RunSql);
        finishRun = Prepare(FinishRunSql);
        PrepareParameters();
    }

    public static DialogueDb Open(string path)
    {
        string fullPath = path == ":memory:" ? path : System.IO.Path.GetFullPath(path);
        if (fullPath != ":memory:")
        {
            string? dir = System.IO.Path.GetDirectoryName(fullPath);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        }

        var builder = new SqliteConnectionStringBuilder
        {
            DataSource = fullPath,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Private,
            Pooling = true,
            DefaultTimeout = 30,
        };
        var connection = new SqliteConnection(builder.ToString());
        connection.Open();

        Execute(connection, "PRAGMA journal_mode=WAL;");
        Execute(connection, "PRAGMA synchronous=NORMAL;");
        Execute(connection, "PRAGMA busy_timeout=5000;");
        Execute(connection, "PRAGMA foreign_keys=ON;");
        Execute(connection, "PRAGMA temp_store=MEMORY;");
        Execute(connection, "PRAGMA mmap_size=268435456;");

        int version = Convert.ToInt32(Scalar(connection, "PRAGMA user_version;"), CultureInfo.InvariantCulture);
        if (version > SchemaVersion)
            throw new InvalidOperationException(
                $"dialogue DB schema {version} is newer than this tool ({SchemaVersion}); update xivdata.");

        bool ftsAvailable = CreateSchema(connection);
        Execute(connection, $"PRAGMA user_version={SchemaVersion};");
        return new DialogueDb(fullPath, connection, ftsAvailable);
    }

    public void BeginBatch()
    {
        batch?.Dispose();
        batch = connection.BeginTransaction();
        upsertSpeaker.Transaction = batch;
        upsertDialog.Transaction = batch;
        upsertDialogSpeaker.Transaction = batch;
    }

    public void CommitBatch()
    {
        if (batch is null) return;
        batch.Commit();
        batch.Dispose();
        batch = null;
        upsertSpeaker.Transaction = null;
        upsertDialog.Transaction = null;
        upsertDialogSpeaker.Transaction = null;
    }

    public void AbortBatch()
    {
        if (batch is null) return;
        try { batch.Rollback(); } catch { }
        batch.Dispose();
        batch = null;
        upsertSpeaker.Transaction = null;
        upsertDialog.Transaction = null;
        upsertDialogSpeaker.Transaction = null;
    }

    public long BeginRun(string lang, string sqpack, string sources, string toolVersion)
    {
        upsertRun.Parameters["@started"].Value = Now();
        upsertRun.Parameters["@lang"].Value = lang;
        upsertRun.Parameters["@sqpack"].Value = sqpack;
        upsertRun.Parameters["@sources"].Value = sources;
        upsertRun.Parameters["@tool"].Value = toolVersion;
        upsertRun.ExecuteNonQuery();
        return Convert.ToInt64(Scalar(connection, "SELECT last_insert_rowid();"), CultureInfo.InvariantCulture);
    }

    public void FinishRun(long runId, string countsJson, long rows, long errors)
    {
        finishRun.Parameters["@id"].Value = runId;
        finishRun.Parameters["@finished"].Value = Now();
        finishRun.Parameters["@counts"].Value = countsJson;
        finishRun.Parameters["@rows"].Value = rows;
        finishRun.Parameters["@errors"].Value = errors;
        finishRun.ExecuteNonQuery();
    }

    public void UpsertSpeaker(SpeakerRow speaker, long runId)
    {
        var p = upsertSpeaker.Parameters;
        p["@voice"].Value = speaker.VoiceId;
        p["@npc"].Value = speaker.NpcId.HasValue ? speaker.NpcId.Value : DBNull.Value;
        p["@name"].Value = speaker.Name;
        p["@npc_key"].Value = speaker.NpcKey;
        p["@norm"].Value = speaker.NormalizedName;
        p["@race"].Value = speaker.RaceId;
        p["@gender"].Value = speaker.Gender.HasValue ? speaker.Gender.Value : DBNull.Value;
        p["@match"].Value = speaker.Match;
        p["@run"].Value = runId;
        p["@now"].Value = Now();
        upsertSpeaker.ExecuteNonQuery();
    }

    public void UpsertDialog(DialogRow row, long runId, IReadOnlyList<SpeakerRef> speakers)
    {
        var p = upsertDialog.Parameters;
        p["@id"].Value = row.Id;
        p["@lang"].Value = row.Lang;
        p["@source"].Value = row.Source;
        p["@sheet"].Value = row.Sheet;
        p["@row"].Value = row.RowId;
        p["@line"].Value = row.LineIndex;
        p["@key"].Value = Db(row.TextKey);
        p["@audio"].Value = Db(row.AudioBase);
        p["@speaker"].Value = Db(row.SpeakerId);
        p["@npc"].Value = row.NpcId.HasValue ? row.NpcId.Value : DBNull.Value;
        p["@speaker_name"].Value = Db(row.SpeakerName);
        p["@npc_key"].Value = row.NpcKey;
        p["@speaker_match"].Value = row.SpeakerMatch;
        p["@raw"].Value = row.TextRaw;
        p["@macro"].Value = row.TextMacro;
        p["@clean"].Value = row.TextClean;
        p["@hash"].Value = row.TextHash;
        p["@line_id"].Value = Db(row.VoiceLineId);
        p["@kind"].Value = row.TextKind;
        p["@has_scd"].Value = row.HasScd ? 1 : 0;
        p["@scd"].Value = Db(row.ScdPath);
        p["@scd_bytes"].Value = row.ScdBytes.HasValue ? row.ScdBytes.Value : DBNull.Value;
        p["@scd_lang"].Value = Db(row.ScdLang);
        p["@status"].Value = row.AudioStatus;
        p["@run"].Value = runId;
        p["@now"].Value = row.Now.ToString("O", CultureInfo.InvariantCulture);
        upsertDialog.ExecuteNonQuery();

        foreach (var speaker in speakers)
        {
            var q = upsertDialogSpeaker.Parameters;
            q["@dialog"].Value = row.Id;
            q["@speaker"].Value = speaker.VoiceId;
            q["@npc"].Value = speaker.NpcId.HasValue ? speaker.NpcId.Value : DBNull.Value;
            q["@match"].Value = speaker.Match;
            q["@primary"].Value = speaker.Primary ? 1 : 0;
            upsertDialogSpeaker.ExecuteNonQuery();
        }
    }

    /// <summary>Recompute derived speaker counts and link existing x-vectors.</summary>
    public void RefreshSpeakers(string xvectorDir)
    {
        // One aggregate scan, not four correlated subqueries per speaker.
        // UPDATE ... FROM is available in the SQLite bundled with Microsoft.Data.Sqlite.
        Execute(connection, """
            WITH stats AS (
              SELECT speaker_id,
                     SUM(CASE WHEN has_scd = 1 THEN 1 ELSE 0 END) AS voiced,
                     SUM(CASE WHEN has_scd = 0 AND text_kind IN ('dialogue','narration') THEN 1 ELSE 0 END) AS unvoiced,
                     SUM(CASE WHEN text_kind IN ('dialogue','narration') THEN 1 ELSE 0 END) AS dialogue,
                     SUM(CASE WHEN audio_status = 'generated' THEN 1 ELSE 0 END) AS generated
              FROM dialogs
              WHERE speaker_id IS NOT NULL
              GROUP BY speaker_id
            )
            UPDATE speakers SET
              voiced_lines = COALESCE(stats.voiced, 0),
              unvoiced_lines = COALESCE(stats.unvoiced, 0),
              dialogue_lines = COALESCE(stats.dialogue, 0),
              generated_lines = COALESCE(stats.generated, 0),
              has_scd = CASE WHEN COALESCE(stats.voiced, 0) > 0 THEN 1 ELSE 0 END,
              updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            FROM stats
            WHERE speakers.voice_id = stats.speaker_id;
            """);

        if (string.IsNullOrWhiteSpace(xvectorDir) || !Directory.Exists(xvectorDir)) return;

        using var select = connection.CreateCommand();
        select.CommandText = "SELECT voice_id FROM speakers;";
        var ids = new List<string>();
        using (var reader = select.ExecuteReader())
        {
            while (reader.Read()) ids.Add(reader.GetString(0));
        }

        using var update = connection.CreateCommand();
        update.CommandText = """
            UPDATE speakers SET xvector_path = @path, xvector_sha256 = @sha, updated_at = @now
            WHERE voice_id = @voice;
            """;
        var pathParam = update.Parameters.Add("@path", SqliteType.Text);
        var shaParam = update.Parameters.Add("@sha", SqliteType.Text);
        var nowParam = update.Parameters.Add("@now", SqliteType.Text);
        var voiceParam = update.Parameters.Add("@voice", SqliteType.Text);
        using var tx = connection.BeginTransaction();
        update.Transaction = tx;
        foreach (string voice in ids)
        {
            string path = System.IO.Path.Combine(xvectorDir, voice + ".npy");
            if (!File.Exists(path)) continue;
            pathParam.Value = System.IO.Path.GetFullPath(path);
            shaParam.Value = Sha256File(path);
            nowParam.Value = Now();
            voiceParam.Value = voice;
            update.ExecuteNonQuery();
        }
        tx.Commit();
    }

    public IReadOnlyList<(string Check, long Failures)> Integrity()
    {
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT check_name, failures FROM v_integrity ORDER BY check_name;";
        using var reader = command.ExecuteReader();
        var results = new List<(string, long)>();
        while (reader.Read())
            results.Add((reader.GetString(0), reader.GetInt64(1)));
        return results;
    }

    public void Complete()
    {
        Execute(connection, "PRAGMA optimize;");
        Execute(connection, "ANALYZE;");
    }

    public void Dispose()
    {
        upsertSpeaker.Dispose();
        upsertDialog.Dispose();
        upsertDialogSpeaker.Dispose();
        upsertRun.Dispose();
        finishRun.Dispose();
        batch?.Dispose();
        connection.Dispose();
    }

    private SqliteCommand Prepare(string sql)
    {
        var command = connection.CreateCommand();
        command.CommandText = sql;
        return command;
    }

    private void PrepareParameters()
    {
        Add(upsertRun, "@started", SqliteType.Text);
        Add(upsertRun, "@lang", SqliteType.Text);
        Add(upsertRun, "@sqpack", SqliteType.Text);
        Add(upsertRun, "@sources", SqliteType.Text);
        Add(upsertRun, "@tool", SqliteType.Text);

        Add(finishRun, "@id", SqliteType.Integer);
        Add(finishRun, "@finished", SqliteType.Text);
        Add(finishRun, "@counts", SqliteType.Text);
        Add(finishRun, "@rows", SqliteType.Integer);
        Add(finishRun, "@errors", SqliteType.Integer);

        Add(upsertSpeaker, "@voice", SqliteType.Text);
        Add(upsertSpeaker, "@npc", SqliteType.Integer);
        Add(upsertSpeaker, "@name", SqliteType.Text);
        Add(upsertSpeaker, "@npc_key", SqliteType.Text);
        Add(upsertSpeaker, "@norm", SqliteType.Text);
        Add(upsertSpeaker, "@race", SqliteType.Integer);
        Add(upsertSpeaker, "@gender", SqliteType.Integer);
        Add(upsertSpeaker, "@match", SqliteType.Text);
        Add(upsertSpeaker, "@run", SqliteType.Integer);
        Add(upsertSpeaker, "@now", SqliteType.Text);

        foreach (string name in new[]
        {
            "@id", "@lang", "@source", "@sheet", "@key", "@audio", "@speaker",
            "@speaker_name", "@npc_key", "@speaker_match", "@raw", "@macro", "@clean", "@hash",
            "@line_id", "@kind", "@scd", "@scd_lang", "@status", "@now",
        })
            Add(upsertDialog, name, SqliteType.Text);
        foreach (string name in new[] { "@row", "@line", "@npc", "@has_scd", "@scd_bytes", "@run" })
            Add(upsertDialog, name, SqliteType.Integer);

        Add(upsertDialogSpeaker, "@dialog", SqliteType.Text);
        Add(upsertDialogSpeaker, "@speaker", SqliteType.Text);
        Add(upsertDialogSpeaker, "@npc", SqliteType.Integer);
        Add(upsertDialogSpeaker, "@match", SqliteType.Text);
        Add(upsertDialogSpeaker, "@primary", SqliteType.Integer);
    }

    private static void Add(SqliteCommand command, string name, SqliteType type) =>
        command.Parameters.Add(name, type);

    private static object Db(object? value) => value ?? DBNull.Value;

    private static string Now() => DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);

    private static string Sha256File(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexStringLower(SHA256.HashData(stream));
    }

    private static void Execute(SqliteConnection connection, string sql)
    {
        using var command = connection.CreateCommand();
        command.CommandText = sql;
        command.ExecuteNonQuery();
    }

    private static object? Scalar(SqliteConnection connection, string sql)
    {
        using var command = connection.CreateCommand();
        command.CommandText = sql;
        return command.ExecuteScalar();
    }

    private static bool CreateSchema(SqliteConnection connection)
    {
        Execute(connection, """
            CREATE TABLE IF NOT EXISTS harvest_runs (
              id INTEGER PRIMARY KEY,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              lang TEXT NOT NULL,
              sqpack TEXT NOT NULL,
              sources TEXT NOT NULL,
              tool_version TEXT NOT NULL,
              rows_upserted INTEGER NOT NULL DEFAULT 0,
              errors INTEGER NOT NULL DEFAULT 0,
              counts_json TEXT
            );

            CREATE TABLE IF NOT EXISTS speakers (
              voice_id TEXT PRIMARY KEY,
              npc_id INTEGER,
              name TEXT NOT NULL,
              npc_key TEXT NOT NULL,
              normalized_name TEXT NOT NULL,
              race_id INTEGER NOT NULL DEFAULT 0,
              gender INTEGER,
              speaker_match TEXT NOT NULL,
              has_scd INTEGER NOT NULL DEFAULT 0,
              voiced_lines INTEGER NOT NULL DEFAULT 0,
              unvoiced_lines INTEGER NOT NULL DEFAULT 0,
              dialogue_lines INTEGER NOT NULL DEFAULT 0,
              generated_lines INTEGER NOT NULL DEFAULT 0,
              xvector_path TEXT,
              xvector_sha256 TEXT,
              notes TEXT,
              first_seen_run INTEGER,
              last_seen_run INTEGER,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dialogs (
              id TEXT PRIMARY KEY,
              lang TEXT NOT NULL,
              source TEXT NOT NULL,
              sheet TEXT NOT NULL,
              row_id INTEGER NOT NULL,
              line_index INTEGER NOT NULL DEFAULT 0,
              text_key TEXT,
              audio_base TEXT,
              speaker_id TEXT,
              npc_id INTEGER,
              speaker_name TEXT,
              npc_key TEXT,
              speaker_match TEXT NOT NULL,
              text_raw TEXT NOT NULL,
              text_macro TEXT NOT NULL,
              text_clean TEXT NOT NULL,
              text_hash TEXT NOT NULL,
              voice_line_id TEXT,
              text_kind TEXT NOT NULL,
              has_scd INTEGER NOT NULL DEFAULT 0,
              scd_path TEXT,
              scd_bytes INTEGER,
              scd_lang TEXT,
              audio_status TEXT NOT NULL DEFAULT 'pending',
              audio_path TEXT,
              audio_stale INTEGER NOT NULL DEFAULT 0,
              duration_ms INTEGER,
              generation_profile TEXT,
              error TEXT,
              notes TEXT,
              first_seen_run INTEGER,
              last_seen_run INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(lang, source, sheet, row_id, line_index)
            );

            CREATE TABLE IF NOT EXISTS dialog_speakers (
              dialog_id TEXT NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
              speaker_id TEXT NOT NULL,
              npc_id INTEGER,
              match_source TEXT NOT NULL,
              is_primary INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(dialog_id, speaker_id)
            );

            CREATE INDEX IF NOT EXISTS idx_dialogs_lang_speaker_scd
              ON dialogs(lang, speaker_id, has_scd, audio_status);
            CREATE INDEX IF NOT EXISTS idx_dialogs_lang_source
              ON dialogs(lang, source, text_kind);
            CREATE INDEX IF NOT EXISTS idx_dialogs_sheet_row
              ON dialogs(sheet, row_id, line_index);
            CREATE INDEX IF NOT EXISTS idx_dialogs_line_id
              ON dialogs(lang, voice_line_id);
            CREATE INDEX IF NOT EXISTS idx_dialogs_npc
              ON dialogs(npc_id, has_scd);
            CREATE INDEX IF NOT EXISTS idx_dialogs_audio_base
              ON dialogs(audio_base);
            CREATE INDEX IF NOT EXISTS idx_dialogs_stale
              ON dialogs(has_scd, audio_status, audio_stale);
            CREATE INDEX IF NOT EXISTS idx_dialogs_speaker_queue
              ON dialogs(speaker_id, has_scd, text_kind, audio_status);
            CREATE INDEX IF NOT EXISTS idx_dialogs_kind_queue
              ON dialogs(has_scd, text_kind, audio_status);
            CREATE INDEX IF NOT EXISTS idx_dialog_speakers_speaker
              ON dialog_speakers(speaker_id, is_primary);
            CREATE INDEX IF NOT EXISTS idx_speakers_scd
              ON speakers(has_scd, voiced_lines DESC);
            """);

        // FTS5 is optional: the DB must still work on a SQLite build without it.
        bool ftsCreated = false;
        try
        {
            int exists = Convert.ToInt32(Scalar(connection,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='dialogs_fts';"),
                CultureInfo.InvariantCulture);
            Execute(connection, """
                CREATE VIRTUAL TABLE IF NOT EXISTS dialogs_fts USING fts5(text_clean, tokenize='unicode61');
                """);
            if (exists == 0)
            {
                Execute(connection, """
                    INSERT INTO dialogs_fts(rowid, text_clean)
                    SELECT rowid, text_clean FROM dialogs;
                    """);
            }
            Execute(connection, """
                CREATE TRIGGER IF NOT EXISTS dialogs_fts_ai AFTER INSERT ON dialogs BEGIN
                  INSERT INTO dialogs_fts(rowid, text_clean) VALUES (new.rowid, new.text_clean);
                END;
                CREATE TRIGGER IF NOT EXISTS dialogs_fts_ad AFTER DELETE ON dialogs BEGIN
                  DELETE FROM dialogs_fts WHERE rowid = old.rowid;
                END;
                CREATE TRIGGER IF NOT EXISTS dialogs_fts_au AFTER UPDATE OF text_clean ON dialogs BEGIN
                  DELETE FROM dialogs_fts WHERE rowid = old.rowid;
                  INSERT INTO dialogs_fts(rowid, text_clean) VALUES (new.rowid, new.text_clean);
                END;
                """);
            ftsCreated = true;
        }
        catch (SqliteException ex)
        {
            Console.Error.WriteLine($"FTS5 unavailable ({ex.Message}); text search falls back to LIKE.");
        }

        Execute(connection, """
            DROP VIEW IF EXISTS v_speaker_workload;
            DROP VIEW IF EXISTS v_generation_queue;
            DROP VIEW IF EXISTS v_pack_npcs;
            DROP VIEW IF EXISTS v_integrity;
            DROP VIEW IF EXISTS v_speakers;

            CREATE VIEW IF NOT EXISTS v_speakers AS
            SELECT voice_id, npc_id, name, npc_key, has_scd, voiced_lines, unvoiced_lines,
                   dialogue_lines, generated_lines, xvector_path
            FROM speakers;

            CREATE VIEW IF NOT EXISTS v_pack_npcs AS
            SELECT npc_key AS speaker, npc_id, voice_id, name
            FROM speakers
            WHERE npc_key <> '';

            CREATE VIEW IF NOT EXISTS v_generation_queue AS
            WITH candidates AS (
              SELECT d.id, d.lang, d.speaker_id, d.npc_id, d.speaker_name, d.npc_key,
                     d.text_clean AS sentence, d.voice_line_id, d.source, d.sheet, d.row_id,
                     d.line_index, d.audio_status, d.audio_stale,
                     s.name AS speaker_display, s.xvector_path,
                     ROW_NUMBER() OVER (
                       PARTITION BY d.lang, d.voice_line_id
                       ORDER BY d.source, d.sheet, d.row_id, d.line_index, d.id
                     ) AS rn
              FROM dialogs d
              JOIN speakers s ON s.voice_id = d.speaker_id
              WHERE d.has_scd = 0
                AND d.voice_line_id IS NOT NULL
                AND d.text_kind IN ('dialogue','narration')
                AND s.has_scd = 1
                AND d.audio_status <> 'skipped'
                AND (d.audio_status IN ('pending','failed') OR d.audio_stale = 1)
                AND NOT EXISTS (
                  SELECT 1 FROM dialogs g
                  WHERE g.lang = d.lang
                    AND g.voice_line_id = d.voice_line_id
                    AND g.has_scd = 1
                )
            )
            SELECT id, lang, speaker_id, npc_id, speaker_name, npc_key, sentence,
                   voice_line_id, source, sheet, row_id, line_index,
                   audio_status, audio_stale, speaker_display, xvector_path
            FROM candidates
            WHERE rn = 1;

            CREATE VIEW IF NOT EXISTS v_speaker_workload AS
            SELECT s.voice_id, s.npc_key, s.name, s.has_scd, s.xvector_path,
                   COUNT(q.id) AS queued
            FROM speakers s
            LEFT JOIN v_generation_queue q ON q.speaker_id = s.voice_id
            GROUP BY s.voice_id
            ORDER BY queued DESC, s.name;

            CREATE VIEW IF NOT EXISTS v_integrity AS
            SELECT 'dialog_npc_mismatch' AS check_name, COUNT(*) AS failures
            FROM dialogs d
            JOIN speakers s ON s.voice_id = d.speaker_id
            WHERE d.speaker_id IS NOT NULL
              AND COALESCE(d.npc_id, 0) <> COALESCE(s.npc_id, 0)
            UNION ALL
            SELECT 'missing_voice_line_id', COUNT(*)
            FROM dialogs
            WHERE speaker_id IS NOT NULL AND voice_line_id IS NULL
            UNION ALL
            SELECT 'missing_npc_key', COUNT(*)
            FROM dialogs
            WHERE speaker_id IS NOT NULL AND (npc_key IS NULL OR npc_key = '')
            UNION ALL
            SELECT 'duplicate_line_id_across_speakers', COUNT(*)
            FROM (
              SELECT lang, voice_line_id
              FROM dialogs
              WHERE voice_line_id IS NOT NULL
              GROUP BY lang, voice_line_id
              HAVING COUNT(DISTINCT speaker_id) > 1
            );
            """);

        return ftsCreated;
    }

    private const string SpeakerSql = """
        INSERT INTO speakers (
          voice_id, npc_id, name, npc_key, normalized_name, race_id, gender, speaker_match,
          first_seen_run, last_seen_run, updated_at
        ) VALUES (
          @voice, @npc, @name, @npc_key, @norm, @race, @gender, @match,
          @run, @run, @now
        )
        ON CONFLICT(voice_id) DO UPDATE SET
          npc_id = CASE WHEN excluded.npc_id IS NOT NULL THEN excluded.npc_id ELSE speakers.npc_id END,
          name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE speakers.name END,
          npc_key = CASE WHEN excluded.npc_key <> '' THEN excluded.npc_key ELSE speakers.npc_key END,
          normalized_name = excluded.normalized_name,
          race_id = CASE WHEN excluded.race_id > 0 THEN excluded.race_id ELSE speakers.race_id END,
          gender = COALESCE(excluded.gender, speakers.gender),
          speaker_match = excluded.speaker_match,
          last_seen_run = excluded.last_seen_run,
          updated_at = excluded.updated_at;
        """;

    private const string DialogSql = """
        INSERT INTO dialogs (
          id, lang, source, sheet, row_id, line_index, text_key, audio_base,
          speaker_id, npc_id, speaker_name, npc_key, speaker_match, text_raw, text_macro,
          text_clean, text_hash, voice_line_id, text_kind, has_scd, scd_path,
          scd_bytes, scd_lang, audio_status, first_seen_run, last_seen_run,
          created_at, updated_at
        ) VALUES (
          @id, @lang, @source, @sheet, @row, @line, @key, @audio,
          @speaker, @npc, @speaker_name, @npc_key, @speaker_match, @raw, @macro,
          @clean, @hash, @line_id, @kind, @has_scd, @scd,
          @scd_bytes, @scd_lang, @status, @run, @run,
          @now, @now
        )
        ON CONFLICT(id) DO UPDATE SET
          lang = excluded.lang,
          source = excluded.source,
          sheet = excluded.sheet,
          row_id = excluded.row_id,
          line_index = excluded.line_index,
          text_key = excluded.text_key,
          audio_base = excluded.audio_base,
          speaker_id = excluded.speaker_id,
          npc_id = excluded.npc_id,
          speaker_name = excluded.speaker_name,
          npc_key = excluded.npc_key,
          speaker_match = excluded.speaker_match,
          text_raw = excluded.text_raw,
          text_macro = excluded.text_macro,
          text_clean = excluded.text_clean,
          text_hash = excluded.text_hash,
          voice_line_id = excluded.voice_line_id,
          text_kind = excluded.text_kind,
          has_scd = excluded.has_scd,
          scd_path = excluded.scd_path,
          scd_bytes = excluded.scd_bytes,
          scd_lang = excluded.scd_lang,
          audio_stale = CASE
            WHEN dialogs.text_hash <> excluded.text_hash
              OR COALESCE(dialogs.speaker_id, '') <> COALESCE(excluded.speaker_id, '')
              OR COALESCE(dialogs.npc_id, 0) <> COALESCE(excluded.npc_id, 0)
              OR dialogs.has_scd <> excluded.has_scd
            THEN 1 ELSE dialogs.audio_stale
          END,
          audio_status = CASE
            WHEN excluded.has_scd = 1 THEN
              CASE WHEN dialogs.audio_status IN ('generated','failed','skipped')
                   THEN dialogs.audio_status ELSE 'covered' END
            WHEN dialogs.audio_status IN ('generated','failed','skipped')
              THEN dialogs.audio_status
            ELSE excluded.audio_status
          END,
          first_seen_run = COALESCE(dialogs.first_seen_run, excluded.first_seen_run),
          last_seen_run = excluded.last_seen_run,
          updated_at = excluded.updated_at;
        """;

    private const string DialogSpeakerSql = """
        INSERT INTO dialog_speakers (dialog_id, speaker_id, npc_id, match_source, is_primary)
        VALUES (@dialog, @speaker, @npc, @match, @primary)
        ON CONFLICT(dialog_id, speaker_id) DO UPDATE SET
          npc_id = excluded.npc_id,
          match_source = excluded.match_source,
          is_primary = excluded.is_primary;
        """;

    private const string RunSql = """
        INSERT INTO harvest_runs (started_at, lang, sqpack, sources, tool_version)
        VALUES (@started, @lang, @sqpack, @sources, @tool);
        """;

    private const string FinishRunSql = """
        UPDATE harvest_runs SET
          finished_at = @finished,
          counts_json = @counts,
          rows_upserted = @rows,
          errors = @errors
        WHERE id = @id;
        """;
}
