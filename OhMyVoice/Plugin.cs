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

    private readonly GameInterop game;
    private readonly PackStore pack;
    private readonly Playback playback;
    private readonly MissLog misses;
    private readonly Dispatcher dispatcher;
    private readonly VoSuppression suppression;
    private readonly TalkCapture capture;

    public Plugin()
    {
        Configuration = PluginInterface.GetPluginConfig() as Configuration ?? new Configuration();

        RefreshGameVersion();

        game = new GameInterop(ClientState, Condition, Framework, ObjectTable, DataManager, Log, Configuration);
        game.RefreshPlayerName();
        ClientState.Login += OnLogin;
        ClientState.Logout += OnLogout;

        pack = new PackStore(PluginInterface, Log, Configuration);
        playback = new Playback(Log, Configuration);
        misses = new MissLog(PluginInterface, Log);
        suppression = new VoSuppression(GameInteropProvider, Log, Configuration, OnRealVo);
        dispatcher = new Dispatcher(Log, Configuration, game, pack, playback, misses, suppression);
        capture = new TalkCapture(Framework, GameGui, AddonLifecycle, Log, Configuration, game, dispatcher);

        pack.Load();
        suppression.Enable();
        capture.Enable();

        ConfigWindow = new ConfigWindow(this);
        MainWindow = new MainWindow(this);

        WindowSystem.AddWindow(ConfigWindow);
        WindowSystem.AddWindow(MainWindow);

        CommandManager.AddHandler(CommandName, new CommandInfo(OnCommand)
        {
            HelpMessage = "Open Oh My Voice status window"
        });

        PluginInterface.UiBuilder.Draw += WindowSystem.Draw;
        PluginInterface.UiBuilder.OpenConfigUi += ToggleConfigUi;
        PluginInterface.UiBuilder.OpenMainUi += ToggleMainUi;

        Log.Information($"OhMyVoice loaded. Pack={(pack.PackEnabled ? "on" : "off")} Sanitizer={Sanitizer.Pin}");
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
        pack.Load();
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
        CommandManager.RemoveHandler(CommandName);

        capture.Dispose();
        suppression.Dispose();
        playback.Dispose();
    }

    internal void ReloadPack() => pack.Load();
    internal bool IsPlaying => playback.IsPlaying;
    internal string? PlayingId => playback.PlayingId;

    private void OnCommand(string command, string args) => MainWindow.Toggle();
    public void ToggleConfigUi() => ConfigWindow.Toggle();
    public void ToggleMainUi() => MainWindow.Toggle();
}
