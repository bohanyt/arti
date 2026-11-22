using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;

namespace ArtiStardewBridge;

/// <summary>
/// Review R5 S1: whether the owner physically holds a keyboard key or mouse button. This reads raw XNA state, not SMAPI's
/// masked view (a button she suppressed reads as up there). Installed as HelperPressPort.OwnerPhysicallyDown, so a
/// suppressed owner button is handed back only while it is really held. Mod only (the test doubles supply their own
/// physical model). No game call.
/// </summary>
internal static class OwnerInputProbe
{
    internal static bool RawDown(SButton button)
    {
        try
        {
            if (button.TryGetKeyboard(out Keys key))
                return Keyboard.GetState().IsKeyDown(key);
            return button == SButton.MouseLeft ? Mouse.GetState().LeftButton == ButtonState.Pressed
                : button == SButton.MouseRight && Mouse.GetState().RightButton == ButtonState.Pressed;
        }
        catch
        {
            return false; // a read fault hands nothing back
        }
    }
}
