using Dalamud.Plugin.Services;

namespace OhMyVoice;

/// <summary>
/// Reads a pack root containing one immediate child directory per voice
/// (<c>&lt;voice&gt;/manifest.json</c>). Manifests are merged into one line/NPC
/// lookup while each line keeps the directory that owns its audio.
/// </summary>
public sealed class PackStore
{
    private readonly IPluginLog log;
    private readonly Configuration config;
    private Catalog catalog = Catalog.Empty;

    public bool PackEnabled => Volatile.Read(ref catalog).Packs.Count > 0;
    public int PackCount => Volatile.Read(ref catalog).Packs.Count;
    public int VoiceCount => Volatile.Read(ref catalog).VoiceIds.Count;
    public int VoicelineCount => Volatile.Read(ref catalog).Voicelines.Count;
    public int InvalidManifestCount => Volatile.Read(ref catalog).InvalidManifestCount;
    public string SanitizerPin => Sanitizer.Pin;

    public PackStore(IPluginLog log, Configuration config)
    {
        this.log = log;
        this.config = config;
    }

    public string PackDirectory => config.PackDirectory.Trim();

    public void Load()
    {
        string root = PackDirectory;
        if (string.IsNullOrWhiteSpace(root))
        {
            Volatile.Write(ref catalog, Catalog.Empty);
            if (config.DebugLogging) log.Debug("No pack directory configured; pack disabled.");
            return;
        }
        if (!Directory.Exists(root))
        {
            Volatile.Write(ref catalog, Catalog.Empty);
            log.Warning($"Pack directory does not exist: {root}; pack disabled.");
            return;
        }
        if (!Sanitizer.SelfCheck())
        {
            Volatile.Write(ref catalog, Catalog.Empty);
            log.Warning("Sanitizer self-check failed; pack disabled.");
            return;
        }

        List<string> manifestPaths;
        try
        {
            manifestPaths = DiscoverManifestPaths(root);
        }
        catch (Exception ex)
        {
            Volatile.Write(ref catalog, Catalog.Empty);
            log.Warning($"Pack directory scan failed ({ex.Message}); pack disabled.");
            return;
        }

        var packs = new List<LoadedPack>();
        var voicelines = new Dictionary<string, LoadedVoiceline>(StringComparer.Ordinal);
        var npcs = new Dictionary<string, NpcDef>(StringComparer.Ordinal);
        var voiceIds = new HashSet<string>(StringComparer.Ordinal);
        int invalidManifests = 0;

        foreach (string manifestPath in manifestPaths)
        {
            LoadedPack? pack = TryLoadPack(manifestPath);
            if (pack == null)
            {
                invalidManifests++;
                continue;
            }

            packs.Add(pack);
            foreach (string voiceId in pack.Manifest.Voices.Keys)
                voiceIds.Add(voiceId);

            foreach ((string speaker, NpcDef npc) in pack.Manifest.Npcs)
            {
                if (npcs.TryAdd(speaker, npc))
                    continue;

                NpcDef existing = npcs[speaker];
                if (existing.NpcId != npc.NpcId || existing.VoiceId != npc.VoiceId)
                {
                    log.Warning(
                        $"Duplicate NPC mapping '{speaker}' in {manifestPath} " +
                        $"(keeping {existing.VoiceId}/{existing.NpcId}, ignoring {npc.VoiceId}/{npc.NpcId}).");
                }
            }

            foreach ((string id, VoicelineEntry entry) in pack.Manifest.Voicelines)
            {
                if (!string.IsNullOrWhiteSpace(entry.VoiceId) && entry.VoiceId != "Unknown")
                    voiceIds.Add(entry.VoiceId);

                var loaded = new LoadedVoiceline(pack, entry);
                if (!voicelines.TryAdd(id, loaded))
                {
                    log.Warning(
                        $"Duplicate voiceline id {id} in {manifestPath}; " +
                        $"keeping {voicelines[id].Pack.Directory}.");
                }
            }
        }

        var next = new Catalog(packs, voicelines, npcs, voiceIds, invalidManifests);
        Volatile.Write(ref catalog, next);

        if (invalidManifests > 0)
            log.Warning($"Skipped {invalidManifests} invalid pack manifest(s) under {root}.");

        if (packs.Count == 0)
        {
            log.Warning(
                $"No valid voice packs found under {root}; expected immediate child " +
                "folders shaped like <voice>/manifest.json.");
            return;
        }

        log.Information(
            $"Pack catalog loaded: root={root} manifests={packs.Count} " +
            $"voices={voiceIds.Count} lines={voicelines.Count}");
    }

    /// <summary>
    /// Resolve one line for the selected pronoun.
    ///
    /// The root OGG is they/them; <c>.she.ogg</c> and <c>.he.ogg</c> are
    /// optional variants. A matching variant override or pack file wins, then
    /// the voice override or neutral pack file. Missing she/he audio falls back
    /// to neutral rather than the other gender.
    /// </summary>
    public string? Resolve(string id, Pronoun pronoun)
    {
        Catalog current = Volatile.Read(ref catalog);
        if (!current.Voicelines.TryGetValue(id, out LoadedVoiceline? line))
            return null;

        VoicelineEntry entry = line.Entry;
        string rootOverride = id + ".ogg";

        if (entry.Variants.Count == 0)
        {
            if (TryOverride(line.Pack, rootOverride, out string? basePath))
                return basePath;
            return ResolvePackFile(line.Pack, rootOverride, entry.Bytes, id);
        }

        if (pronoun != Pronoun.They)
        {
            string key = pronoun == Pronoun.She ? "she" : "he";
            if (entry.Variants.TryGetValue(key, out VoicelineVariant? variant)
                && TryResolveVariant(line.Pack, variant, out string? variantPath))
                return variantPath;
        }

        // The root is the neutral they/them rendering. It is also the safe
        // fallback when a selected she/he variant is absent.
        if (TryOverride(line.Pack, rootOverride, out string? rootPath))
            return rootPath;
        return ResolvePackFile(line.Pack, rootOverride, entry.Bytes, id);
    }

