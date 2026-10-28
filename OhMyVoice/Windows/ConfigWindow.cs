using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Windowing;
using Dalamud.Utility;

namespace OhMyVoice.Windows;

public class ConfigWindow : Window, IDisposable
{
    // TODO: Replace with the public voice-pack distribution URL when available.
    private const string VoicePackDownloadUrl = "";

    private readonly Plugin plugin;
    private readonly Configuration configuration;
    private string packDirectoryInput;
    private string packDirectoryMessage = string.Empty;
    private bool packDirectoryError;

    public ConfigWindow(Plugin plugin)
        : base("Oh My Voice Settings###omv-config")
    {
        Flags = ImGuiWindowFlags.NoCollapse;
        Size = new Vector2(520, 540);
        SizeCondition = ImGuiCond.Always;
        this.plugin = plugin;
        configuration = plugin.Configuration;
        packDirectoryInput = configuration.PackDirectory;
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

        ImGui.Spacing();
        var pronounChoice = (int)configuration.PlayerPronouns;
        string[] pronounLabels =
        [
            "Auto (character sex)", "They/them", "She/her", "He/him",
        ];
        if (ImGui.Combo("Player pronouns", ref pronounChoice,
                        pronounLabels, pronounLabels.Length))
        {
            configuration.PlayerPronouns = (PronounChoice)pronounChoice;
            configuration.Save();
        }
        ImGui.TextDisabled("Auto follows character sex; manual overrides it.");

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
        ImGui.Separator();
        ImGui.Spacing();
        ImGui.Text("Voice pack directory");
        ImGui.TextWrapped(
            "Enter an existing pack root containing one folder per voice. " +
            "Each voice folder must contain manifest.json and voicelines/.");
        ImGui.SetNextItemWidth(-1);
        ImGui.InputText("##omv-pack-directory", ref packDirectoryInput, 4096);
        if (ImGui.Button("Confirm directory"))
            ConfirmPackDirectory();

        if (packDirectoryMessage.Length > 0)
        {
            var color = packDirectoryError
                ? new Vector4(0.95f, 0.35f, 0.35f, 1f)
                : new Vector4(0.95f, 0.75f, 0.3f, 1f);
            ImGui.TextColored(color, packDirectoryMessage);
        }

        if (plugin.IsPackLoaded)
        {
            ImGui.TextDisabled(
                $"Loaded {plugin.LoadedVoiceCount} voices, " +
                $"{plugin.LoadedVoicelineCount} lines from " +
                $"{plugin.LoadedPackCount} manifest(s).");
            if (plugin.InvalidManifestCount > 0)
                ImGui.TextDisabled(
                    $"Skipped {plugin.InvalidManifestCount} invalid manifest(s); see /xllog.");
        }
        else if (configuration.PackDirectory.Length > 0)
        {
            ImGui.TextDisabled("No valid voice packs loaded.");
        }

        if (VoicePackDownloadUrl.Length == 0)
        {
            ImGui.BeginDisabled();
            ImGui.Button("Download voice packs (coming soon)");
            ImGui.EndDisabled();
            ImGui.TextDisabled("Voice-pack distribution link is not available yet.");
        }
        else if (ImGui.Button("Download voice packs"))
        {
            Util.OpenLink(VoicePackDownloadUrl);
        }

        ImGui.Spacing();
        ImGui.TextDisabled($"Sanitizer: {Sanitizer.Pin}");
    }

    private void ConfirmPackDirectory()
    {
        string candidate = packDirectoryInput.Trim().Trim('"');
        if (candidate.Length == 0)
        {
            packDirectoryError = true;
            packDirectoryMessage = "Enter an existing directory.";
            return;
        }

        string fullPath;
        try
        {
            fullPath = Path.GetFullPath(candidate);
        }
        catch (Exception ex)
        {
            packDirectoryError = true;
            packDirectoryMessage = $"Invalid directory: {ex.Message}";
            return;
        }

        if (!Directory.Exists(fullPath))
        {
            packDirectoryError = true;
            packDirectoryMessage = "That directory does not exist.";
            return;
        }

        configuration.PackDirectory = fullPath;
        configuration.Save();
        packDirectoryInput = fullPath;
        plugin.ReloadPack();

        packDirectoryError = false;
        if (plugin.IsPackLoaded)
        {
            packDirectoryMessage =
                $"Directory saved. Loaded {plugin.LoadedVoiceCount} voices, " +
                $"{plugin.LoadedVoicelineCount} lines from " +
                $"{plugin.LoadedPackCount} manifest(s).";
            if (plugin.InvalidManifestCount > 0)
                packDirectoryMessage += $" Skipped {plugin.InvalidManifestCount} invalid manifest(s).";
        }
        else
        {
            packDirectoryMessage =
                "Directory saved, but no valid voice folders were found. Select the " +
                "parent folder that directly contains folders such as thancred/ " +
                "and urianger/. Pack remains disabled.";
        }
    }
}
