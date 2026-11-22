using ArtiStardewBridge.Protocol;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using HarmonyLib;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Characters;
using StardewValley.Menus;
using StardewValley.Monsters;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;

namespace ArtiStardewBridge;

/// <summary>
/// TEST-ONLY reflex probe ops (Primitives v2 WS-G4/G5 prep; PLAN §4 PRB-1/3/8). Runs inside the
/// <see cref="GamepadProbe"/> op engine (same trigger.json, same probe-log.jsonl, same run ids) and only when
/// BOTH <see cref="ModConfig.GamepadProbeEnabled"/> and <see cref="ModConfig.ReflexProbeEnabled"/> are on
/// (default OFF). Never on a production path. SMAPI lines carry "GAMEPAD_PROBE REFLEX_PROBE" so
/// <c>probe_gamepad.py collect</c> picks them up.
///
/// Ops (JSON fields as in G7's probe_ops/prb1.json, prb3.json, prb8.json):
///   old_pad      {button: "X"|"A"|null, ticks, sample_every, wait_bar_ticks}: write <c>Game1.oldPadState</c> once per
///                UpdateTicking ONLY while the BobberBar captured at op start is the deepest active menu, the
///                window is inactive and gamepadControls is on (PLAN §1.5 c). button null = neutral each tick.
///                Neutral is written on every exit (op end, bar closed, restore, fault). One bar_tick line per tick.
///                wait_bar_ticks (default 0) lets one run hook and drive: wait up to N ticks for the bar to open.
///                Refuses (no write at all) when the entry is not neutral (oldMouseState left / oldKBState
///                use-tool key down): this probe never writes mouse or keyboard state.
///   charge_hold  {ticks | ms, settle, allow_any}: pad X held N ticks with a charging tool, toolPower per tick,
///                then the affected-area readback (expected tiles for the reached power, tilled/watered counts).
///   combat_swing {presses, cadence_ticks, settle, hp_floor_pct}: N pad X edges at a cadence with a melee
///                weapon; monsters (count/HP), kills, damage taken, swings skipped while UsingTool.
///   pet_a        {target: "pet"|"animal"|"horse"|"any", ticks}: one pad A edge only when that target touches
///                the grab tile and nothing is held; pet/animal/horse facts before and after.
/// The snapshot of every GamepadProbe line gains a "reflex" block (rod, BobberBar, old* bits, counters,
/// player combat facts, nearest monsters/pet/animal).
/// </summary>
internal sealed class ReflexProbe
{
    internal static readonly string[] Ops = { "old_pad", "charge_hold", "combat_swing", "pet_a" };
    private const string Prefix = "GAMEPAD_PROBE REFLEX_PROBE";

    private readonly IModHelper helper;
    private readonly IMonitor monitor;
    private readonly Func<bool> enabled;
    private readonly string dir;
    private readonly FieldInfo? usedGamePadToCast;
    private readonly MethodInfo? tilesAffected;

    // Harmony-fed counters (probe only)
    private static int endUsingToolCount;
    private static int barUpdateCount;
    private static uint barUpdateTick;
    private static int barUpdatesThisTick;
    private static bool lastBarButtonPressed;

    // op state
    private string? runId;
    private string kind = "";
    private JsonElement op;
    private int tick;
    private bool done;
    private string endNote = "";

    // old_pad
    private BobberBar? capturedBar;
    private string? padButton;
    private int padTicks;
    private int sampleEvery;
    private bool padDirty;           // a non-neutral oldPadState is (or may be) in place
    private int waitBarTicks;        // old_pad may wait this many ticks for the BobberBar to open (hook + bar in one run)
    private bool waitingBar;
    private int barStartTick;
    private int padWrites, neutralWrites;
    private string lastWrite = "none";

    // pressed SButtons this op (for Abort)
    private readonly List<SButton> pressed = new();

    // charge_hold
    private int holdTicks, settleTicks;
    private bool suppressed;
    private int maxPower, powerAtRelease = -1;
    private Vector2 chargeTile;
    private int chargeFacing;
    private Dictionary<string, int>? areaBefore;

