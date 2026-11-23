namespace ArtiHostHelper;

/// <summary>What the helper imposed on the HOST and has not given up yet (review M4d item 1). The mod reads these facts.</summary>
public readonly record struct HoldFacts(bool PausedByHelper, bool HelperWalking, bool OwnPromptOpen, bool OwnSleepWait,
    bool OwnGiftInFlight);

/// <summary>The release of each hold, done by the mod in the game (the tests pass a recorder).</summary>
public interface IHelperHoldPort
{
    /// <summary>Lift its startup pause (<c>netWorldState.IsPaused = false</c>).</summary>
    void Unpause();
    /// <summary>Drop its own bed path (the PathFindController it installed).</summary>
    void DropPath();
    /// <summary>Close its own open prompt (its sleep prompt, its gift prompt or the gift reply), by reference.</summary>
    void CloseOwnPrompt();
    /// <summary>Cancel its own night's sleep wait (the native ReadyCheckDialog cancel button, while cancelable).</summary>
    void CancelOwnSleepWait();
    /// <summary>Withdraw the gift proposal it sent and that is still unanswered.</summary>
    void WithdrawOwnGift();
}

/// <summary>
/// ADR 255 (review M4d item 1): what the helper itself imposed on the HOST, and when it gives it up. Its holds are:
/// <list type="bullet">
/// <item>its startup pause (<c>netWorldState.IsPaused</c>, StartupPauseGate);</item>
/// <item>its own bed path (the PathFindController it installed);</item>
/// <item>its own open prompt (sleep prompt, gift prompt, gift reply);</item>
/// <item>its own sleep wait: the sleep ready dialog that followed its own Yes, by reference, the same day
/// (<see cref="HelperSleep"/>), while it is still cancelable;</item>
/// <item>its gift proposal still in flight.</item>
/// </list>
/// They are released as the FIRST act when he gives any input, any stop is seen, the helper becomes inert, or the helper
/// faults, with a release token or without one. Releasing its own holds is the one thing the helper may do while he is
/// present or while it is inert. Review M4e item 1: a ready check it did not start itself (any check but its own sleep
/// wait) is never cancelled, answered or dismissed by a release; the HOST ready marks it sets for her checks are left to
/// the game, as on main.
/// </summary>
public static class HelperHolds
{
    /// <summary>The release actions, in order: lift its pause first, then its movement, its prompt, its sleep wait, its
    /// gift. Nothing that the helper did not impose itself is in this list.</summary>
    public static IReadOnlyList<string> ReleasePlan(HoldFacts f)
    {
        var plan = new List<string>();
        if (f.PausedByHelper) plan.Add("unpause");
        if (f.HelperWalking) plan.Add("drop_path");
        if (f.OwnPromptOpen) plan.Add("close_own_prompt");
        if (f.OwnSleepWait) plan.Add("cancel_own_sleep_wait");
        if (f.OwnGiftInFlight) plan.Add("withdraw_own_gift");
        return plan;
    }

    /// <summary>Carries out the plan. Each step is its own try: one failed release never keeps another hold in force.
    /// Returns what was done, a failed step as <c>step:failed:Type</c>.</summary>
    public static IReadOnlyList<string> Release(HoldFacts f, IHelperHoldPort port)
    {
        var done = new List<string>();
        foreach (string step in ReleasePlan(f))
        {
            try
            {
                switch (step)
                {
                    case "unpause": port.Unpause(); break;
                    case "drop_path": port.DropPath(); break;
                    case "close_own_prompt": port.CloseOwnPrompt(); break;
                    case "cancel_own_sleep_wait": port.CancelOwnSleepWait(); break;
                    default: port.WithdrawOwnGift(); break;
                }
                done.Add(step);
            }
            catch (Exception ex) { done.Add(step + ":failed:" + ex.GetType().Name); }
        }
        return done;
    }
}

/// <summary>
/// ADR 255 (review M4e item 1): the helper's sleep state belongs to ONE night. It is its own record of having answered its
/// own sleep prompt Yes on a given day, and of the sleep ready dialog that followed, captured by reference. The record
/// is cleared at the game's day start (SMAPI DayStarted, raised after the night's save even while he is active), on any
/// owner input, and on any stop. A ready check is the helper's only while this record says so: never by the check's
/// name. Anything it cannot prove is its own is left to the game, as on main.
/// </summary>
public sealed class HelperSleep
{
    int pressedDay = -1;
    int answeredDay = -1;
    object? wait;

    /// <summary>The sleep-due flag of this night (the walk delay runs from it).</summary>
    public bool Due { get; set; }

    internal bool MayPressYes(int today) => today >= 0 && pressedDay != today;

    // Releasing the ready check on owner input must never re-arm a Yes for this day.
    internal bool TryPressYes(int today)
    {
        if (!MayPressYes(today)) return false;
        pressedDay = today;
        return true;
    }

    /// <summary>It answered its own sleep prompt Yes today.</summary>
    public void Answered(int today) { answeredDay = today; wait = null; }

    /// <summary>The first sleep ready dialog seen after its own Yes, the same day, with nothing cleared since, is its own:
    /// captured by reference. Any other dialog, a later one, or one on another day is not.</summary>
    public bool Capture(object? menu, bool sleepCheck, int today)
    {
        if (answeredDay < 0 || today != answeredDay || wait is not null || menu is null || !sleepCheck) return false;
        wait = menu;
        return true;
    }

    /// <summary>This dialog is the sleep wait it started itself today (by reference and by day).</summary>
    public bool Owns(object? menu, int today) => wait is not null && menu is not null && ReferenceEquals(menu, wait)
        && today == answeredDay;

    /// <summary>The night it started is still its own: it answered Yes, nothing cleared the record since, and at most one
    /// date change has passed (the end-of-night menus of that night).</summary>
    public bool OwnNight(int today) => answeredDay >= 0 && today >= answeredDay && today - answeredDay <= 1;

    /// <summary>True when there was anything to clear.</summary>
    public bool Clear()
    {
        bool any = answeredDay >= 0 || wait is not null || Due;
        answeredDay = -1; wait = null; Due = false;
        return any;
    }
}
