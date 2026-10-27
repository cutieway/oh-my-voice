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
            if (manifest.Schema != PackManifestJson.CurrentSchema)
            {
                log.Warning($"Unsupported manifest schema {manifest.Schema} (expected {PackManifestJson.CurrentSchema}); pack disabled.");
                return;
            }
            if (manifest.Sanitizer != Sanitizer.Pin)
            {
                log.Warning($"Sanitizer mismatch (pack='{manifest.Sanitizer}' runtime='{Sanitizer.Pin}'). Pack DISABLED to avoid silent misses.");
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

    /// <summary>
    /// Resolve one line for the selected pronoun.
    ///
    /// The root OGG is they/them; <c>.she.ogg</c> and <c>.he.ogg</c> are
    /// optional variants. A matching variant override or pack file wins, then
    /// the root override or neutral pack file. Missing she/he audio falls back
    /// to neutral rather than the other gender.
    /// </summary>
    public string? Resolve(string id, Pronoun pronoun)
    {
        if (!PackEnabled || Manifest == null) return null;
        if (!Manifest.Voicelines.TryGetValue(id, out var entry)) return null;

        string rootOverride = id + ".ogg";

        if (entry.Variants.Count == 0)
        {
            if (TryOverride(rootOverride, out string? basePath)) return basePath;
            return ResolvePackFile(rootOverride, entry.Bytes, id);
        }

        if (pronoun != Pronoun.They)
        {
            string key = pronoun == Pronoun.She ? "she" : "he";
            if (entry.Variants.TryGetValue(key, out var variant)
                && TryResolveVariant(variant, out string? variantPath))
                return variantPath;
        }

        // The root is the neutral they/them rendering. It is also the safe
        // fallback when a selected she/he variant is absent.
        if (TryOverride(rootOverride, out string? rootPath)) return rootPath;
        return ResolvePackFile(rootOverride, entry.Bytes, id);
    }

    private string? ResolvePackFile(string fileName, long expectedBytes, string id)
    {
        if (!IsPlainFileName(fileName)) return null;
        string packPath = Path.Combine(PackDir, fileName);
        if (!File.Exists(packPath)) return null;
        try
        {
            long size = new FileInfo(packPath).Length;
            if (expectedBytes > 0 && size != expectedBytes)
            {
                if (config.DebugLogging)
                    log.Debug($"Size mismatch {id}: manifest={expectedBytes} disk={size}");
                return null;
            }
        }
        catch { return null; }
        return packPath;
    }

    private bool TryResolveVariant(VoicelineVariant variant, out string? path)
    {
        path = null;
        if (!IsPlainFileName(variant.File)) return false;

        string overridePath = Path.Combine(OverrideDir, variant.File);
        if (File.Exists(overridePath))
        {
            path = overridePath;
            return true;
        }

        path = ResolvePackFile(variant.File, variant.Bytes, variant.File);
        return path != null;
    }

    private bool TryOverride(string fileName, out string? path)
    {
        path = null;
        if (!IsPlainFileName(fileName)) return false;
        string overridePath = Path.Combine(OverrideDir, fileName);
        if (!File.Exists(overridePath)) return false;
        path = overridePath;
        return true;
    }

    private static bool IsPlainFileName(string name) =>
        !string.IsNullOrWhiteSpace(name)
        && name != "."
        && name != ".."
        && name.IndexOfAny(['/', '\\']) < 0
        && Path.GetFileName(name) == name;

    public NpcDef ResolveNpc(string cleanedSpeaker)
    {
        if (Manifest != null && Manifest.Npcs.TryGetValue(cleanedSpeaker, out var npc))
            return npc;
        return new NpcDef { NpcId = cleanedSpeaker, VoiceId = "Unknown" };
    }
}
