using FFXIVClientStructs.FFXIV.Client.System.Input;
using FFXIVClientStructs.FFXIV.Client.UI;

namespace OhMyVoice;

/// <summary>
/// Whether the player advanced a dialog box recently, read from the game's own
/// input state. Three sources matter because the game accepts all of them in a
/// cutscene: the keyboard confirm keys, the gamepad face/start buttons, and the
/// mouse buttons.
///
/// The state is read before the game consumes it - <see cref="InputData"/>'s
/// per-frame flags are cleared by the game - so the check uses the held state
/// plus a short window instead of a single frame's "pressed" flag. Our own
/// synthetic click never appears here: it is delivered straight to the addon,
/// not through the input system.
/// </summary>
public sealed class InputWatch
{
    /// <summary>How long a confirm press still counts as the player's doing.</summary>
    private const int WindowMs = 250;

    private const GamepadButtonsFlags AdvanceButtons =
        GamepadButtonsFlags.Cross | GamepadButtonsFlags.Circle |
        GamepadButtonsFlags.Triangle | GamepadButtonsFlags.Start;

    private const MouseButtonFlags AdvanceButtonsMouse =
        MouseButtonFlags.LBUTTON | MouseButtonFlags.RBUTTON;

    // Space, Enter and Numpad 0 are the game's default "advance dialog" keys.
    private static readonly SeVirtualKey[] AdvanceKeys =
        [SeVirtualKey.SPACE, SeVirtualKey.RETURN, SeVirtualKey.NUMPAD0];

    private long lastConfirmTicks;

    /// <summary>Called once per frame, before anything reads the state.</summary>
    public unsafe void Poll()
    {
        var input = UIInputData.Instance();
        if (input == null) return;

        // Held state plus any button edge: GamepadInputs.Buttons can be cleared
        // while a UI element has focus, the edge flags cannot.
        var pad = input->GamepadInputs;
        var pad2 = input->GamepadInputs2;
        bool pressed =
            (input->CursorInputs.MouseButtonHeldFlags & AdvanceButtonsMouse) != 0
            || (pad.Buttons & AdvanceButtons) != 0
            || ((pad.ButtonsPressed | pad.ButtonsReleased) & AdvanceButtons) != 0
            || (pad2.Buttons & AdvanceButtons) != 0
            || ((pad2.ButtonsPressed | pad2.ButtonsReleased) & AdvanceButtons) != 0;
        if (!pressed)
        {
            foreach (var key in AdvanceKeys)
            {
                if (input->IsKeyDown(key))
                {
                    pressed = true;
                    break;
                }
            }
        }
        if (pressed) Volatile.Write(ref lastConfirmTicks, DateTime.UtcNow.Ticks);
    }

    /// <summary>
    /// True when the player pressed something that advances a dialog within the
    /// last <see cref="WindowMs"/> milliseconds. False before the first poll or
    /// without input data, which callers read as "not the player": that can
    /// only ever suppress a warning, never invent one.
    /// </summary>
    public bool ConfirmedRecently()
    {
        long ticks = Volatile.Read(ref lastConfirmTicks);
        return ticks != 0
            && DateTime.UtcNow.Ticks - ticks < WindowMs * TimeSpan.TicksPerMillisecond;
    }
}
