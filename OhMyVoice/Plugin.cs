using Dalamud.Game.ClientState.Conditions;
using Dalamud.Game.Command;
using Dalamud.Interface.Windowing;
using Dalamud.IoC;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;
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
        Condition.ConditionChange += OnConditionChange;

        pack = new PackStore(Log, Configuration);
        playback = new Playback(Log, Configuration);
        events = new VoiceLog(PluginInterface, Log);
        npcMetadata = new NpcMetadataService(DataManager, Log);
        npcMetadata.WarmIndex();
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
            $"PackDirectory={(pack.PackDirectory.Length > 0 ? pack.PackDirectory : "(none)")} " +
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
            _ = ReloadPackAsync();
    }

    private void EnableRuntime()
    {
        if (runtimeEnabled) return;
        runtimeEnabled = true;
        _ = ReloadPackAsync();
        suppression.Enable();
        capture.Enable();
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
        dispatcher.ClearQueue();
        playback.Stop();
    }

    private void OnRealVo() => dispatcher.OnRealVoStarted();

    /// <summary>
    /// A cutscene ending - finished or skipped from the "Skip cutscene?" prompt -
    /// stops the pack line and drops the queue: the player wants out, so nothing
    /// from that scene keeps talking. Stopping is immediate: the cutscene flags
    /// are checked again on the spot, so a transition that only clears one of
    /// them leaves playback alone.
    /// </summary>
    private void OnConditionChange(ConditionFlag flag, bool value)
    {
        if (value) return;
        if (flag is not (ConditionFlag.OccupiedInCutSceneEvent
            or ConditionFlag.WatchingCutscene
            or ConditionFlag.WatchingCutscene78)) return;
        if (game.IsInCutscene()) return; // another cutscene flag is still set

        int queued = dispatcher.QueuedCount;
        bool playing = playback.IsPlaying;
        dispatcher.ClearQueue();
        playback.Stop();
        if (Configuration.DebugLogging && (queued > 0 || playing))
        {
            Log.Information(
                $"Cutscene ended: cleared {queued} queued line(s), " +
                $"playback {(playing ? "stopped" : "idle")}");
        }
    }

    public void Dispose()
    {
        ClientState.Login -= OnLogin;
        ClientState.Logout -= OnLogout;
        Condition.ConditionChange -= OnConditionChange;

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

    /// <summary>
    /// Reloads the pack catalog off the UI thread. Faults are logged here so
    /// fire-and-forget callers cannot swallow them.
    /// </summary>
    internal Task ReloadPackAsync()
    {
        if (!runtimeEnabled) return Task.CompletedTask;

        Task task = pack.LoadAsync();
        _ = task.ContinueWith(
            static (faulted, state) => ((IPluginLog)state!).Error(faulted.Exception!, "Pack reload failed."),
            Log,
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);
        return task;
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
