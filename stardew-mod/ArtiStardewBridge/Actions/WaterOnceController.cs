using System.Globalization;
using System.Text.Json;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.TerrainFeatures;
using StardewValley.Tools;

namespace ArtiStardewBridge.Actions;

/// <summary>Explicit local command for one adjacent tile. Uses native tool events, not soil mutation.</summary>
public sealed class WaterOnceController
{
    private readonly IModHelper helper;
    private readonly IMonitor monitor;
    private readonly Func<bool> enabled;
    private readonly Func<bool> otherMotorBusy;
    private readonly Action? prepareOwner;
    private readonly Func<MutationAdvice>? consultOwner;
    private readonly WaterOnceAction action;
    private Farmer? farmer;
    private GameLocation? location;
    private WateringCan? can;
    private HoeDirt? soil;
    private Vector2 target;
    private Vector2 origin;
    private int screen;
    private int beforeWater;
    private int? beforeSoil;
    private long run;
    private string lastReport = "No watering command yet.";

    public bool IsActive => this.action.IsActive;

    public int RunId => (int)this.run;

    /// <summary>
    /// Dipanggil persis saat <see cref="Report"/> menulis status terminal
    /// (bukan <c>Queued</c>). Kotak masuk memakai ini supaya tidak ada
    /// lifecycle watering kedua.
    /// </summary>
    public Action<int, string>? OnTerminal { get; set; }

    public WaterOnceController(IModHelper helper, IMonitor monitor, Func<bool> enabled, Func<bool> otherMotorBusy)
        : this(helper, monitor, enabled, otherMotorBusy, null, null)
    {
    }

    public WaterOnceController(
        IModHelper helper,
        IMonitor monitor,
        Func<bool> enabled,
        Func<bool> otherMotorBusy,
        Action? prepareOwner,
        Func<MutationAdvice>? consultOwner)
    {
        this.helper = helper;
        this.monitor = monitor;
        this.enabled = enabled;
        this.otherMotorBusy = otherMotorBusy;
        this.prepareOwner = prepareOwner;
        this.consultOwner = consultOwner;
        this.action = new(this.Begin, () => this.farmer!.EndUsingTool());
        helper.ConsoleCommands.Add("arti_water", "Water one adjacent dry soil tile: arti_water <tileX> <tileY>. Requires WaterOnceEnabled.", this.OnCommand);
        helper.ConsoleCommands.Add("arti_water_status", "Show the last watering result, read from game state.", (_, _) => monitor.Log(this.lastReport, LogLevel.Info));
        helper.Events.GameLoop.UpdateTicking += this.OnTick;
        helper.Events.GameLoop.ReturnedToTitle += (_, _) => this.Abort("ReturnedToTitle");
    }

