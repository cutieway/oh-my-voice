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
/// NPC metadata resolved from the running game, not from the pack. Kept
/// separate from NpcId (the manifest lookup key) because one speaker_token
/// keeps the same voice/race/sex across expansions while expansion and zone
/// vary per line.
/// </summary>
public sealed class NpcMetadata
{
    public string Name { get; set; } = "";
    public uint? NpcId { get; set; }
    /// <summary>id = numeric row key, name = normalized name, none = unresolved.</summary>
    public string Match { get; set; } = "none";
    public int Candidates { get; set; }
    public bool Ambiguous { get; set; }
    public uint RaceId { get; set; }
    public string Race { get; set; } = "unknown";
    public byte? Gender { get; set; }
    public string Sex { get; set; } = "unknown";
    public uint? ExpansionId { get; set; }
    public string Expansion { get; set; } = "unknown";
}

/// <summary>
/// One voice-event row: cache hit or miss. Misses are the generation
/// worklist; hits are evidence that a line is already covered. Metadata is
/// developer-facing and never triggers generation.
/// </summary>
public sealed class VoiceEvent
{
    /// <summary>"hit" or "miss".</summary>
    public string Result { get; set; } = "miss";
    public string Id { get; set; } = "";
    public string Lang { get; set; } = "en";
    /// <summary>Pack voice id, i.e. the speaker_token (Unknown on an unmapped miss).</summary>
    public string VoiceId { get; set; } = "Unknown";
    /// <summary>Manifest lookup key; not necessarily a numeric NPC row id.</summary>
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
    public NpcMetadata Npc { get; set; } = new();
    public DateTimeOffset Utc { get; set; }

    [JsonIgnore]
    public string ComputedId => Common.VoiceId.ComputeId(Lang, VoiceId, NpcId, Sentence);
}
