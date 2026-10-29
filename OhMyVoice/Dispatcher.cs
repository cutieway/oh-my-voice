using Dalamud.Plugin.Services;

namespace OhMyVoice;

/// <summary>
/// Dispatcher (Q2/Q3/Q4/Q13). Subtitle dedupe by final ID + window; 100ms VO
/// wait before start; stop on real VO. Lines never overlap: cut mode supersedes
/// the playing line, queue mode plays every accepted line in dialog order.
/// </summary>
public sealed class Dispatcher
{
    private const string Lang = "en";

    /// <summary>
    /// Queue-mode backlog cap. A long skip streak must not turn into minutes of
    /// talking after the player has moved on, so the oldest pending line is
    /// dropped first.
    /// </summary>
    private const int MaxQueuedLines = 8;

    private readonly IPluginLog log;
    private readonly Configuration config;
    private readonly GameInterop game;
    private readonly PackStore pack;
    private readonly Playback playback;
    private readonly VoiceLog events;
    private readonly NpcMetadataService npcMetadata;
    private readonly VoSuppression suppression;
    private readonly DialogAdvance advance;

    private readonly object gate = new();
    private CancellationTokenSource? pendingCts;
    private (string Id, DateTime Utc)? lastPlayed;

    private readonly LineQueue lineQueue;

    public Dispatcher(
        IPluginLog log, Configuration config, GameInterop game,
        PackStore pack, Playback playback, VoiceLog events,
        NpcMetadataService npcMetadata, VoSuppression suppression,
        DialogAdvance advance)
    {
        this.log = log;
        this.config = config;
        this.game = game;
        this.pack = pack;
        this.playback = playback;
        this.events = events;
        this.npcMetadata = npcMetadata;
        this.suppression = suppression;
        this.advance = advance;

        lineQueue = new LineQueue(
            MaxQueuedLines,
            playback.WaitForIdleAsync,
            playback.WaitGapAsync,
            () => config.LineGapMs,
            PlayLine,
            () => config.Enabled,
            message => { if (config.DebugLogging) log.Information(message); });
    }

    public void OnRealVoStarted()
    {
        // (Q4b/Q13) Real VO starting stops the playing pack line and drops the
        // backlog: the game is talking now, not the pack.
        if (!config.StopOnVo) return;
        ClearQueue();
        playback.Stop();
    }

    /// <summary>
    /// Drop every queued line and cancel a pending auto-advance. The line
    /// already playing is left alone.
    /// </summary>
    public void ClearQueue()
    {
        lineQueue.Clear();
        advance.Disarm();
    }

    /// <summary>Lines waiting for their turn in queue mode.</summary>
    public int QueuedCount => lineQueue.Count;

    public void Dispatch(string source, string rawSpeaker, string rawSentence)
    {
        if (!config.Enabled) return;
        _ = DispatchAsync(source, rawSpeaker, rawSentence);
    }

    private async Task DispatchAsync(string source, string rawSpeaker, string rawSentence)
    {
        long? ticket = null;
        CancellationToken token;
        if (config.QueueLines)
        {
            ticket = lineQueue.Enqueue();
            token = CancellationToken.None;
        }
        else
        {
            CancellationTokenSource cts;
            lock (gate)
            {
                pendingCts?.Cancel();
                pendingCts?.Dispose();
                pendingCts = new CancellationTokenSource();
                cts = pendingCts;
            }
            token = cts.Token;
        }

        try
        {
            var (id, path, sentence) = await ResolveLineAsync(source, rawSpeaker, rawSentence, token);
            if (ticket is long queued)
            {
                // The pump plays it in dialog order once the previous line ends.
                lineQueue.Publish(queued, id, path, sentence);
                if (id != null && path != null && config.DebugLogging)
                    log.Information($"{id}: queued");
                return;
            }
            if (id == null || path == null) return;
            await StartCutLineAsync(id, path, sentence, token);
        }
        catch (TaskCanceledException)
        {
            // Superseded by a newer line before it started.
        }
        catch (Exception ex)
        {
            log.Warning($"Dispatch failed: {ex.Message}");
        }
        finally
        {
            // Always release the pump, even on an unexpected failure. A second
            // publish is ignored, so the resolved file above always wins.
            if (ticket is long queued) lineQueue.Publish(queued, null, null);
        }
    }

