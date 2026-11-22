using System.Runtime.InteropServices;
using HarmonyLib;
using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    NightLedger? nightLedger;
    BarrierFacts? barriers;
    bool nightEnding, nightStarted;
    long nativeDrawCount, nativeDrawMs;
    object? drawnSave;
    static ModEntry? nightObserver;
    int nightLevelIndex;
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr window);
    static object NativeDate => new { year = Game1.year, season = Game1.currentSeason, day = Game1.dayOfMonth };
    void SetUpBarriers(IModHelper helper)
    {
        nightObserver = this;
        helper.Events.GameLoop.DayEnding += (_, _) => this.NightSignal("DayEnding");
        helper.Events.GameLoop.Saving += (_, _) => this.NightSignal("Saving");
        helper.Events.GameLoop.Saved += (_, _) => this.NightSignal("Saved");
        helper.Events.GameLoop.DayStarted += (_, _) => this.NightSignal("DayStarted");
        helper.Events.Display.Rendered += (_, _) => {
            ++this.nativeDrawCount; this.nativeDrawMs = NowUnixMs(); this.uiMotorLastRender = Environment.TickCount64;
        };
        new Harmony(this.ModManifest.UniqueID + ".NightDraw").Patch(AccessTools.Method(typeof(SaveGameMenu), "draw", new[] { typeof(Microsoft.Xna.Framework.Graphics.SpriteBatch) }),
            postfix: new HarmonyMethod(typeof(ModEntry), nameof(SaveDrawn)));
    }
    static void SaveDrawn(SaveGameMenu __instance)
    {
        if (nightObserver is not { } mod || !Context.IsWorldReady || ReferenceEquals(mod.drawnSave, __instance)) return;
        mod.drawnSave = __instance; mod.Stage("night", "save_menu"); mod.Stage("night", "first_draw");
    }
    void Stage(string kind, string stage, int index = 0) =>
        this.barriers?.Stage(kind, stage, Game1.ticks, NowUnixMs(), index);
    static bool NativeSaveMenuOpen => Game1.newDay || NativeNightMenus().Any(m => m is SaveGameMenu or ShippingMenu);
    static IEnumerable<IClickableMenu> NativeNightMenus()
    {
        var seen = new HashSet<IClickableMenu>();
        for (var m = Game1.activeClickableMenu; m is not null && seen.Add(m); m = m.GetChildMenu()) yield return m;
        if (Game1.endOfNightMenus is not null)
            foreach (var m in Game1.endOfNightMenus) yield return m;
    }
    void ObserveSleepAnswer(IClickableMenu? menu, string? question)
    {
        if (menu is DialogueBox { isQuestion: true, characterDialogue: null } box
            && question == "Sleep" && box.selectedResponse >= 0
            && box.selectedResponse < box.responses.Length && box.responses[box.selectedResponse].responseKey == "Yes"
            && Game1.player.timeWentToBed.Value != 0 && this.nightLedger?.HasIntent == false)
            this.nightLedger.Intent(Game1.ticks, this.commandInbox.ExecutionSlot.ActiveAttempt?.AttemptId);
    }
    void NightSignal(string signal)
    {
        this.TickOccurrences(); // binds the same save/player history before a synchronous lifecycle edge
        var slot = this.commandInbox.ExecutionSlot;
        if (signal is "DayEnding" or "Saving") {
            slot.Cancel("save:" + signal); slot.Neutralize(NowUnixMs()); this.AbortRoutesForSave(signal);
            this.nightEnding = true; this.Stage("night", signal == "DayEnding" ? "day_ending" : "save_menu");
            this.barriers?.Close("sleep_ready");
        }
        this.padAdapter?.OnSaveEvent(signal);
        // Crop growth/death mutates terrain in place, without a terrain-list event.
        if (signal == "DayStarted") this.screens.Value.Scene.RequestSnapshot();
        // PM-1 fix10e (a14 d10:J612): the native day boundary (bed or pass-out) and the save retire every uncertain row
        // whose release receipt proves input idle, with a recorded outcome; held-input rows stay and still count.
        if (signal is "Saved" or "DayStarted") {
            int retired = this.commandInbox.RetireReleasedUncertain(CommandSettledJournal.RetiredDayBoundary);
            if (retired > 0) this.Monitor.Log($"journal retired {retired} input-released uncertain rows at {signal}", LogLevel.Info);
        }
        if (signal == "Saved") this.Stage("night", "saved"); // candidate only; save stays unknown (Q7)
        if (signal == "DayStarted" && this.nightLedger is not null) {
            this.Stage("night", "day_started"); this.nightLedger?.Commit(NativeDate, Game1.ticks, dayStarted: true);
            this.nightStarted = true; this.nightEnding = false;
        }
        this.nightLedger?.Flush(); this.occurrenceRing?.Flush();
    }
    bool NativeControlRestored => Context.CanPlayerMove && Game1.activeClickableMenu is null && !NativeSaveMenuOpen
        && Game1.newDaySync?.hasInstance() != true && !Game1.showingEndOfNightStuff && Game1.farmEvent is null
        && !Game1.game1.IsSaving && !Game1.freezeControls && !Game1.eventUp
        && !Game1.fadeToBlack && !Game1.globalFade && !Game1.isWarping && Game1.locationRequest is null;
    object NativeDayTransition() => new { showing_end_of_night_stuff = Game1.showingEndOfNightStuff,
        end_of_night_menus_pending = Game1.endOfNightMenus?.Count ?? 0,
        date = new object[] { Game1.year, Game1.currentSeason, Game1.dayOfMonth },
        new_day_sync_pending = Game1.newDaySync?.hasInstance() == true, morning_ready = this.NativeControlRestored,
        farm_event_type = Game1.farmEvent?.GetType().Name, event_up = Game1.eventUp,
        event_player_control = Game1.CurrentEvent?.playerControlSequence,
        event_id = Game1.CurrentEvent?.id, event_command = Game1.CurrentEvent?.CurrentCommand };
    void TickBarriers()
    {
        if (!Context.IsWorldReady || this.nightLedger is not { } n) return;
        var menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
        bool control = this.NativeControlRestored;
        if (Game1.paused || Game1.HostPaused) this.Stage("host_pause", "paused"); else this.barriers?.Close("host_pause");
        var ready = ReadyCheckObservation.Capture(Game1.netReady, "sleep", 0, 0, Game1.netReady.IsReady("sleep"), false);
        if (ready["local_ready"] is true && menu is ReadyCheckDialog { checkName: "sleep" })
            this.Stage("sleep_ready", ready["all_ready"] is true ? "all_ready" : "local_ready");
        else if (ready["local_ready"] is false && this.barriers?.Contains("sleep_ready") == true) {
            n.Clear("ready_cancel"); this.barriers.Close("sleep_ready");
        }
        if (Game1.isWarping || Game1.fadeToBlack || Game1.globalFade) this.Stage("warp_fade", "start");
        else if (this.barriers?.Contains("warp_fade") == true) {
            this.Stage("warp_fade", control ? "control_restored" : "location_loaded");
            if (control) this.barriers.Close("warp_fade");
        }
        if (Game1.CurrentEvent is { } evt) this.Stage("event", "command_index", evt.CurrentCommand);
        else this.barriers?.Close("event");
        if (Game1.killScreen || Game1.player.health <= 0) {
            if (n.Pending.collapse is null) n.Collapse("ko");
            this.Stage("ko_recovery", "killscreen");
        } else if (Game1.player.FarmerSprite.isPassingOut() && n.Pending.collapse is null) {
            string kind = Game1.timeOfDay >= 2600 ? "passout_0200" : "passout_same_day";
            n.Collapse(kind == "passout_0200" ? kind : "exhaustion"); this.Stage(kind, "collapse");
        }
        if (!this.nightEnding && n.Pending.collapse is "ko" or "exhaustion") {
            bool ko = n.Pending.collapse == "ko";
            string kind = ko ? "ko_recovery" : "passout_same_day";
            if (control) { this.Stage(kind, "control_restored"); this.barriers?.Close(kind); n.Recovered(Game1.ticks); }
            else if (menu is ItemListMenu && ko) this.Stage(kind, "item_list");
            else if (Game1.eventUp && ko) this.Stage(kind, "event_prose");
            else if (Game1.player.isInBed.Value) this.Stage(kind, ko ? "revive_warp" : "bed_warp");
        }
        if (Game1.showingEndOfNightStuff && n.Pending.plan is null) {
            var menus = NativeNightMenus().Distinct().ToArray();
            var levels = menus.OfType<LevelUpMenu>().Select(m => this.ReadUiKeyFacts(m).LevelUp!).Select(l => (object)new {
                skill = new[] { "farming", "fishing", "foraging", "mining", "combat", "luck" }[l.Skill],
                level = l.Level, profession = l.Level is 5 or 10 }).ToArray();
            bool shipping = menus.Any(m => m is ShippingMenu);
            n.Plan(levels, shipping, shipping ? "nested" : menus.Any(m => m is SaveGameMenu) ? "direct" : "unknown",
                this.occurrenceRing, Game1.ticks, this.commandInbox.ExecutionSlot.ModTick, NowUnixMs());
        }
        if (this.nightEnding) {
            if (menu is ShippingMenu s) {
                var f = this.ReadUiKeyFacts(s).Shipping!;
                if (f.SaveMenuActive) this.Stage("night", "save_menu");
                else if (f.Outro) { this.Stage("night", "ok_accepted"); this.Stage("night", "outro"); }
                else this.Stage("night", "shipping_summary");
            } else if (menu is SaveGameMenu) this.Stage("night", "save_menu");
        }
        if (this.nightStarted && control) {
            this.Stage("night", "control_restored");
            foreach (string kind in new[] { "night", "passout_0200", "passout_same_day", "ko_recovery" }) this.barriers?.Close(kind);
            this.nightStarted = false; this.drawnSave = null; this.nightLevelIndex = 0;
        }
        if (!n.Flush()) this.Monitor.Log("Night persistence failed; retry next tick", LogLevel.Error);
    }
    object NativeObserverFacts() => new { proposals = this.ReadNativeProposals(), input_idle = this.commandInbox.ExecutionSlot.InputIdle,
        occurrences = this.occurrenceRing?.Tail, barrier = this.barriers?.Current, night_plan = this.nightLedger?.Pending.plan is System.Text.Json.Nodes.JsonObject p ? p : null,
        nights = this.nightLedger?.Nights.ToArray(), window = new { state = Game1.game1 is null ? "unknown"
            : IsIconic(Game1.game1.Window.Handle) ? "minimized" : Game1.game1.IsActive ? "focused" : "visible_inactive" },
        draw = new { count = this.nativeDrawCount, last_ms = this.nativeDrawMs } };
}
