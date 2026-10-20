using Dalamud.Plugin;
using Dalamud.Plugin.Services;

namespace OhMyVoice;

/// <summary>
/// Silent-miss JSONL worklist (Q3/Q7). Appends are deduped by final ID
/// across sessions; the file is compacted on load so duplicates and torn
/// lines do not accumulate. Records source addon + game context so the
/// miner can replay without reverse lookup. NEVER includes reference-audio
/// paths (Q6).
/// </summary>
public sealed class MissLog
{
    private readonly IDalamudPluginInterface pluginInterface;
    private readonly IPluginLog log;
    private readonly HashSet<string> seen = [];
    private readonly object gate = new();

    public MissLog(IDalamudPluginInterface pluginInterface, IPluginLog log)
    {
        this.pluginInterface = pluginInterface;
        this.log = log;
        LoadExisting();
    }

    /// <summary>
    /// Rebuild the dedupe set from disk and compact duplicate/torn rows.
    /// Without this, every plugin reload forgot what had already been
    /// recorded and re-clicking a missing line appended a duplicate.
    /// </summary>
    private void LoadExisting()
    {
        try
        {
            if (!File.Exists(Path)) return;
            string[] lines = File.ReadAllLines(Path);
            var kept = new List<string>(lines.Length);
            foreach (string line in lines)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                MissEntry? entry;
                try { entry = JsonSerializer.Deserialize<MissEntry>(line); }
                catch (JsonException) { continue; }
                if (entry == null) continue;
                if (seen.Add(entry.Id)) kept.Add(line);
            }
            if (kept.Count == lines.Length) return;

            string tmp = Path + ".tmp";
            File.WriteAllLines(tmp, kept);
            File.Move(tmp, Path, overwrite: true);
            log.Information($"MissLog compacted: {lines.Length - kept.Count} duplicate/torn rows removed.");
        }
        catch (Exception ex)
        {
            log.Warning($"MissLog load failed: {ex.Message}");
        }
    }

    private string Path =>
        System.IO.Path.Combine(pluginInterface.ConfigDirectory.FullName, "misses.jsonl");

    public void Record(MissEntry entry)
    {
        string id = entry.Id;
        lock (gate)
        {
            if (!seen.Add(id)) return;
            try
            {
                Directory.CreateDirectory(pluginInterface.ConfigDirectory.FullName);
                File.AppendAllText(Path, JsonSerializer.Serialize(entry) + "\n");
            }
            catch (Exception ex)
            {
                log.Warning($"MissLog write failed: {ex.Message}");
            }
        }
    }
}
