using ArtiStardewBridge.Protocol;
using HarmonyLib;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

/// <summary>
/// Primitives v2 WS-G0 OS-cursor guard (PLAN section 1.5, WS-G0 brief item 3). In gamepad mode every
/// menu snap, the ItemGrabMenu open snap, clamp menus and the right stick call
/// <c>Game1.setMousePositionRaw</c> -> <c>StardewValley.InputState.SetMousePosition(int,int)</c> (not
/// overridden by SMAPI) -> <c>Mouse.SetPosition</c> on the shared OS cursor (probe Part 1 (d)). While
/// the farmhand window is inactive that only moves the OWNER's cursor (probe step 4a, live); with it active
/// the cursor is still the owner's. This prefix skips the call whenever UI motor or pad input is enabled,
/// window active or not (ADR 244; copy of the probe's <c>GamepadProbe.SetMousePositionPrefix</c>, live
/// step 4b, widened to the active window). Installed only when the
/// either flag is on at startup, including mouse-mode ItemGrabMenu Escape with a held item;
/// a prefix only (no transpiler, no postfix, no state write). The guard dies
/// with the process: a crash never leaves the OS cursor blocked.
/// </summary>
internal static class PadCursorWarpGuard
{
    public static readonly OsWarpGuardCounters Counters = new();
    private static Func<bool> enabled = static () => false;

    public static bool Installed { get; private set; }

    private static Type? checkedType;
    private static bool checkedOk;

    /// <summary>
    /// Installed AND the live Game1.input does not override SetMousePosition (re-checked when the
    /// instance type changes, e.g. SMAPI replaced Game1.input after Entry). The pad path needs this.
    /// </summary>
    public static bool Verified
    {
        get
        {
            if (!Installed)
                return false;
            Type? live = Game1.input?.GetType();
            if (live is null)
                return false;
            if (live != checkedType)
            {
                checkedType = live;
                checkedOk = live.GetMethod(nameof(InputState.SetMousePosition), new[] { typeof(int), typeof(int) })?.DeclaringType == typeof(InputState);
            }
            return checkedOk;
        }
    }

    public static void Install(string modId, Func<bool> guardEnabled, IMonitor monitor)
    {
        enabled = guardEnabled;
        try
        {
            var target = AccessTools.Method(typeof(InputState), nameof(InputState.SetMousePosition), new[] { typeof(int), typeof(int) })
                ?? throw new MissingMethodException("InputState.SetMousePosition(int,int)");
            // Review minor 7: SetMousePosition is virtual; an override on SMAPI's InputState subclass would
            // silently bypass a prefix on the base method. Refuse to arm the pad path in that case.
            Type? live = Game1.input?.GetType();
            Type? declaring = live?.GetMethod(nameof(InputState.SetMousePosition), new[] { typeof(int), typeof(int) })?.DeclaringType;
            if (live is not null && declaring != typeof(InputState))
                throw new InvalidOperationException($"SetMousePosition is overridden by {declaring?.FullName ?? "?"} on {live.FullName}");
            new Harmony(modId + ".PadCursorWarpGuard").Patch(target,
                prefix: new HarmonyMethod(typeof(PadCursorWarpGuard), nameof(SetMousePositionPrefix)) { priority = Priority.First });
            Installed = true;
            monitor.Log("PAD os-cursor guard installed (InputState.SetMousePosition skipped, window active or not: ADR 244).", LogLevel.Info);
        }
        catch (Exception ex)
        {
            Installed = false;
            monitor.Log($"PAD os-cursor guard NOT installed; the pad path stays disabled: {ex.GetType().Name}: {ex.Message}", LogLevel.Error);
        }
    }

    // Harmony prefix: false = skip the original (the OS cursor does not move).
    private static bool SetMousePositionPrefix()
    {
        bool active;
        bool guard;
        try
        {
            active = Game1.game1?.IsActive == true;
            guard = enabled();
        }
        catch
        {
            active = false;
            guard = true; // unknown => block (fail closed for the owner's cursor)
        }
        return Counters.Decide(guard, active);
    }
}
