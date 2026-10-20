using Dalamud.Plugin;
using Dalamud.Plugin.Services;

namespace OhMyVoice;

/// <summary>
/// Pack store: manifest + voicelines/{id}.ogg with voicelines_override/ winning (Q8).
/// Verifies manifest.sanitizer against Sanitizer.Pin on load (Q12):
/// mismatch means warn and disable the pack, never silently miss every line.
/// </summary>
public sealed class PackStore
{
    private readonly IDalamudPluginInterface pluginInterface;
    private readonly IPluginLog log;
    private readonly Configuration config;

    public PackManifestJson? Manifest { get; private set; }
    public bool PackEnabled { get; private set; }
    public string SanitizerPin => Sanitizer.Pin;

    public PackStore(IDalamudPluginInterface pluginInterface, IPluginLog log, Configuration config)
    {
        this.pluginInterface = pluginInterface;
        this.log = log;
        this.config = config;
    }

    public string PackDir =>
        Path.Combine(pluginInterface.ConfigDirectory.FullName, "voicelines");

    public string OverrideDir =>
        Path.Combine(pluginInterface.ConfigDirectory.FullName, "voicelines_override");

    public string ManifestPath =>
        Path.Combine(pluginInterface.ConfigDirectory.FullName, "manifest.json");

    public void Load()
    {
        PackEnabled = false;
        Manifest = null;
        try
        {
            Directory.CreateDirectory(PackDir);
            Directory.CreateDirectory(OverrideDir);
            if (!File.Exists(ManifestPath))
            {
                if (config.DebugLogging) log.Debug("No manifest.json; pack disabled.");
                return;
            }
            string json = File.ReadAllText(ManifestPath);
            var manifest = JsonSerializer.Deserialize<PackManifestJson>(json);
            if (manifest == null)
            {
                log.Warning("manifest.json failed to parse; pack disabled.");
                return;
            }
            if (!string.IsNullOrEmpty(manifest.Sanitizer) && manifest.Sanitizer != Sanitizer.Pin)
            {
                log.Warning($"Sanitizer mismatch: pack={manifest.Sanitizer} runtime={Sanitizer.Pin}. Pack DISABLED to avoid silent misses.");
                return;
            }
            if (!Sanitizer.SelfCheck())
            {
                log.Warning("Sanitizer self-check failed; pack disabled.");
                return;
            }
            Manifest = manifest;
            PackEnabled = true;
            log.Information($"Pack loaded: lang={manifest.Language} lines={manifest.Voicelines.Count} sanitizer={manifest.Sanitizer}");
        }
        catch (Exception ex)
        {
            log.Warning($"Pack load failed ({ex.Message}); pack disabled.");
        }
    }

    /// <summary>Override wins over pack (Q8). Returns null on size mismatch.</summary>
    public string? Resolve(string id)
    {
        if (!PackEnabled || Manifest == null) return null;
        if (!Manifest.Voicelines.TryGetValue(id, out var entry)) return null;

        string overridePath = Path.Combine(OverrideDir, id + ".ogg");
        if (File.Exists(overridePath)) return overridePath;

        string packPath = Path.Combine(PackDir, id + ".ogg");
        if (!File.Exists(packPath)) return null;
        try
        {
            long size = new FileInfo(packPath).Length;
            if (entry.Bytes > 0 && size != entry.Bytes)
            {
                if (config.DebugLogging) log.Debug($"Size mismatch {id}: manifest={entry.Bytes} disk={size}");
                return null;
            }
        }
        catch { return null; }
        return packPath;
    }

    public NpcDef ResolveNpc(string cleanedSpeaker)
    {
        if (Manifest != null && Manifest.Npcs.TryGetValue(cleanedSpeaker, out var npc))
            return npc;
        return new NpcDef { NpcId = cleanedSpeaker, VoiceId = "Unknown" };
    }
}
