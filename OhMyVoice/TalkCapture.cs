using Dalamud.Game.Addon.Lifecycle;
using Dalamud.Game.Addon.Lifecycle.AddonArgTypes;
using Dalamud.Plugin.Services;
using FFXIVClientStructs.FFXIV.Client.System.String;
using FFXIVClientStructs.FFXIV.Client.UI;
using FFXIVClientStructs.FFXIV.Component.GUI;

namespace OhMyVoice;

/// <summary>
/// P1 capture: AddonTalk (polled, change-detected) + TalkSubtitle (PostDraw).
/// No other providers in P1 — no dead flags (Q2). P1 acceptance includes
/// verifying Subtitle does not duplicate Talk (dispatcher dedupes by final ID).
/// </summary>
public sealed class TalkCapture : IDisposable
{
    private readonly IFramework framework;
    private readonly IGameGui gameGui;
    private readonly IAddonLifecycle addonLifecycle;
    private readonly IPluginLog log;
    private readonly Configuration config;
    private readonly GameInterop game;
    private readonly Dispatcher dispatcher;
    private readonly DialogAdvance advance;
    private readonly InputWatch input;

    private string lastSpeaker = "";
    private string lastSentence = "";
    private string lastSubtitle = "";

    /// <summary>
    /// Raw bytes of the last text seen in each source. The per-frame path
    /// compares these instead of decoding: <see cref="GameInterop.ReadUtf8"/>
    /// allocates a SeString string and runs a whitespace regex, which is wasted
    /// work on every frame the line is merely displayed.
    /// </summary>
    private byte[] lastSpeakerBytes = [];
    private byte[] lastSentenceBytes = [];
    private byte[] lastSubtitleBytes = [];

    public TalkCapture(
        IFramework framework, IGameGui gameGui, IAddonLifecycle addonLifecycle,
        IPluginLog log, Configuration config, GameInterop game, Dispatcher dispatcher,
        DialogAdvance advance, InputWatch input)
    {
        this.framework = framework;
        this.gameGui = gameGui;
        this.addonLifecycle = addonLifecycle;
        this.log = log;
        this.config = config;
        this.game = game;
        this.dispatcher = dispatcher;
        this.advance = advance;
        this.input = input;
    }

    public void Enable()
    {
        framework.Update += OnFrameworkUpdate;
        addonLifecycle.RegisterListener(AddonEvent.PostDraw, "TalkSubtitle", OnSubtitlePostDraw);
    }

    public void Dispose()
    {
        framework.Update -= OnFrameworkUpdate;
        try { addonLifecycle.UnregisterListener(OnSubtitlePostDraw); } catch { }
    }

    private void OnFrameworkUpdate(IFramework _)
    {
        if (!config.Enabled) return;
        // Read the player's input before the game consumes it: the box-change
        // check below has to know whether a confirm press is in flight.
        input.Poll();
        PollTalk();
        ResetSubtitleGuard();
    }

    private unsafe void PollTalk()
    {
        try
        {
            var addon = (AddonTalk*)gameGui.GetAddonByName("Talk").Address;
            if (addon == null) return;
            if (!addon->IsVisible)
            {
                // Clearing the byte caches too, so a later byte-identical line
                // re-dispatches instead of being swallowed by the gate below.
                lastSpeaker = "";
                lastSentence = "";
                lastSpeakerBytes = [];
                lastSentenceBytes = [];
                return;
            }

            AtkTextNode* speakerNode = addon->AtkTextNode220;
            bool speakerChanged = BytesChanged(
                speakerNode == null ? null : &speakerNode->NodeText, ref lastSpeakerBytes);
            bool sentenceChanged = BytesChanged(&addon->String268, ref lastSentenceBytes);
            if (!speakerChanged && !sentenceChanged) return;

            string speaker = game.ReadTextNode(speakerNode);
            if (string.IsNullOrEmpty(speaker)) speaker = "Narrator";
            string sentence = game.ReadUtf8(addon->String268);
            if (string.IsNullOrEmpty(sentence)) return;
            // A line still waiting to be advanced just lost its box. Cutscenes
            // only: at scene end the box also swaps while the scene is tearing
            // down, which is not the game's auto-advance.
            if (sentenceChanged && game.IsInCutscene()) advance.NoteBoxChanged(sentence);
            if (speaker == lastSpeaker && sentence == lastSentence) return;
            lastSpeaker = speaker;
            lastSentence = sentence;
            if (config.DebugLogging) log.Debug($"Talk :: {speaker} :: {sentence}");
            dispatcher.Dispatch("Talk", speaker, sentence);
        }
        catch (Exception ex)
        {
            if (config.DebugLogging) log.Debug($"Talk poll failed: {ex.Message}");
        }
    }

    private unsafe void ResetSubtitleGuard()
    {
        // Nothing to reset while no subtitle text is remembered, so the common
        // idle frame skips the second addon lookup entirely.
        if (lastSubtitle.Length == 0) return;
        try
        {
            // Reset the subtitle frame-guard when the addon hides, so a later
            // byte-identical line ("...", "Hm.") re-dispatches instead of being
            // dropped for the rest of the session. True dupes are still caught
            // by the dispatcher's final-ID + time-window dedupe.
            var sub = (AddonTalkSubtitle*)gameGui.GetAddonByName("TalkSubtitle").Address;
            if (sub == null || !sub->IsVisible)
            {
                lastSubtitle = "";
                lastSubtitleBytes = [];
            }
        }
        catch (Exception ex)
        {
            if (config.DebugLogging) log.Debug($"Subtitle hide-check failed: {ex.Message}");
        }
    }

    private unsafe void OnSubtitlePostDraw(AddonEvent type, AddonArgs args)
    {
        if (!config.Enabled) return;
        try
        {
            var addon = (AddonTalkSubtitle*)args.Addon.Address;
            if (addon == null) return;
            if (!BytesChanged(&addon->SubtitleText, ref lastSubtitleBytes)) return;
            string sentence = game.ReadUtf8(addon->SubtitleText);
            if (string.IsNullOrEmpty(sentence) || sentence == lastSubtitle) return;
            lastSubtitle = sentence;
            if (config.DebugLogging) log.Debug($"Subtitle :: {sentence}");
            dispatcher.Dispatch("TalkSubtitle", "Narrator", sentence);
        }
        catch (Exception ex)
        {
            if (config.DebugLogging) log.Debug($"Subtitle read failed: {ex.Message}");
        }
    }

    /// <summary>
    /// True when the native string's bytes differ from <paramref name="cache"/>,
    /// which is updated to the current bytes. Pure byte comparison, so an
    /// unchanged line costs a memcmp instead of a decode plus regex.
    /// </summary>
    private static unsafe bool BytesChanged(Utf8String* text, ref byte[] cache)
    {
        ReadOnlySpan<byte> current = text == null ? default : text->AsSpan();
        if (current.SequenceEqual(cache)) return false;
        cache = current.ToArray();
        return true;
    }
}
