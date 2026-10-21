using System.Diagnostics;
using System.Globalization;
using System.Text;
using Lumina;
using Lumina.Data;
using Lumina.Excel;
using Lumina.Excel.Sheets;
using Lumina.Text.ReadOnly;

namespace XivData;

/// <summary>
/// Extracts FFXIV cutscene voice SCDs and attributes each line to an NPC.
///
/// The mapping is game-data driven, not filename guessing:
///   cut_scene/* Excel rows carry TEXT_VOICEMAN_&lt;scene&gt;_&lt;line&gt;_&lt;SPEAKER&gt;.
///   TEXT_ -> vo_, drop _SPEAKER -> vo_voiceman_&lt;scene&gt;_&lt;line&gt;.
///   VoiceScdPaths turns that base into cut/&lt;exp&gt;/sound/voicem/...
///   The speaker token is resolved against ENpcResident's normalized names.
/// </summary>
internal static class Program
{
    private static readonly string[] Expansions = ["ffxiv", "ex1", "ex2", "ex3", "ex4", "ex5"];
    private static readonly string[] GenderSuffixes = ["_m_", "_", "_f_"];
    private const int MinVoicedScdBytes = 1024;

    private sealed record Options(
        string SqPack,
        string OutDir,
        Language Language,
        string LangCode,
        int Limit,
        string? SheetFilter,
        string? SpeakerFilter,
        string? Vgmstream,
        bool ExtractAudio,
        bool ByToken);

    private sealed record NpcInfo(uint Id, string Name, uint RaceId, byte Gender);

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

        if (options.SqPack is "help")
        {
            PrintHelp();
            return 0;
        }

        Directory.CreateDirectory(options.OutDir);
        var data = new GameData(options.SqPack, new LuminaOptions
        {
            PanicOnSheetChecksumMismatch = false,
            DefaultExcelLanguage = options.Language,
        });

        var npcIndex = BuildNpcIndex(data, options.Language);
        Console.WriteLine($"NPC names indexed: {npcIndex.Names.Count}");
        Console.WriteLine($"Cutscene sheets: {data.Excel.SheetNames.Count(n => n.StartsWith("cut_scene/", StringComparison.OrdinalIgnoreCase))}");

        var rows = new List<ExtractedLine>();
        var sheets = data.Excel.SheetNames
            .Where(n => n.StartsWith("cut_scene/", StringComparison.OrdinalIgnoreCase))
            .Where(n => options.SheetFilter is null || n.Contains(options.SheetFilter, StringComparison.OrdinalIgnoreCase))
            .OrderBy(n => n, StringComparer.Ordinal)
            .ToArray();

        var unparsed = 0;
        var unvoiced = 0;
        var unresolved = 0;
        var extracted = 0;
        var scdBytes = 0L;
        var unvoicedByPrefix = new Dictionary<string, int>(StringComparer.Ordinal);

        foreach (var sheetName in sheets)
        {
            ExcelSheet<RawRow> sheet;
            try
            {
                sheet = data.Excel.GetSheet<RawRow>(options.Language, sheetName);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"skip {sheetName}: {ex.Message}");
                continue;
            }

