using Dalamud.Game.ClientState.Conditions;
using Dalamud.Plugin.Services;
using FFXIVClientStructs.FFXIV.Client.System.String;
using FFXIVClientStructs.FFXIV.Component.GUI;

namespace OhMyVoice;

/// <summary>
/// Thin game-state + text readers. No pack, no audio, no decisions here.
/// </summary>
public sealed class GameInterop
{
    private readonly IClientState clientState;
    private readonly ICondition condition;
    private readonly IFramework framework;
    private readonly IObjectTable objectTable;
    private readonly IDataManager dataManager;
    private readonly IPluginLog log;
    private readonly Configuration config;

    public GameInterop(
        IClientState clientState,
        ICondition condition,
        IFramework framework,
        IObjectTable objectTable,
        IDataManager dataManager,
        IPluginLog log,
        Configuration config)
    {
        this.clientState = clientState;
        this.condition = condition;
        this.framework = framework;
        this.objectTable = objectTable;
        this.dataManager = dataManager;
        this.log = log;
        this.config = config;
    }

    public string? PlayerFirstName { get; private set; }
    public string? PlayerLastName { get; private set; }

    public void RefreshPlayerName()
    {
        PlayerFirstName = null;
        PlayerLastName = null;
        try
        {
            var name = objectTable.LocalPlayer?.Name.TextValue;
            if (string.IsNullOrEmpty(name)) return;
            string[] parts = name.Split(' ');
            if (parts.Length >= 2)
            {
                PlayerFirstName = parts[0];
                PlayerLastName = parts[1];
            }
        }
        catch (Exception ex)
        {
            if (config.DebugLogging) log.Debug($"RefreshPlayerName failed: {ex.Message}");
        }
    }

    public bool IsInCutscene() =>
        condition.Any(ConditionFlag.OccupiedInCutSceneEvent, ConditionFlag.WatchingCutscene, ConditionFlag.WatchingCutscene78);

    public bool IsInDuty() =>
        condition.Any(ConditionFlag.BoundByDuty);

    public Task RunOnFramework(Action action) => framework.Run(action);

    public uint TerritoryTypeId => clientState.TerritoryType;

    public string GetZone()
    {
        try
        {
            uint territory = clientState.TerritoryType;
            var sheet = dataManager.GetExcelSheet<Lumina.Excel.Sheets.TerritoryType>();
            if (sheet.TryGetRow(territory, out var row))
                return row.PlaceName.Value.Name.ToString();
            return $"Unknown:{territory}";
        }
        catch
        {
            return "unknown";
        }
    }

    public unsafe string ReadUtf8(Utf8String str)
    {
        try
        {
            string text = new Lumina.Text.ReadOnly.ReadOnlySeString(str).ToString();
            return System.Text.RegularExpressions.Regex.Replace(text, @"\s+", " ").Trim();
        }
        catch
        {
            return "";
        }
    }

    public unsafe string ReadTextNode(AtkTextNode* textNode)
    {
        if (textNode == null) return "";
        return ReadUtf8(textNode->NodeText);
    }
}