    /// <summary>
    /// Sanitize, resolve, and record one line. Nulls mean the line was dropped
    /// (dedupe, real VO, or no pack file), and nothing should play. The third
    /// value is the Talk-box text for auto-advance: it is only set for lines
    /// captured from the Talk addon itself, since only those can be clicked.
    /// </summary>
    private async Task<(string? Id, string? Path, string? Sentence)> ResolveLineAsync(
        string source, string rawSpeaker, string rawSentence, CancellationToken token)
    {
        string first = game.PlayerFirstName ?? "Forename";
        string last = game.PlayerLastName ?? "Surname";
        var (speaker, sentence) = Sanitizer.Clean(rawSpeaker, rawSentence, first, last);
        if (string.IsNullOrWhiteSpace(sentence)) return (null, null, null);

        var npc = pack.ResolveNpc(speaker);
        string voiceId = npc.VoiceId ?? "Unknown";
        string npcId = npc.NpcId ?? speaker;
        string id = VoiceId.ComputeId(Lang, voiceId, npcId, sentence);
        var metadata = npcMetadata.Resolve(npcId, speaker, game.TerritoryTypeId);

        // (Q13) Subtitle dedupe by final ID + window, not raw bytes.
        lock (gate)
        {
            if (lastPlayed is { } prev && prev.Id == id &&
                (DateTime.UtcNow - prev.Utc).TotalSeconds < config.SubtitleDedupeSeconds)
            {
                if (config.DebugLogging) log.Information($"Dedupe {source} {id}");
                return (null, null, null);
            }
        }

        // (Q4a) Wait to let real VO win before starting.
        try
        {
            await Task.Delay(config.VoWaitMs, token);
        }
        catch (TaskCanceledException)
        {
            return (null, null, null); // superseded by a newer line
        }
        if ((DateTime.UtcNow - suppression.LastRealVoUtc).TotalMilliseconds < 1000)
        {
            if (config.DebugLogging) log.Information($"Suppressed {id}: real VO {suppression.LastRealVoPath}");
            return (null, null, null);
        }

        Pronoun pronoun = Pronouns.Effective(
            config.PlayerPronouns, game.PlayerIsFemale);
        string? path = pack.Resolve(id, pronoun);
        if (path == null)
        {
            // (Q3) Silent miss + worklist. Sentence/Speaker are the
            // plugin-sanitized forms — the generator uses them verbatim.
            events.Record(new VoiceEvent
            {
                Result = "miss",
                Id = id,
                Lang = Lang,
                VoiceId = voiceId,
                NpcId = npcId,
                Speaker = speaker,
                Sentence = sentence,
                RawSpeaker = rawSpeaker,
                RawSentence = rawSentence,
                Source = source,
                Zone = game.GetZone(),
                GameVersion = Plugin.GameVersion,
                Npc = metadata,
                Utc = DateTimeOffset.UtcNow,
            });
            if (config.DebugLogging) log.Information($"Miss {source} {id} ({speaker})");
            return (null, null, null);
        }

        // A hit is evidence for the developer that this line is covered.
        // It is only a log row; generation remains an offline action.
        events.Record(new VoiceEvent
        {
            Result = "hit",
            Id = id,
            Lang = Lang,
            VoiceId = voiceId,
            NpcId = npcId,
            Speaker = speaker,
            Sentence = sentence,
            RawSpeaker = rawSpeaker,
            RawSentence = rawSentence,
            Source = source,
            Zone = game.GetZone(),
            GameVersion = Plugin.GameVersion,
            Npc = metadata,
            Utc = DateTimeOffset.UtcNow,
        });

        // Dedupe counts from acceptance, not from playback: in queue mode a
        // repeat that is still waiting must not be queued twice.
        lock (gate) lastPlayed = (id, DateTime.UtcNow);
        return (id, path, source == "Talk" ? rawSentence : null);
    }

    /// <summary>
    /// Queue mode's play step. Arming here, not at enqueue time, keeps the
    /// advancer tied to the line that actually reaches the speakers.
    /// </summary>
    private void PlayLine(string id, string path, string? sentence)
    {
        playback.Play(id, path);
        if (sentence != null) advance.Arm(id, sentence);
    }

    /// <summary>Cut mode: stop the playing line, wait the gap, then play.</summary>
    private async Task StartCutLineAsync(
        string id, string path, string? sentence, CancellationToken token)
    {
        long handoffStart = Environment.TickCount64;
        playback.Stop();
        await playback.WaitGapAsync(config.LineGapMs, token);
        if (config.DebugLogging)
            log.Information($"{id}: cut, waited {Environment.TickCount64 - handoffStart}ms");
        PlayLine(id, path, sentence);
    }

}