    // combat_swing
    private int presses, cadence, swingsSent, swingsSkipped, hpFloorPct;
    private int startHealth;
    private int minHealth;
    private Dictionary<int, int> monstersAtStart = new();
    private int pressTickPending = -1;

    // pet_a
    private object? petBefore;

    public ReflexProbe(IModHelper helper, IMonitor monitor, Func<bool> enabled, string dir)
    {
        this.helper = helper;
        this.monitor = monitor;
        this.enabled = enabled;
        this.dir = dir;
        this.usedGamePadToCast = AccessTools.Field(typeof(FishingRod), "usedGamePadToCast");
        this.tilesAffected = AccessTools.Method(typeof(Tool), "tilesAffected", new[] { typeof(Vector2), typeof(int), typeof(Farmer) });
        try
        {
            var harmony = new Harmony(helper.ModRegistry.ModID + ".ReflexProbe");
            harmony.Patch(AccessTools.Method(typeof(Farmer), nameof(Farmer.EndUsingTool)),
                postfix: new HarmonyMethod(typeof(ReflexProbe), nameof(EndUsingToolPostfix)));
            harmony.Patch(AccessTools.Method(typeof(BobberBar), nameof(BobberBar.update)),
                postfix: new HarmonyMethod(typeof(ReflexProbe), nameof(BobberBarUpdatePostfix)));
            this.Log("init", new { dir, patches = "installed", usedGamePadToCast = this.usedGamePadToCast != null, tilesAffected = this.tilesAffected != null });
        }
        catch (Exception ex)
        {
            this.Log("init", new { dir, patches = "FAILED " + ex.Message });
        }
    }

    private static void EndUsingToolPostfix() => endUsingToolCount++;

    private static void BobberBarUpdatePostfix(BobberBar __instance)
    {
        barUpdateCount++;
        if (barUpdateTick != Game1.ticks)
        {
            barUpdateTick = (uint)Game1.ticks;
            barUpdatesThisTick = 0;
        }
        barUpdatesThisTick++;
        lastBarButtonPressed = __instance.buttonPressed;
    }

    public bool Owns(string? opKind) => opKind != null && this.enabled() && Ops.Contains(opKind);

    // ================================================================ engine hooks

    public void Begin(JsonElement opElement, string? run)
    {
        this.runId = run;
        this.op = opElement;
        this.kind = Str(opElement, "op") ?? "";
        this.tick = 0;
        this.done = false;
        this.endNote = "";
        this.pressed.Clear();
        this.suppressed = false;
        switch (this.kind)
        {
            case "old_pad": this.BeginOldPad(); break;
            case "charge_hold": this.BeginCharge(); break;
            case "combat_swing": this.BeginCombat(); break;
            case "pet_a": this.BeginPet(); break;
        }
    }

    /// <summary>UpdateTicking: before SMAPI applies overrides and before Game1.Update reads oldPadState.</summary>
    public void OnUpdateTicking()
    {
        if (this.done)
            return;
        switch (this.kind)
        {
            case "old_pad": this.TickingOldPad(); break;
            case "charge_hold": this.TickingCharge(); break;
            case "combat_swing": this.TickingCombat(); break;
            case "pet_a": this.TickingPet(); break;
        }
    }

    /// <summary>UpdateTicked: after Game1.Update. True when the op is finished.</summary>
    public bool OnUpdateTicked()
    {
        this.tick++;
        return this.kind switch
        {
            "old_pad" => this.TickedOldPad(),
            "charge_hold" => this.TickedCharge(),
            "combat_swing" => this.TickedCombat(),
            "pet_a" => this.TickedPet(),
            _ => true,
        };
    }

    /// <summary>restore / fault: Suppress what this probe pressed, neutral oldPadState if it wrote one.</summary>
    public void Abort(string why)
    {
        foreach (SButton b in this.pressed)
            this.helper.Input.Suppress(b);
        if (this.padDirty)
            this.WriteOldPad(null, "exit:" + why);
        if (this.kind.Length > 0 && !this.done)
            this.Log("abort", new { run = this.runId, op = this.kind, why, tick = this.tick });
        this.done = true;
    }

    // ================================================================ old_pad (PRB-1 step 3)

