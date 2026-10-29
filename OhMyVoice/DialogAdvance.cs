using Dalamud.Plugin.Services;
using FFXIVClientStructs.FFXIV.Client.UI;
using FFXIVClientStructs.FFXIV.Component.GUI;

namespace OhMyVoice;

/// <summary>
/// Auto-advance (Q14): click the game's Talk box once the pack line that voices
/// it has finished, so the next line appears without a manual click.
///
/// The click is a synthetic event delivered straight to the addon
/// (<c>AtkEventListener.ReceiveEvent</c> with MouseDown, MouseClick, MouseUp),
/// the same call TextAdvance makes through ECommons' <c>AddonMaster.Talk.Click</c>.
/// No keyboard input is synthesized, so the game window does not need focus and
/// other applications never see a stray key.
///
/// Advancing is only ever armed for a line the plugin is about to play, and the
/// click itself re-checks the box: Talk visible, same sentence still displayed,
/// no real VO. A box the player already advanced, closed, or one the game
/// replaced is left alone.
/// </summary>
public sealed class DialogAdvance : IDisposable
{
    /// <summary>
    /// AtkEvent state byte for the click, as the game's own handler sets it
    /// (ECommons builds the same event with <c>CreateAtkEvent(132)</c>).
    /// </summary>
    private const byte ClickStateFlags = 132;

    /// <summary>Real-VO guard window, the same rule the dispatcher uses to stay silent.</summary>
    private const int VoGuardMs = 1000;

    private readonly IPluginLog log;
    private readonly Configuration config;
    private readonly IGameGui gameGui;
    private readonly GameInterop game;
    private readonly Playback playback;
    private readonly VoSuppression suppression;
    private readonly InputWatch input;
    private readonly Action onGameAdvanced;

    private readonly object gate = new();
    private string? armedId;
    private string? armedSentence;
    private long armToken;
    private int gameAdvancedCount;
    private long lastGameAdvanceTicks;

    public DialogAdvance(
        IPluginLog log, Configuration config, IGameGui gameGui, GameInterop game,
        Playback playback, VoSuppression suppression, InputWatch input,
        Action onGameAdvanced)
    {
        this.log = log;
        this.config = config;
        this.gameGui = gameGui;
        this.game = game;
        this.playback = playback;
        this.suppression = suppression;
        this.input = input;
        this.onGameAdvanced = onGameAdvanced;
        playback.LineFinished += OnLineFinished;
    }

    public void Dispose() => playback.LineFinished -= OnLineFinished;

    /// <summary>
    /// Lines in the current cutscene that moved on with no confirm press from
    /// the player, which is the only readable evidence that the game's own
    /// cutscene text auto-advance is on: its live flag is unmapped and
    /// AgentCutscene exposes only its dialog's pending values.
    /// </summary>
    public int GameAdvancedCount => Volatile.Read(ref gameAdvancedCount);

    public DateTime LastGameAdvanceUtc =>
        new(Volatile.Read(ref lastGameAdvanceTicks), DateTimeKind.Utc);

    /// <summary>
    /// The Talk box moved on while a line was still waiting to be advanced.
    /// The game's own cutscene text auto-advance and a player clicking ahead
    /// look identical except for one thing: input. No confirm press in the last
    /// quarter second means the game moved the box itself, so the race is
    /// recorded for the settings warning; a press means the player was faster
    /// and nothing is recorded.
    /// </summary>
    public void NoteBoxChanged(string shown)
    {
        lock (gate)
        {
            if (armedSentence == null) return;
            armedId = null;
            armedSentence = null;
            armToken++;
        }

        if (input.ConfirmedRecently())
        {
            Trace($"box changed early, player input: \"{Shorten(shown)}\"");
            return;
        }

        Trace($"box changed early, no player input: \"{Shorten(shown)}\"");
        Interlocked.Increment(ref gameAdvancedCount);
        Volatile.Write(ref lastGameAdvanceTicks, DateTime.UtcNow.Ticks);
        onGameAdvanced();
    }

    /// <summary>Forget the current cutscene's game advances.</summary>
    public void ResetGameAdvances()
    {
        Volatile.Write(ref gameAdvancedCount, 0);
        Volatile.Write(ref lastGameAdvanceTicks, 0);
    }

