using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G0 slice 2: input_once family world_key primitive "pad_button" for the world
// buttons that are not a v1 action (PLAN 1.1 / 1.2 world row): LB / RB (Farmer.shiftToolbar),
// Start (GameMenu, inventory tab), Y (GameMenu, crafting tab), Select (QuestLog; P2 sub-flag).
// Executed as one direct call that mirrors the vanilla UpdateControlInput branch and its predicate
// exactly (G1:12721-12728, 13383-13428) - no pad Press, no device state (like ui_key). Gated by
// PadContextTable before the call. Pure parse / verdict here; the game call is in ModEntry.Pad.cs.
// Evidence ceiling: UNIT_TESTED.

public readonly record struct PadButtonTarget(string Button)
{
    public const string Primitive = "pad_button";
    public const string ArgButton = "button";

    /// <summary>The buttons this primitive carries (stick / A / X / LT / RT / select_slot ride the v1 families).</summary>
    public static readonly IReadOnlyList<string> Buttons = new[]
    {
        PadAbstractButton.LB, PadAbstractButton.RB, PadAbstractButton.Start, PadAbstractButton.Y, PadAbstractButton.Select,
    };

    public bool IsValid => Buttons.Contains(this.Button, StringComparer.Ordinal);

    public static bool TryParse(JsonElement args, out PadButtonTarget target)
    {
        target = default;
        if (args.ValueKind != JsonValueKind.Object)
            return false;
        int count = 0;
        string? button = null;
        foreach (JsonProperty p in args.EnumerateObject())
        {
            count++;
            if (!string.Equals(p.Name, ArgButton, StringComparison.Ordinal) || p.Value.ValueKind != JsonValueKind.String)
                return false;
            button = p.Value.GetString();
        }
        if (count != 1 || button is null)
            return false;
        target = new PadButtonTarget(button);
        return target.IsValid;
    }

    public Dictionary<string, object> Args() => new(StringComparer.Ordinal) { [ArgButton] = this.Button };
}

/// <summary>One pad_button verdict (always cleanup-confirmed: a direct call, no device state).</summary>
public sealed record PadButtonVerdict(string Outcome, bool Applied, int InvokeCount, string? Detail, string? ResultMenuType)
{
    public const string ToolbarShifted = "ToolbarShifted";
    public const string MenuOpened = "MenuOpened";
    public const string NoEffect = "PadButtonNoEffect";
    public const string Uncertain = "PadButtonUncertain";
    public const string EventSkipped = "EventSkipped"; // FIXB31

    public bool IsUncertain => this.Outcome == Uncertain;
}

/// <summary>Pure readback rules for the pad_button effects.</summary>
public static class PadButtonEffect
{
    /// <summary>
    /// Farmer.shiftToolbar: right moves the first 12 slots to the end; left moves the last 12 to the
    /// front. The expected order of the item references after the call (Items.Count &lt;= 12 = no change).
    /// </summary>
    public static IReadOnlyList<T> ExpectedShift<T>(IReadOnlyList<T> before, bool right)
    {
        int n = before.Count;
        if (n <= PadContextTable.ToolbarRowSlots)
            return before.ToArray();
        return right
            ? before.Skip(PadContextTable.ToolbarRowSlots).Concat(before.Take(PadContextTable.ToolbarRowSlots)).ToArray()
            : before.Skip(n - PadContextTable.ToolbarRowSlots).Concat(before.Take(n - PadContextTable.ToolbarRowSlots)).ToArray();
    }

    /// <summary>LB / RB verdict from the item references before / after (reference equality per slot).</summary>
    public static PadButtonVerdict Toolbar<T>(IReadOnlyList<T> before, IReadOnlyList<T> after, bool right) where T : class?
    {
        IReadOnlyList<T> expected = ExpectedShift(before, right);
        bool same = after.Count == before.Count && after.Select((x, i) => ReferenceEquals(x, before[i])).All(b => b);
        bool shifted = after.Count == expected.Count && after.Select((x, i) => ReferenceEquals(x, expected[i])).All(b => b);
        if (before.Count <= PadContextTable.ToolbarRowSlots)
            return new PadButtonVerdict(same ? PadButtonVerdict.NoEffect : PadButtonVerdict.Uncertain, false, 1,
                same ? "single_row_backpack" : "unexpected_order", null);
        if (shifted && !same)
            return new PadButtonVerdict(PadButtonVerdict.ToolbarShifted, true, 1, right ? "right" : "left", null);
        return new PadButtonVerdict(same ? PadButtonVerdict.NoEffect : PadButtonVerdict.Uncertain, false, 1,
            same ? "game_refused" : "unexpected_order", null);
    }

    /// <summary>fix63A: the native leave question is up (ReadyCheckDialog festivalEnd, or the single-player confirmation).</summary>
    public static PadButtonVerdict FestivalLeave(bool asked, string? menuAfter) =>
        !asked ? new PadButtonVerdict(PadButtonVerdict.NoEffect, false, 1, "game_refused", null)
        : menuAfter is "ReadyCheckDialog" or "ConfirmationDialog"
            ? new PadButtonVerdict(PadButtonVerdict.MenuOpened, true, 1, "festival_leave_asked", menuAfter)
            : new PadButtonVerdict(PadButtonVerdict.Uncertain, false, 1, "festival_leave_no_question", menuAfter);

    /// <summary>FIXB31: the event skip verdict from Event.skipped / the current event after the call.</summary>
    public static PadButtonVerdict Skip(bool eventBefore, bool skippedBefore, bool called, bool eventAfterIsSame, bool skippedAfter)
    {
        if (!eventBefore || skippedBefore)
            return new PadButtonVerdict(PadButtonVerdict.Uncertain, false, 1, "precondition", null);
        if (!called)
            return new PadButtonVerdict(PadButtonVerdict.NoEffect, false, 1, "game_refused", null);
        // skipEvent ends the event or leaves it marked skipped while its end behaviours run (fade / warp).
        return !eventAfterIsSame || skippedAfter
            ? new PadButtonVerdict(PadButtonVerdict.EventSkipped, true, 1, eventAfterIsSame ? "skipped" : "ended", null)
            : new PadButtonVerdict(PadButtonVerdict.Uncertain, false, 1, "not_marked_skipped", null);
    }

    /// <summary>Start / Y / Select verdict from the menu readback after the call.</summary>
    public static PadButtonVerdict Menu(string button, bool menuWasNull, string? menuTypeAfter, int? gameMenuTabAfter, int craftingTab)
    {
        string? expected = button switch
        {
            PadAbstractButton.Start or PadAbstractButton.Y => "GameMenu",
            PadAbstractButton.Select => "QuestLog",
            _ => null,
        };
        if (expected is null || !menuWasNull)
            return new PadButtonVerdict(PadButtonVerdict.Uncertain, false, 1, "precondition", menuTypeAfter);
        if (menuTypeAfter is null)
            return new PadButtonVerdict(PadButtonVerdict.NoEffect, false, 1, "game_refused", null);
        bool tabOk = button != PadAbstractButton.Y || gameMenuTabAfter == craftingTab;
        return menuTypeAfter == expected && tabOk
            ? new PadButtonVerdict(PadButtonVerdict.MenuOpened, true, 1,
                expected + (gameMenuTabAfter is int tab ? ":tab=" + tab : ""), menuTypeAfter)
            : new PadButtonVerdict(PadButtonVerdict.Uncertain, false, 1, "unexpected_menu", menuTypeAfter);
    }
}
