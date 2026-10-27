using System.Security.Cryptography;
using System.Text;

namespace OhMyVoice.Common;

/// <summary>
/// Voice-line content addressing: id = md5hex("lang:voiceId:npcId:sentence")
/// over SANITIZED text. Lang is mandatory: identical short lines ("...",
/// "No.") are byte-identical across languages and would otherwise collide
/// in the worklist and any future shared pack namespace.
/// </summary>
public static class VoiceId
{
    public static string ComputeId(string lang, string voiceId, string npcId, string sentence)
    {
        string combined = string.Join(":", lang, voiceId, npcId, sentence);
        byte[] inputBytes = Encoding.UTF8.GetBytes(combined);
        byte[] hashBytes = MD5.HashData(inputBytes);
        StringBuilder sb = new(hashBytes.Length * 2);
        foreach (byte b in hashBytes)
            sb.Append(b.ToString("x2", System.Globalization.CultureInfo.InvariantCulture));
        return sb.ToString();
    }
}
