using Dalamud.Plugin;
using Dalamud.Plugin.Services;

namespace OhMyVoice;

/// <summary>
/// Silent-miss JSONL worklist (Q3/Q7). Appends are deduped by final ID and
/// written append-only (a torn final line is fine; the miner skips it).
/// Records source addon + game context so the miner can replay without
/// reverse lookup. NEVER includes reference-audio paths (Q6).
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
                File.AppendAllText(Path, JsonSerializer.Serialize(entry) + "\n");
            }
            catch (Exception ex)
            {
                log.Warning($"MissLog write failed: {ex.Message}");
            }
        }
    }
}
