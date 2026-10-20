using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace OhMyVoice.Common;

/// <summary>
/// Sanitizer v1 — the single shared CleanMessage implementation used by both
/// the runtime plugin and the offline sheet miner (Q7/Q12).
///
/// NEVER change behavior under v1: the voice-line ID is md5(lang:voiceId:npcId:sentence)
/// over SANITIZED text, so a one-byte divergence orphans the whole pack.
/// Behavior changes require a new version (v2) plus a re-generated pack.
///
/// The pin is v1:&lt;sha256 over canonical golden vectors&gt; — it survives
/// formatting/comments and changes exactly when vectors (behavior) change.
/// The plugin verifies its pin against manifest.sanitizer on pack load:
/// mismatch means warn and disable the pack, never silently miss every line.
/// </summary>
public static partial class Sanitizer
{
    public const string Version = "v1";

    public static string Pin => $"{Version}:{GoldenVectorsHash()}";

    public static (string Speaker, string Sentence) Clean(string speaker, string sentence, string firstName, string lastName)
    {
        // --- Speaker sanitization ---
        if (speaker != "???")
            speaker = speaker.Replace("!", "").Replace("?", "");

        foreach (string suffix in new[] { "'s Voice", "'s Avatar" })
        {
            if (speaker.EndsWith(suffix, StringComparison.Ordinal))
            {
                speaker = speaker[..^suffix.Length];
                break;
            }
        }

        // --- Generic sentence sanitization ---
        // Remove text in and including angled brackets, e.g. <sigh> <sniffle>
        sentence = Regex.Replace(sentence, "<[^<]*>", "");

        // Convert Roman numerals (VI..XXV, multi-char only)
        sentence = ConvertRomanNumerals(sentence);

        // ASCII equivalents / whitespace
        sentence = Regex.Replace(sentence, @"\s+", " ");
        sentence = sentence
            .Replace("─", " - ")
            .Replace("—", " - ")
            .Replace("–", "-")
            .Replace("\n", " ");

        sentence = Regex.Replace(sentence, @"\b" + Regex.Escape(firstName) + @"\b", "_FIRSTNAME_");
        sentence = Regex.Replace(sentence, @"\b" + Regex.Escape(lastName) + @"\b", "_LASTNAME_");

        sentence = sentence
            .Replace("\\u00e1", "á").Replace("\\u00e9", "é").Replace("\\u00ed", "í")
            .Replace("\\u00f3", "ó").Replace("\\u00fa", "ú").Replace("\\u00f1", "ñ")
            .Replace("\\u00e0", "à").Replace("\\u00e8", "è").Replace("\\u00ec", "ì")
            .Replace("\\u00f2", "ò").Replace("\\u00f9", "ù");

        // "hi...there" -> "hi... there"
        sentence = Regex.Replace(sentence, @"(\.{3})(\w)", "$1 $2");

        // Normalize quotes: straight -> alternating curly
        sentence = Regex.Replace(sentence, "[“”]", "\"");
        bool isOpeningQuote = true;
        sentence = Regex.Replace(sentence, "\"", _ =>
        {
            if (isOpeningQuote) { isOpeningQuote = false; return "“"; }
            isOpeningQuote = true;
            return "”";
        });

        // --- Specific sentence sanitization ---
        sentence = Regex.Replace(sentence, @"Come one, come all - drawing number \d{4}",
            "Come one, come all - drawing number");

        if (speaker == "Cactpot Cashier" && sentence.StartsWith("Congratulations! You have won", StringComparison.Ordinal))
            sentence = "Congratulations! You have won!";

        sentence = Regex.Replace(sentence, @"Your postal prowess has earned you carrier level \d{2}",
            "Your postal prowess has earned you this carrier level");

        sentence = Regex.Replace(sentence, @"^Congratulations.*eligible to participate in sanctioned chocobo races\.*",
            "Congratulations! Your chocobo is now eligible to participate in sanctioned chocobo races.");

        sentence = Regex.Replace(sentence, @"^What sort of training did you have in mind for .*, (madam|sir)\?$",
            "What sort of training did you have in mind for your chocobo?");

        sentence = Regex.Replace(sentence, @"^You wish to teach .*, (madam|sir)\? Then, if you would be so kind as to provide the requisite manual\.$",
            "You wish to teach your chocobo an ability? Then, if you would be so kind as to provide the requisite manual.");

        sentence = Regex.Replace(sentence, @"^You wish for .+ to unlearn an ability\? Very well, if you would be so kind as to specify the undesired ability\.\.\.$",
            "You wish for your chocobo to unlearn an ability? Very well, if you would be so kind as to specify the undesired ability...");

        if (speaker == "Feo Ul")
        {
            if (sentence.StartsWith("A whispered word, and off", StringComparison.Ordinal))
                sentence = "A whispered word, and off goes yours on a grand adventure! What wonders await at journey's end?";
            else if (sentence.StartsWith("Carried by the wind, the leaf flutters to the ground", StringComparison.Ordinal))
                sentence = "Carried by the wind, the leaf flutters to the ground - and so does yours return to your side. Was the journey a fruitful one?";
            else if (sentence.StartsWith("From verdant green to glittering gold", StringComparison.Ordinal))
                sentence = "From verdant green to glittering gold, so does the leaf take on delightful hues with each new season. If you would see yours dressed in new colors, your beautiful branch will attend to the task.";
            else if (sentence.StartsWith("Oh, my adorable sapling! You have need", StringComparison.Ordinal))
                sentence = "Oh, my adorable sapling! You have need of yours, yes? But sing the word, and let your beautiful branch do what only they can.";
            else if (sentence.StartsWith("Very well. I shall slip quietly from", StringComparison.Ordinal))
                sentence = "Very well. I shall slip quietly from your servant's dreams. May your leaf flutter, float, and find a way back to you.";
            else if (sentence.StartsWith("You have no more need of", StringComparison.Ordinal))
                sentence = "You have no more need of yours? So be it! I shall steal quietly from your loyal servant's dreams.";
        }

        sentence = Regex.Replace(sentence, @"And the winning number for draw \d+ is... \d+!",
            "And here is the winning number!");
        sentence = Regex.Replace(sentence, @"And the Early Bird Bonus grants everyone an extra \d+%! Make sure you lucky folk claim your winnings promptly!",
            "And the Early Bird Bonus grants everyone an extra! Make sure you lucky folk claim your winnings promptly!");

        sentence = Regex.Replace(sentence, @"Welcome to drawing number \d+ of the Jumbo Cactpot! Can I interest you in a ticket to fame and fortune?",
            "Welcome to drawing number of the Jumbo Cactpot! Can I interest you in a ticket to fame and fortune?");

        sentence = Regex.Replace(sentence, @"Tickets for drawing number \d+ of the Mini Cactpot are now on sale. To test your fortunes, make your way to Entrance Square!",
            "Tickets for this drawing number of the Mini Cactpot are now on sale. To test your fortunes, make your way to Entrance Square!");
        sentence = Regex.Replace(sentence, @"Entries are now being accepted for drawing number \d+ of the Mini Cactpot! Venture to Entrance Square to test your luck!",
            "Entries are now being accepted for this drawing number of the Mini Cactpot! Venture to Entrance Square to test your luck!");
        sentence = Regex.Replace(sentence, @"Entries for drawing number \d+ of the Mini Cactpot will close momentarily. Those still wishing to purchase a ticket are encouraged to act quickly!",
            "Entries for the drawing number of the Mini Cactpot will close momentarily. Those still wishing to purchase a ticket are encouraged to act quickly!");

        sentence = Regex.Replace(sentence, @"Your mailbox is a complete and utter mess! There wasn't any room left, so I had to send back \d+ letters, kupo!",
            "Your mailbox is a complete and utter mess! There wasn't any room left, so I had to send back some letters, kupo!");

        if (speaker == "Mini Cactpot Broker")
        {
            if (sentence.StartsWith("We have a winner! Please accept my congratulations", StringComparison.Ordinal))
                sentence = "We have a winner! Please accept my congratulations!";
            if (sentence.StartsWith("Congratulations! Here is your prize", StringComparison.Ordinal))
                sentence = "Congratulations! Here is your prize. Would you like to purchase another ticket?";
        }

        sentence = Regex.Replace(sentence, @"\s+", " ").Trim();

        // Remove leading "..." if present (after e.g. "<gulp> <gulp> ...See, empty already.")
        if (sentence.Trim().StartsWith("...", StringComparison.Ordinal))
            sentence = sentence[3..];

        sentence = Regex.Replace(sentence, @"\s+", " ").Trim();

        return (speaker, sentence);
    }

