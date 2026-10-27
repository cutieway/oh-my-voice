using Dalamud.Plugin;
using Dalamud.Plugin.Services;
using OhMyVoice.Common;

namespace OhMyVoice;

/// <summary>
/// Cache hit/miss JSONL log (Q3/Q7). Miss rows are the generation worklist;
/// hit rows prove a line is already covered. Rows are deduped by
/// (Result, final ID) across sessions. Metadata is game-derived context for
/// developers and never causes generation.
///
/// The legacy misses.jsonl is read once and rewritten as events.jsonl so an
/// existing worklist survives the upgrade.
/// </summary>
public sealed class VoiceLog
{
    public const string FileName = "events.jsonl";
    private const string LegacyFileName = "misses.jsonl";

    private readonly IDalamudPluginInterface pluginInterface;
    private readonly IPluginLog log;
    private readonly HashSet<string> seen = [];
    private readonly object gate = new();

    public VoiceLog(IDalamudPluginInterface pluginInterface, IPluginLog log)
    {
        this.pluginInterface = pluginInterface;
        this.log = log;
        LoadExisting();
    }

    public string Path =>
        System.IO.Path.Combine(pluginInterface.ConfigDirectory.FullName, FileName);

    private string LegacyPath =>
        System.IO.Path.Combine(pluginInterface.ConfigDirectory.FullName, LegacyFileName);

    /// <summary>Result+ID: a miss then a hit are both retained; repeats are not.</summary>
    private static string Key(VoiceEvent entry) => entry.Result + ":" + entry.Id;

    private static void Normalize(VoiceEvent entry)
    {
        entry.Result = string.Equals(entry.Result, "hit", StringComparison.OrdinalIgnoreCase)
            ? "hit" : "miss";
        if (string.IsNullOrWhiteSpace(entry.Id)) entry.Id = entry.ComputedId;
        entry.Npc ??= new NpcMetadata();
    }

    private void LoadExisting()
    {
        try
        {
            bool migrating = !File.Exists(Path) && File.Exists(LegacyPath);
            string source = File.Exists(Path) ? Path : LegacyPath;
            if (!File.Exists(source)) return;

            string[] lines = File.ReadAllLines(source);
            var kept = new List<string>(lines.Length);
            foreach (string line in lines)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                VoiceEvent? entry;
                try { entry = JsonSerializer.Deserialize<VoiceEvent>(line); }
                catch (JsonException) { continue; }
                if (entry == null) continue;
                Normalize(entry);
                if (seen.Add(Key(entry))) kept.Add(JsonSerializer.Serialize(entry));
            }

            if (!migrating && kept.Count == lines.Length) return;

            string tmp = Path + ".tmp";
            File.WriteAllLines(tmp, kept);
            File.Move(tmp, Path, overwrite: true);
            if (migrating)
                log.Information($"VoiceLog migrated {kept.Count} rows from {LegacyFileName} -> {FileName}.");
            else
                log.Information($"VoiceLog compacted: {lines.Length - kept.Count} duplicate/torn rows removed.");
        }
        catch (Exception ex)
        {
            log.Warning($"VoiceLog load failed: {ex.Message}");
        }
    }

    public void Record(VoiceEvent entry)
    {
        Normalize(entry);
        lock (gate)
        {
            if (!seen.Add(Key(entry))) return;
            try
            {
                Directory.CreateDirectory(pluginInterface.ConfigDirectory.FullName);
                File.AppendAllText(Path, JsonSerializer.Serialize(entry) + "\n");
            }
            catch (Exception ex)
            {
                log.Warning($"VoiceLog write failed: {ex.Message}");
            }
        }
    }
}
