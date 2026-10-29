using System.Globalization;
using Microsoft.Data.Sqlite;

namespace XivData;

/// <summary>
/// Re-apply the schema and derived counts to an existing dialogue DB without
/// re-harvesting. Views (v_generation_queue, v_speaker_workload, ...) are
/// recreated on every read-write open, so this is the cheap step after a
/// schema change or a patch-day harvest.
/// </summary>
internal static class MigrateCommand
{
    public static int Run(string[] args)
    {
        string dbPath = Path.Combine(Environment.CurrentDirectory, "data", "dialogue.db");
        string xvectorDir = Path.Combine(Environment.CurrentDirectory, "data", "xvectors");

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
                case "--xvectors":
                    xvectorDir = Path.GetFullPath(Next());
                    break;
                default:
                    throw new ArgumentException($"unknown migrate option: {arg}");
            }
        }

        if (!File.Exists(dbPath))
        {
            Console.Error.WriteLine($"database not found: {dbPath}");
            return 2;
        }

        using var db = DialogueDb.Open(dbPath);
        db.RefreshSpeakers(xvectorDir);
        db.Complete();

        foreach (var (check, failures) in db.Integrity())
        {
            if (failures != 0)
                Console.Error.WriteLine($"integrity: {check} = {failures}");
        }

        using var connection = new SqliteConnection($"Data Source={dbPath};Mode=ReadOnly");
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = """
            SELECT
              (SELECT COUNT(*) FROM dialogs WHERE audio_status = 'generated'),
              (SELECT COUNT(*) FROM v_generation_queue),
              (SELECT COUNT(*) FROM v_speaker_workload WHERE queued > 0);
            """;
        using var reader = command.ExecuteReader();
        if (reader.Read())
        {
            long generated = reader.GetInt64(0);
            long queued = reader.GetInt64(1);
            long speakers = reader.GetInt64(2);
            Console.WriteLine($"{dbPath}: {generated.ToString(CultureInfo.InvariantCulture)} generated line(s), "
                + $"{queued.ToString(CultureInfo.InvariantCulture)} queued, "
                + $"{speakers.ToString(CultureInfo.InvariantCulture)} speaker(s) with work");
        }
        return 0;
    }

    private static void PrintHelp()
    {
        Console.WriteLine("""
            xivdata migrate — re-apply the schema and counts to a dialogue DB.

            Usage:
              xivdata migrate
              xivdata migrate --db data/dialogue.db --xvectors data/xvectors

            Options:
              --db FILE        dialogue DB (default ./data/dialogue.db)
              --xvectors DIR   link x-vectors from this directory (default ./data/xvectors)

            Recreates the derived views and recomputes speaker counts. It never
            touches harvested rows or generated audio.
            """);
    }
}
