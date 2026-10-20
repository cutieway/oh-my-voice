using System.Text.Json.Serialization;

namespace OhMyVoice.Common;

/// <summary>
/// Pack v1 manifest contract (Q8/Q11).
/// Encoder is pinned per pack generation (libopus, 32 kbps, constrained VBR,
/// 48 kHz mono, speech mode) but is NEVER a reader-side rejection rule:
/// a 48 kbps re-encode is a new pack generation, not a manifest violation.
/// Duration is derived from the encoded Ogg granule position, not the source WAV.
/// </summary>
public sealed class EncoderSettings
{
    public string Codec { get; set; } = "opus";
    public string Container { get; set; } = "ogg";
    public string Encoder { get; set; } = "libopus";
    public int BitrateKbps { get; set; } = 32;
    public int SampleRateHz { get; set; } = 48000;
    public int Channels { get; set; } = 1;
    public string Vbr { get; set; } = "constrained";
    public string Application { get; set; } = "voip";
}

public sealed class VoiceDef
{
    public bool IsGeneric { get; set; }
}

public sealed class NpcDef
{
    public string NpcId { get; set; } = "Unknown";
    public string VoiceId { get; set; } = "Unknown";
}

public sealed class VoicelineEntry
{
    public string Lang { get; set; } = "en";
    public string VoiceId { get; set; } = "Unknown";
    public string NpcId { get; set; } = "Unknown";
    public string Sentence { get; set; } = "";
    public long Bytes { get; set; }
    public double DurationSeconds { get; set; }
    public string Codec { get; set; } = "opus";
}

public sealed class PackManifestJson
{
    public int Schema { get; set; } = 1;
    public string Language { get; set; } = "en";

    /// <summary>Sanitizer pin, e.g. "v1:&lt;sha256(golden-vectors)&gt;".</summary>
    public string Sanitizer { get; set; } = "";

    /// <summary>Generator backend that produced this pack, e.g. "indextts2".</summary>
    public string Backend { get; set; } = "";

    public EncoderSettings Encoder { get; set; } = new();
    public Dictionary<string, VoiceDef> Voices { get; set; } = new();
    public Dictionary<string, NpcDef> Npcs { get; set; } = new();
    public List<string> IgnoredSpeakers { get; set; } = [];
    public Dictionary<string, VoicelineEntry> Voicelines { get; set; } = new();
}

/// <summary>
/// One silent-miss worklist row (Q3/Q7). Residue entries record source addon
/// and game context so the miner can replay them without reverse lookup.
/// </summary>
public sealed class MissEntry
{
    public string Lang { get; set; } = "en";
    public string VoiceId { get; set; } = "Unknown";
    public string NpcId { get; set; } = "Unknown";
    /// <summary>Cleaned speaker at capture time. NpcId becomes the mapped id
    /// once a manifest is loaded, so the cleaned name is kept separately.</summary>
    public string Speaker { get; set; } = "";
    public string Sentence { get; set; } = "";
    public string RawSpeaker { get; set; } = "";
    public string RawSentence { get; set; } = "";
    public string Source { get; set; } = "";
    public string Zone { get; set; } = "";
    public string GameVersion { get; set; } = "unknown";
    public DateTimeOffset Utc { get; set; }

    [JsonIgnore]
    public string Id => Common.VoiceId.ComputeId(Lang, VoiceId, NpcId, Sentence);
}
