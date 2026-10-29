using Dalamud.Configuration;

namespace OhMyVoice;

[Serializable]
public class Configuration : IPluginConfiguration
{
    public int Version { get; set; } = 0;

    /// <summary>Master switch. Off = capture nothing, play nothing.</summary>
    public bool Enabled { get; set; } = true;

    /// <summary>Explicit acceptance of the third-party/modding usage notice.</summary>
    public bool TermsAccepted { get; set; } = false;

    /// <summary>User-selected pack root containing one or more voice manifests.</summary>
    public string PackDirectory { get; set; } = string.Empty;

    /// <summary>Debug log flag. OFF by default to avoid /xllog spam (Q13).</summary>
    public bool DebugLogging { get; set; } = false;

    /// <summary>Playback volume 1-200 (%).</summary>
    public int Volume { get; set; } = 100;

    /// <summary>Wait after a Talk line appears before starting pack audio, to let real VO win (Q4).</summary>
    public int VoWaitMs { get; set; } = 100;

    /// <summary>Stop an already-playing pack line when real VO starts (Q4b).</summary>
    public bool StopOnVo { get; set; } = true;

    /// <summary>
    /// Let a playing line finish instead of cutting it when the next line
    /// arrives. Off by default: the next line cuts the current one.
    /// </summary>
    public bool QueueLines { get; set; } = false;

    /// <summary>Silence between the end of one line and the start of the next, in ms.</summary>
    public int LineGapMs { get; set; } = 1000;

    /// <summary>
    /// Click the game's Talk box once a voiced line has finished, after
    /// <see cref="LineGapMs"/>, so the next line shows up without a click.
    /// Cutscenes advance the same way as NPC dialogue.
    /// </summary>
    public bool AutoAdvance { get; set; } = false;

    /// <summary>
    /// The game's own cutscene text auto-advance was caught cutting a pack line
    /// short. Its state is unreadable (AgentCutscene models only its dialog's
    /// pending values, and no ConfigOption covers it), so this observation is
    /// the only way the settings pane can warn without a fresh cutscene.
    /// </summary>
    public bool InGameAutoAdvanceSeen { get; set; } = false;

    /// <summary>Subtitle dedupe window in seconds: same final ID within this window is skipped (Q13).</summary>
    public double SubtitleDedupeSeconds { get; set; } = 1.0;

    /// <summary>
    /// Auto follows the character's male/female sex. The game exposes no
    /// pronouns, so a manual They/She/He choice overrides it.
    /// </summary>
    public PronounChoice PlayerPronouns { get; set; } = PronounChoice.Auto;

    public void Save()
    {
        Plugin.PluginInterface.SavePluginConfig(this);
    }
}