    private void BeginOldPad()
    {
        this.padButton = Str(this.op, "button");
        this.padTicks = Int(this.op, "ticks", 60);
        this.sampleEvery = Int(this.op, "sample_every", 0);
        this.waitBarTicks = Math.Clamp(Int(this.op, "wait_bar_ticks", 0), 0, 600);
        this.padWrites = this.neutralWrites = 0;
        this.barStartTick = 0;
        this.capturedBar = null;
        if (this.padButton != null && this.padButton != "X" && this.padButton != "A")
        {
            this.Refuse("old_pad", "button_not_X_A_or_null");
            return;
        }
        this.waitingBar = true;
        this.TryStartOldPad(final: this.waitBarTicks == 0);
    }

    /// <summary>Capture the BobberBar and run the entry checks; while waiting (wait_bar_ticks) a missing bar is not a refusal.</summary>
    private void TryStartOldPad(bool final)
    {
        this.capturedBar = DeepestMenu() as BobberBar;
        if (!this.BarGuard(out string why))
        {
            this.capturedBar = null;
            if (final)
                this.Refuse("old_pad", why);
            return;
        }
        this.waitingBar = false;
        this.barStartTick = this.tick;
        // §1.5 c (iii): a pressed oldMouseState.LeftButton or a use-tool key in oldKBState keeps buttonPressed true
        // whatever we write. This probe never writes mouse/keyboard state: it refuses instead.
        bool mouseLeft = Game1.oldMouseState.LeftButton == ButtonState.Pressed;
        bool kbUseTool = Game1.isOneOfTheseKeysDown(Game1.oldKBState, Game1.options.useToolButton);
        if (mouseLeft || kbUseTool)
        {
            this.Refuse("old_pad", $"entry_not_neutral mouseLeft={mouseLeft} kbUseTool={kbUseTool}");
            return;
        }
        this.Log("old_pad_begin", new { run = this.runId, button = this.padButton, ticks = this.padTicks, bar = BarFacts(this.capturedBar), old = OldFacts() });
        this.monitor.Log($"{Prefix} BAR_DRIVE start run={this.runId} button={this.padButton ?? "neutral"} ticks={this.padTicks}", LogLevel.Info);
    }

    private void TickingOldPad()
    {
        if (this.waitingBar)
        {
            this.TryStartOldPad(final: this.tick >= this.waitBarTicks);
            if (this.waitingBar || this.done)
                return;
        }
        if (this.BarGuard(out string why))
        {
            this.WriteOldPad(this.padButton, "tick");
            return;
        }
        // The game closed the bar (or focus came back / the menu changed): neutral in this same tick, then stop.
        if (this.padDirty)
            this.WriteOldPad(null, "exit:" + why);
        this.endNote = "bar_guard_lost:" + why;
        this.done = true;
    }

    private bool TickedOldPad()
    {
        BobberBar? bar = DeepestMenu() as BobberBar;
        bool sample = this.sampleEvery > 0 && this.tick % this.sampleEvery == 0;
        this.Log("bar_tick", new
        {
            run = this.runId,
            tick = this.tick,
            wrote = this.lastWrite,
            barUpdatesThisTick = barUpdateTick == (uint)Game1.ticks ? barUpdatesThisTick : 0,
            updButtonPressed = lastBarButtonPressed,
            bar = bar == null ? null : new
            {
                bar.buttonPressed, bar.bobberBarPos, bar.bobberBarSpeed, bar.bobberPosition, bar.bobberInBar,
                bar.distanceFromCatching, bar.treasure, bar.treasureCaught, bar.treasurePosition, bar.fadeIn, bar.fadeOut,
            },
            oldX = Game1.oldPadState.IsButtonDown(Buttons.X),
            oldA = Game1.oldPadState.IsButtonDown(Buttons.A),
            facts = sample ? this.Facts() : null,
        });
        this.lastWrite = "none";
        if (!this.done && (this.waitingBar || this.tick - this.barStartTick < this.padTicks))
            return false;
        if (this.padDirty)
            this.WriteOldPad(null, "exit:op_end");
        this.done = true;
        this.monitor.Log($"{Prefix} BAR_DRIVE stop run={this.runId} ticks={this.tick} writes={this.padWrites} neutral={this.neutralWrites} note={this.endNote}", LogLevel.Info);
        this.Log("old_pad_end", new { run = this.runId, ticks = this.tick, writes = this.padWrites, neutral = this.neutralWrites, note = this.endNote, old = OldFacts(), endUsingTool = endUsingToolCount });
        return true;
    }

