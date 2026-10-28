using Dalamud.Game.Command;
using Dalamud.Interface.Windowing;
using Dalamud.IoC;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;
using FFXIVClientStructs.FFXIV.Client.System.Framework;
using OhMyVoice.Windows;

namespace OhMyVoice;

public sealed class Plugin : IDalamudPlugin
{
    [PluginService] internal static IDalamudPluginInterface PluginInterface { get; private set; } = null!;
    [PluginService] internal static ICommandManager CommandManager { get; private set; } = null!;
    [PluginService] internal static IClientState ClientState { get; private set; } = null!;
    [PluginService] internal static IDataManager DataManager { get; private set; } = null!;
    [PluginService] internal static IFramework Framework { get; private set; } = null!;
    [PluginService] internal static ICondition Condition { get; private set; } = null!;
    [PluginService] internal static IGameGui GameGui { get; private set; } = null!;
    [PluginService] internal static IObjectTable ObjectTable { get; private set; } = null!;
    [PluginService] internal static IPlayerState PlayerState { get; private set; } = null!;
    [PluginService] internal static IAddonLifecycle AddonLifecycle { get; private set; } = null!;
    [PluginService] internal static IGameInteropProvider GameInteropProvider { get; private set; } = null!;
    [PluginService] internal static IPluginLog Log { get; private set; } = null!;

    private const string CommandName = "/omv";

    /// <summary>Game build string for worklist rows (Q7). "unknown" when unreadable.</summary>
    public static string GameVersion { get; private set; } = "unknown";

    public Configuration Configuration { get; init; }
    public readonly WindowSystem WindowSystem = new("OhMyVoice");
    private ConfigWindow ConfigWindow { get; init; }
    private MainWindow MainWindow { get; init; }
    private ConsentWindow ConsentWindow { get; init; }

    private readonly GameInterop game;
    private readonly PackStore pack;
    private readonly Playback playback;
    private readonly VoiceLog events;
    private readonly NpcMetadataService npcMetadata;
    private readonly Dispatcher dispatcher;
    private readonly VoSuppression suppression;
    private readonly TalkCapture capture;
    private bool runtimeEnabled;

    public Plugin()
    {
        Configuration = PluginInterface.GetPluginConfig() as Configuration ?? new Configuration();

        RefreshGameVersion();

        game = new GameInterop(ClientState, Condition, Framework, ObjectTable, DataManager, PlayerState, Log, Configuration);
        game.RefreshPlayerName();
        ClientState.Login += OnLogin;
        ClientState.Logout += OnLogout;

        pack = new PackStore(Log, Configuration);
        playback = new Playback(Log, Configuration);
        events = new VoiceLog(PluginInterface, Log);
        npcMetadata = new NpcMetadataService(DataManager, Log);
        suppression = new VoSuppression(GameInteropProvider, Log, Configuration, OnRealVo);
        dispatcher = new Dispatcher(Log, Configuration, game, pack, playback, events, npcMetadata, suppression);
        capture = new TalkCapture(Framework, GameGui, AddonLifecycle, Log, Configuration, game, dispatcher);

        ConfigWindow = new ConfigWindow(this);
        MainWindow = new MainWindow(this);
        ConsentWindow = new ConsentWindow(this);

        WindowSystem.AddWindow(ConfigWindow);
        WindowSystem.AddWindow(MainWindow);
        WindowSystem.AddWindow(ConsentWindow);

        if (Configuration.TermsAccepted)
            EnableRuntime();
        else
            ConsentWindow.Present();

        CommandManager.AddHandler(CommandName, new CommandInfo(OnCommand)
        {
            HelpMessage = "Open Oh My Voice status window"
        });

        PluginInterface.UiBuilder.Draw += WindowSystem.Draw;
        PluginInterface.UiBuilder.OpenConfigUi += ToggleConfigUi;
        PluginInterface.UiBuilder.OpenMainUi += ToggleMainUi;

        Log.Information(
            $"OhMyVoice loaded. Terms={(Configuration.TermsAccepted ? "accepted" : "pending")} " +
            $"Pack={(pack.PackEnabled ? $"on ({pack.VoiceCount} voices, {pack.VoicelineCount} lines)" : "off")} " +
            $"Sanitizer={Sanitizer.Pin} Events={events.Path}");
    }

    private static unsafe void RefreshGameVersion()
    {
        try
        {
            var fw = FFXIVClientStructs.FFXIV.Client.System.Framework.Framework.Instance();
            if (fw != null) GameVersion = fw->GameVersionString;
        }
        catch { GameVersion = "unknown"; }
    }

    private void OnLogin()
    {
        RefreshGameVersion();
        game.RefreshPlayerName();
        if (Configuration.TermsAccepted)
            pack.Load();
    }

    private void EnableRuntime()
    {
        if (runtimeEnabled) return;
        pack.Load();
        suppression.Enable();
        capture.Enable();
        runtimeEnabled = true;
    }

    internal void AcceptTerms()
    {
        if (!Configuration.TermsAccepted)
        {
            Configuration.TermsAccepted = true;
            Configuration.Save();
        }

        ConsentWindow.IsOpen = false;
        EnableRuntime();
        ConfigWindow.IsOpen = true;
        ConfigWindow.BringToFront();
    }

    private void OnLogout(int type, int code)
    {
        playback.Stop();
    }

    private void OnRealVo() => dispatcher.OnRealVoStarted();

    public void Dispose()
    {
        ClientState.Login -= OnLogin;
        ClientState.Logout -= OnLogout;

        PluginInterface.UiBuilder.Draw -= WindowSystem.Draw;
        PluginInterface.UiBuilder.OpenConfigUi -= ToggleConfigUi;
        PluginInterface.UiBuilder.OpenMainUi -= ToggleMainUi;

        WindowSystem.RemoveAllWindows();
        ConfigWindow.Dispose();
        MainWindow.Dispose();
        ConsentWindow.Dispose();
        CommandManager.RemoveHandler(CommandName);

        capture.Dispose();
        suppression.Dispose();
        playback.Dispose();
    }

    internal void ReloadPack()
    {
        if (runtimeEnabled) pack.Load();
    }

    internal bool IsPlaying => playback.IsPlaying;
    internal string? PlayingId => playback.PlayingId;
    internal bool IsPackLoaded => pack.PackEnabled;
    internal int LoadedPackCount => pack.PackCount;
    internal int LoadedVoiceCount => pack.VoiceCount;
    internal int LoadedVoicelineCount => pack.VoicelineCount;
    internal int InvalidManifestCount => pack.InvalidManifestCount;

    private void OnCommand(string command, string args) => ToggleMainUi();

    public void ToggleConfigUi()
    {
        if (!Configuration.TermsAccepted)
        {
            ConsentWindow.Present();
            return;
        }
        ConfigWindow.Toggle();
    }

    public void ToggleMainUi()
    {
        if (!Configuration.TermsAccepted)
        {
            ConsentWindow.Present();
            return;
        }
        MainWindow.Toggle();
    }
}
