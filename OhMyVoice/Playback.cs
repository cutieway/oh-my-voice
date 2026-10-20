using Concentus;
using Concentus.Oggfile;
using Dalamud.Plugin.Services;
using NAudio.Wave;
using NAudio.Wave.SampleProviders;

namespace OhMyVoice;

/// <summary>
/// Playback (Q9): Opus .ogg decode via Concentus (pure managed, no native DLL),
/// NAudio WaveOut (winmm) output. MixingSampleProvider 48k stereo float.
/// Stop() is synchronous: second Talk line stops the first (Q13);
/// real VO starting stops the playing pack line (Q4b).
/// </summary>
public sealed class Playback : IDisposable
{
    private readonly IPluginLog log;
    private readonly Configuration config;

    private readonly object gate = new();
    private WaveOut? device;
    private RawSourceWaveStream? activePcm;
    private string? playingId;

    public event EventHandler<string>? LineStopped;

    public Playback(IPluginLog log, Configuration config)
    {
        this.log = log;
        this.config = config;
    }

    public bool IsPlaying
    {
        get { lock (gate) return device != null; }
    }

    public string? PlayingId
    {
        get { lock (gate) return playingId; }
    }

    /// <summary>Decode Ogg-Opus file to 48k mono float PCM. Port of XivVoices DecodeOggOpusToPCM.</summary>
    public static RawSourceWaveStream DecodeOggOpusToPcm(Stream stream)
    {
        IOpusDecoder decoder = OpusCodecFactory.CreateDecoder(48000, 1);
        OpusOggReadStream oggStream = new(decoder, stream);
        List<float> pcmSamples = [];
        while (oggStream.HasNextPacket)
        {
            short[]? packet = oggStream.DecodeNextPacket();
            if (packet == null) continue;
            foreach (short sample in packet)
                pcmSamples.Add(sample / 32768f);
        }
        var waveFormat = WaveFormat.CreateIeeeFloatWaveFormat(48000, 1);
        MemoryStream memory = new(pcmSamples.Count * 4);
        using (BinaryWriter writer = new(memory, Encoding.UTF8, leaveOpen: true))
            foreach (float sample in pcmSamples)
                writer.Write(sample);
        memory.Position = 0;
        return new RawSourceWaveStream(memory, waveFormat);
    }

    public void Play(string id, string oggPath)
    {
        StopInternal(locked: false);
        RawSourceWaveStream? pcm = null;
        try
        {
            using FileStream fs = File.OpenRead(oggPath);
            pcm = DecodeOggOpusToPcm(fs);

            var mixer = new MixingSampleProvider(WaveFormat.CreateIeeeFloatWaveFormat(48000, 2));
            var mono = new WdlResamplingSampleProvider(pcm.ToSampleProvider(), 48000);
            var stereo = new MonoToStereoSampleProvider(mono);
            float volume = Math.Clamp(config.Volume / 100f, 0.01f, 2f);
            var vol = new VolumeSampleProvider(stereo) { Volume = volume };
            mixer.AddMixerInput(vol);

            var player = new WaveOut { BufferMilliseconds = 100, NumberOfBuffers = 2 };
            player.PlaybackStopped += (_, _) => OnDeviceStopped(id);
            player.Init(mixer);
            lock (gate)
            {
                device = player;
                activePcm = pcm;
                pcm = null; // ownership moved to field; disposed on stop
                playingId = id;
            }
            player.Play();
            if (config.DebugLogging) log.Debug($"Playing {id}");
        }
        catch (Exception ex)
        {
            pcm?.Dispose();
            log.Warning($"Playback failed for {id}: {ex.Message}");
            StopInternal(locked: false);
        }
    }

    /// <summary>Called on the NAudio callback thread when a line ends naturally.</summary>
    private void OnDeviceStopped(string id)
    {
        bool notify = false;
        lock (gate)
        {
            if (playingId == id)
            {
                notify = true;
                device?.Dispose();
                device = null;
                activePcm?.Dispose();
                activePcm = null;
                playingId = null;
            }
        }
        if (notify) LineStopped?.Invoke(this, id);
    }

    public void Stop()
    {
        StopInternal(locked: false);
    }

    private void StopInternal(bool locked)
    {
        WaveOut? old = null;
        RawSourceWaveStream? oldPcm = null;
        string? oldId = null;
        if (locked)
        {
            old = device; oldPcm = activePcm; oldId = playingId;
            device = null; activePcm = null; playingId = null;
        }
        else
        {
            lock (gate)
            {
                old = device; oldPcm = activePcm; oldId = playingId;
                device = null; activePcm = null; playingId = null;
            }
        }
        if (old != null)
        {
            try { old.Stop(); } catch { }
            try { old.Dispose(); } catch { }
            try { oldPcm?.Dispose(); } catch { }
            if (oldId != null)
            {
                if (config.DebugLogging) log.Debug($"Stopped {oldId}");
                LineStopped?.Invoke(this, oldId);
            }
        }
        else
        {
            try { oldPcm?.Dispose(); } catch { }
        }
    }

    public void Dispose()
    {
        StopInternal(locked: false);
    }
}
