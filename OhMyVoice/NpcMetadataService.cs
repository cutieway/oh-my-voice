using System.Text;
using Dalamud.Plugin.Services;
using Lumina.Excel;
using Lumina.Excel.Sheets;
using OhMyVoice.Common;

namespace OhMyVoice;

/// <summary>
/// Resolve developer-facing NPC metadata from live game sheets. This is only
/// for logging: it never changes IDs, playback, or generation. The identity
/// that matters for audio is VoiceId (speaker_token); race/sex are stable for
/// a token, while expansion/zone vary per line.
/// </summary>
public sealed class NpcMetadataService
{
    private static readonly string[] RaceNames =
    [
        "unknown", "Hyur", "Elezen", "Lalafell", "Miqo'te",
        "Roegadyn", "Au Ra", "Hrothgar", "Viera",
    ];

    private static readonly string[] ExpansionNames =
    [
        "A Realm Reborn", "Heavensward", "Stormblood",
        "Shadowbringers", "Endwalker", "Dawntrail",
    ];

    private readonly IDataManager dataManager;
    private readonly IPluginLog log;
    private Dictionary<string, List<uint>>? names;
    private ExcelSheet<ENpcResident>? residents;
    private ExcelSheet<ENpcBase>? bases;
    private ExcelSheet<TerritoryType>? territories;
    private bool indexFailed;

    public NpcMetadataService(IDataManager dataManager, IPluginLog log)
    {
        this.dataManager = dataManager;
        this.log = log;
    }

    public NpcMetadata Resolve(string npcKey, string displayName, uint territoryType)
    {
        var meta = new NpcMetadata
        {
            Name = string.IsNullOrWhiteSpace(displayName) ? npcKey : displayName,
        };
        FillExpansion(meta, territoryType);

        if (!EnsureIndex()) return meta;

        uint? rowId = null;
        if (uint.TryParse(npcKey, out uint numeric) &&
            (residents!.TryGetRow(numeric, out _) || bases!.TryGetRow(numeric, out _)))
        {
            rowId = numeric;
            meta.Match = "id";
            meta.Candidates = 1;
        }
        else
        {
            string key = Normalize(displayName);
            if (key.Length == 0) key = Normalize(npcKey);
            if (key.Length > 0 && names!.TryGetValue(key, out var ids) && ids.Count > 0)
            {
                rowId = Choose(ids);
                meta.Match = "name";
                meta.Candidates = ids.Count;
                meta.Ambiguous = ids.Count > 1;
            }
        }

        if (rowId is not uint id) return meta;

        meta.NpcId = id;
        if (residents!.TryGetRow(id, out var resident))
        {
            string canonical = resident.Singular.ToString().Trim();
            if (canonical.Length > 0 && string.IsNullOrWhiteSpace(displayName))
                meta.Name = canonical;
        }
        if (!bases!.TryGetRow(id, out var npcBase)) return meta;

        meta.RaceId = npcBase.Race.RowId;
        meta.Race = npcBase.Race.RowId < RaceNames.Length
            ? RaceNames[npcBase.Race.RowId]
            : "unknown";
        meta.Gender = npcBase.Gender;
        meta.Sex = npcBase.Gender switch
        {
            0 => "male",
            1 => "female",
            _ => "unknown",
        };
        return meta;
    }

    private bool EnsureIndex()
    {
        if (names != null) return true;
        if (indexFailed) return false;
        try
        {
            residents = dataManager.GetExcelSheet<ENpcResident>();
            bases = dataManager.GetExcelSheet<ENpcBase>();
            names = new Dictionary<string, List<uint>>(StringComparer.Ordinal);
            foreach (var row in residents)
            {
                string name = row.Singular.ToString().Trim();
                if (name.Length == 0) continue;
                string key = Normalize(name);
                if (key.Length == 0) continue;
                if (!names.TryGetValue(key, out var ids))
                    names[key] = ids = [];
                ids.Add(row.RowId);
            }
            foreach (var ids in names.Values) ids.Sort();
            log.Information($"NpcMetadata: indexed {names.Count} normalized names.");
            return true;
        }
        catch (Exception ex)
        {
            indexFailed = true;
            log.Warning($"NpcMetadata index failed: {ex.Message}");
            return false;
        }
    }

    /// <summary>
    /// Prefer the lowest row with usable race/gender. ENpcResident often has
    /// hundreds of duplicate names (Alphinaud has 798 rows); the lowest row is
    /// the canonical one the offline roster uses.
    /// </summary>
    private uint Choose(List<uint> ids)
    {
        foreach (uint id in ids)
        {
            if (!bases!.TryGetRow(id, out var npcBase)) continue;
            if (npcBase.Race.RowId > 0 && npcBase.Gender <= 1) return id;
        }
        return ids[0];
    }

    private void FillExpansion(NpcMetadata meta, uint territoryType)
    {
        try
        {
            var sheet = territories ??= dataManager.GetExcelSheet<TerritoryType>();
            if (!sheet.TryGetRow(territoryType, out var territory)) return;
            uint id = territory.ExVersion.RowId;
            meta.ExpansionId = id;
            meta.Expansion = id < ExpansionNames.Length ? ExpansionNames[id] : "unknown";
        }
        catch
        {
            meta.Expansion = "unknown";
        }
    }

    private static string Normalize(string value)
    {
        var sb = new StringBuilder(value.Length);
        foreach (char ch in value)
        {
            if (char.IsLetterOrDigit(ch))
                sb.Append(char.ToLowerInvariant(ch));
        }
        return sb.ToString();
    }
}
