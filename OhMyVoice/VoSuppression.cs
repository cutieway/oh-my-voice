using Dalamud.Game;
using Dalamud.Hooking;
using Dalamud.Plugin.Services;
using FFXIVClientStructs.FFXIV.Client.Sound;
using InteropGenerator.Runtime;

namespace OhMyVoice;

/// <summary>
/// Real-VO suppression (Q4). Data-driven blocklist, never mutes game audio.
/// Two behaviors:
/// (a) wait ~100ms before starting a pack line to see if real VO begins;
/// (b) stop a pack line that already started if real VO begins.
/// This class only DETECTS; the dispatcher owns wait/stop policy.
/// </summary>
public sealed class VoSuppression : IDisposable
{
    private readonly IGameInteropProvider interop;
    private readonly IPluginLog log;
    private readonly Configuration config;
    private readonly Action onRealVo;

    private Hook<SoundManager.Delegates.PlaySound>? playSoundHook;
    private Hook<SoundManager.Delegates.PlayCutsceneVoSound>? playVoHook;

    /// <summary>Patterns matched against lowercase "path/soundNumber".</summary>
    private static readonly string[] VoicePathPrefixes = ["sound/voice/"];
    private static readonly string[] VoicePathContains = ["vo_man", "vo_voiceman", "sound/voicem/voiceman"];

    public DateTime LastRealVoUtc { get; private set; } = DateTime.MinValue;
    public string LastRealVoPath { get; private set; } = "";

    public VoSuppression(IGameInteropProvider interop, IPluginLog log, Configuration config, Action onRealVo)
    {
        this.interop = interop;
        this.log = log;
        this.config = config;
        this.onRealVo = onRealVo;
    }

    public unsafe void Enable()
    {
        try
        {
            playSoundHook = interop.HookFromAddress<SoundManager.Delegates.PlaySound>(
                SoundManager.Addresses.PlaySound.Value, PlaySoundDetour);
            playSoundHook.Enable();
            playVoHook = interop.HookFromAddress<SoundManager.Delegates.PlayCutsceneVoSound>(
                SoundManager.Addresses.PlayCutsceneVoSound.Value, PlayVoDetour);
            playVoHook.Enable();
        }
        catch (Exception ex)
        {
            log.Error($"VoSuppression hook failed: {ex.Message}");
        }
    }

    public void Dispose()
    {
        playSoundHook?.Dispose();
        playVoHook?.Dispose();
    }

    public static bool IsVoicePath(string lowerPath)
    {
        if (!lowerPath.EndsWith("/0", StringComparison.Ordinal)) return false;
        foreach (var prefix in VoicePathPrefixes)
            if (lowerPath.StartsWith(prefix, StringComparison.Ordinal)) return true;
        if (lowerPath.StartsWith("cut/", StringComparison.Ordinal))
            foreach (var infix in VoicePathContains)
                if (lowerPath.Contains(infix, StringComparison.Ordinal)) return true;
        return false;
    }

    private unsafe SoundData* PlaySoundDetour(
        SoundManager* thisPtr, CStringPointer path, float volume, uint fadeInDuration,
        float posX, float posY, float posZ, float speed, int a9, uint soundNumber,
        bool autoRelease, SoundVolumeCategory volumeCategory, bool a13, int midiNote,
        bool a15, bool defaultFadeOut, bool isPositional, bool a18)
    {
        SoundData* data;
        try
        {
            data = playSoundHook!.Original(thisPtr, path, volume, fadeInDuration, posX, posY, posZ,
                speed, a9, soundNumber, autoRelease, volumeCategory, a13, midiNote, a15,
                defaultFadeOut, isPositional, a18);
        }
        catch { return null; }
        if (data == null) return data;
        CheckPath(path.ToString(), data->SoundNumber);
        return data;
    }

    private unsafe SoundData* PlayVoDetour(SoundManager* thisPtr, CStringPointer path)
    {
        SoundData* data;
        try { data = playVoHook!.Original(thisPtr, path); }
        catch { return null; }
        if (data == null) return data;
        CheckPath(path.ToString(), data->SoundNumber);
        return data;
    }

    private void CheckPath(string path, uint soundNumber)
    {
        string lower = $"{path.ToLowerInvariant()}/{soundNumber}";
        if (!IsVoicePath(lower)) return;
        LastRealVoUtc = DateTime.UtcNow;
        LastRealVoPath = lower;
        if (config.DebugLogging) log.Debug($"Real VO detected: {lower}");
        try { onRealVo(); } catch { }
    }
}