    /// <summary>The probe's ONE Game1.oldPadState writer. A non-neutral value only passes the BobberBar guard.</summary>
    private bool WriteOldPad(string? button, string why)
    {
        bool neutral = button == null;
        if (!neutral && !this.BarGuard(out string guard))
        {
            this.Log("old_pad_write_refused", new { run = this.runId, button, guard, why });
            return false;
        }
        Game1.oldPadState = neutral
            ? new GamePadState()
            : new GamePadState(new GamePadThumbSticks(), new GamePadTriggers(), new GamePadButtons(button == "A" ? Buttons.A : Buttons.X), new GamePadDPad());
        this.padDirty = !neutral;
        if (neutral) this.neutralWrites++; else this.padWrites++;
        this.lastWrite = neutral ? "neutral" : button!;
        if (why.StartsWith("exit:", StringComparison.Ordinal))
            this.Log("old_pad_neutral", new { run = this.runId, why, tick = this.tick });
        return true;
    }

    /// <summary>PLAN §1.5 c (ii): deepest menu is the captured BobberBar, the window is inactive, pad controls on.</summary>
    private bool BarGuard(out string why)
    {
        IClickableMenu? deepest = DeepestMenu();
        if (deepest is not BobberBar bar)
        {
            why = deepest == null ? "no_menu" : "deepest_is_" + deepest.GetType().Name;
            return false;
        }
        if (this.capturedBar != null && !ReferenceEquals(bar, this.capturedBar))
        {
            why = "bar_instance_changed";
            return false;
        }
        if (Game1.game1.IsActive)
        {
            why = "window_active";
            return false;
        }
        if (!Game1.options.gamepadControls)
        {
            why = "gamepad_controls_off";
            return false;
        }
        why = "ok";
        return true;
    }

    // ================================================================ charge_hold (PRB-3)

    private void BeginCharge()
    {
        Farmer p = Game1.player;
        int ms = Int(this.op, "ms", 0);
        this.holdTicks = Int(this.op, "ticks", ms > 0 ? (int)Math.Ceiling(ms / (1000.0 / 60.0)) : 45);
        this.settleTicks = Int(this.op, "settle", 60);
        this.maxPower = 0;
        this.powerAtRelease = -1;
        Tool? tool = p.CurrentTool;
        bool chargeable = (tool is Hoe || tool is WateringCan) && tool.UpgradeLevel >= 1;
        if (!chargeable && !Bool(this.op, "allow_any", false))
        {
            this.Refuse("charge_hold", $"not_chargeable tool={tool?.QualifiedItemId} level={tool?.UpgradeLevel}");
            return;
        }
        if (Game1.activeClickableMenu != null || p.UsingTool)
        {
            this.Refuse("charge_hold", "menu_open_or_using_tool");
            return;
        }
        this.chargeTile = p.GetToolLocation() / 64f;
        this.chargeTile = new Vector2((int)this.chargeTile.X, (int)this.chargeTile.Y);
        this.chargeFacing = p.FacingDirection;
        this.areaBefore = this.AreaCounts(tool, 5);
        this.Log("charge_begin", new
        {
            run = this.runId, ticks = this.holdTicks, tool = tool?.QualifiedItemId, level = tool?.UpgradeLevel,
            animSpeed = tool?.AnimationSpeedModifier, tile = new { x = (int)this.chargeTile.X, y = (int)this.chargeTile.Y }, facing = this.chargeFacing,
            area = this.areaBefore,
        });
    }

    private void TickingCharge()
    {
        if (this.tick < this.holdTicks)
            this.Press(SButton.ControllerX);
        else if (!this.suppressed)
        {
            this.Release(SButton.ControllerX);
            this.powerAtRelease = Game1.player.toolPower.Value;
        }
    }

