using Dalamud.Plugin;
using Dalamud.Plugin.Services;

namespace OhMyVoice;

/// <summary>
/// Silent-miss JSONL worklist (Q3/Q7). Appends are deduped by final ID and
/// flushed atomically. Records source addon + game context so the miner can
/// replay without reverse lookup. NEVER includes reference-audio paths (Q6).
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
                string line = JsonSerializer.Serialize(entry);
                string tmp = Path + ".tmp";
                string existing = File.Exists(Path) ? File.ReadAllText(Path) : "";
                File.WriteAllText(tmp, existing + line + "\n");
                File.Move(tmp, Path, overwrite: true);
            }
            catch (Exception ex)
            {
                log.Warning($"MissLog write failed: {ex.Message}");
            }
        }
    }
}
