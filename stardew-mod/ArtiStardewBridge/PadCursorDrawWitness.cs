using HarmonyLib;
using System.Reflection.Emit;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

/// <summary>
/// FIXB47 investigation: one read-only witness of the native drawMouseCursor return.
/// It grants no input, never hides a cursor and never replaces a native call.
/// A rendering config flag alone does not prove that this method ran after takeover.
/// </summary>
internal static class PadCursorDrawWitness
{
    private static Func<bool> enabled = static () => false;
    private static Snapshot? last;
    private static Snapshot? lastBefore;
    private static long returns;
    private static bool installed;
    internal static object Shape => new { target = "Game1.drawMouseCursor", expected_latch_stores = 1, verified = installed };
    private static CallStatus? lastCall;
    private static long latchWrites;

    // Independent of the diagnostic eligibility (focus/config/menu/read faults). A native
    // assignment of false over our false still revokes our snapshot restoration authority.
    public static long? LatchWriteGeneration => installed ? latchWrites : null;
    internal static void OnNativeLatchWrite() => latchWrites++;

    internal static IEnumerable<CodeInstruction> TrackLatchWrites(IEnumerable<CodeInstruction> instructions)
    {
        var code = instructions.ToList();
        var field = typeof(Game1).GetField(nameof(Game1.wasMouseVisibleThisFrame))!;
        if (code.Count(i => i.opcode == OpCodes.Stsfld && Equals(i.operand, field)) != 1)
            throw new InvalidOperationException("cursor_latch_write_shape");
        var receipt = typeof(PadCursorDrawWitness).GetMethod(nameof(OnNativeLatchWrite),
            System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic)!;
        foreach (var instruction in code)
        {
            yield return instruction;
            if (instruction.opcode == OpCodes.Stsfld && Equals(instruction.operand, field))
                yield return new CodeInstruction(OpCodes.Call, receipt);
        }
    }

    // Scalars only: a skipped/faulted native call is visible diagnostically, never a draw receipt.
    private sealed record CallStatus(long? Tick, bool Skipped, string? Fault, bool ReadFault)
    {
        public string Facts() => $"tick={Tick?.ToString() ?? "unknown"},skipped={Skipped},fault={Fault ?? "none"},read_fault={ReadFault}";
    }

    private sealed record Snapshot(object Game, object Player, object Options, object Location,
        long Tick, bool Menu, bool EmoteBlocksFade, bool Controls, int Fade,
        bool Visible, float Transparency, int Cursor, bool Hud, int Mode, bool Freeze, bool Pan, bool Screenshot,
        string GamepadMode, bool LastMotionMouse, int MouseX, int MouseY, int OldMouseX, int OldMouseY,
        int ViewportX, int ViewportY, int ViewportWidth, int ViewportHeight, bool ActionHint)
    {
        public string Facts() => $"tick={Tick},menu={Menu},emote_blocks={EmoteBlocksFade},controls={Controls},fade={Fade},"
            + $"visible={Visible},transparency={Transparency},cursor={Cursor},hud={Hud},mode={Mode},freeze={Freeze},pan={Pan},screenshot={Screenshot},"
            + $"gamepad_mode={GamepadMode},last_motion_mouse={LastMotionMouse},mouse=({MouseX},{MouseY}),old_mouse=({OldMouseX},{OldMouseY}),"
            + $"viewport=({ViewportX},{ViewportY},{ViewportWidth},{ViewportHeight}),action_hint={ActionHint}";
    }

    public static void Install(string modId, Func<bool> isEnabled, IMonitor monitor)
    {
        enabled = isEnabled;
        try
        {
            if (typeof(Game1).Module.ModuleVersionId != BackgroundMoveRuntime.PinnedGameMvid)
                throw new InvalidOperationException("game_mvid");
            var target = AccessTools.Method(typeof(Game1), nameof(Game1.drawMouseCursor), Type.EmptyTypes)
                ?? throw new MissingMethodException("Game1.drawMouseCursor()");
            new Harmony(modId + ".PadCursorDrawWitness").Patch(target,
                prefix: new HarmonyMethod(typeof(PadCursorDrawWitness), nameof(BeforeDraw)),
                transpiler: new HarmonyMethod(typeof(PadCursorDrawWitness), nameof(TrackLatchWrites)),
                finalizer: new HarmonyMethod(typeof(PadCursorDrawWitness), nameof(AfterDraw)));
            installed = true;
            monitor.Log("PAD native cursor draw witness installed (read-only).", LogLevel.Info);
        }
        catch (Exception ex)
        {
            installed = false;
            monitor.Log("PAD native cursor draw witness unavailable: " + ex.GetType().Name, LogLevel.Warn);
        }
    }

    private static Snapshot? Read()
    {
        if (!enabled() || !Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen
            || Game1.game1 is not { IsActive: false } game || Game1.player is not { } player
            || Game1.options is not { } options || Game1.currentLocation is not { } location)
            return null;
        return new(game, player, options, location, Game1.ticks,
            Game1.activeClickableMenu is not null, Game1.emoteMenu is { gamepadMode: false },
            options.gamepadControls, Game1.timerUntilMouseFade, Game1.wasMouseVisibleThisFrame,
            Game1.mouseCursorTransparency, Game1.mouseCursor, Game1.displayHUD, Game1.gameMode,
            Game1.freezeControls, Game1.panMode, game.takingMapScreenshot,
            options.gamepadMode.ToString(), Game1.lastCursorMotionWasMouse, Game1.getMouseX(false), Game1.getMouseY(false),
            Game1.getOldMouseX(false), Game1.getOldMouseY(false), Game1.viewport.X, Game1.viewport.Y,
            Game1.viewport.Width, Game1.viewport.Height, Game1.isActionAtCurrentCursorTile);
    }

    private static void BeforeDraw(out Snapshot? __state)
    {
        try { __state = Read(); }
        catch { __state = null; }
    }

    private static bool SameBinding(Snapshot? a, Snapshot? b) => a is not null && b is not null
        && ReferenceEquals(a.Game, b.Game) && ReferenceEquals(a.Player, b.Player)
        && ReferenceEquals(a.Options, b.Options) && ReferenceEquals(a.Location, b.Location);

    // Preserve vanilla/other patches' exceptions. A skipped or faulted call is never a draw receipt.
    private static Exception? AfterDraw(bool __runOriginal, Snapshot? __state, Exception? __exception)
    {
        lastCall = new(__state?.Tick, !__runOriginal, __exception?.GetType().Name, false);
        try
        {
            last = __runOriginal && __exception is null ? Read() : null;
            if (!SameBinding(last, __state)) last = null;
            lastBefore = __state;
            if (last is not null) returns++;
        }
        catch
        {
            last = null;
            lastBefore = __state;
            lastCall = lastCall with { ReadFault = true };
        }
        return __exception;
    }

    public static string Describe()
    {
        try
        {
            Snapshot? now = Read();
            bool same = SameBinding(now, last);
            return $"installed={installed} now=[{now?.Facts() ?? "unreadable"}] native_returns={returns} native_latch_writes={latchWrites} "
                + $"last_call_same_binding={SameBinding(now, lastBefore)} last_call=[{lastCall?.Facts() ?? "none"}] "
                + $"last_before_same_binding=[{(same ? lastBefore?.Facts() ?? "none" : "none")}] "
                + $"last_after_same_binding=[{(same ? last!.Facts() : "none")}]";
        }
        catch { return "unreadable"; }
    }
}