    private static readonly Dictionary<int, string> NumberRomanDictionary = new()
    {
        { 1000, "M" }, { 900, "CM" }, { 500, "D" }, { 400, "CD" },
        { 100, "C" }, { 90, "XC" }, { 50, "L" }, { 40, "XL" },
        { 10, "X" }, { 9, "IX" }, { 5, "V" }, { 4, "IV" }, { 1, "I" },
    };

    private static string RomanTo(int number)
    {
        StringBuilder roman = new();
        foreach (var item in NumberRomanDictionary)
        {
            while (number >= item.Key)
            {
                roman.Append(item.Value);
                number -= item.Key;
            }
        }
        return roman.ToString();
    }

    private static string ConvertRomanNumerals(string text)
    {
        string value = text;
        for (int i = 25; i > 5; i--)
        {
            string numeral = RomanTo(i);
            if (numeral.Length > 1)
                value = value.Replace(numeral, i.ToString(System.Globalization.CultureInfo.InvariantCulture));
        }
        return value;
    }

    /// <summary>
    /// Golden vectors: (speaker, sentence, first, last) -> (expectedSpeaker, expectedSentence).
    /// The pin hashes these, so behavior drift fails SelfCheck() even if the pin string is stale.
    /// </summary>
    public static readonly (string Speaker, string Sentence, string First, string Last, string ExpSpeaker, string ExpSentence)[] GoldenVectors =
    [
        ("Y'shtola", "Hello.", "Forename", "Surname", "Y'shtola", "Hello."),
        ("Hydaelyn's Voice", "<sigh>   Hear me...", "Forename", "Surname", "Hydaelyn", "Hear me..."),
        ("???", "...?!", "Forename", "Surname", "???", "?!"),
        ("Narrator", "Forename Surname, well met.", "Forename", "Surname", "Narrator", "_FIRSTNAME_ _LASTNAME_, well met."),
        ("Y'shtola!", "Chapter VI.", "Forename", "Surname", "Y'shtola", "Chapter 6."),
        ("Narrator", "\"Hello,\" she said.", "Forename", "Surname", "Narrator", "“Hello,” she said."),
        ("Narrator", "a  b\nc—d", "Forename", "Surname", "Narrator", "a b c - d"),
        ("Ramuh's Avatar", "Talk \\u00e1 now", "Forename", "Surname", "Ramuh", "Talk á now"),
        ("Mini Cactpot Broker", "We have a winner! Please accept my congratulations, Forename!", "Forename", "Surname",
            "Mini Cactpot Broker", "We have a winner! Please accept my congratulations!"),
        ("Narrator", "Come one, come all - drawing number 1234", "Forename", "Surname",
            "Narrator", "Come one, come all - drawing number"),
    ];

    public static string GoldenVectorsHash()
    {
        StringBuilder sb = new();
        foreach (var v in GoldenVectors)
            sb.Append(v.Speaker).Append('\0').Append(v.Sentence).Append('\0')
              .Append(v.First).Append('\0').Append(v.Last).Append('\0')
              .Append(v.ExpSpeaker).Append('\0').Append(v.ExpSentence).Append('\n');
        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(sb.ToString())));
    }

    /// <summary>Run at startup: false means the algorithm drifted from its vectors — fail loudly.</summary>
    public static bool SelfCheck()
    {
        foreach (var v in GoldenVectors)
        {
            var (speaker, sentence) = Clean(v.Speaker, v.Sentence, v.First, v.Last);
            if (speaker != v.ExpSpeaker || sentence != v.ExpSentence)
                return false;
        }
        return true;
    }
}
