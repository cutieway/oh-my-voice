using Dalamud.Game.Addon.Lifecycle;
using Dalamud.Game.Addon.Lifecycle.AddonArgTypes;
using Dalamud.Plugin.Services;
using FFXIVClientStructs.FFXIV.Client.UI;

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

    private string lastSpeaker = "";
    private string lastSentence = "";
    private string lastSubtitle = "";

    public TalkCapture(
        IFramework framework, IGameGui gameGui, IAddonLifecycle addonLifecycle,
        IPluginLog log, Configuration config, GameInterop game, Dispatcher dispatcher)
    {
        this.framework = framework;
        this.gameGui = gameGui;
        this.addonLifecycle = addonLifecycle;
        this.log = log;
        this.config = config;
        this.game = game;
        this.dispatcher = dispatcher;
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

    private unsafe void OnFrameworkUpdate(IFramework _)
    {
        if (!config.Enabled) return;
        try
        {
            var addon = (AddonTalk*)gameGui.GetAddonByName("Talk").Address;
            if (addon == null) return;
            if (!addon->IsVisible)
            {
                lastSpeaker = "";
                lastSentence = "";
                return;
            }
            string speaker = game.ReadTextNode(addon->AtkTextNode220);
            if (string.IsNullOrEmpty(speaker)) speaker = "Narrator";
            string sentence = game.ReadUtf8(addon->String268);
            if (string.IsNullOrEmpty(sentence)) return;
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
        try
        {
            // Reset the subtitle frame-guard when the addon hides, so a later
            // byte-identical line ("...", "Hm.") re-dispatches instead of being
            // dropped for the rest of the session. True dupes are still caught
            // by the dispatcher's final-ID + time-window dedupe.
            var sub = (AddonTalkSubtitle*)gameGui.GetAddonByName("TalkSubtitle").Address;
            if (sub == null || !sub->IsVisible)
                lastSubtitle = "";
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
}
