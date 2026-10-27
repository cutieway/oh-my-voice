namespace OhMyVoice.Common;

/// <summary>The grammatical pronoun set used to resolve a pack line.</summary>
public enum Pronoun
{
    They,
    She,
    He,
}

/// <summary>
/// Player setting. Auto follows Dalamud's binary character sex; the manual
/// choices are the only way to select neutral they/them, because the game
/// exposes no pronoun field.
/// </summary>
public enum PronounChoice
{
    Auto = 0,
    They = 1,
    She = 2,
    He = 3,
}

public static class Pronouns
{
    public static Pronoun Effective(PronounChoice choice, bool? isFemale) =>
        choice switch
        {
            PronounChoice.They => Pronoun.They,
            PronounChoice.She => Pronoun.She,
            PronounChoice.He => Pronoun.He,
            _ => isFemale switch
            {
                true => Pronoun.She,
                false => Pronoun.He,
                null => Pronoun.They,
            },
        };
}