    private static List<string> DiscoverManifestPaths(string root)
    {
        var paths = new List<string>();
        IEnumerable<string> directories = Directory.EnumerateDirectories(root)
            .OrderBy(static directory => Path.GetFileName(directory), StringComparer.Ordinal);
        foreach (string directory in directories)
        {
            string manifest = Path.Combine(directory, "manifest.json");
            if (File.Exists(manifest))
                paths.Add(manifest);
        }
        return paths;
    }

    private LoadedPack? TryLoadPack(string manifestPath)
    {
        try
        {
            string packDirectory = Path.GetDirectoryName(manifestPath) ?? string.Empty;
            if (packDirectory.Length == 0)
            {
                log.Warning($"Cannot determine pack directory for {manifestPath}; skipping.");
                return null;
            }

            string json = File.ReadAllText(manifestPath);
            var manifest = JsonSerializer.Deserialize<PackManifestJson>(json);
            if (manifest == null)
            {
                log.Warning($"manifest.json failed to parse: {manifestPath}; skipping.");
                return null;
            }
            if (manifest.Schema != PackManifestJson.CurrentSchema)
            {
                log.Warning(
                    $"Unsupported manifest schema {manifest.Schema} in {manifestPath} " +
                    $"(expected {PackManifestJson.CurrentSchema}); skipping.");
                return null;
            }
            if (manifest.Sanitizer != Sanitizer.Pin)
            {
                log.Warning(
                    $"Sanitizer mismatch in {manifestPath} " +
                    $"(pack='{manifest.Sanitizer}' runtime='{Sanitizer.Pin}'); skipping.");
                return null;
            }
            if (manifest.Voices is null || manifest.Npcs is null || manifest.Voicelines is null)
            {
                log.Warning($"Manifest collections are missing in {manifestPath}; skipping.");
                return null;
            }
            if (manifest.Voices.Values.Any(static voice => voice is null)
                || manifest.Npcs.Values.Any(static npc => npc is null))
            {
                log.Warning($"Manifest contains a null voice or NPC entry in {manifestPath}; skipping.");
                return null;
            }
            if (manifest.Voicelines.Any(static pair => pair.Value is null))
            {
                log.Warning($"Manifest contains a null voiceline entry in {manifestPath}; skipping.");
                return null;
            }
            if (manifest.Voicelines.Any(static pair => pair.Value.Variants is null))
            {
                log.Warning($"Manifest contains a null variant map in {manifestPath}; skipping.");
                return null;
            }

            return new LoadedPack(
                packDirectory,
                Path.Combine(packDirectory, "voicelines"),
                Path.Combine(packDirectory, "voicelines_override"),
                manifest);
        }
        catch (Exception ex)
        {
            log.Warning($"Pack load failed for {manifestPath} ({ex.Message}); skipping.");
            return null;
        }
    }

    private string? ResolvePackFile(
        LoadedPack pack, string fileName, long expectedBytes, string id)
    {
        if (!IsPlainFileName(fileName)) return null;
        string packPath = Path.Combine(pack.VoicelinesDirectory, fileName);
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

    private bool TryResolveVariant(
        LoadedPack pack, VoicelineVariant variant, out string? path)
    {
        path = null;
        if (!IsPlainFileName(variant.File)) return false;

        if (TryOverride(pack, variant.File, out path))
            return true;

        path = ResolvePackFile(pack, variant.File, variant.Bytes, variant.File);
        return path != null;
    }

    private static bool TryOverride(LoadedPack pack, string fileName, out string? path)
    {
        path = null;
        if (!IsPlainFileName(fileName)) return false;

        string overridePath = Path.Combine(pack.OverrideDirectory, fileName);
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
        Catalog current = Volatile.Read(ref catalog);
        if (current.Npcs.TryGetValue(cleanedSpeaker, out NpcDef? npc))
            return npc;
        return new NpcDef { NpcId = cleanedSpeaker, VoiceId = "Unknown" };
    }

    private sealed record LoadedPack(
        string Directory,
        string VoicelinesDirectory,
        string OverrideDirectory,
        PackManifestJson Manifest);

    private sealed record LoadedVoiceline(LoadedPack Pack, VoicelineEntry Entry);

    private sealed record Catalog(
        IReadOnlyList<LoadedPack> Packs,
        IReadOnlyDictionary<string, LoadedVoiceline> Voicelines,
        IReadOnlyDictionary<string, NpcDef> Npcs,
        IReadOnlySet<string> VoiceIds,
        int InvalidManifestCount)
    {
        public static Catalog Empty { get; } = new(
            Array.Empty<LoadedPack>(),
            new Dictionary<string, LoadedVoiceline>(),
            new Dictionary<string, NpcDef>(),
            new HashSet<string>(),
            0);
    }
}
