using System.Globalization;
using System.Text.Json;
using Lumina;
using Lumina.Data;

namespace XivData;

/// <summary>
/// Offline FFXIV dialogue harvester.
///
/// Reads only local game data (Lumina). It never hooks the game and never
/// needs Dalamud running. The canonical output is a SQLite build database:
/// every text line, its speaker, whether the game ships an SCD for it, and the
/// generation state. CSV/SCD/WAV remain available for the cutscene pipeline.
/// </summary>
internal static class Program
{
    private static readonly string[] AllSources =
    [
        "cutscene",
        "defaulttalk",
        "balloon",
        "contenttalk",
        "npcyell",
        "instancecontent",
        "publiccontent",
        "partycontent",
        "massivepc",
        "gimmicktalk",
        "goldsaucertalk",
        "quest",
        "custom",
        "raid",
        "content",
        "warp",
        "transport",
        "opening",
        "guild_order",
        "leve",
    ];

    private sealed record Options(
        string SqPack,
        string OutDir,
        string DbPath,
        string XVectorDir,
        Language Language,
        string LangCode,
        int Limit,
        string? SheetFilter,
        string? SpeakerFilter,
        string? Vgmstream,
        bool ExtractAudio,
        bool ByToken,
        IReadOnlyList<string> Sources);