    private bool TickedCharge()
    {
        Farmer p = Game1.player;
        this.maxPower = Math.Max(this.maxPower, p.toolPower.Value);
        if (this.tick <= this.holdTicks + 4)
        {
            this.Log("charge_tick", new
            {
                run = this.runId, tick = this.tick, held = this.tick <= this.holdTicks, toolPower = p.toolPower.Value,
                toolHold = p.toolHold.Value, usingTool = p.UsingTool, canReleaseTool = p.canReleaseTool, stamina = p.Stamina,
                padX = Game1.input.GetGamePadState().IsButtonDown(Buttons.X),
            });
        }
        if (this.done || this.tick < this.holdTicks + this.settleTicks)
            return this.done;
        Dictionary<string, int>? after = this.AreaCounts(p.CurrentTool, 5);
        this.Log("charge_end", new
        {
            run = this.runId, holdTicks = this.holdTicks, powerAtRelease = this.powerAtRelease, maxPower = this.maxPower,
            suppressed = this.helper.Input.IsSuppressed(SButton.ControllerX), endUsingTool = endUsingToolCount,
            expectedTiles = this.ExpectedTiles(p.CurrentTool, this.powerAtRelease < 0 ? this.maxPower : this.powerAtRelease)?.Count,
            areaBefore = this.areaBefore, areaAfter = after,
        });
        this.done = true;
        return true;
    }

    /// <summary>Per power 0..max: tiles in Tool.tilesAffected and how many are tilled / watered (read-only).</summary>
    private Dictionary<string, int>? AreaCounts(Tool? tool, int maxPower)
    {
        if (tool == null || this.tilesAffected == null)
            return null;
        GameLocation loc = Game1.currentLocation;
        var outMap = new Dictionary<string, int>();
        for (int power = 0; power <= maxPower; power++)
        {
            List<Vector2>? tiles = this.ExpectedTiles(tool, power);
            if (tiles == null)
                return null;
            int tilled = 0, watered = 0;
            foreach (Vector2 t in tiles)
            {
                if (loc.terrainFeatures.TryGetValue(t, out TerrainFeature tf) && tf is HoeDirt dirt)
                {
                    tilled++;
                    if (dirt.state.Value == HoeDirt.watered)
                        watered++;
                }
            }
            outMap[$"p{power}_tiles"] = tiles.Count;
            outMap[$"p{power}_tilled"] = tilled;
            outMap[$"p{power}_watered"] = watered;
        }
        return outMap;
    }

    private List<Vector2>? ExpectedTiles(Tool? tool, int power)
    {
        if (tool == null || this.tilesAffected == null)
            return null;
        try
        {
            return (List<Vector2>?)this.tilesAffected.Invoke(tool, new object[] { this.chargeTile, power, Game1.player });
        }
        catch
        {
            return null;
        }
    }

    // ================================================================ combat_swing (PRB-3/G5 prep, PRB-8 neighbour)

    private void BeginCombat()
    {
        Farmer p = Game1.player;
        this.presses = Math.Clamp(Int(this.op, "presses", 3), 1, 20);
        this.cadence = Math.Clamp(Int(this.op, "cadence_ticks", 24), 2, 120);
        this.settleTicks = Int(this.op, "settle", 60);
        this.hpFloorPct = Math.Clamp(Int(this.op, "hp_floor_pct", 40), 10, 90);
        this.swingsSent = this.swingsSkipped = 0;
        this.pressTickPending = -1;
        if (p.CurrentTool is not MeleeWeapon w || w.isScythe())
        {
            this.Refuse("combat_swing", $"not_a_melee_weapon tool={p.CurrentTool?.QualifiedItemId}");
            return;
        }
        if (Game1.activeClickableMenu != null)
        {
            this.Refuse("combat_swing", "menu_open");
            return;
        }
        this.startHealth = this.minHealth = p.health;
        this.monstersAtStart = Monsters(12f).ToDictionary(m => RuntimeHelpers.GetHashCode(m), m => m.Health);
        this.Log("combat_begin", new { run = this.runId, presses = this.presses, cadence = this.cadence, weapon = w.QualifiedItemId, type = w.type.Value, health = p.health, maxHealth = p.maxHealth, monsters = MonsterFacts(12f, 8) });
    }

