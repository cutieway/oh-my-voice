using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Windowing;
using Dalamud.Utility;

namespace OhMyVoice.Windows;

public sealed class ConsentWindow : Window, IDisposable
{
    private const string ModUsageUrl =
        "https://eu.finalfantasyxiv.com/lodestone/topics/detail/1e4a8b0e8b84ea8dac61ae07af02e0c425de74aa";

    private readonly Plugin plugin;
    private bool agreed;

    public ConsentWindow(Plugin plugin)
        : base("Oh My Voice — Usage Agreement###omv-consent",
               ImGuiWindowFlags.NoResize | ImGuiWindowFlags.NoCollapse |
               ImGuiWindowFlags.NoScrollbar | ImGuiWindowFlags.NoScrollWithMouse |
               ImGuiWindowFlags.NoSavedSettings)
    {
        this.plugin = plugin;
        Size = new Vector2(640, 360);
        SizeCondition = ImGuiCond.Always;
        ShowCloseButton = false;
        RespectCloseHotkey = false;
        AllowPinning = false;
        IsTopMost = true;
    }

    public void Dispose() { }

    public void Present()
    {
        IsOpen = true;
        BringToFront();
    }

    public override void Draw()
    {
        ImGui.TextWrapped(
            "This plugin (and any voice packs) is a third-party tool and is against " +
            "Square Enix’s Terms of Service.");
        ImGui.Spacing();
        ImGui.TextWrapped(
            "I will only use it for personal, client-side purposes that do not affect " +
            "other players, and I will not discuss or display it in-game.");
        ImGui.Spacing();
        ImGui.TextWrapped("I take full personal responsibility for using it.");
        ImGui.Spacing();
        ImGui.Separator();
        ImGui.Spacing();

        ImGui.TextWrapped("Read Square Enix’s official statement:");
        if (ImGui.Button("Regarding Mod Usage and Culture"))
            Util.OpenLink(ModUsageUrl);

        ImGui.Spacing();
        ImGui.Separator();
        ImGui.Spacing();
        ImGui.Checkbox("I understand and agree", ref agreed);
        ImGui.Spacing();

        ImGui.BeginDisabled(!agreed);
        if (ImGui.Button("Continue"))
            plugin.AcceptTerms();
        ImGui.EndDisabled();

        ImGui.SameLine();
        ImGui.TextDisabled("The plugin remains disabled until you agree.");
    }
}