    private static int Main(string[] args)
    {
        Options options;
        try
        {
            options = ParseArgs(args);
        }
        catch (ArgumentException ex)
        {
            Console.Error.WriteLine(ex.Message);
            Console.Error.WriteLine("Try --help.");
            return 2;
        }

        if (options.SqPack == "help")
        {
            PrintHelp();
            return 0;
        }

        try
        {
            var data = new GameData(options.SqPack, new LuminaOptions
            {
                PanicOnSheetChecksumMismatch = false,
                DefaultExcelLanguage = options.Language,
            });

            using var db = DialogueDb.Open(options.DbPath);
            string sources = string.Join(",", options.Sources);
            long runId = db.BeginRun(options.LangCode, options.SqPack, sources, "xivdata/2");
            var result = Harvester.Run(new HarvestOptions
            {
                Data = data,
                Db = db,
                Language = options.Language,
                LangCode = options.LangCode,
                OutDir = options.OutDir,
                Sources = options.Sources,
                RunId = runId,
                SheetFilter = options.SheetFilter,
                SpeakerFilter = options.SpeakerFilter,
                Limit = options.Limit,
                ExtractAudio = options.ExtractAudio,
                Vgmstream = options.Vgmstream,
                ByToken = options.ByToken,
                XVectorDir = options.XVectorDir,
            });

            string countsJson = JsonSerializer.Serialize(new
            {
                result.Stats.Rows,
                result.Stats.WithScd,
                result.Stats.WithoutScd,
                result.Stats.ResolvedSpeakers,
                result.Stats.UnresolvedSpeakers,
                result.Stats.Skipped,
                result.Stats.Errors,
                Sources = result.Stats.BySource,
                Kinds = result.Stats.ByKind,
            });
            db.FinishRun(runId, countsJson, result.Stats.Rows, result.Stats.Errors);

            Console.WriteLine();
            Console.WriteLine($"rows written:          {result.Stats.Rows}");
            Console.WriteLine($"  with SCD:            {result.Stats.WithScd}");
            Console.WriteLine($"  without SCD:         {result.Stats.WithoutScd}");
            Console.WriteLine($"  resolved speaker:    {result.Stats.ResolvedSpeakers}");
            Console.WriteLine($"  unresolved speaker:  {result.Stats.UnresolvedSpeakers}");
            Console.WriteLine($"  skipped:             {result.Stats.Skipped}");
            Console.WriteLine($"  errors:              {result.Stats.Errors}");
            PrintCounts("  by source", result.Stats.BySource);
            PrintCounts("  by kind", result.Stats.ByKind);
            foreach (var (check, failures) in db.Integrity())
            {
                if (failures > 0)
                    Console.WriteLine($"WARN integrity: {check} = {failures}");
            }
            Console.WriteLine($"DB:   {db.Path}");
            if (result.CsvPath is not null && result.CutsceneRows.Count > 0)
                Console.WriteLine($"CSV:  {result.CsvPath}");
            if (options.ExtractAudio && result.CutsceneRows.Count > 0)
                Console.WriteLine($"Audio: {Path.Combine(options.OutDir, "scd")} and {Path.Combine(options.OutDir, "wav")}");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"fatal: {ex.Message}");
            if (ex.InnerException is not null)
                Console.Error.WriteLine($"       {ex.InnerException.Message}");
            return 1;
        }
    }

    private static void PrintCounts(string title, Dictionary<string, long> counts)
    {
        if (counts.Count == 0) return;
        Console.WriteLine(title + ":");
        foreach (var (key, value) in counts.OrderBy(kv => kv.Key, StringComparer.Ordinal))
            Console.WriteLine($"    {key,-22} {value}");
    }

    private static Options ParseArgs(string[] args)
    {
        string sqpack = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Public/xlcore/ffxiv/game/sqpack");
        string outDir = Path.Combine(Environment.CurrentDirectory, "xivdata-out");
        string dbPath = Path.Combine(Environment.CurrentDirectory, "data", "dialogue.db");
        string xvectorDir = Path.Combine(Environment.CurrentDirectory, "data", "xvectors");
        var language = Language.English;
        string langCode = "en";
        var limit = 0;
        string? sheetFilter = null;
        string? speakerFilter = null;
        string? vgmstream = "vgmstream-cli";
        var extract = false;
        var byToken = false;
        var sources = new List<string>();

        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"missing value for {arg}");
            switch (arg)
            {
                case "-h" or "--help":
                    sqpack = "help";
                    return new Options(sqpack, outDir, dbPath, xvectorDir, language, langCode,
                        limit, sheetFilter, speakerFilter, vgmstream, extract, byToken, sources);
                case "--sqpack":
                    sqpack = Path.GetFullPath(Next());
                    break;
                case "--game":
                    sqpack = Path.Combine(Path.GetFullPath(Next()), "sqpack");
                    break;
                case "--out":
                    outDir = Path.GetFullPath(Next());
                    break;
                case "--db":
                    dbPath = Path.GetFullPath(Next());
                    break;
                case "--xvectors":
                    xvectorDir = Path.GetFullPath(Next());
                    break;
                case "--limit":
                    limit = int.Parse(Next(), CultureInfo.InvariantCulture);
                    if (limit < 0) throw new ArgumentException("--limit must be >= 0");
                    break;
                case "--sheet":
                    sheetFilter = Next();
                    break;
                case "--speaker":
                    speakerFilter = Next().ToLowerInvariant();
                    break;
                case "--lang":
                    var lang = Next();
                    (language, langCode) = lang.ToLowerInvariant() switch
                    {
                        "en" => (Language.English, "en"),
                        "de" => (Language.German, "de"),
                        "fr" => (Language.French, "fr"),
                        "ja" => (Language.Japanese, "ja"),
                        _ => throw new ArgumentException($"unsupported language: {lang}"),
                    };
                    break;
                case "--source":
                    AddSources(sources, Next());
                    break;
                case "--extract":
                    extract = true;
                    break;
                case "--by-token":
                    byToken = true;
                    break;
                case "--vgmstream":
                    vgmstream = Next();
                    break;
                default:
                    throw new ArgumentException($"unknown option: {arg}");
            }
        }

        if (!Directory.Exists(sqpack))
            throw new ArgumentException($"sqpack directory not found: {sqpack}");
        if (sources.Count == 0) sources.Add("cutscene");
        if (extract && !sources.Contains("cutscene", StringComparer.OrdinalIgnoreCase))
            throw new ArgumentException("--extract only applies to the cutscene source; add --source cutscene");
        return new Options(sqpack, outDir, dbPath, xvectorDir, language, langCode,
            limit, sheetFilter, speakerFilter, vgmstream, extract, byToken, sources);
    }

    private static void AddSources(List<string> sources, string value)
    {
        foreach (string raw in value.Split([',', ' ', ';'], StringSplitOptions.RemoveEmptyEntries))
        {
            string source = raw.Trim().ToLowerInvariant();
            if (source == "all")
            {
                foreach (string all in AllSources)
                    if (!sources.Contains(all, StringComparer.OrdinalIgnoreCase)) sources.Add(all);
                continue;
            }
            if (source is "scripted")
            {
                if (!sources.Contains(source, StringComparer.OrdinalIgnoreCase)) sources.Add(source);
                continue;
            }
            if (!sources.Contains(source, StringComparer.OrdinalIgnoreCase)) sources.Add(source);
        }
    }

    private static void PrintHelp()
    {
        Console.WriteLine("""
            xivdata — offline FFXIV dialogue harvester (SQLite).

            Usage:
              xivdata [--sqpack DIR] [--out DIR] [--db FILE] [--source LIST]
                      [--limit N] [--sheet FILTER] [--speaker TOKEN]
                      [--lang en|de|fr|ja] [--extract] [--by-token]
                      [--xvectors DIR] [--vgmstream PATH]

            Defaults:
              --sqpack   ~/Public/xlcore/ffxiv/game/sqpack
              --out      ./xivdata-out
              --db       ./data/dialogue.db
              --xvectors ./data/xvectors
              --lang     en
              --source   cutscene

            Sources:
              cutscene, defaulttalk, balloon, contenttalk, npcyell,
              instancecontent, publiccontent, partycontent, massivepc,
              gimmicktalk, goldsaucertalk, quest, custom, raid, content,
              warp, transport, opening, guild_order, leve, scripted, all

            Notes:
              --source all is the full text corpus (~252k dialogue, 379k total rows).
              --limit caps total rows written (0 = no cap).
              --extract writes SCD/WAV for cutscene rows that have game audio.
              The DB is the source of truth; dialogue.csv is cutscene-only.

            Tables/views:
              dialogs, dialog_speakers, speakers, harvest_runs
              v_generation_queue, v_speaker_workload, v_pack_npcs, v_integrity

            Useful next-step query:
              SELECT * FROM v_generation_queue
              WHERE speaker_id = 'alphinaud' ORDER BY source, sheet, row_id;
            """);
    }
}
