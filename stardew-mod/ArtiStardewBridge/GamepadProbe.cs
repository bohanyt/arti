using ArtiStardewBridge.Protocol;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using HarmonyLib;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewValley.TerrainFeatures;
using IInputSimulator = StardewValley.Util.IInputSimulator;

namespace ArtiStardewBridge;

/// <summary>
/// TEST-ONLY virtual-gamepad probe (a probe branch, issue #278). Default OFF behind
/// <see cref="ModConfig.GamepadProbeEnabled"/>. Never on a production path.
///
/// Trigger: the operator writes <c>&lt;run&gt;/probe-gamepad/trigger.json</c> = {"id":..,"ops":[..]}.
/// The probe renames it to <c>consumed-&lt;id&gt;.json</c> and runs the ops one after the other on the
/// game thread. Every step is logged to SMAPI with the prefix GAMEPAD_PROBE and appended as one JSON
/// line to <c>probe-log.jsonl</c>. Ops:
///   setup        {force_on, snappy}: remember options; gamepadMode ForceOn (default) + gamepadControls
///                + snappyMenus; Suppress every retained keyboard key / mouse button; install a no-write
///                IInputSimulator so UpdateControlInput runs while the window is inactive.
///   press        {buttons:[SButton names], ticks, sample_every}: Press in UpdateTicking for N ticks, then
///                Suppress, then 3 settle ticks. Snapshots before / during / after.
///   wait         {ticks}
///   snapshot     {label}
///   menu         {action: snap_default|move|pad|key_for_pad|click_snapped|close, dir, button}: one direct
///                call on the deepest active menu (the dispatcher is gated off while inactive).
///   warp_block   {on}: Harmony prefix on InputState.SetMousePosition skips Mouse.SetPosition while the
///                window is inactive (calls are always counted, blocked or not).
///   restore      : options back, latch removed (identity), pad buttons Suppressed.
/// </summary>
internal sealed class GamepadProbe
{
    private const string Prefix = "GAMEPAD_PROBE";
    private readonly IModHelper helper;
    private readonly IMonitor monitor;
    private readonly Func<bool> enabled;
    private readonly string dir;
    private readonly FieldInfo? inputSimulatorField;
    private readonly ReflexProbe? reflex;   // WS-G4/G5 prep ops (ReflexProbeEnabled, default OFF)

    private ProbeLatch? latch;
    private bool? origGamepadControls;
    private Options.GamepadModes? origGamepadMode;
    private bool? origSnappy;

    private string? runId;
    private List<JsonElement> ops = new();
    private int opIndex = -1;
    private int opTick;
    private uint lastPoll;
    private readonly List<SButton> pressing = new();
    private int pressTicks, sampleEvery;
    private bool suppressedThisOp;