    private void TickingCombat()
    {
        Farmer p = Game1.player;
        if (this.pressTickPending >= 0)
        {
            this.Release(SButton.ControllerX);   // one-tick press -> X edge, then up
            this.pressTickPending = -1;
            return;
        }
        if (p.maxHealth > 0 && p.health * 100 < this.hpFloorPct * p.maxHealth)
        {
            this.endNote = "hp_floor_abort";
            this.done = true;
            return;
        }
        int k = this.tick / this.cadence;
        if (this.tick % this.cadence == 0 && k < this.presses)
        {
            if (p.UsingTool)
            {
                this.swingsSkipped++;
                return;
            }
            this.suppressed = false;
            this.Press(SButton.ControllerX);
            this.pressTickPending = this.tick;
            this.swingsSent++;
        }
    }

    private bool TickedCombat()
    {
        Farmer p = Game1.player;
        this.minHealth = Math.Min(this.minHealth, p.health);
        if (this.tick % 6 == 0 || this.pressTickPending >= 0)
        {
            this.Log("combat_tick", new
            {
                run = this.runId, tick = this.tick, usingTool = p.UsingTool, anim = p.FarmerSprite.currentAnimationIndex,
                health = p.health, invincible = p.temporarilyInvincible, special = (p.CurrentTool as MeleeWeapon)?.isOnSpecial,
                monsters = MonsterFacts(12f, 6),
            });
        }
        bool end = this.done || this.tick >= this.presses * this.cadence + this.settleTicks;
        if (!end)
            return false;
        if (this.pressTickPending >= 0)
            this.Release(SButton.ControllerX);
        var now = Monsters(30f).ToDictionary(m => RuntimeHelpers.GetHashCode(m), m => m.Health);
        int kills = this.monstersAtStart.Keys.Count(id => !now.ContainsKey(id));
        int damageDealt = this.monstersAtStart.Sum(kv => now.TryGetValue(kv.Key, out int hp) ? Math.Max(0, kv.Value - hp) : kv.Value);
        this.Log("combat_end", new
        {
            run = this.runId, note = this.endNote, swingsSent = this.swingsSent, swingsSkipped = this.swingsSkipped, kills, damageDealt,
            damageTaken = this.startHealth - this.minHealth, health = p.health, suppressed = this.helper.Input.IsSuppressed(SButton.ControllerX),
        });
        this.done = true;
        return true;
    }

    // ================================================================ pet_a (PRB-8)

    private void BeginPet()
    {
        Farmer p = Game1.player;
        this.holdTicks = Int(this.op, "ticks", 60);
        string target = Str(this.op, "target") ?? "any";
        if (p.ActiveObject != null)
        {
            this.Refuse("pet_a", $"held_object {p.ActiveObject.QualifiedItemId}: A would gift/eat/place");
            return;
        }
        if (p.CurrentTool is MeleeWeapon)
        {
            this.Refuse("pet_a", "melee_weapon_held: A with nothing to check is the special move");
            return;
        }
        string? found = GrabTileTarget(target);
        if (found == null)
        {
            this.Refuse("pet_a", "no_" + target + "_at_grab_tile");
            return;
        }
        this.petBefore = PetFacts();
        this.Log("pet_begin", new { run = this.runId, target, found, facts = this.petBefore });
    }

    private void TickingPet()
    {
        if (this.tick == 0)
            this.Press(SButton.ControllerA);
        else if (!this.suppressed)
            this.Release(SButton.ControllerA);
    }

    private bool TickedPet()
    {
        if (!this.done && this.tick < this.holdTicks)
            return false;
        if (!this.done)
            this.Log("pet_end", new { run = this.runId, before = this.petBefore, after = PetFacts(), menu = DeepestMenu()?.GetType().Name, suppressed = this.helper.Input.IsSuppressed(SButton.ControllerA) });
        this.done = true;
        return true;
    }