    private void OnCommand(string command, string[] args)
    {
        if (args.Length != 2
            || !int.TryParse(args[0], NumberStyles.None, CultureInfo.InvariantCulture, out int x)
            || !int.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out int y)
            || x > 10000 || y > 10000)
        { this.Reject("Usage: arti_water <tileX> <tileY>"); return; }
        string status = this.TryStart(x, y);
        if (status != "Queued") this.Reject(status);
    }

    /// <summary>
    /// Jalur yang sama dengan <c>arti_water</c>. Mengembalikan <c>Queued</c>
    /// kalau lifecycle yang sudah ada menerima aksi; selain itu status
    /// penolakan persis seperti konsol, tanpa motor kedua.
    /// </summary>
    public string TryStart(int x, int y)
    {
        this.prepareOwner?.Invoke();
        if (this.consultOwner is not null)
        {
            MutationAdvice advice = this.consultOwner();
            if (!advice.AllowNewMutation)
                return advice.RejectReason ?? "RejectedBusy";
        }
        if (this.IsActive) return "RejectedBusy";
        if (!this.enabled()) return "RejectedDisabled";
        if (x < 0 || y < 0 || x > 10000 || y > 10000) return "Usage: arti_water <tileX> <tileY>";
        if (!Context.IsWorldReady || !Context.IsMultiplayer || Context.IsMainPlayer || Context.IsSplitScreen)
            return "RejectedFarmhandContext";

        this.farmer = Game1.player;
        this.location = this.farmer.currentLocation;
        this.screen = Context.ScreenId;
        this.target = new Vector2(x, y);
        this.origin = this.farmer.Position;
        this.can = this.farmer.CurrentTool as WateringCan;
        this.soil = this.location.terrainFeatures.TryGetValue(this.target, out var feature) ? feature as HoeDirt : null;
        this.beforeWater = this.can?.WaterLeft ?? 0;
        this.beforeSoil = this.soil?.state.Value;
        string status = this.action.Start(this.Observe(starting: true));
        if (status != "Queued") return status;
        this.run++;
        this.Report(status);
        return status;
    }

    private WaterObservation Observe(bool starting = false)
    {
        string? reason = null;
        if (!this.enabled()) reason = "RejectedDisabled";
        else if (!Context.IsWorldReady || !Context.IsMultiplayer || Context.IsMainPlayer || Context.IsSplitScreen
            || Context.ScreenId != this.screen || !ReferenceEquals(Game1.player, this.farmer)
            || !ReferenceEquals(this.farmer?.currentLocation, this.location)) reason = "ContextChanged";
        else if (Game1.activeClickableMenu is not null || Game1.eventUp || Game1.dialogueUp || Game1.fadeToBlack)
            reason = "RejectedMenuOrEvent";
        else if (this.otherMotorBusy()) reason = "RejectedOtherMotor";
        else if (this.farmer!.Position != this.origin) reason = "FarmerMoved";
        else if (this.can is null || this.can.GetType() != typeof(WateringCan)
            || this.can.UpgradeLevel != 0 || this.can.enchantments.Count != 0
            || !ReferenceEquals(this.farmer.CurrentTool, this.can)) reason = "RequiresEquippedBasicWateringCan";
        else if (this.soil is null || !this.location!.terrainFeatures.TryGetValue(this.target, out var current)
            || !ReferenceEquals(current, this.soil)) reason = "TargetReplacedOrNotSoil";
        else if (this.soil.state.Value is not (0 or 1)) reason = "UnsupportedSoilState";
        else if (Math.Abs(this.farmer.Tile.X - this.target.X) + Math.Abs(this.farmer.Tile.Y - this.target.Y) != 1)
            reason = "RequiresAdjacentTile";
        else if (this.location!.Objects.ContainsKey(this.target) || this.location.CanRefillWateringCanOnTile((int)this.target.X, (int)this.target.Y))
            reason = "RejectedOccupiedOrRefillTile";
        else if (starting && (this.can.WaterLeft <= 0 || this.farmer.Stamina < 2)) reason = "InsufficientWaterOrStamina";
        else if (starting && (!this.farmer.CanMove || this.farmer.UsingTool || this.farmer.isRidingHorse()))
            reason = "RejectedFarmerBusy";
        else if (this.helper.Input.IsDown(SButton.MouseLeft)
            || Game1.options.useToolButton.Any(binding => this.helper.Input.IsDown(binding.ToSButton())))
            reason = "RejectedManualToolInput";

        return new(reason, this.soil?.state.Value == 1, this.farmer?.UsingTool ?? false, this.farmer?.canReleaseTool ?? false);
    }

    private void Begin()
    {
        // Revalidate resources/readiness immediately before dispatch, not just at queue time.
        WaterObservation current = this.Observe(starting: true);
        if (current.Rejection is not null || current.Watered) throw new InvalidOperationException("PreconditionChanged");
        Farmer player = this.farmer!;
        Vector2 aim = this.target * 64f + new Vector2(32f, 32f);
        // Mirrors the public native tool lifecycle used by Game1.pressUseToolButton
        // on installed 1.6.15: zero charge, normal facing/aim, then BeginUsingTool.
        // Never call DoFunction or write soil, water, stamina, inventory or position.
        player.toolPower.Value = 0;
        player.toolHold.Value = 0;
        player.faceDirection(this.target.Y < player.Tile.Y ? 0 : this.target.X > player.Tile.X ? 1 : this.target.Y > player.Tile.Y ? 2 : 3);
        player.lastClick = aim;
        player.BeginUsingTool();
    }

    private void OnTick(object? sender, UpdateTickingEventArgs e)
    {
        // prepareOwner hanya konsumsi snapshot poller di game thread.
        // I/O control/stop tetap di poller ~50ms, bukan di jalur tool.
        this.prepareOwner?.Invoke();
        if (this.IsActive && this.consultOwner is not null)
        {
            MutationAdvice advice = this.consultOwner();
            if (advice.RequestOwnedRelease)
            {
                this.Abort(advice.ReleaseReason ?? CommandRejectReason.StopLatched.ToString());
                return;
            }
        }
        if (!this.IsActive) return;
        // Never act on another split-screen callback; context loss aborts without extra native calls.
        try { this.action.Tick(e.Ticks, () => this.Observe()); }
        catch (Exception ex)
        {
            this.action.Abort("UnknownObservationError");
            this.monitor.Log($"Water action observation failed: {ex.GetType().Name}", LogLevel.Warn);
        }
        if (!this.IsActive) this.Report(this.action.Status);
    }

    private void Abort(string reason)
    {
        if (!this.IsActive) return;
        this.action.Abort(reason);
        this.Report(reason);
    }

    private void Reject(string reason) => this.monitor.Log($"arti_water: {reason}", LogLevel.Warn);

    private void Report(string status)
    {
        this.lastReport = JsonSerializer.Serialize(new {
            action = "water_once", run_id = this.run, status,
            tile_x = (int)this.target.X, tile_y = (int)this.target.Y,
            before_soil = this.beforeSoil, observed_soil = this.soil?.state.Value,
            before_water = this.beforeWater, observed_water = this.can?.WaterLeft,
            using_tool = this.farmer?.UsingTool,
            evidence = status == "ObservedWatered" ? "same_target_dry_to_wet_and_native_tool_idle" : "not_verified"
        });
        this.monitor.Log(this.lastReport, LogLevel.Info);
        if (status != "Queued")
            this.OnTerminal?.Invoke((int)this.run, status);
    }
}
