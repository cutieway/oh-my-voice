using Dalamud.Configuration;

namespace OhMyVoice;

[Serializable]
public class Configuration : IPluginConfiguration
{
    public int Version { get; set; } = 0;

    /// <summary>Master switch. Off = capture nothing, play nothing.</summary>
    public bool Enabled { get; set; } = true;

    /// <summary>Debug log flag. OFF by default to avoid /xllog spam (Q13).</summary>
    public bool DebugLogging { get; set; } = false;

    /// <summary>Playback volume 1-200 (%).</summary>
    public int Volume { get; set; } = 100;

    /// <summary>Wait after a Talk line appears before starting pack audio, to let real VO win (Q4).</summary>
    public int VoWaitMs { get; set; } = 100;

    /// <summary>Stop an already-playing pack line when real VO starts (Q4b).</summary>
    public bool StopOnVo { get; set; } = true;

    /// <summary>Subtitle dedupe window in seconds: same final ID within this window is skipped (Q13).</summary>
    public double SubtitleDedupeSeconds { get; set; } = 1.0;

    public void Save()
    {
        Plugin.PluginInterface.SavePluginConfig(this);
    }
}