    internal static bool BlockWarp;
    internal static int WarpCalls, WarpBlocked;
    internal static string WarpLast = "";

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT p);

    public GamepadProbe(IModHelper helper, IMonitor monitor, Func<bool> enabled, string dir, Func<bool>? reflexEnabled = null)
    {
        this.helper = helper;
        this.monitor = monitor;
        this.enabled = enabled;
        this.dir = dir;
        this.inputSimulatorField = typeof(Game1).GetField("inputSimulator", BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public);
        if (reflexEnabled != null && reflexEnabled())
            this.reflex = new ReflexProbe(helper, monitor, () => enabled() && reflexEnabled(), dir);
        try
        {
            MethodInfo target = AccessTools.Method(typeof(InputState), nameof(InputState.SetMousePosition));
            new Harmony(helper.ModRegistry.ModID + ".GamepadProbeWarp").Patch(target,
                prefix: new HarmonyMethod(typeof(GamepadProbe), nameof(SetMousePositionPrefix)));
            this.Log("init", new { dir, warp_patch = "installed", simulator_field = this.inputSimulatorField != null });
        }
        catch (Exception ex)
        {
            this.Log("init", new { dir, warp_patch = "FAILED " + ex.Message });
        }
    }

    private static bool SetMousePositionPrefix(int x, int y)
    {
        bool active = Game1.game1?.IsActive == true;
        WarpCalls++;
        bool block = BlockWarp && !active;
        if (block)
            WarpBlocked++;
        WarpLast = $"x={x} y={y} active={active} blocked={block} gameTicks={Game1.ticks}";
        return !block;
    }

    private sealed class ProbeLatch : IInputSimulator
    {
        public int Receipts;
        public void SimulateInput(ref bool actionButtonPressed, ref bool switchToolButtonPressed, ref bool useToolButtonPressed,
            ref bool useToolButtonReleased, ref bool addItemToInventoryButtonPressed, ref bool cancelButtonPressed,
            ref bool moveUpPressed, ref bool moveRightPressed, ref bool moveLeftPressed, ref bool moveDownPressed,
            ref bool moveUpReleased, ref bool moveRightReleased, ref bool moveLeftReleased, ref bool moveDownReleased,
            ref bool moveUpHeld, ref bool moveRightHeld, ref bool moveLeftHeld, ref bool moveDownHeld)
        {
            this.Receipts++; // writes none of the flags: reachability only
        }
    }

    // ---------------------------------------------------------------- ticking (before ApplyOverrides)

    public void OnUpdateTicking()
    {
        if (!this.enabled() || this.opIndex < 0 || this.opIndex >= this.ops.Count)
            return;
        JsonElement op = this.ops[this.opIndex];
        string ticking = Str(op, "op") ?? "";
        if (this.reflex != null && this.reflex.Owns(ticking))
        {
            this.reflex.OnUpdateTicking();
            return;
        }
        if (ticking != "press")
            return;
        if (this.opTick < this.pressTicks)
        {
            foreach (SButton b in this.pressing)
                this.helper.Input.Press(b);
        }
        else if (!this.suppressedThisOp)
        {
            foreach (SButton b in this.pressing)
                this.helper.Input.Suppress(b);
            this.suppressedThisOp = true;
            this.Log("press_suppress", new { run = this.runId, buttons = this.pressing.Select(b => b.ToString()).ToArray(), tick = this.opTick });
        }
    }

    // ---------------------------------------------------------------- ticked (after Game1.Update)

    public void OnUpdateTicked(uint ticks)
    {
        if (!this.enabled())
            return;
        try
        {
            if (this.opIndex < 0)
            {
                if (ticks - this.lastPoll >= 15)
                {
                    this.lastPoll = ticks;
                    this.PollTrigger();
                }
                return;
            }
            this.StepOp();
        }
        catch (Exception ex)
        {
            this.reflex?.Abort("fault");
            this.Log("fault", new { run = this.runId, op = this.opIndex, error = ex.ToString() });
            this.opIndex = -1;
        }
    }

    private void PollTrigger()
    {
        string trig = Path.Combine(this.dir, "trigger.json");
        if (!File.Exists(trig))
            return;
        string text = AtomicFileWriter.ReadAllText(trig);
        using JsonDocument doc = JsonDocument.Parse(text);
        string id = Str(doc.RootElement, "id") ?? DateTime.UtcNow.ToString("HHmmss");
        AtomicFileWriter.Move(trig, Path.Combine(this.dir, $"consumed-{id}.json"), overwrite: true);
        this.ops = doc.RootElement.GetProperty("ops").EnumerateArray().Select(e => e.Clone()).ToList();
        this.runId = id;
        this.opIndex = 0;
        this.BeginOp();
        this.Log("run_begin", new { run = id, ops = this.ops.Count, snapshot = this.Snapshot() });
    }

    private void BeginOp()
    {
        this.opTick = 0;
        this.pressing.Clear();
        this.suppressedThisOp = false;
        if (this.opIndex >= this.ops.Count)
            return;
        JsonElement op = this.ops[this.opIndex];
        if (Str(op, "op") == "press")
        {
            foreach (JsonElement b in op.GetProperty("buttons").EnumerateArray())
                this.pressing.Add(Enum.Parse<SButton>(b.GetString()!));
            this.pressTicks = Int(op, "ticks", 1);
            this.sampleEvery = Int(op, "sample_every", 0);
            this.Log("press_begin", new { run = this.runId, buttons = this.pressing.Select(b => b.ToString()).ToArray(), ticks = this.pressTicks, before = this.Snapshot() });
        }
        else if (this.reflex != null && this.reflex.Owns(Str(op, "op")))
            this.reflex.Begin(op, this.runId);
    }

    private void NextOp()
    {
        this.opIndex++;
        if (this.opIndex >= this.ops.Count)
        {
            this.Log("run_end", new { run = this.runId, snapshot = this.Snapshot() });
            this.opIndex = -1;
            return;
        }
        this.BeginOp();
    }

    private void StepOp()
    {
        JsonElement op = this.ops[this.opIndex];
        string kind = Str(op, "op") ?? "";
        switch (kind)
        {
            case "setup":
                this.Setup(op);
                this.NextOp();
                return;
            case "restore":
                this.Restore();
                this.NextOp();
                return;
            case "snapshot":
                this.Log("snapshot", new { run = this.runId, label = Str(op, "label"), snapshot = this.Snapshot() });
                this.NextOp();
                return;
            case "warp_block":
                BlockWarp = Bool(op, "on", true);
                this.Log("warp_block", new { run = this.runId, on = BlockWarp });
                this.NextOp();
                return;
            case "options":
                this.SetOptions(op);
                this.NextOp();
                return;
            case "menu":
                this.Menu(op);
                this.NextOp();
                return;
            case "wait":
                this.opTick++;
                if (this.opTick >= Int(op, "ticks", 1))
                {
                    this.Log("wait_end", new { run = this.runId, ticks = this.opTick, snapshot = this.Snapshot() });
                    this.NextOp();
                }
                return;
            case "press":
                // Runs after this tick's Game1.Update: opTick input ticks have been consumed.
                this.opTick++;
                if (this.sampleEvery > 0 && this.opTick % this.sampleEvery == 0 && this.opTick <= this.pressTicks)
                    this.Log("press_sample", new { run = this.runId, tick = this.opTick, snapshot = this.Snapshot() });
                if (this.opTick >= this.pressTicks + 4)
                {
                    this.Log("press_end", new
                    {
                        run = this.runId,
                        buttons = this.pressing.Select(b => b.ToString()).ToArray(),
                        suppressed = this.pressing.Select(b => this.helper.Input.IsSuppressed(b)).ToArray(),
                        after = this.Snapshot(),
                    });
                    this.NextOp();
                }
                return;
            default:
                if (this.reflex != null && this.reflex.Owns(kind))
                {
                    if (this.reflex.OnUpdateTicked())
                        this.NextOp();
                    return;
                }
                this.Log("unknown_op", new { run = this.runId, op = kind });
                this.NextOp();
                return;
        }
    }

    // ---------------------------------------------------------------- ops

    private void Setup(JsonElement op)
    {
        Options o = Game1.options;
        this.origGamepadControls ??= o.gamepadControls;
        this.origGamepadMode ??= o.gamepadMode;
        this.origSnappy ??= o.snappyMenus;
        if (Bool(op, "force_on", true))
            o.gamepadMode = Options.GamepadModes.ForceOn;
        o.gamepadControls = true;
        o.snappyMenus = Bool(op, "snappy", true);

        // Neutralize the retained keyboard / mouse buttons (Alt from Alt+Tab etc.).
        var suppressed = new List<string>();
        foreach (Keys k in Game1.input.GetKeyboardState().GetPressedKeys())
        {
            SButton b = k.ToSButton();
            this.helper.Input.Suppress(b);
            suppressed.Add(b.ToString());
        }
        MouseState m = Game1.input.GetMouseState();
        if (m.LeftButton == ButtonState.Pressed) { this.helper.Input.Suppress(SButton.MouseLeft); suppressed.Add("MouseLeft"); }
        if (m.RightButton == ButtonState.Pressed) { this.helper.Input.Suppress(SButton.MouseRight); suppressed.Add("MouseRight"); }

        string latchState;
        object? cur = this.inputSimulatorField?.GetValue(null);
        if (cur == null && this.inputSimulatorField != null)
        {
            this.latch = new ProbeLatch();
            this.inputSimulatorField.SetValue(null, this.latch);
            latchState = "installed";
        }
        else
            latchState = cur == null ? "no_field" : "occupied_by_" + cur.GetType().FullName;
        this.Log("setup", new
        {
            run = this.runId,
            orig = new { gamepadControls = this.origGamepadControls, gamepadMode = this.origGamepadMode?.ToString(), snappyMenus = this.origSnappy },
            suppressed,
            latch = latchState,
            snapshot = this.Snapshot(),
        });
    }

    private void SetOptions(JsonElement op)
    {
        Options o = Game1.options;
        if (op.TryGetProperty("gamepadControls", out JsonElement gc)) o.gamepadControls = gc.GetBoolean();
        if (op.TryGetProperty("snappyMenus", out JsonElement sm)) o.snappyMenus = sm.GetBoolean();
        if (op.TryGetProperty("gamepadMode", out JsonElement gm)) o.gamepadMode = Enum.Parse<Options.GamepadModes>(gm.GetString()!);
        if (op.TryGetProperty("timerUntilMouseFade", out JsonElement tf)) Game1.timerUntilMouseFade = tf.GetInt32();
        this.Log("options", new { run = this.runId, snapshot = this.Snapshot() });
    }

    private void Restore()
    {
        this.reflex?.Abort("restore");
        foreach (SButton b in new[] { SButton.ControllerA, SButton.ControllerB, SButton.ControllerX, SButton.ControllerY,
                     SButton.LeftThumbstickLeft, SButton.LeftThumbstickRight, SButton.LeftThumbstickUp, SButton.LeftThumbstickDown,
                     SButton.DPadLeft, SButton.DPadRight, SButton.DPadUp, SButton.DPadDown })
            this.helper.Input.Suppress(b);
        Options o = Game1.options;
        if (this.origGamepadMode is { } gm) o.gamepadMode = gm;
        if (this.origGamepadControls is { } gc) o.gamepadControls = gc;
        if (this.origSnappy is { } sn) o.snappyMenus = sn;
        string latchState = "none";
        if (this.latch != null && this.inputSimulatorField != null)
        {
            object? cur = this.inputSimulatorField.GetValue(null);
            if (ReferenceEquals(cur, this.latch))
            {
                this.inputSimulatorField.SetValue(null, null);
                latchState = "removed receipts=" + this.latch.Receipts;
            }
            else
                latchState = "not_owned_left_alone";
            this.latch = null;
        }
        BlockWarp = false;
        this.Log("restore", new { run = this.runId, latch = latchState, snapshot = this.Snapshot() });
        this.origGamepadControls = null; this.origGamepadMode = null; this.origSnappy = null;
    }

    private void Menu(JsonElement op)
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        while (menu?.GetChildMenu() != null)
            menu = menu.GetChildMenu();
        string action = Str(op, "action") ?? "";
        object before = this.Snapshot();
        string detail = "";
        if (menu == null)
        {
            this.Log("menu", new { run = this.runId, action, error = "no active menu", before });
            return;
        }
        switch (action)
        {
            case "populate_snap_default":
                menu.populateClickableComponentList();
                menu.snapToDefaultClickableComponent();
                detail = "populate+snapToDefault";
                break;
            case "snap_default":
                menu.snapToDefaultClickableComponent();
                break;
            case "move":
                menu.applyMovementKey(Int(op, "dir", 2));
                break;
            case "pad":
            {
                // Mirror of Game1.updateActiveMenu's gamepad branch for one pressed button.
                Buttons b = Enum.Parse<Buttons>(Str(op, "button")!);
                menu.receiveGamePadButton(b);
                if (Bool(op, "also_key", true) && menu == TopMenu())
                {
                    Keys k = Utility.mapGamePadButtonToKey(b);
                    menu.receiveKeyPress(k);
                    detail = "mapped_key=" + k;
                }
                break;
            }
            case "click_snapped":
            {
                ClickableComponent? c = menu.getCurrentlySnappedComponent();
                if (c == null) { detail = "no snapped component"; break; }
                Point p = c.bounds.Center;
                menu.receiveLeftClick(p.X, p.Y);
                detail = $"clicked id={c.myID} at {p.X},{p.Y}";
                break;
            }
            case "close":
                menu.exitThisMenu();
                break;
            default:
                detail = "unknown action";
                break;
        }
        this.Log("menu", new { run = this.runId, action, dir = Int(op, "dir", -1), button = Str(op, "button"), detail, before, after = this.Snapshot() });
    }

    private static IClickableMenu? TopMenu()
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        while (menu?.GetChildMenu() != null)
            menu = menu.GetChildMenu();
        return menu;
    }

    // ---------------------------------------------------------------- readback

    private object Snapshot()
    {
        Farmer p = Game1.player;
        GamePadState pad = Game1.input.GetGamePadState();
        MouseState ms = Game1.input.GetMouseState();
        GetCursorPos(out POINT os);
        Vector2 face = p.GetGrabTile();
        Vector2 toolLoc = p.GetToolLocation();
        Vector2 toolTile = new((int)(toolLoc.X / 64f), (int)(toolLoc.Y / 64f));
        GameLocation? loc = Game1.currentLocation;
        string faced = "none";
        if (loc != null)
        {
            if (loc.terrainFeatures.TryGetValue(face, out TerrainFeature tf))
                faced = tf is Tree t ? $"Tree(health={t.health.Value:0.##},stage={t.growthStage.Value},stump={t.stump.Value})" : tf.GetType().Name;
            else if (loc.Objects.TryGetValue(face, out StardewValley.Object obj))
                faced = $"{obj.QualifiedItemId}({obj.Name})";
        }
        IClickableMenu? menu = TopMenu();
        object? menuInfo = null;
        if (menu != null)
        {
            ClickableComponent? snap = menu.getCurrentlySnappedComponent();
            int? chestStacks = null, chestSlots = null;
            if (menu is ItemGrabMenu igm && igm.ItemsToGrabMenu?.actualInventory != null)
            {
                chestSlots = igm.ItemsToGrabMenu.actualInventory.Count(i => i != null);
                chestStacks = igm.ItemsToGrabMenu.actualInventory.Where(i => i != null).Sum(i => i.Stack);
            }
            menuInfo = new
            {
                type = menu.GetType().Name,
                snapped = snap == null ? null : new { id = snap.myID, name = snap.name, x = snap.bounds.Center.X, y = snap.bounds.Center.Y },
                chestSlots,
                chestStacks,
                heldItem = (menu as MenuWithInventory)?.heldItem?.QualifiedItemId,
            };
        }
        object? sim = this.inputSimulatorField?.GetValue(null);
        return new
        {
            updateTicks = Game1.ticks,
            isActive = Game1.game1.IsActive,
            pos = new { x = p.Position.X, y = p.Position.Y, tx = p.TilePoint.X, ty = p.TilePoint.Y, facing = p.FacingDirection },
            loc = loc?.NameOrUniqueName,
            tool = p.CurrentTool?.QualifiedItemId,
            toolLevel = p.CurrentTool?.UpgradeLevel,
            slot = p.CurrentToolIndex,
            usingTool = p.UsingTool,
            canMove = p.CanMove,
            toolPower = p.toolPower.Value,
            toolHold = p.toolHold.Value,
            stamina = p.Stamina,
            facedTile = new { x = (int)face.X, y = (int)face.Y, what = faced },
            toolTile = new { x = (int)toolTile.X, y = (int)toolTile.Y },
            opts = new { gc = Game1.options.gamepadControls, mode = Game1.options.gamepadMode.ToString(), snappy = Game1.options.snappyMenus, snappyEff = Game1.options.SnappyMenus },
            cursor = new
            {
                wasVisible = Game1.wasMouseVisibleThisFrame,
                transparency = Game1.mouseCursorTransparency,
                fadeTimer = Game1.timerUntilMouseFade,
                lastMotionMouse = Game1.lastCursorMotionWasMouse,
                gameX = ms.X, gameY = ms.Y,
                gameTileX = (int)((Game1.getMouseX(false) + Game1.viewport.X) / 64f),
                gameTileY = (int)((Game1.getMouseY(false) + Game1.viewport.Y) / 64f),
                osX = os.X, osY = os.Y,
                warpCalls = WarpCalls, warpBlocked = WarpBlocked, warpLast = WarpLast,
            },
            pad = new
            {
                connected = pad.IsConnected,
                a = pad.IsButtonDown(Buttons.A), b = pad.IsButtonDown(Buttons.B), x = pad.IsButtonDown(Buttons.X),
                lx = pad.ThumbSticks.Left.X, ly = pad.ThumbSticks.Left.Y,
                oldX = Game1.oldPadState.IsButtonDown(Buttons.X), oldA = Game1.oldPadState.IsButtonDown(Buttons.A),
            },
            keys = Game1.input.GetKeyboardState().GetPressedKeys().Select(k => k.ToString()).ToArray(),
            menu = menuInfo,
            simulator = sim == null ? null : sim.GetType().Name,
            latchReceipts = this.latch?.Receipts,
            items = p.Items.Where(i => i != null).Sum(i => i.Stack),
            reflex = this.reflex?.Facts(),
        };
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
                "{\"utc\":\"" + DateTime.UtcNow.ToString("o") + "\",\"ev\":\"" + ev + "\",\"data\":" + json + "}\n");
        }
        catch
        {
            // SMAPI log stays the record
        }
    }

    private static string? Str(JsonElement e, string name) => e.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
    private static int Int(JsonElement e, string name, int dflt) => e.TryGetProperty(name, out JsonElement v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : dflt;
    private static bool Bool(JsonElement e, string name, bool dflt) => e.TryGetProperty(name, out JsonElement v) && (v.ValueKind == JsonValueKind.True || v.ValueKind == JsonValueKind.False) ? v.GetBoolean() : dflt;
}