    private static string? GrabTileTarget(string target)
    {
        Farmer p = Game1.player;
        Vector2 g = p.GetGrabTile();
        Rectangle r = new((int)g.X * 64, (int)g.Y * 64, 64, 64);
        GameLocation loc = Game1.currentLocation;
        if (target is "pet" or "horse" or "any")
        {
            foreach (NPC c in loc.characters)
            {
                if (!c.GetBoundingBox().Intersects(r))
                    continue;
                if (c is Pet && target is "pet" or "any")
                    return "pet:" + c.Name;
                if (c is Horse && target is "horse" or "any")
                    return "horse:" + c.Name;
            }
        }
        if (target is "animal" or "any")
        {
            foreach (FarmAnimal a in loc.animals.Values)
                if (a.GetBoundingBox().Intersects(r))
                    return "animal:" + a.Name;
        }
        return null;
    }

    // ================================================================ facts (snapshot extension)

    public object? Facts()
    {
        if (!this.enabled())
            return null;
        try
        {
            Farmer p = Game1.player;
            return new
            {
                rod = this.RodFacts(p.CurrentTool as FishingRod),
                bar = BarFacts(DeepestMenu() as BobberBar),
                old = OldFacts(),
                counters = new { endUsingTool = endUsingToolCount, barUpdates = barUpdateCount, barUpdatesThisTick = barUpdateTick == (uint)Game1.ticks ? barUpdatesThisTick : 0 },
                player = new
                {
                    p.health, p.maxHealth, invincible = p.temporarilyInvincible, p.isEating, killScreen = Game1.killScreen,
                    timeOfDay = Game1.timeOfDay, toolPower = p.toolPower.Value, toolHold = p.toolHold.Value, p.canReleaseTool,
                    mounted = p.mount != null, freeSlots = p.freeSpotsInInventory(),
                },
                weapon = p.CurrentTool is MeleeWeapon w ? new
                {
                    id = w.QualifiedItemId, type = w.type.Value, w.isOnSpecial, anim = p.FarmerSprite.currentAnimationIndex,
                    defenseCd = MeleeWeapon.defenseCooldown, daggerCd = MeleeWeapon.daggerCooldown, clubCd = MeleeWeapon.clubCooldown,
                } : null,
                monsters = MonsterFacts(10f, 8),
                pet = PetFacts(),
            };
        }
        catch (Exception ex)
        {
            return new { error = ex.GetType().Name + ": " + ex.Message };
        }
    }

    private object? RodFacts(FishingRod? r)
    {
        if (r == null)
            return null;
        return new
        {
            id = r.QualifiedItemId, level = r.UpgradeLevel, r.isTimingCast, r.castingPower, r.isCasting, r.castedButBobberStillInAir,
            r.isFishing, r.isNibbling, r.hit, r.isReeling, r.pullingOutOfWater, r.fishCaught, r.showingTreasure, r.lastCatchWasJunk,
            usedGamePadToCast = this.usedGamePadToCast?.GetValue(r), r.timeUntilFishingBite,
            bait = r.GetBait()?.QualifiedItemId, baitStack = r.GetBait()?.Stack, tackle = r.GetTackleQualifiedItemIDs(),
        };
    }

    private static object? BarFacts(BobberBar? b)
    {
        if (b == null)
            return null;
        return new
        {
            b.whichFish, b.difficulty, b.motionType, b.buttonPressed, b.bobberBarPos, b.bobberBarSpeed, b.bobberBarHeight,
            b.bobberPosition, b.bobberSpeed, b.bobberInBar, b.distanceFromCatching, b.treasure, b.treasureCaught, b.treasurePosition,
            b.treasureCatchLevel, b.treasureAppearTimer, b.perfect, b.fishSize, b.fadeIn, b.fadeOut, b.scale, b.handledFishResult,
        };
    }

    private static object OldFacts() => new
    {
        padX = Game1.oldPadState.IsButtonDown(Buttons.X),
        padA = Game1.oldPadState.IsButtonDown(Buttons.A),
        mouseLeft = Game1.oldMouseState.LeftButton == ButtonState.Pressed,
        kbUseTool = Game1.isOneOfTheseKeysDown(Game1.oldKBState, Game1.options.useToolButton),
        livePadX = Game1.input.GetGamePadState().IsButtonDown(Buttons.X),
    };

