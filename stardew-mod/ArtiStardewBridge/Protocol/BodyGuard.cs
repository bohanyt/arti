namespace ArtiStardewBridge.Protocol;

// =====================================================================================================
// ADR 179 body guard: pure trigger (no game, no SMAPI). Each UpdateTicked the mod samples BodyGuardFacts; when she is on
// a Mines floor, a live visible monster is within ContactRadius, her native HP dropped within HpFallingWindowTicks and
// no fight batch / input attempt is in flight, the mod itself starts the disengage leg (ModEntry.BodyGuard.cs) through a
// journaled-free "body_guard" execution-slot client. The runtime learns it from the published fact afterwards.
// STOP, lease loss, a recorded fault and an event always win; a question dialog is never closed for her.
// =====================================================================================================

public readonly record struct BodyGuardFacts(
    long Tick,
    string Location,
    bool InMines,
    int Health,
    int MaxHealth,
    float TileX,
    float TileY,
    IReadOnlyList<MonsterView> Monsters,
    bool ExitKnown,
    bool FightInFlight,
    bool SlotIdle,
    bool LeaseOk,
    bool EventUp,
    string Menu,    // BodyGuardMenu.None | Closable | Question
    bool OnExitStand = false,   // ADR 185: already standing on the up-ladder stand (nothing left to walk)
    bool HandBound = true);     // ADR 189: a Tool/weapon is in her hand (the controller binds it; food/objects cannot)

public static class BodyGuardMenu
{
    public const string None = "none";
    public const string Closable = "closable";   // no answer pending and no cursor item: exitActiveMenu is safe
    public const string Question = "question";   // her answer, never closed for her: the guard waits
}

public enum BodyGuardAction { None, CloseMenu, Start, Unbound }

/// <summary>ADR 189 (a76 SMAPI 49598-49599): the guard's admitted start either runs or gives the slot back. A start that
/// cannot bind (``tool_binding``: food in hand) used to throw out of UpdateTicked with the attempt still admitted, and
/// every later runtime command was refused slot_busy until the session stopped.</summary>
public static class BodyGuardStart
{
    /// <summary>Null when <paramref name="start"/> ran; else the failure, with the slot already neutralized.</summary>
    public static string? Run(ExecutionSlot slot, Action start, long nowMs)
    {
        try
        {
            start();
            return null;
        }
        catch (InvalidOperationException ex)
        {
            slot.Neutralize(nowMs, "body_guard_start_failed");
            return "start_failed:" + ex.Message;
        }
    }
}

public sealed class BodyGuardPolicy
{
    public const float ContactRadius = 1.5f;          // mirrors the runtime's CONTACT_RADIUS_TILES
    public const int HpFallingWindowTicks = 600;      // 10 s, mirrors HP_FALLING_WINDOW_S
    public const int StartsPerFloorMax = 8;           // mirrors GUARD_STARTS_MAX
    public const int CooldownTicks = 30;              // after a leg ends, before the next start on the same floor

    private string? location;
    private int lastHealth = -1;
    private long lastDropTick = long.MinValue / 2;
    private int dropFrom = -1;
    private long lastEndTick = long.MinValue / 2;
    private long unboundDropTick = long.MinValue;
    public int Starts { get; private set; }
    public int DropFrom => this.dropFrom;
    public long LastDropTick => this.lastDropTick;

    /// <summary>Every tick: track HP and decide. Close a closable menu first; start when everything else holds.</summary>
    public BodyGuardAction Observe(in BodyGuardFacts f)
    {
        if (f.Location != this.location)
        {
            this.location = f.Location;
            this.Starts = 0;
            this.lastHealth = f.Health;
            this.lastDropTick = long.MinValue / 2;
            this.dropFrom = -1;
            return BodyGuardAction.None;
        }
        if (this.lastHealth >= 0 && f.Health < this.lastHealth)
        {
            if (f.Tick - this.lastDropTick > HpFallingWindowTicks)
                this.dropFrom = this.lastHealth;
            this.lastDropTick = f.Tick;
        }
        this.lastHealth = f.Health;
        if (!f.InMines || f.Health <= 0 || f.MaxHealth <= 0 || !f.ExitKnown || !f.LeaseOk || f.EventUp || f.FightInFlight
            || f.Tick - this.lastDropTick > HpFallingWindowTicks || f.Tick - this.lastEndTick < CooldownTicks
            || this.Starts >= StartsPerFloorMax || !InContact(f) || f.OnExitStand)
            return BodyGuardAction.None;   // ADR 185: on the stand the runtime's up press (ExitMine question) owns it
        if (f.Menu == BodyGuardMenu.Question)
            return BodyGuardAction.None;
        if (f.Menu == BodyGuardMenu.Closable)
            return BodyGuardAction.CloseMenu;
        if (!f.SlotIdle)
            return BodyGuardAction.None;   // the runtime's own input is in flight: its contact hooks own this tick
        if (!f.HandBound)
        {
            // ADR 189 (a76 S49598-S49599): food/object in hand cannot bind the controller. Say so once per drop (a
            // named refusal the runtime's contact guard answers); no start, no cap, never a crashed admission.
            if (this.unboundDropTick == this.lastDropTick)
                return BodyGuardAction.None;
            this.unboundDropTick = this.lastDropTick;
            return BodyGuardAction.Unbound;
        }
        this.Starts++;
        return BodyGuardAction.Start;
    }

    /// <summary>The leg ended (any verdict) or its admission was refused: the cooldown starts.</summary>
    public void Ended(long tick) => this.lastEndTick = tick;

    /// <summary>ADR 185 (a75 native 44624-44691: seven 1-tick at-stand results spent the cap): a leg that pressed
    /// nothing and moved nothing is not a guard start; it gives its start back. The cap counts real legs only.</summary>
    public void EndedWithoutMoving(long tick)
    {
        this.lastEndTick = tick;
        if (this.Starts > 0)
            this.Starts--;
    }

    public static bool InContact(in BodyGuardFacts f)
    {
        foreach (MonsterView m in f.Monsters)
        {
            if (m.Health <= 0 || m.Hidden)
                continue;
            float dx = m.TileX - f.TileX, dy = m.TileY - f.TileY;
            if (Math.Sqrt(dx * dx + dy * dy) <= ContactRadius)
                return true;
        }
        return false;
    }
}
