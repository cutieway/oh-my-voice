using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using Lumina;
using Lumina.Data;
using Lumina.Excel;
using Lumina.Excel.Sheets;
using Lumina.Text.ReadOnly;
using OhMyVoice.Common;

namespace XivData;

public sealed record NpcInfo(uint Id, string Name, uint RaceId, byte Gender);

internal sealed record TokenMatch(NpcInfo? Primary, int Candidates);

public sealed record CutsceneLine(
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

public sealed class HarvestStats
{
    public long Rows;
    public long WithScd;
    public long WithoutScd;
    public long ResolvedSpeakers;
    public long UnresolvedSpeakers;
    public long Skipped;
    public long Errors;
    public Dictionary<string, long> BySource { get; } = new(StringComparer.Ordinal);
    public Dictionary<string, long> ByKind { get; } = new(StringComparer.Ordinal);

    public void Count(string source, string kind)
    {
        Rows++;
        BySource[source] = BySource.GetValueOrDefault(source) + 1;
        ByKind[kind] = ByKind.GetValueOrDefault(kind) + 1;
    }
}

public sealed class HarvestResult
{
    public List<CutsceneLine> CutsceneRows { get; } = [];
    public HarvestStats Stats { get; } = new();
    public string? CsvPath { get; set; }
}

public sealed class HarvestOptions
{
    public required GameData Data { get; init; }
    public required DialogueDb Db { get; init; }
    public required Language Language { get; init; }
    public required string LangCode { get; init; }
    public required string OutDir { get; init; }
    public required IReadOnlyList<string> Sources { get; init; }
    public required long RunId { get; init; }
    public string? SheetFilter { get; init; }
    public string? SpeakerFilter { get; init; }
    public int Limit { get; init; }
    public bool ExtractAudio { get; init; }
    public string? Vgmstream { get; init; }
    public bool ByToken { get; init; }
    public string XVectorDir { get; init; } = "data/xvectors";
}

public static class Harvester
{
    private static readonly string[] Expansions = ["ffxiv", "ex1", "ex2", "ex3", "ex4", "ex5"];
    private static readonly string[] GenderSuffixes = ["_m_", "_", "_f_"];
    private const int MinVoicedScdBytes = 1024;

    private static readonly string[] ScriptedFamilies =
        ["custom", "raid", "content", "warp", "transport", "opening", "guild_order", "leve"];

    private static readonly HashSet<string> NonSpeechTokens = new(StringComparer.OrdinalIgnoreCase)
    {
        "SYSTEM", "NARRATION", "NARRATOR", "LOG", "NOTICE", "WARNING", "ERROR", "INFO",
        "MENU", "TITLE", "SELECT", "START", "END", "CANCEL", "YES", "NO", "OK",
        "ABOUT", "EXPLAIN", "EXPLANATION", "HELP", "CONFIRM", "DESC", "DESCRIPTION",
        "EXIT", "RETURN", "NEXT", "BACK", "SHOP", "BUY", "SELL",
        "ENTER", "LEAVE", "LCUT", "CUT", "TALK", "POPUP", "BALLOON",
        "SE", "BGM", "UI", "TEST", "TODO", "DUMMY",
    };

    private static readonly HashSet<string> JournalTokens = new(StringComparer.OrdinalIgnoreCase)
    {
        "SEQ", "QUEST", "JOURNAL", "OBJECTIVE", "REWARD", "HEADER", "TODO",
    };

    private static readonly Regex ChoicePrompt = new(@"^Q\d+$", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex ChoiceAnswer = new(@"^A\d+$", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex TrailingDigits = new(@"\d+$", RegexOptions.Compiled);
    private static readonly Regex PlayerSplitMacro = new(
        @"<split\(<string\(gstr1\)>,\s*,1\)>", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex PlayerStringMacro = new(
        @"<string\(gstr(\d+)\)>", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex AnyMacro = new(@"<[^<>]*>", RegexOptions.Compiled);

    public static HarvestResult Run(HarvestOptions options)
    {
        var result = new HarvestResult();
        var npc = NpcIndex.Build(options.Data, options.Language);
        var refs = BuildNpcReferences(options.Data, options.Language);
        var context = new Context(options, npc, refs, result);
        context.BuildSpeakers();
        options.Db.BeginBatch();
        try
        {
        foreach (string source in options.Sources)
        {
            if (context.ShouldStop) break;
            switch (source)
            {
                case "cutscene":
                    context.HarvestCutscene();
                    break;
                case "defaulttalk":
                    context.HarvestDefaultTalk();
                    break;
                case "balloon":
                    context.HarvestSimpleSheet("balloon", "Balloon", "balloon",
                        options.Data.Excel.GetSheet<Balloon>(options.Language)
                            .Select(r => (r.RowId, r.Dialogue)),
                        typeof(Balloon));
                    break;
                case "contenttalk":
                    context.HarvestSimpleSheet("contenttalk", "ContentTalk", "content",
                        options.Data.Excel.GetSheet<ContentTalk>(options.Language)
                            .Select(r => (r.RowId, r.Text)),
                        typeof(ContentTalk));
                    break;
                case "npcyell":
                    context.HarvestSimpleSheet("npcyell", "NpcYell", "battle_talk",
                        options.Data.Excel.GetSheet<NpcYell>(options.Language)
                            .Select(r => (r.RowId, r.Text)),
                        typeof(NpcYell));
                    break;
                case "instancecontent":
                    context.HarvestSimpleSheet("instancecontent", "InstanceContentTextData", "content",
                        options.Data.Excel.GetSheet<InstanceContentTextData>(options.Language)
                            .Select(r => (r.RowId, r.Text)),
                        typeof(InstanceContentTextData));
                    break;
                case "publiccontent":
                    context.HarvestSimpleSheet("publiccontent", "PublicContentTextData", "content",
                        options.Data.Excel.GetSheet<PublicContentTextData>(options.Language)
                            .Select(r => (r.RowId, r.TextData)),
                        typeof(PublicContentTextData));
                    break;
                case "partycontent":
                    context.HarvestSimpleSheet("partycontent", "PartyContentTextData", "content",
                        options.Data.Excel.GetSheet<PartyContentTextData>(options.Language)
                            .Select(r => (r.RowId, r.Data)),
                        typeof(PartyContentTextData));
                    break;
                case "massivepc":
                    context.HarvestSimpleSheet("massivepc", "MassivePcContentTextData", "content",
                        options.Data.Excel.GetSheet<MassivePcContentTextData>(options.Language)
                            .Select(r => (r.RowId, r.Text)),
                        typeof(MassivePcContentTextData));
                    break;
                case "gimmicktalk":
                    context.HarvestSimpleSheet("gimmicktalk", "GimmickTalk", "content",
                        options.Data.Excel.GetSheet<GimmickTalk>(options.Language)
                            .Select(r => (r.RowId, r.Message)),
                        typeof(GimmickTalk));
                    break;
                case "goldsaucertalk":
                    context.HarvestSimpleSheet("goldsaucertalk", "GoldSaucerTextData", "content",
                        options.Data.Excel.GetSheet<GoldSaucerTextData>(options.Language)
                            .Select(r => (r.RowId, r.Text)),
                        typeof(GoldSaucerTextData));
                    break;
                case "quest":
                    context.HarvestTextSheets("quest", "quest/", options.SheetFilter);
                    break;
                case "scripted":
                    foreach (string family in ScriptedFamilies)
                        context.HarvestTextSheets(family, family + "/", options.SheetFilter);
                    break;
                default:
                    if (ScriptedFamilies.Contains(source, StringComparer.OrdinalIgnoreCase))
                        context.HarvestTextSheets(source, source + "/", options.SheetFilter);
                    else
                        throw new ArgumentException($"unknown source: {source}");
                    break;
            }
        }
        options.Db.CommitBatch();
        }
        catch
        {
            options.Db.AbortBatch();
            throw;
        }

        context.Finish();
        result.CsvPath = WriteCsv(options.OutDir, result.CutsceneRows);
        return result;
    }

    private static Dictionary<(Type Type, uint RowId), List<uint>> BuildNpcReferences(
        GameData data, Language language)
    {
        var map = new Dictionary<(Type, uint), List<uint>>();
        var bases = data.Excel.GetSheet<ENpcBase>(language);
        foreach (var npcBase in bases)
        {
            foreach (var rowRef in npcBase.ENpcData)
            {
                if (rowRef.RowId == 0 || rowRef.RowType is null) continue;
                AddRef(map, rowRef.RowType, rowRef.RowId, npcBase.RowId);
            }

            if (npcBase.Balloon.RowId != 0)
                AddRef(map, typeof(Balloon), npcBase.Balloon.RowId, npcBase.RowId);
        }

        // NpcYell.Name is a RowRef to the speaking NPC for some battle barks.
        try
        {
            foreach (var yell in data.Excel.GetSheet<NpcYell>(language))
            {
                if (yell.Name.RowId != 0 && yell.Name.RowType == typeof(ENpcResident))
                    AddRef(map, typeof(NpcYell), yell.RowId, yell.Name.RowId);
            }
        }
        catch
        {
            // Best effort only.
        }

        return map;
    }

    private static void AddRef(
        Dictionary<(Type Type, uint RowId), List<uint>> map, Type type, uint rowId, uint npcId)
    {
        if (npcId == 0) return;
        var key = (type, rowId);
        if (!map.TryGetValue(key, out var ids))
            map[key] = ids = [];
        if (!ids.Contains(npcId)) ids.Add(npcId);
    }

    private static string WriteCsv(string outDir, List<CutsceneLine> rows)
    {
        Directory.CreateDirectory(outDir);
        string path = Path.Combine(outDir, "dialogue.csv");
        static string Csv(string value) => $"\"{value.Replace("\"", "\"\"")}\"";
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        writer.NewLine = "\n";
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
        return path;
    }

    private sealed class Context
    {
        private readonly HarvestOptions options;
        private readonly NpcIndex npc;
        private readonly Dictionary<(Type Type, uint RowId), List<uint>> refs;
        private readonly HarvestResult result;
        private readonly Dictionary<string, SpeakerRow> knownSpeakers = new(StringComparer.Ordinal);
        private readonly HashSet<string> oversizedScdWarnings = new(StringComparer.Ordinal);
        private bool finished;

        public Context(
            HarvestOptions options,
            NpcIndex npc,
            Dictionary<(Type Type, uint RowId), List<uint>> refs,
            HarvestResult result)
        {
            this.options = options;
            this.npc = npc;
            this.refs = refs;
            this.result = result;
        }

        public bool ShouldStop => options.Limit > 0 && result.Stats.Rows >= options.Limit;

        private string Lang => options.LangCode;
        private HarvestStats Stats => result.Stats;

        public void BuildSpeakers()
        {
            // Seed every resident so an unresolved token still has a stable row?
            // No: speakers are created only when a line references them.
        }

        public void HarvestCutscene()
        {
            var sheetNames = options.Data.Excel.SheetNames
                .Where(n => n.StartsWith("cut_scene/", StringComparison.OrdinalIgnoreCase))
                .Where(MatchesSheet)
                .OrderBy(n => n, StringComparer.Ordinal)
                .ToArray();

            int processed = 0;
            int total = sheetNames.Length;
            foreach (string sheetName in sheetNames)
            {
                if (ShouldStop) break;
                processed++;
                if (processed % 10 == 0)
                    Console.WriteLine($"cutscene: {processed}/{total} sheets, rows {Stats.Rows}");
                ExcelSheet<RawRow> sheet;
                try
                {
                    sheet = options.Data.Excel.GetSheet<RawRow>(options.Language, sheetName);
                }
                catch (Exception ex)
                {
                    Warn($"skip {sheetName}: {ex.Message}");
                    continue;
                }

                foreach (var row in sheet)
                {
                    if (ShouldStop) break;
                    try
                    {
                        HarvestCutsceneRow(sheetName, row);
                    }
                    catch (Exception ex)
                    {
                        Stats.Errors++;
                        Warn($"cutscene {sheetName}/{row.RowId}: {ex.Message}");
                    }
                }
            }
        }

        private void HarvestCutsceneRow(string sheetName, RawRow row)
        {
            string textKey;
            ReadOnlySeString textValue;
            try
            {
                textKey = row.ReadStringColumn(0).ExtractText() ?? "";
                textValue = row.ReadStringColumn(1);
            }
            catch
            {
                Stats.Skipped++;
                return;
            }
            if (string.IsNullOrWhiteSpace(textKey) || textValue.IsEmpty) return;

            bool parsed = TryParseCutsceneKey(textKey, out string? token, out string? audioBase, out string kind);
            token ??= "";
            if (options.SpeakerFilter is not null &&
                !token.Equals(options.SpeakerFilter, StringComparison.OrdinalIgnoreCase))
            {
                Stats.Skipped++;
                return;
            }

            var scd = audioBase is null ? null : FindScd(options.Data, audioBase, Lang);
            var (raw, macro) = TextOf(textValue);
            if (string.IsNullOrWhiteSpace(raw) && string.IsNullOrWhiteSpace(macro))
            {
                Stats.Skipped++;
                return;
            }

            var speakerRefs = new List<SpeakerRef>();
            SpeakerRow? primarySpeaker = null;
            int tokenCandidates = 0;

            if (parsed && IsDialogueToken(kind) && token.Length > 0)
            {
                var match = npc.ResolveToken(token);
                tokenCandidates = match.Candidates;
                if (match.Primary is { } info)
                {
                    var speaker = SpeakerForToken(token, info,
                        tokenCandidates > 1 ? "token_ambiguous" : "token");
                    EnsureSpeaker(speaker);
                    speakerRefs.Add(new SpeakerRef(speaker.VoiceId, speaker.NpcId,
                        speaker.Match, true));
                    primarySpeaker = speaker;
                }
                else
                {
                    var speaker = SpeakerForToken(token);
                    EnsureSpeaker(speaker);
                    speakerRefs.Add(new SpeakerRef(speaker.VoiceId, null, "unresolved", true));
                    primarySpeaker = speaker;
                }
            }

            string speakerName = primarySpeaker?.Name ?? (token.Length > 0 ? token : "");
            string speakerMatch = primarySpeaker?.Match ?? "none";
            string cleanSentence = SanitizeText(speakerName, macro, raw);
            if (cleanSentence.Length == 0)
            {
                Stats.Skipped++;
                return;
            }

            string dialogId = DialogId(Lang, "cutscene", sheetName, row.RowId, 0, textKey);
            string textHash = HashText(cleanSentence);
            string? voiceLineId = primarySpeaker is not null
                ? VoiceId.ComputeId(Lang, primarySpeaker.VoiceId,
                    primarySpeaker.NpcId?.ToString(CultureInfo.InvariantCulture) ?? primarySpeaker.VoiceId,
                    cleanSentence)
                : null;
            bool hasScd = scd is not null;
            string audioStatus = InitialStatus(hasScd, primarySpeaker?.VoiceId, kind);

            var dialog = new DialogRow(
                dialogId, Lang, "cutscene", sheetName, row.RowId, 0, textKey, audioBase,
                primarySpeaker?.VoiceId, primarySpeaker?.NpcId, speakerName,
                primarySpeaker?.NpcKey ?? CleanSpeaker(token), speakerMatch,
                raw, macro, cleanSentence, textHash, voiceLineId, kind,
                hasScd, scd?.Path, scd?.Bytes, hasScd ? Lang : null, audioStatus,
                DateTimeOffset.UtcNow);

            options.Db.UpsertDialog(dialog, options.RunId, speakerRefs);
            Stats.Count("cutscene", kind);
            if (hasScd) Stats.WithScd++; else Stats.WithoutScd++;
            if (primarySpeaker is not null)
            {
                if (primarySpeaker.NpcId.HasValue) Stats.ResolvedSpeakers++;
                else Stats.UnresolvedSpeakers++;
            }

            if (scd is { } resolved && parsed && audioBase is not null)
            {
                var line = new CutsceneLine(
                    sheetName, row.RowId, textKey, audioBase, token.ToLowerInvariant(),
                    primarySpeaker?.NpcId, speakerName,
                    primarySpeaker?.RaceId ?? 0, primarySpeaker?.Gender ?? 0,
                    resolved.Path, resolved.Bytes, raw);
                result.CutsceneRows.Add(line);
                if (options.ExtractAudio)
                    Extract(line, options.Data, options);
            }
        }

        public void HarvestDefaultTalk()
        {
            var sheet = options.Data.Excel.GetSheet<DefaultTalk>(options.Language);
            foreach (var row in sheet)
            {
                if (ShouldStop) break;
                try
                {
                    for (int i = 0; i < row.Text.Count; i++)
                    {
                        if (ShouldStop) break;
                        var text = row.Text[i];
                        if (text.IsEmpty) continue;
                        var (raw, macro) = TextOf(text);
                        if (string.IsNullOrWhiteSpace(raw) && string.IsNullOrWhiteSpace(macro)) continue;

                        var npcIds = CanonicalNpcIds(NpcIds(typeof(DefaultTalk), row.RowId));
                        var speakerRefs = new List<SpeakerRef>();
                        SpeakerRow? primary = null;
                        foreach (uint npcId in npcIds)
                        {
                            var info = npc.InfoById(npcId);
                            if (info is null) continue;
                            var speaker = SpeakerForNpc(info, npcIds.Count > 1 ? "direct_ambiguous" : "direct");
                            EnsureSpeaker(speaker);
                            speakerRefs.Add(new SpeakerRef(speaker.VoiceId, speaker.NpcId,
                                npcIds.Count > 1 ? "direct_ambiguous" : "direct", primary is null));
                            primary ??= speaker;
                        }

                        if (options.SpeakerFilter is not null &&
                            !MatchesSpeaker(primary?.Name ?? ""))
                        {
                            Stats.Skipped++;
                            continue;
                        }

                        string speakerName = primary?.Name ?? "";
                        string kind = primary is null ? "unknown" : "dialogue";
                        string clean = SanitizeText(speakerName, macro, raw);
                        if (clean.Length == 0)
                        {
                            Stats.Skipped++;
                            continue;
                        }

                        string sheetName = "DefaultTalk";
                        string dialogId = DialogId(Lang, "defaulttalk", sheetName, row.RowId, i, null);
                        string hash = HashText(clean);
                        string? lineId = primary is null
                            ? null
                            : VoiceId.ComputeId(Lang, primary.VoiceId,
                                primary.NpcId?.ToString(CultureInfo.InvariantCulture) ?? primary.VoiceId,
                                clean);
                        var dialog = new DialogRow(
                            dialogId, Lang, "defaulttalk", sheetName, row.RowId, i, null, null,
                            primary?.VoiceId, primary?.NpcId, speakerName,
                            primary?.NpcKey ?? "",
                            primary is null ? "none" : (npcIds.Count > 1 ? "direct_ambiguous" : "direct"),
                            raw, macro, clean, hash, lineId, kind, false, null, null, null,
                            InitialStatus(false, primary?.VoiceId, kind), DateTimeOffset.UtcNow);
                        options.Db.UpsertDialog(dialog, options.RunId, speakerRefs);
                        Stats.Count("defaulttalk", kind);
                        Stats.WithoutScd++;
                        if (primary is not null)
                        {
                            if (primary.NpcId.HasValue) Stats.ResolvedSpeakers++;
                            else Stats.UnresolvedSpeakers++;
                        }
                    }
                }
                catch (Exception ex)
                {
                    Stats.Errors++;
                    Warn($"DefaultTalk/{row.RowId}: {ex.Message}");
                }
            }
        }

        public void HarvestSimpleSheet(
            string source,
            string sheetName,
            string kind,
            IEnumerable<(uint RowId, ReadOnlySeString Text)> rows,
            Type rowType)
        {
            foreach (var (rowId, textValue) in rows)
            {
                if (ShouldStop) break;
                if (textValue.IsEmpty) continue;
                try
                {
                    var (raw, macro) = TextOf(textValue);
                    if (string.IsNullOrWhiteSpace(raw) && string.IsNullOrWhiteSpace(macro)) continue;

                    var npcIds = CanonicalNpcIds(NpcIds(rowType, rowId));
                    var speakerRefs = new List<SpeakerRef>();
                    SpeakerRow? primary = null;
                    foreach (uint npcId in npcIds)
                    {
                        var info = npc.InfoById(npcId);
                        if (info is null) continue;
                        var speaker = SpeakerForNpc(info, npcIds.Count > 1 ? "direct_ambiguous" : "direct");
                        EnsureSpeaker(speaker);
                        speakerRefs.Add(new SpeakerRef(speaker.VoiceId, speaker.NpcId,
                            npcIds.Count > 1 ? "direct_ambiguous" : "direct", primary is null));
                        primary ??= speaker;
                    }

                    if (options.SpeakerFilter is not null && !MatchesSpeaker(primary?.Name ?? ""))
                    {
                        Stats.Skipped++;
                        continue;
                    }

                    string speakerName = primary?.Name ?? "";
                    string textKind = primary is null
                        ? kind
                        : kind switch
                        {
                            "balloon" => "dialogue",
                            "npcyell" => "battle_talk",
                            "content" => "content",
                            _ => "dialogue",
                        };
                    string clean = SanitizeText(speakerName, macro, raw);
                    if (clean.Length == 0)
                    {
                        Stats.Skipped++;
                        continue;
                    }

                    string dialogId = DialogId(Lang, source, sheetName, rowId, 0, null);
                    string hash = HashText(clean);
                    string? lineId = primary is null
                        ? null
                        : VoiceId.ComputeId(Lang, primary.VoiceId,
                            primary.NpcId?.ToString(CultureInfo.InvariantCulture) ?? primary.VoiceId,
                            clean);
                    var dialog = new DialogRow(
                        dialogId, Lang, source, sheetName, rowId, 0, null, null,
                        primary?.VoiceId, primary?.NpcId, speakerName,
                        primary?.NpcKey ?? "",
                        primary is null ? "none" : (npcIds.Count > 1 ? "direct_ambiguous" : "direct"),
                        raw, macro, clean, hash, lineId, textKind, false, null, null, null,
                        InitialStatus(false, primary?.VoiceId, textKind), DateTimeOffset.UtcNow);
                    options.Db.UpsertDialog(dialog, options.RunId, speakerRefs);
                    Stats.Count(source, textKind);
                    Stats.WithoutScd++;
                    if (primary is not null)
                    {
                        if (primary.NpcId.HasValue) Stats.ResolvedSpeakers++;
                        else Stats.UnresolvedSpeakers++;
                    }
                }
                catch (Exception ex)
                {
                    Stats.Errors++;
                    Warn($"{source}/{sheetName}/{rowId}: {ex.Message}");
                }
            }
        }

        public void HarvestTextSheets(string source, string prefix, string? sheetFilter)
        {
            var names = options.Data.Excel.SheetNames
                .Where(n => n.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                .Where(n => string.IsNullOrWhiteSpace(sheetFilter) ||
                            n.Contains(sheetFilter, StringComparison.OrdinalIgnoreCase))
                .OrderBy(n => n, StringComparer.Ordinal)
                .ToArray();

            int processed = 0;
            int total = names.Length;
            foreach (string sheetName in names)
            {
                if (ShouldStop) break;
                processed++;
                if (processed % 250 == 0)
                    Console.WriteLine($"{source}: {processed}/{total} sheets, rows {Stats.Rows}");
                ExcelSheet<RawRow> sheet;
                try
                {
                    sheet = options.Data.Excel.GetSheet<RawRow>(options.Language, sheetName);
                }
                catch (Exception ex)
                {
                    Warn($"skip {sheetName}: {ex.Message}");
                    continue;
                }

                string leaf = sheetName[(sheetName.LastIndexOf('/') + 1)..];
                foreach (var row in sheet)
                {
                    if (ShouldStop) break;
                    try
                    {
                        HarvestTextRow(source, sheetName, leaf, row);
                    }
                    catch (Exception ex)
                    {
                        Stats.Errors++;
                        Warn($"{source}/{sheetName}/{row.RowId}: {ex.Message}");
                    }
                }
            }
        }

        private void HarvestTextRow(string source, string sheetName, string leaf, RawRow row)
        {
            string textKey;
            ReadOnlySeString textValue;
            try
            {
                textKey = row.ReadStringColumn(0).ExtractText() ?? "";
                textValue = row.ReadStringColumn(1);
            }
            catch
            {
                Stats.Skipped++;
                return;
            }
            if (string.IsNullOrWhiteSpace(textKey) || textValue.IsEmpty) return;

            if (!TryGetSpeakerToken(leaf, textKey, out string token))
            {
                Stats.Skipped++;
                return;
            }
            if (options.SpeakerFilter is not null &&
                !token.Equals(options.SpeakerFilter, StringComparison.OrdinalIgnoreCase))
            {
                Stats.Skipped++;
                return;
            }

            var (raw, macro) = TextOf(textValue);
            if (string.IsNullOrWhiteSpace(raw) && string.IsNullOrWhiteSpace(macro))
            {
                Stats.Skipped++;
                return;
            }

            string kind = ClassifyToken(token);
            var speakerRefs = new List<SpeakerRef>();
            SpeakerRow? primary = null;

            if (IsDialogueToken(kind))
            {
                var match = npc.ResolveToken(token);
                if (match.Primary is { } info)
                {
                    var speaker = SpeakerForToken(token, info,
                        match.Candidates > 1 ? "token_ambiguous" : "token");
                    EnsureSpeaker(speaker);
                    speakerRefs.Add(new SpeakerRef(speaker.VoiceId, speaker.NpcId,
                        speaker.Match, true));
                    primary = speaker;
                }
                else
                {
                    var speaker = SpeakerForToken(token);
                    EnsureSpeaker(speaker);
                    speakerRefs.Add(new SpeakerRef(speaker.VoiceId, null, "unresolved", true));
                    primary = speaker;
                }
            }

            string speakerName = primary?.Name ?? (IsDialogueToken(kind) ? token : "");
            string clean = SanitizeText(speakerName, macro, raw);
            if (clean.Length == 0)
            {
                Stats.Skipped++;
                return;
            }

            string dialogId = DialogId(Lang, source, sheetName, row.RowId, 0, textKey);
            string hash = HashText(clean);
            string? lineId = primary is null
                ? null
                : VoiceId.ComputeId(Lang, primary.VoiceId,
                    primary.NpcId?.ToString(CultureInfo.InvariantCulture) ?? primary.VoiceId,
                    clean);
            var dialog = new DialogRow(
                dialogId, Lang, source, sheetName, row.RowId, 0, textKey, null,
                primary?.VoiceId, primary?.NpcId, speakerName, primary?.NpcKey ?? "",
                primary?.Match ?? "none",
                raw, macro, clean, hash, lineId, kind, false, null, null, null,
                InitialStatus(false, primary?.VoiceId, kind), DateTimeOffset.UtcNow);
            options.Db.UpsertDialog(dialog, options.RunId, speakerRefs);
            Stats.Count(source, kind);
            Stats.WithoutScd++;
            if (primary is not null)
            {
                if (primary.NpcId.HasValue) Stats.ResolvedSpeakers++;
                else Stats.UnresolvedSpeakers++;
            }
        }

        private IReadOnlyList<uint> CanonicalNpcIds(IReadOnlyList<uint> ids)
        {
            var seen = new HashSet<string>(StringComparer.Ordinal);
            var result = new List<uint>();
            foreach (uint id in ids.OrderBy(i => i))
            {
                var info = npc.InfoById(id);
                if (info is null) continue;
                if (!seen.Add(Normalize(info.Name))) continue;
                result.Add(id);
            }
            return result;
        }

        public void Finish()
        {
            if (finished) return;
            finished = true;
            options.Db.RefreshSpeakers(options.XVectorDir);
            options.Db.Complete();
        }

        private IReadOnlyList<uint> NpcIds(Type type, uint rowId) =>
            refs.TryGetValue((type, rowId), out var ids) ? ids : [];

        private bool MatchesSheet(string sheetName) =>
            string.IsNullOrWhiteSpace(options.SheetFilter) ||
            sheetName.Contains(options.SheetFilter, StringComparison.OrdinalIgnoreCase);

        private bool MatchesSpeaker(string name)
        {
            if (string.IsNullOrWhiteSpace(options.SpeakerFilter)) return true;
            return Normalize(name) == Normalize(options.SpeakerFilter);
        }

        private SpeakerRow SpeakerForNpc(NpcInfo info, string match)
        {
            // Canonicalize by name. The same character has hundreds of
            // ENpcResident instances (Alphinaud has 798); the runtime manifest
            // can only map one numeric NPC id, so every line for this voice
            // must use that same id or voice_line_id will not match.
            var canonical = npc.ResolveName(info.Name) ?? info;
            return new SpeakerRow(VoiceIdFromToken(info.Name), canonical.Id, info.Name,
                CleanSpeaker(info.Name), Normalize(info.Name),
                canonical.RaceId, canonical.Gender, match);
        }

        private SpeakerRow SpeakerForToken(string token, NpcInfo? info = null, string match = "unresolved")
        {
            string voiceId = VoiceIdFromToken(token);
            var canonical = (info is null ? null : npc.ResolveToken(token).Primary) ?? info;
            string name = canonical?.Name ?? token;
            return new SpeakerRow(voiceId, canonical?.Id, name, CleanSpeaker(name),
                Normalize(name), canonical?.RaceId ?? 0, canonical?.Gender, match);
        }

        private void EnsureSpeaker(SpeakerRow speaker)
        {
            if (knownSpeakers.TryGetValue(speaker.VoiceId, out var existing))
            {
                if (existing.NpcId.HasValue || !speaker.NpcId.HasValue) return;
            }
            knownSpeakers[speaker.VoiceId] = speaker;
            options.Db.UpsertSpeaker(speaker, options.RunId);
        }

        private void Warn(string message)
        {
            if (oversizedScdWarnings.Add(message)) Console.Error.WriteLine(message);
        }
    }

    private static (string Raw, string Macro) TextOf(ReadOnlySeString value)
    {
        string raw;
        string macro;
        try { raw = value.ExtractText() ?? ""; } catch { raw = ""; }
        try { macro = value.ToMacroString() ?? raw; } catch { macro = raw; }
        return (raw.Trim(), macro.Trim());
    }

    private static string SanitizeText(string speaker, string macro, string raw)
    {
        string input = NormalizeMacros(macro);
        if (string.IsNullOrWhiteSpace(input)) input = raw;
        string speakerForClean = string.IsNullOrWhiteSpace(speaker) ? "Narrator" : speaker;
        try
        {
            var (_, sentence) = Sanitizer.Clean(speakerForClean, input, "Forename", "Surname");
            string trimmed = sentence.Trim();
            return IsPaddingText(trimmed) || IsDeveloperPlaceholder(trimmed) ? "" : trimmed;
        }
        catch
        {
            string trimmed = input.Trim();
            return IsPaddingText(trimmed) ? "" : trimmed;
        }
    }

    private static string NormalizeMacros(string text)
    {
        if (string.IsNullOrEmpty(text)) return "";
        string value = PlayerSplitMacro.Replace(text, "Forename");
        value = PlayerStringMacro.Replace(value, m => m.Groups[1].Value == "2" ? "Surname" : "Forename");
        for (int i = 0; i < 4; i++)
            value = AnyMacro.Replace(value, "");
        var sb = new StringBuilder(value.Length);
        foreach (char ch in value)
        {
            if (char.IsControl(ch) && ch is not '\n' and not '\r' and not '\t') continue;
            sb.Append(ch);
        }
        return Regex.Replace(sb.ToString(), @"\s+", " ").Trim();
    }

    private static string CleanSpeaker(string speaker)
    {
        if (string.IsNullOrWhiteSpace(speaker)) return "";
        try
        {
            return Sanitizer.Clean(speaker, "", "Forename", "Surname").Speaker.Trim();
        }
        catch
        {
            return speaker.Trim();
        }
    }

    private static bool IsPaddingText(string text) =>
        text.Length == 0 || !text.Any(char.IsLetter);

    private static bool IsDeveloperPlaceholder(string text) =>
        text.Contains('★') && text.Contains("未使用", StringComparison.Ordinal);

    private static string HashText(string text) =>
        Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(text)));

    private static string DialogId(
        string lang, string source, string sheet, uint rowId, int lineIndex, string? textKey)
    {
        // textKey is stored for provenance but is not part of the identity:
        // a patch can rename a key without creating a duplicate row.
        string key = string.Join("\u001f", lang, source, sheet,
            rowId.ToString(CultureInfo.InvariantCulture),
            lineIndex.ToString(CultureInfo.InvariantCulture));
        return Convert.ToHexStringLower(System.Security.Cryptography.MD5.HashData(Encoding.UTF8.GetBytes(key)));
    }

    private static string InitialStatus(bool hasScd, string? speakerId, string kind)
    {
        if (hasScd) return "covered";
        if (speakerId is not null && kind is "dialogue" or "narration") return "pending";
        return "unassigned";
    }

    private static bool IsDialogueToken(string kind) =>
        kind is "dialogue" or "narration";

    private static bool TryParseCutsceneKey(
        string textKey, out string? token, out string? audioBase, out string kind)
    {
        token = null;
        audioBase = null;
        kind = "unknown";
        if (string.IsNullOrWhiteSpace(textKey)) return false;

        string key = textKey;
        if (key.EndsWith("_NONE_VOICE", StringComparison.OrdinalIgnoreCase))
            key = key[..^"_NONE_VOICE".Length];

        var parts = key.Split('_');
        if (parts.Length < 2 || !parts[0].Equals("TEXT", StringComparison.OrdinalIgnoreCase))
            return false;

        if (parts.Length == 5)
        {
            token = parts[4];
            var withoutSpeaker = key[..^(parts[4].Length + 1)];
            audioBase = withoutSpeaker.Replace("TEXT", "vo", StringComparison.OrdinalIgnoreCase)
                .ToLowerInvariant();
            kind = ClassifyToken(token);
            return true;
        }

        if (parts.Length >= 4)
        {
            token = ClassifyToken(parts[3]) is not "dialogue" ? parts[3] : parts[^1];
            kind = ClassifyToken(token);
            return true;
        }

        return false;
    }

    private static bool TryGetSpeakerToken(string leaf, string textKey, out string token)
    {
        token = "";
        string prefix = "TEXT_" + leaf + "_";
        if (!textKey.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return false;
        string rest = textKey[prefix.Length..];
        if (rest.Length == 0) return false;
        int end = rest.IndexOf('_');
        token = end < 0 ? rest : rest[..end];
        return token.Length > 0;
    }

    private static string ClassifyToken(string token)
    {
        if (string.IsNullOrWhiteSpace(token)) return "unknown";
        if (ChoicePrompt.IsMatch(token)) return "choice_prompt";
        if (ChoiceAnswer.IsMatch(token)) return "choice_answer";

        // Speaker tokens frequently carry a numeric instance suffix
        // (BOY05426, MEMBERC01548). Strip it for classification, but keep
        // it in the voice id. Flow markers use the same shape (ENTER2).
        string stem = TrailingDigits.Replace(token, "");
        if (stem.Length == 0) return "system";
        if (JournalTokens.Contains(stem)) return "journal";
        if (stem.Equals("NARRATION", StringComparison.OrdinalIgnoreCase) ||
            stem.Equals("NARRATOR", StringComparison.OrdinalIgnoreCase))
            return "narration";
        if (NonSpeechTokens.Contains(stem)) return "system";
        if (stem.Length > 5 && stem.EndsWith("MENU", StringComparison.OrdinalIgnoreCase))
            return "system";
        return "dialogue";
    }

    private static string VoiceIdFromToken(string token)
    {
        var sb = new StringBuilder(token.Length);
        foreach (char ch in token)
        {
            if (char.IsLetterOrDigit(ch)) sb.Append(char.ToLowerInvariant(ch));
        }
        return sb.Length == 0 ? "unknown" : sb.ToString();
    }

    private static string Normalize(string value)
    {
        var sb = new StringBuilder(value.Length);
        foreach (char ch in value)
        {
            if (char.IsLetterOrDigit(ch)) sb.Append(char.ToLowerInvariant(ch));
        }
        return sb.ToString();
    }

    private static (string Path, int Bytes)? FindScd(GameData data, string audioBase, string langCode)
    {
        if (audioBase.Length < 17) return null;
        string folder6 = audioBase.Substring(3, 6);
        string folder14 = audioBase.Substring(3, 14);
        bool isManFst = audioBase.Contains("manfst", StringComparison.Ordinal);

        foreach (string exp in Expansions)
        {
            foreach (string suffix in GenderSuffixes)
            {
                string path;
                if (isManFst && audioBase.Length >= 12)
                {
                    string folder9 = audioBase.Substring(3, 9);
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

    private static void Extract(CutsceneLine line, GameData data, HarvestOptions options)
    {
        string owner = options.ByToken
            ? line.SpeakerToken
            : string.IsNullOrEmpty(line.NpcName) ? "unresolved" : SafeName(line.NpcName);
        string stem = SafeName(line.AudioBase);
        string scdDir = Path.Combine(options.OutDir, "scd", owner);
        string wavDir = Path.Combine(options.OutDir, "wav", owner);
        Directory.CreateDirectory(scdDir);
        Directory.CreateDirectory(wavDir);

        string scdPath = Path.Combine(scdDir, stem + ".scd");
        string wavPath = Path.Combine(wavDir, stem + ".wav");
        if (!File.Exists(scdPath))
        {
            var file = data.GetFile(line.ScdPath) ??
                throw new InvalidOperationException($"SCD vanished: {line.ScdPath}");
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
        foreach (char ch in value)
            sb.Append(invalid.Contains(ch) || ch == Path.DirectorySeparatorChar ? '_' : ch);
        return sb.ToString();
    }

    private sealed class NpcIndex(
        Dictionary<string, List<uint>> names,
        Dictionary<uint, NpcInfo> infos)
    {
        public Dictionary<string, List<uint>> Names { get; } = names;
        public Dictionary<uint, NpcInfo> Infos { get; } = infos;

        public static NpcIndex Build(GameData data, Language language)
        {
            var resident = data.Excel.GetSheet<ENpcResident>(language);
            var baseSheet = data.Excel.GetSheet<ENpcBase>(language);
            var names = new Dictionary<string, List<uint>>(StringComparer.OrdinalIgnoreCase);
            var infos = new Dictionary<uint, NpcInfo>();

            foreach (var row in resident)
            {
                string name = row.Singular.ExtractText().Trim();
                if (name.Length == 0) continue;

                uint raceId = 0;
                byte gender = 0;
                if (baseSheet.TryGetRow(row.RowId, out var npcBase))
                {
                    raceId = npcBase.Race.RowId;
                    gender = npcBase.Gender;
                }

                infos[row.RowId] = new NpcInfo(row.RowId, name, raceId, gender);
                AddName(names, Normalize(name), row.RowId);
                int firstSpace = name.IndexOf(' ');
                if (firstSpace > 0)
                    AddName(names, Normalize(name[..firstSpace]), row.RowId);
            }

            return new NpcIndex(names, infos);
        }

        public NpcInfo? InfoById(uint id) => Infos.GetValueOrDefault(id);

        public TokenMatch ResolveToken(string token)
        {
            string key = Normalize(token);
            if (key.Length > 0 && Names.TryGetValue(key, out var ids) && ids.Count > 0)
                return new TokenMatch(ChooseOne(ids), DistinctNames(ids));

            string stripped = TrailingDigits.Replace(key, "");
            if (stripped.Length > 0 && stripped != key &&
                Names.TryGetValue(stripped, out var strippedIds) && strippedIds.Count > 0)
                return new TokenMatch(ChooseOne(strippedIds), DistinctNames(strippedIds));

            return new TokenMatch(null, 0);
        }

        public NpcInfo? ResolveName(string name)
        {
            string key = Normalize(name);
            if (key.Length == 0) return null;
            return Names.TryGetValue(key, out var ids) && ids.Count > 0 ? ChooseOne(ids) : null;
        }

        private int DistinctNames(List<uint> ids)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (uint id in ids)
            {
                if (Infos.TryGetValue(id, out var info)) names.Add(Normalize(info.Name));
            }
            return names.Count;
        }

        private NpcInfo? ChooseOne(List<uint> ids)
        {
            // Mirror NpcMetadataService.Choose: prefer the lowest row with
            // usable race/gender, so DB ids agree with the runtime manifest.
            NpcInfo? fallback = null;
            foreach (uint id in ids.OrderBy(i => i))
            {
                if (!Infos.TryGetValue(id, out var info)) continue;
                fallback ??= info;
                if (info.RaceId > 0 && info.Gender <= 1) return info;
            }
            return fallback;
        }
    }

    private static void AddName(Dictionary<string, List<uint>> index, string key, uint id)
    {
        if (key.Length == 0) return;
        if (!index.TryGetValue(key, out var ids))
            index[key] = ids = [];
        if (!ids.Contains(id)) ids.Add(id);
    }
}
