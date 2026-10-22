using System.Globalization;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace XivData;

/// <summary>
/// Export one speaker's unvoiced worklist from the dialogue DB as the
/// events.jsonl format the batch generator already consumes.
///
/// This is the bridge between the offline harvest and generation: the DB
/// holds the full corpus, but generation still wants a small, speaker-scoped
/// JSONL file with runtime-sanitized sentences.
/// </summary>
internal static class ExportCommand
{
    public static int Run(string[] args)
    {
        string dbPath = Path.Combine(Environment.CurrentDirectory, "data", "dialogue.db");
        string? speaker = null;
        string? outPath = null;
        string lang = "en";
        int limit = 0;

        for (var i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"missing value for {arg}");
            switch (arg)
            {
                case "-h" or "--help":
                    PrintHelp();
                    return 0;
                case "--db":
                    dbPath = Path.GetFullPath(Next());
                    break;
                case "--speaker":
                    speaker = Next();
                    break;
                case "--out":
                    outPath = Path.GetFullPath(Next());
                    break;
                case "--lang":
                    lang = Next().ToLowerInvariant();
                    break;
                case "--limit":
                    limit = int.Parse(Next(), CultureInfo.InvariantCulture);
                    if (limit < 0) throw new ArgumentException("--limit must be >= 0");
                    break;
                default:
                    throw new ArgumentException($"unknown export option: {arg}");
            }
        }

        if (string.IsNullOrWhiteSpace(speaker))
        {
            Console.Error.WriteLine("export requires --speaker <voice_id|npc_key|npc_id>");
            return 2;
        }
        if (!File.Exists(dbPath))
        {
            Console.Error.WriteLine($"database not found: {dbPath}");
            return 2;
        }

        outPath ??= Path.Combine(Environment.CurrentDirectory, "data", "worklists", SafeFile(speaker) + ".jsonl");

        var builder = new SqliteConnectionStringBuilder
        {
            DataSource = dbPath,
            Mode = SqliteOpenMode.ReadOnly,
            Cache = SqliteCacheMode.Private,
            Pooling = false,
            DefaultTimeout = 30,
        };
        using var connection = new SqliteConnection(builder.ToString());
        connection.Open();

        using var command = connection.CreateCommand();
        command.CommandText = """
            SELECT q.lang, q.voice_line_id, q.speaker_id, q.npc_id, q.npc_key,
                   q.sentence, q.source, q.sheet, q.row_id, q.line_index,
                   s.name AS speaker_name, s.race_id, s.gender,
                   d.text_raw
            FROM v_generation_queue q
            JOIN speakers s ON s.voice_id = q.speaker_id
            JOIN dialogs d ON d.id = q.id
            WHERE q.lang = @lang
              AND (
                q.speaker_id = @value COLLATE NOCASE
                OR q.npc_key = @value COLLATE NOCASE
                OR CAST(q.npc_id AS TEXT) = @value
              )
            ORDER BY q.source, q.sheet, q.row_id, q.line_index
            LIMIT @limit;
            """;
        command.Parameters.AddWithValue("@lang", lang);
        command.Parameters.AddWithValue("@value", speaker);
        command.Parameters.AddWithValue("@limit", limit > 0 ? limit : -1);

        using var reader = command.ExecuteReader();
        string? dir = Path.GetDirectoryName(outPath);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        string tempPath = outPath + ".tmp";
        long exported = 0;
        var bySource = new Dictionary<string, long>(StringComparer.Ordinal);

        using (var writer = new StreamWriter(tempPath, false, new UTF8Encoding(false)))
        {
            writer.NewLine = "\n";
            while (reader.Read())
            {
                string voiceLineId = reader.GetString(1);
                string speakerId = reader.GetString(2);
                string? npcId = reader.IsDBNull(3) ? null : reader.GetInt64(3).ToString(CultureInfo.InvariantCulture);
                string npcKey = reader.IsDBNull(4) ? "" : reader.GetString(4);
                string sentence = reader.GetString(5);
                string source = reader.GetString(6);
                string speakerName = reader.IsDBNull(10) ? npcKey : reader.GetString(10);
                uint raceId = reader.IsDBNull(11) ? 0 : (uint)reader.GetInt64(11);
                byte? gender = reader.IsDBNull(12) ? null : (byte)reader.GetInt64(12);
                string rawSentence = reader.IsDBNull(13) ? sentence : reader.GetString(13);
                var now = DateTimeOffset.UtcNow;

                var evt = new
                {
                    Result = "miss",
                    Id = voiceLineId,
                    Lang = lang,
                    VoiceId = speakerId,
                    NpcId = npcId ?? speakerId,
                    Speaker = npcKey,
                    Sentence = sentence,
                    RawSpeaker = speakerName,
                    RawSentence = rawSentence,
                    Source = source,
                    Zone = "",
                    GameVersion = "xivdata",
                    Npc = new
                    {
                        Name = speakerName,
                        NpcId = npcId is null ? (long?)null : long.Parse(npcId, CultureInfo.InvariantCulture),
                        Match = "canonical",
                        Candidates = 1,
                        Ambiguous = false,
                        RaceId = raceId,
                        Race = "unknown",
                        Gender = gender,
                        Sex = gender switch { 0 => "male", 1 => "female", _ => "unknown" },
                        ExpansionId = (uint?)null,
                        Expansion = "unknown",
                    },
                    Utc = now,
                };
                writer.WriteLine(JsonSerializer.Serialize(evt));
                exported++;
                bySource[source] = bySource.GetValueOrDefault(source) + 1;
            }
        }

        if (exported == 0)
        {
            File.Delete(tempPath);
            Console.Error.WriteLine($"no queued lines for '{speaker}' (lang={lang}).");
            using var suggestions = connection.CreateCommand();
            suggestions.CommandText = """
                SELECT voice_id, name, queued
                FROM v_speaker_workload
                WHERE queued > 0
                ORDER BY queued DESC
                LIMIT 10;
                """;
            using var r = suggestions.ExecuteReader();
            while (r.Read())
                Console.Error.WriteLine($"  {r.GetString(0),-24} {r.GetString(1),-24} queued={r.GetInt64(2)}");
            return 1;
        }

        File.Move(tempPath, outPath, true);
        Console.WriteLine($"exported {exported} queued lines -> {outPath}");
        foreach (var (source, count) in bySource.OrderBy(kv => kv.Key, StringComparer.Ordinal))
            Console.WriteLine($"  {source,-25} {count}");
        Console.WriteLine("note: speaker-scoped worklist; use --merge (or tools/voice.sh) to add it to an existing pack.");
        return 0;
    }

    private static string SafeFile(string value)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var sb = new StringBuilder(value.Length);
        foreach (char ch in value)
            sb.Append(invalid.Contains(ch) || ch == Path.DirectorySeparatorChar ? '_' : ch);
        return sb.ToString();
    }

    private static void PrintHelp()
    {
        Console.WriteLine("""
            xivdata export — write one speaker's generation queue as events.jsonl.

            Usage:
              xivdata export --db data/dialogue.db --speaker alphinaud
              xivdata export --speaker alphinaud --out data/worklists/alphinaud.jsonl
              xivdata export --speaker yshtola --limit 50

            Options:
              --db FILE       dialogue DB (default ./data/dialogue.db)
              --speaker ID    voice_id, npc_key, or numeric npc_id
              --out FILE      output JSONL (default ./data/worklists/<speaker>.jsonl)
              --lang en|de|fr|ja  default en
              --limit N       max rows (0 = all)

            Rows match the plugin's miss-event schema, so the existing
            tools/qwen/generate.py can consume the file directly.
            """);
    }
}