    private static IEnumerable<Monster> Monsters(float radiusTiles)
    {
        Farmer p = Game1.player;
        Vector2 me = p.Tile;
        return Game1.currentLocation.characters.OfType<Monster>().Where(m => Vector2.Distance(m.Tile, me) <= radiusTiles);
    }

    private static object[] MonsterFacts(float radiusTiles, int max)
    {
        Vector2 me = Game1.player.Tile;
        return Monsters(radiusTiles).OrderBy(m => Vector2.Distance(m.Tile, me)).Take(max).Select(m => (object)new
        {
            id = RuntimeHelpers.GetHashCode(m), type = m.GetType().Name, name = m.Name, tx = m.TilePoint.X, ty = m.TilePoint.Y,
            hp = m.Health, max = m.MaxHealth, inv = m.isInvincible(), d = Math.Round(Vector2.Distance(m.Tile, me), 2),
        }).ToArray();
    }

    private static object? PetFacts()
    {
        Farmer p = Game1.player;
        GameLocation loc = Game1.currentLocation;
        Vector2 me = p.Tile;
        Pet? pet = loc.characters.OfType<Pet>().OrderBy(c => Vector2.Distance(c.Tile, me)).FirstOrDefault();
        FarmAnimal? animal = loc.animals.Values.OrderBy(a => Vector2.Distance(a.Tile, me)).FirstOrDefault();
        Horse? horse = loc.characters.OfType<Horse>().OrderBy(c => Vector2.Distance(c.Tile, me)).FirstOrDefault();
        return new
        {
            pet = pet == null ? null : new
            {
                pet.Name, d = Math.Round(Vector2.Distance(pet.Tile, me), 2), friendship = pet.friendshipTowardFarmer.Value,
                granted = pet.grantedFriendshipForPet.Value,
                pettedToday = pet.lastPetDay.TryGetValue(p.UniqueMultiplayerID, out int day) && day == Game1.Date.TotalDays,
            },
            animal = animal == null ? null : new
            {
                animal.Name, d = Math.Round(Vector2.Distance(animal.Tile, me), 2), wasPet = animal.wasPet.Value,
                friendship = animal.friendshipTowardFarmer.Value,
            },
            horse = horse == null ? null : new { horse.Name, d = Math.Round(Vector2.Distance(horse.Tile, me), 2) },
            mounted = p.mount != null,
        };
    }

    // ================================================================ helpers

    private void Press(SButton b)
    {
        this.helper.Input.Press(b);
        if (!this.pressed.Contains(b))
            this.pressed.Add(b);
    }

    private void Release(SButton b)
    {
        this.helper.Input.Suppress(b);
        this.suppressed = true;
    }

    private void Refuse(string opName, string why)
    {
        this.Log(opName + "_refused", new { run = this.runId, why, facts = this.Facts() });
        this.endNote = "refused:" + why;
        this.done = true;
    }

    private static IClickableMenu? DeepestMenu()
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        while (menu?.GetChildMenu() != null)
            menu = menu.GetChildMenu();
        return menu;
    }

    private void Log(string ev, object payload)
    {
        string json;
        try { json = JsonSerializer.Serialize(payload); }
        catch (Exception ex) { json = "{\"serialize_error\":" + JsonSerializer.Serialize(ex.Message) + "}"; }
        this.monitor.Log($"{Prefix} {ev} {json}", LogLevel.Info);
        try
        {
            Directory.CreateDirectory(this.dir);
            AtomicFileWriter.AppendAllText(Path.Combine(this.dir, "probe-log.jsonl"),
                "{\"utc\":\"" + DateTime.UtcNow.ToString("o") + "\",\"ev\":\"reflex_" + ev + "\",\"data\":" + json + "}\n");
        }
        catch
        {
            // SMAPI log stays the record
        }
    }

    private static string? Str(JsonElement e, string name) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
    private static int Int(JsonElement e, string name, int dflt) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : dflt;
    private static bool Bool(JsonElement e, string name, bool dflt) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out JsonElement v) && (v.ValueKind == JsonValueKind.True || v.ValueKind == JsonValueKind.False) ? v.GetBoolean() : dflt;
}
