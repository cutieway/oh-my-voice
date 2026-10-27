using System.Numerics;
using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Windowing;

namespace OhMyVoice.Windows;

public class MainWindow : Window, IDisposable
{
    private readonly Plugin plugin;

    public MainWindow(Plugin plugin)
        : base("Oh My Voice##omv-main", ImGuiWindowFlags.NoScrollbar | ImGuiWindowFlags.NoScrollWithMouse)
    {
        SizeConstraints = new WindowSizeConstraints
        {
            MinimumSize = new Vector2(375, 200),
            MaximumSize = new Vector2(float.MaxValue, float.MaxValue)
        };
        this.plugin = plugin;
    }

    public void Dispose() { }

    public override void Draw()
    {
        var config = plugin.Configuration;
        ImGui.Text($"Enabled: {config.Enabled}");
        ImGui.Text($"Playing: {(plugin.IsPlaying ? plugin.PlayingId ?? "yes" : "no")}");
        ImGui.Spacing();
        if (ImGui.Button("Reload pack"))
            plugin.ReloadPack();
        ImGui.SameLine();
        if (ImGui.Button("Settings"))
            plugin.ToggleConfigUi();
    }
}
