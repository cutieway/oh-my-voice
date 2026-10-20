using System.Numerics;
using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Windowing;

namespace OhMyVoice.Windows;

public class ConfigWindow : Window, IDisposable
{
    private readonly Configuration configuration;

    public ConfigWindow(Plugin plugin)
        : base("Oh My Voice Settings###omv-config")
    {
        Flags = ImGuiWindowFlags.NoResize | ImGuiWindowFlags.NoCollapse | ImGuiWindowFlags.NoScrollbar |
                ImGuiWindowFlags.NoScrollWithMouse;
        Size = new Vector2(360, 260);
        SizeCondition = ImGuiCond.Always;
        configuration = plugin.Configuration;
    }

    public void Dispose() { }

    public override void Draw()
    {
        var enabled = configuration.Enabled;
        if (ImGui.Checkbox("Enabled", ref enabled))
        {
            configuration.Enabled = enabled;
            configuration.Save();
        }

        var volume = configuration.Volume;
        if (ImGui.SliderInt("Volume %", ref volume, 1, 200))
        {
            configuration.Volume = volume;
            configuration.Save();
        }

        var voWait = configuration.VoWaitMs;
        if (ImGui.SliderInt("Real-VO wait (ms)", ref voWait, 0, 500))
        {
            configuration.VoWaitMs = voWait;
            configuration.Save();
        }

        var stopOnVo = configuration.StopOnVo;
        if (ImGui.Checkbox("Stop pack line when real VO starts", ref stopOnVo))
        {
            configuration.StopOnVo = stopOnVo;
            configuration.Save();
        }

        var debug = configuration.DebugLogging;
        if (ImGui.Checkbox("Debug logging (/xllog)", ref debug))
        {
            configuration.DebugLogging = debug;
            configuration.Save();
        }

        ImGui.Spacing();
        ImGui.TextDisabled($"Sanitizer: {Sanitizer.Pin}");
    }
}