            for (var rowIndex = 0; rowIndex < sheet.Count; rowIndex++)
            {
                var row = sheet.GetRowAt(rowIndex);
                var key = row.ReadStringColumn(0).ExtractText();
                if (string.IsNullOrWhiteSpace(key)) continue;
                if (!TryParseKey(key, out var speakerToken, out var audioBase))
                {
                    unparsed++;
                    continue;
                }

                if (options.SpeakerFilter is not null &&
                    !speakerToken.Equals(options.SpeakerFilter, StringComparison.OrdinalIgnoreCase))
                    continue;

                var resolved = FindScd(data, audioBase, options.LangCode);
                if (resolved is null)
                {
                    unvoiced++;
                    var prefix = AudioPrefix(audioBase);
                    unvoicedByPrefix[prefix] = unvoicedByPrefix.GetValueOrDefault(prefix) + 1;
                    continue;
                }

                var text = row.ReadStringColumn(1).ExtractText();
                var npc = npcIndex.Resolve(speakerToken);
                if (npc is null) unresolved++;
                else extracted++;

                var line = new ExtractedLine(
                    Sheet: sheetName,
                    RowId: row.RowId,
                    TextKey: key,
                    AudioBase: audioBase,
                    SpeakerToken: speakerToken,
                    NpcId: npc?.Id,
                    NpcName: npc?.Name ?? string.Empty,
                    RaceId: npc?.RaceId ?? 0,
                    Gender: npc?.Gender ?? 0,
                    ScdPath: resolved.Value.Path,
                    ScdBytes: resolved.Value.Bytes,
                    Text: text);

                rows.Add(line);
                scdBytes += resolved.Value.Bytes;

                if (options.ExtractAudio)
                {
                    Extract(line, data, options);
                }

                if (options.Limit > 0 && rows.Count >= options.Limit)
                    goto Done;
            }
        }

    Done:
        var csvPath = Path.Combine(options.OutDir, "dialogue.csv");
        WriteCsv(csvPath, rows);
        Console.WriteLine();
        Console.WriteLine($"rows with resolved SCD: {rows.Count}");
        Console.WriteLine($"  speaker resolved:      {extracted}");
        Console.WriteLine($"  speaker unresolved:    {unresolved}");
        Console.WriteLine($"  unparsed text keys:    {unparsed}");
        Console.WriteLine($"  keys without SCD:      {unvoiced}");
        if (unvoicedByPrefix.Count > 0)
            Console.WriteLine($"    by prefix:           {string.Join(", ", unvoicedByPrefix.OrderBy(kv => kv.Key, StringComparer.Ordinal).Select(kv => $"{kv.Key}={kv.Value}"))}");
        Console.WriteLine($"  SCD payload:           {scdBytes / 1024.0 / 1024.0:F1} MiB");
        Console.WriteLine($"CSV: {csvPath}");
        if (options.ExtractAudio)
            Console.WriteLine($"Audio: {Path.Combine(options.OutDir, "scd")} and {Path.Combine(options.OutDir, "wav")}");
        return 0;
    }

    private static Options ParseArgs(string[] args)
    {
        string sqpack = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Public/xlcore/ffxiv/game/sqpack");
        string outDir = Path.Combine(Environment.CurrentDirectory, "xivdata-out");
        var language = Language.English;
        string langCode = "en";
        var limit = 0;
        string? sheetFilter = null;
        string? speakerFilter = null;
        string? vgmstream = "vgmstream-cli";
        var extract = false;
        var byToken = false;

        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"missing value for {arg}");
            switch (arg)
            {
                case "-h" or "--help":
                    sqpack = "help";
                    return new Options(sqpack, outDir, language, langCode, limit, sheetFilter, null, vgmstream, extract, byToken);
                case "--sqpack":
                    sqpack = Path.GetFullPath(Next());
                    break;
                case "--game":
                    sqpack = Path.Combine(Path.GetFullPath(Next()), "sqpack");
                    break;
                case "--out":
                    outDir = Path.GetFullPath(Next());
                    break;
                case "--limit":
                    limit = int.Parse(Next(), CultureInfo.InvariantCulture);
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
        return new Options(sqpack, outDir, language, langCode, limit, sheetFilter, speakerFilter, vgmstream, extract, byToken);
    }

    private static void PrintHelp()
    {
        Console.WriteLine("""
            xivdata — extract FFXIV cutscene SCD audio and attribute it to NPCs.

            Usage:
              xivdata [--sqpack DIR] [--out DIR] [--limit N] [--sheet FILTER]
                      [--speaker TOKEN] [--lang en|de|fr|ja] [--extract]
                      [--by-token] [--vgmstream PATH]

            Defaults:
              --sqpack ~/Public/xlcore/ffxiv/game/sqpack
              --out    ./xivdata-out
              --lang   en

            Options:
              --extract    write SCDs and decoded WAVs
              --by-token   group scd/ and wav/ by speaker_token (for x-vectors)

            Output:
              dialogue.csv  sheet,row,text_key,audio_base,speaker_token,npc_id,
                            npc_name,race_id,gender,scd_path,scd_bytes,text
              scd/          raw SCDs (with --extract)
              wav/          decoded WAVs (with --extract; needs vgmstream-cli)
                            grouped by speaker_token with --by-token
            """);
    }

    private static bool TryParseKey(string textKey, out string speakerToken, out string audioBase)
    {
        speakerToken = string.Empty;
        audioBase = string.Empty;
        if (textKey.EndsWith("_NONE_VOICE", StringComparison.OrdinalIgnoreCase))
            textKey = textKey[..^"_NONE_VOICE".Length];

        var parts = textKey.Split('_');
        if (parts.Length != 5) return false;
        if (!parts[0].Equals("TEXT", StringComparison.OrdinalIgnoreCase)) return false;

        speakerToken = parts[4].ToLowerInvariant();
        var withoutSpeaker = textKey[..^(parts[4].Length + 1)];
        audioBase = withoutSpeaker.Replace("TEXT", "vo", StringComparison.OrdinalIgnoreCase).ToLowerInvariant();
        return true;
    }

    private static string AudioPrefix(string audioBase)
    {
        if (audioBase.Contains("manfst", StringComparison.Ordinal)) return "manfst";
        if (audioBase.Contains("voiceman", StringComparison.Ordinal)) return "voiceman";
        return "other";
    }

    private static (string Path, int Bytes)? FindScd(GameData data, string audioBase, string langCode)
    {
        if (audioBase.Length < 17) return null;
        var folder6 = audioBase.Substring(3, 6);
        var folder14 = audioBase.Substring(3, 14);
        var isManFst = audioBase.Contains("manfst", StringComparison.Ordinal);

        foreach (var exp in Expansions)
        {
            foreach (var suffix in GenderSuffixes)
            {
                string path;
                if (isManFst && audioBase.Length >= 12)
                {
                    var folder9 = audioBase.Substring(3, 9);
                    path = $"cut/{exp}/sound/{folder6}/{folder9}/{audioBase[..12]}{suffix}{langCode}.scd";
                }
                else
                {
                    path = $"cut/{exp}/sound/{folder6}/{folder14}/{audioBase}{suffix}{langCode}.scd";
                }

                try
                {
                    if (!data.FileExists(path)) continue;
                    var file = data.GetFile(path);
                    if (file is null || file.Data.Length < MinVoicedScdBytes) continue;
                    return (path, file.Data.Length);
                }
                catch
                {
                    // Lumina throws on some speculative paths; keep scanning.
                }
            }
        }

        return null;
    }

    private static NpcIndex BuildNpcIndex(GameData data, Language language)
    {
        var resident = data.Excel.GetSheet<ENpcResident>(language);
        var baseSheet = data.Excel.GetSheet<ENpcBase>(language);
        var names = new Dictionary<string, List<uint>>(StringComparer.OrdinalIgnoreCase);
        var infos = new Dictionary<uint, NpcInfo>();

        foreach (var row in resident)
        {
            var name = row.Singular.ExtractText().Trim();
            if (name.Length == 0) continue;

            var raceId = 0u;
            var gender = (byte)0;
            if (baseSheet.TryGetRow(row.RowId, out var npcBase))
            {
                raceId = npcBase.Race.RowId;
                gender = npcBase.Gender;
            }

            var info = new NpcInfo(row.RowId, name, raceId, gender);
            infos[row.RowId] = info;

            AddName(names, Normalize(name), row.RowId);
            var firstSpace = name.IndexOf(' ');
            if (firstSpace > 0)
                AddName(names, Normalize(name[..firstSpace]), row.RowId);
        }

        return new NpcIndex(names, infos);
    }

    private static void AddName(Dictionary<string, List<uint>> index, string key, uint id)
    {
        if (key.Length == 0) return;
        if (!index.TryGetValue(key, out var ids))
            index[key] = ids = [];
        if (!ids.Contains(id)) ids.Add(id);
    }

    private static string Normalize(string value)
    {
        var sb = new StringBuilder(value.Length);
        foreach (var ch in value)
        {
            if (ch is ' ' or '\'' or '-') continue;
            sb.Append(char.ToLowerInvariant(ch));
        }
        return sb.ToString();
    }

    private static void Extract(ExtractedLine line, GameData data, Options options)
    {
        var owner = options.ByToken
            ? line.SpeakerToken
            : string.IsNullOrEmpty(line.NpcName) ? "unresolved" : SafeName(line.NpcName);
        var stem = SafeName(line.AudioBase);
        var scdDir = Path.Combine(options.OutDir, "scd", owner);
        var wavDir = Path.Combine(options.OutDir, "wav", owner);
        Directory.CreateDirectory(scdDir);
        Directory.CreateDirectory(wavDir);

        var scdPath = Path.Combine(scdDir, stem + ".scd");
        var wavPath = Path.Combine(wavDir, stem + ".wav");
        if (!File.Exists(scdPath))
        {
            var file = data.GetFile(line.ScdPath) ?? throw new InvalidOperationException($"SCD vanished: {line.ScdPath}");
            File.WriteAllBytes(scdPath, file.Data);
        }

        if (options.Vgmstream is null || File.Exists(wavPath)) return;

        var psi = new ProcessStartInfo(options.Vgmstream)
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add("-o");
        psi.ArgumentList.Add(wavPath);
        psi.ArgumentList.Add(scdPath);
        using var process = Process.Start(psi);
        if (process is null)
        {
            Console.Error.WriteLine($"could not start {options.Vgmstream}; SCD saved only");
            return;
        }
        process.WaitForExit();
        if (process.ExitCode != 0)
            Console.Error.WriteLine($"vgmstream failed ({process.ExitCode}) for {scdPath}");
    }

    private static string SafeName(string value)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var sb = new StringBuilder(value.Length);
        foreach (var ch in value)
            sb.Append(invalid.Contains(ch) || ch == Path.DirectorySeparatorChar ? '_' : ch);
        return sb.ToString();
    }

    private static void WriteCsv(string path, List<ExtractedLine> rows)
    {
        static string Csv(string value) => $"\"{value.Replace("\"", "\"\"")}\"";
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        writer.WriteLine("sheet,row_id,text_key,audio_base,speaker_token,npc_id,npc_name,race_id,gender,scd_path,scd_bytes,text");
        foreach (var r in rows)
        {
            writer.Write(Csv(r.Sheet)); writer.Write(',');
            writer.Write(r.RowId.ToString(CultureInfo.InvariantCulture)); writer.Write(',');
            writer.Write(Csv(r.TextKey)); writer.Write(',');
            writer.Write(Csv(r.AudioBase)); writer.Write(',');
            writer.Write(Csv(r.SpeakerToken)); writer.Write(',');
            writer.Write(r.NpcId?.ToString(CultureInfo.InvariantCulture) ?? ""); writer.Write(',');
            writer.Write(Csv(r.NpcName)); writer.Write(',');
            writer.Write(r.RaceId.ToString(CultureInfo.InvariantCulture)); writer.Write(',');
            writer.Write(r.Gender.ToString(CultureInfo.InvariantCulture)); writer.Write(',');
            writer.Write(Csv(r.ScdPath)); writer.Write(',');
            writer.Write(r.ScdBytes.ToString(CultureInfo.InvariantCulture)); writer.Write(',');
            writer.Write(Csv(r.Text));
            writer.WriteLine();
        }
    }

    private sealed record ExtractedLine(
        string Sheet,
        uint RowId,
        string TextKey,
        string AudioBase,
        string SpeakerToken,
        uint? NpcId,
        string NpcName,
        uint RaceId,
        byte Gender,
        string ScdPath,
        int ScdBytes,
        string Text);

    private sealed class NpcIndex(Dictionary<string, List<uint>> names, Dictionary<uint, NpcInfo> infos)
    {
        public Dictionary<string, List<uint>> Names { get; } = names;

        public NpcInfo? Resolve(string token)
        {
            var key = Normalize(token);
            if (key.Length == 0) return null;
            if (!Names.TryGetValue(key, out var ids) || ids.Count == 0) return null;
            return infos.TryGetValue(ids[0], out var info) ? info : null;
        }
    }
}
