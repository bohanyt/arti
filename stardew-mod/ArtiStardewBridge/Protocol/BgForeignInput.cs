namespace ArtiStardewBridge.Protocol;

/// <summary>
/// ADR 244 (window focus stops mattering): names a foreign input in the game's own device terms.
/// Pure, SMAPI-free. Two uses:
/// <list type="bullet">
/// <item>the active-window hygiene in UpdateTicking: every real button that is down while one of her
/// requests is in flight and is not her own pinned button is Suppressed, and named in one log line;</item>
/// <item>a VERIFY mismatch with the window active: the difference between the frame her press expected
/// and the frame the game was about to read, appended to the divergence label.</item>
/// </list>
/// A mouse position, a wheel step or a pad deflection below the button threshold cannot be suppressed;
/// they reach VERIFY and end the press uncertain with this name, never silently.
/// </summary>
public static class BgForeignInput
{
    public const string Prefix = "FOREIGN_INPUT";
    public const string MissingPrefix = "MISSING_OWN_INPUT";

    /// <summary>"keyboard:A", "mouse:Left", "pad:B" (the game's Keys / mouse button / Buttons names).</summary>
    public static string Name(BgButton button) => button.Device switch
    {
        BgDevice.Keyboard => "keyboard:" + button.Name,
        BgDevice.Mouse => "mouse:" + button.Name,
        _ => "pad:" + button.Name,
    };

    /// <summary>The real buttons in <paramref name="now"/> other than <paramref name="own"/>, in a stable order.</summary>
    public static IReadOnlyList<BgButton> ForeignButtons(BgInputState now, BgButton? own) =>
        now.Down.Where(b => own is not { } o || !b.Equals(o))
            .OrderBy(b => b.Device).ThenBy(b => b.Name, StringComparer.Ordinal).ToArray();

    /// <summary>
    /// What differs between the frame her press expected and the frame the game was about to read, or null
    /// when they are equal. Extra buttons, a mouse move, a wheel step and pad analog input are foreign
    /// (<see cref="Prefix"/>); a missing button of hers alone is <see cref="MissingPrefix"/>.
    /// </summary>
    public static string? Describe(BgInputState expected, BgInputState retained)
    {
        List<string> foreign = new();
        foreach (BgButton b in retained.Down.Where(b => !expected.IsDown(b))
                     .OrderBy(b => b.Device).ThenBy(b => b.Name, StringComparer.Ordinal))
            foreign.Add(Name(b));
        if (retained.MouseX != expected.MouseX || retained.MouseY != expected.MouseY)
            foreign.Add("mouse:moved");
        if (retained.ScrollWheel != expected.ScrollWheel)
            foreign.Add("mouse:wheel");
        if (retained.PadAnalogActive != expected.PadAnalogActive
            || retained.LeftStickX != expected.LeftStickX || retained.LeftStickY != expected.LeftStickY)
            foreign.Add("pad:stick");
        if (foreign.Count > 0)
            return Prefix + ":" + string.Join("+", foreign);
        List<string> missing = expected.Down.Where(b => !retained.IsDown(b))
            .OrderBy(b => b.Device).ThenBy(b => b.Name, StringComparer.Ordinal).Select(Name).ToList();
        return missing.Count > 0 ? MissingPrefix + ":" + string.Join("+", missing) : null;
    }
}
