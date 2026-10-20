using Dalamud.Plugin.Services;

namespace OhMyVoice;

/// <summary>
/// Dispatcher (Q2/Q3/Q4/Q13). Single in-flight Talk line; subtitle dedupe by
/// final ID + window; 100ms VO wait before start; stop on real VO.
/// </summary>
public sealed class Dispatcher
{
    private const string Lang = "en";

    private readonly IPluginLog log;
    private readonly Configuration config;
    private readonly GameInterop game;
    private readonly PackStore pack;
    private readonly Playback playback;
    private readonly MissLog misses;
    private readonly VoSuppression suppression;

    private readonly object gate = new();
    private CancellationTokenSource? pendingCts;
    private (string Id, DateTime Utc)? lastPlayed;

    public Dispatcher(
        IPluginLog log, Configuration config, GameInterop game,
        PackStore pack, Playback playback, MissLog misses, VoSuppression suppression)
    {
        this.log = log;
        this.config = config;
        this.game = game;
        this.pack = pack;
        this.playback = playback;
        this.misses = misses;
        this.suppression = suppression;
    }

    public void OnRealVoStarted()
    {
        // (Q4b/Q13) Real VO starting stops the playing pack line.
        if (config.StopOnVo) playback.Stop();
    }

    public void Dispatch(string source, string rawSpeaker, string rawSentence)
    {
        if (!config.Enabled) return;
        _ = DispatchAsync(source, rawSpeaker, rawSentence);
    }

    private async Task DispatchAsync(string source, string rawSpeaker, string rawSentence)
    {
        string first = game.PlayerFirstName ?? "Forename";
        string last = game.PlayerLastName ?? "Surname";
        var (speaker, sentence) = Sanitizer.Clean(rawSpeaker, rawSentence, first, last);
        if (string.IsNullOrWhiteSpace(sentence)) return;

        var npc = pack.ResolveNpc(speaker);
        string voiceId = npc.VoiceId ?? "Unknown";
        string npcId = npc.NpcId ?? speaker;
        string id = VoiceId.ComputeId(Lang, voiceId, npcId, sentence);

        // (Q13) Subtitle dedupe by final ID + window, not raw bytes.
        lock (gate)
        {
            if (lastPlayed is { } prev && prev.Id == id &&
                (DateTime.UtcNow - prev.Utc).TotalSeconds < config.SubtitleDedupeSeconds)
            {
                if (config.DebugLogging) log.Debug($"Dedupe {source} {id}");
                return;
            }
        }

        // (Q4a) Wait to let real VO win before starting.
        CancellationTokenSource cts;
        lock (gate)
        {
            pendingCts?.Cancel();
            pendingCts?.Dispose();
            pendingCts = new CancellationTokenSource();
            cts = pendingCts;
        }
        try
        {
            await Task.Delay(config.VoWaitMs, cts.Token);
        }
        catch (TaskCanceledException)
        {
            return; // superseded by a newer line
        }
        if ((DateTime.UtcNow - suppression.LastRealVoUtc).TotalMilliseconds < 1000)
        {
            if (config.DebugLogging) log.Debug($"Suppressed {id}: real VO {suppression.LastRealVoPath}");
            return;
        }

        string? path = pack.Resolve(id);
        if (path == null)
        {
            // (Q3) Silent miss + worklist.
            misses.Record(new MissEntry
            {
                Lang = Lang,
                VoiceId = voiceId,
                NpcId = npcId,
                Sentence = sentence,
                RawSpeaker = rawSpeaker,
                RawSentence = rawSentence,
                Source = source,
                Zone = game.GetZone(),
                GameVersion = Plugin.GameVersion,
                Utc = DateTimeOffset.UtcNow,
            });
            if (config.DebugLogging) log.Debug($"Miss {source} {id} ({speaker})");
            return;
        }

        lock (gate) { lastPlayed = (id, DateTime.UtcNow); }
        playback.Play(id, path);
    }
}
