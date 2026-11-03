using System.Reflection;
using System.Text.Json;
using Lumina;
using Lumina.Data;
using Lumina.Excel.Sheets;
using Lumina.Text.ReadOnly;

namespace XivData;

/// <summary>Exports canonical ENpcResident identity metadata without writing to the dialogue DB.</summary>
internal static class NpcIndexCommand
{
    private sealed record Options(string SqPack, Language Language, string LanguageCode);
    private sealed record NpcRecord(
        uint NpcId,
        string Name,
        uint? RaceId,
        string? RaceName,
        uint? TribeId,
        string? TribeName,
        string Sex,
        uint? BodyType,
        uint? Height,
        float? Scale,
        byte? BustOrTone1,
        byte? ExtraFeature2OrBust,
        byte? Jaw,
        byte? Mouth,
        byte? Face,
        byte? Nose,
        byte? EyeShape,
        byte? Eyebrows,
        byte? HairStyle,
        byte? SkinColor,
        uint? ModelChara,
        uint? ModelBody,
        bool? NotRewriteHeight);

    public static int Run(string[] args)
    {
        if (args.Any(arg => arg is "-h" or "--help"))
        {
            Console.WriteLine("xivdata npc-index [--sqpack DIR] [--lang en|de|fr|ja] > npc-index.json");
            Console.WriteLine("Exports localized ENpcResident names plus ENpcBase identity and appearance metadata as JSON; does not write dialogue.db.");
            return 0;
        }
        Options options;
        try
        {
            options = Parse(args);
        }
        catch (ArgumentException ex)
        {
            Console.Error.WriteLine(ex.Message);
            Console.Error.WriteLine("Usage: xivdata npc-index [--sqpack DIR] [--lang en|de|fr|ja]");
            return 2;
        }

        try
        {
            var data = new GameData(options.SqPack, new LuminaOptions
            {
                PanicOnSheetChecksumMismatch = false,
                DefaultExcelLanguage = options.Language,
            });
            var bases = data.Excel.GetSheet<ENpcBase>(options.Language);
            var races = data.Excel.GetSheet<Race>(options.Language).ToDictionary(row => row.RowId);
            var tribes = data.Excel.GetSheet<Tribe>(options.Language).ToDictionary(row => row.RowId);
            var records = new List<NpcRecord>();

            foreach (var resident in data.Excel.GetSheet<ENpcResident>(options.Language))
            {
                var name = resident.Singular.ExtractText().Trim();
                if (name.Length == 0) continue;

                uint? raceId = null;
                uint? tribeId = null;
                byte? gender = null;
                string? raceName = null;
                string? tribeName = null;
                uint? bodyType = null;
                uint? height = null;
                float? scale = null;
                byte? bustOrTone1 = null;
                byte? extraFeature2OrBust = null;
                byte? jaw = null;
                byte? mouth = null;
                byte? face = null;
                byte? nose = null;
                byte? eyeShape = null;
                byte? eyebrows = null;
                byte? hairStyle = null;
                byte? skinColor = null;
                uint? modelChara = null;
                uint? modelBody = null;
                bool? notRewriteHeight = null;
                if (bases.TryGetRow(resident.RowId, out var npcBase))
                {
                    raceId = npcBase.Race.RowId is > 0 ? npcBase.Race.RowId : null;
                    tribeId = npcBase.Tribe.RowId is > 0 ? npcBase.Tribe.RowId : null;
                    gender = npcBase.Gender;
                    if (raceId is uint rid && races.TryGetValue(rid, out var race))
                        raceName = GenderedName(race, gender.Value);
                    if (tribeId is uint tid && tribes.TryGetValue(tid, out var tribe))
                        tribeName = GenderedName(tribe, gender.Value);
                    bodyType = npcBase.BodyType;
                    height = npcBase.Height;
                    scale = npcBase.Scale;
                    bustOrTone1 = npcBase.BustOrTone1;
                    extraFeature2OrBust = npcBase.ExtraFeature2OrBust;
                    jaw = npcBase.Jaw;
                    mouth = npcBase.Mouth;
                    face = npcBase.Face;
                    nose = npcBase.Nose;
                    eyeShape = npcBase.EyeShape;
                    eyebrows = npcBase.Eyebrows;
                    hairStyle = npcBase.HairStyle;
                    skinColor = npcBase.SkinColor;
                    modelChara = npcBase.ModelChara.RowId;
                    modelBody = npcBase.ModelBody;
                    notRewriteHeight = npcBase.NotRewriteHeight;
                }

                records.Add(new NpcRecord(
                    resident.RowId,
                    name,
                    raceId,
                    EmptyToNull(raceName),
                    tribeId,
                    EmptyToNull(tribeName),
                    gender switch { 0 => "male", 1 => "female", _ => "unknown" },
                    bodyType,
                    height,
                    scale,
                    bustOrTone1,
                    extraFeature2OrBust,
                    jaw,
                    mouth,
                    face,
                    nose,
                    eyeShape,
                    eyebrows,
                    hairStyle,
                    skinColor,
                    modelChara,
                    modelBody,
                    notRewriteHeight));
            }

            Console.WriteLine(JsonSerializer.Serialize(new
            {
                schema_version = "1.0",
                language = options.LanguageCode,
                source = "Lumina ENpcResident, ENpcBase, Race, Tribe",
                npcs = records.OrderBy(row => row.NpcId).Select(row => new
                {
                    npc_id = row.NpcId,
                    name = row.Name,
                    race_id = row.RaceId,
                    race_name = row.RaceName,
                    tribe_id = row.TribeId,
                    tribe_name = row.TribeName,
                    sex = row.Sex,
                    body_type = row.BodyType,
                    height = row.Height,
                    scale = row.Scale,
                    bust_or_tone_1 = row.BustOrTone1,
                    extra_feature_2_or_bust = row.ExtraFeature2OrBust,
                    jaw = row.Jaw,
                    mouth = row.Mouth,
                    face = row.Face,
                    nose = row.Nose,
                    eye_shape = row.EyeShape,
                    eyebrows = row.Eyebrows,
                    hair_style = row.HairStyle,
                    skin_color = row.SkinColor,
                    model_chara = row.ModelChara,
                    model_body = row.ModelBody,
                    not_rewrite_height = row.NotRewriteHeight,
                }),
            }, new JsonSerializerOptions { WriteIndented = true }));
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"npc-index failed: {ex.Message}");
            return 1;
        }
    }

    private static Options Parse(string[] args)
    {
        string sqpack = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Public/xlcore/ffxiv/game/sqpack");
        var language = Language.English;
        var languageCode = "en";
        for (var i = 0; i < args.Length; i++)
        {
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"missing value for {args[i]}");
            switch (args[i])
            {
                case "--sqpack":
                    sqpack = Path.GetFullPath(Next());
                    break;
                case "--lang":
                    languageCode = Next().ToLowerInvariant();
                    (language, languageCode) = languageCode switch
                    {
                        "en" => (Language.English, "en"),
                        "de" => (Language.German, "de"),
                        "fr" => (Language.French, "fr"),
                        "ja" => (Language.Japanese, "ja"),
                        _ => throw new ArgumentException($"unsupported language: {languageCode}"),
                    };
                    break;
                default:
                    throw new ArgumentException($"unknown option: {args[i]}");
            }
        }
        if (!Directory.Exists(sqpack))
            throw new ArgumentException($"sqpack directory not found: {sqpack}");
        return new Options(sqpack, language, languageCode);
    }

    private static string GenderedName<T>(T row, byte gender)
    {
        string preferred = gender == 0 ? "Masculine" : "Feminine";
        string fallback = gender == 0 ? "Feminine" : "Masculine";
        return ReadText(GetMember(row!, preferred)) ?? ReadText(GetMember(row!, fallback)) ?? "";
    }

    private static object? GetMember(object row, string name)
    {
        var type = row.GetType();
        return type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public)?.GetValue(row)
            ?? type.GetField(name, BindingFlags.Instance | BindingFlags.Public)?.GetValue(row);
    }

    private static string? ReadText(object? value)
    {
        if (value is null) return null;
        if (value is ReadOnlySeString seString) return seString.ExtractText();
        var extract = value.GetType().GetMethod("ExtractText", Type.EmptyTypes);
        return extract?.Invoke(value, null)?.ToString() ?? value.ToString();
    }

    private static string? EmptyToNull(string? value) =>
        string.IsNullOrWhiteSpace(value) ? null : value.Trim();
}