    /// <summary>
    /// Only the line that is starting now may advance the box when it ends.
    /// Arming a newer line invalidates any wait still in flight.
    /// </summary>
    public void Arm(string id, string sentence)
    {
        lock (gate)
        {
            armedId = id;
            armedSentence = sentence;
            armToken++;
        }
        Trace($"armed {id}");
    }

    /// <summary>Forget the armed line. A wait already running re-checks the token.</summary>
    public void Disarm()
    {
        lock (gate)
        {
            armedId = null;
            armedSentence = null;
            armToken++;
        }
    }

    private void OnLineFinished(string id)
    {
        bool armed;
        string sentence = "";
        long token = 0;
        lock (gate)
        {
            armed = armedId == id && armedSentence != null;
            if (armed)
            {
                sentence = armedSentence!;
                armedId = null;
                armedSentence = null;
                token = ++armToken;
            }
        }
        if (!armed)
        {
            Trace($"finished {id}, not armed");
            return;
        }
        if (!config.Enabled || !config.AutoAdvance)
        {
            Trace("skipped: auto-advance off");
            return;
        }
        Trace($"finished {id}, waiting {config.LineGapMs}ms");
        _ = AdvanceAsync(token, sentence);
    }

    private async Task AdvanceAsync(long token, string sentence)
    {
        try
        {
            // Same silence the game leaves between two lines: the next box
            // appears LineGapMs after the line that was just voiced.
            await playback.WaitGapAsync(config.LineGapMs, CancellationToken.None);
            await game.RunOnFramework(() => AdvanceOnFramework(token, sentence));
        }
        catch (ObjectDisposedException)
        {
            // Plugin unloaded mid-wait.
        }
        catch (Exception ex)
        {
            log.Warning($"Auto-advance failed: {ex.Message}");
        }
    }

    private unsafe void AdvanceOnFramework(long token, string sentence)
    {
        lock (gate)
        {
            if (token != armToken)
            {
                Trace("skipped: superseded");
                return;
            }
            armToken++;
        }
        if (!config.Enabled || !config.AutoAdvance)
        {
            Trace("skipped: auto-advance off");
            return;
        }
        if ((DateTime.UtcNow - suppression.LastRealVoUtc).TotalMilliseconds < VoGuardMs)
        {
            Trace($"skipped: real VO {suppression.LastRealVoPath}");
            return;
        }
        if (playback.IsPlaying)
        {
            Trace("skipped: still playing");
            return;
        }

        var addon = (AtkUnitBase*)gameGui.GetAddonByName("Talk").Address;
        if (addon == null || !addon->IsVisible)
        {
            Trace("skipped: Talk hidden");
            return;
        }

        // The player advanced, closed the box, or the game moved on: clicking
        // now would skip a line whose audio was never played.
        var talk = (AddonTalk*)addon;
        string shown = game.ReadUtf8(talk->String268);
        if (shown != sentence)
        {
            Trace($"skipped: box changed to \"{Shorten(shown)}\"");
            return;
        }

        AtkStage* stage = AtkStage.Instance();
        if (stage == null)
        {
            Trace("skipped: no AtkStage");
            return;
        }

        Trace("clicking Talk");
        Click(addon, stage);
    }

    /// <summary>Debug-only trace. Every gate is visible so a live test that
    /// does nothing still leaves a reason in the log.</summary>
    private void Trace(string message)
    {
        if (config.DebugLogging) log.Information($"Auto-advance: {message}");
    }

    private static string Shorten(string text) =>
        text.Length <= 60 ? text : text[..60] + "…";

    private static unsafe void Click(AtkUnitBase* addon, AtkStage* stage)
    {
        AtkEvent* evt = stackalloc AtkEvent[1];
        *evt = default;
        evt->Listener = (AtkEventListener*)addon;
        evt->Target = &stage->AtkEventTarget;
        evt->State.StateFlags = (AtkEventStateFlags)ClickStateFlags;

        AtkEventData* data = stackalloc AtkEventData[1];
        *data = default;

        addon->ReceiveEvent(AtkEventType.MouseDown, 0, evt, data);
        addon->ReceiveEvent(AtkEventType.MouseClick, 0, evt, data);
        addon->ReceiveEvent(AtkEventType.MouseUp, 0, evt, data);
    }
}
