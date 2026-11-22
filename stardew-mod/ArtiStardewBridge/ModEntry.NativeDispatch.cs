using System.Text.Json;
using ArtiCore.Pure;
using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    VerbAdmissionFacts? ObserveNativeVerb(MenuStepCommandRequest request)
    {
        var surface = this.CaptureReceiverFacts();
        var r = request;
        string? verb = AdmissionRefusals.Verb(r);
        if (verb == "observe") return new(new("any"), surface.Epoch, true,
            Args: JsonSerializer.SerializeToElement(new { scope = "surface" }),
            Dispatch: r.VerbEnvelope is null ? r : r with { Op = "inspect_context", ExpectedLocationId = this.CurrentCommandIdentity().LocationId });
        if (!Context.IsWorldReady || Game1.player is not { } player) return null;
        var args = r.VerbEnvelope?.GetProperty("args");
        string S(string key, string fallback = "") => args is { } a && a.TryGetProperty(key, out var v) ? v.GetString()! : fallback;
        int N(string key, int fallback) => args is { } a && a.TryGetProperty(key, out var v) ? v.GetInt32() : fallback;
        string targetKind = "self", role = "", gesture = "";
        string? callback = null;
        var pins = new Dictionary<string, object?>();
        var nativeArgs = new Dictionary<string, object?>();
        if (r.VerbEnvelope is not null) {
            r = r with { Op = MenuStepCommandInbox.OpInputOnce, Family = "world_key",
                ExpectedLocationId = r.VerbEnvelope.Value.GetProperty("pins").GetProperty("location").GetString()!,
                ContextGeneration = this.CurrentCommandIdentity().ContextGeneration };
            switch (verb) {
                case "move":
                    if (S("heading") is { Length: > 0 } heading) {
                        string direction = heading switch { "n" => "up", "s" => "down", "e" => "right", "w" => "left", _ => "" };
                        if (direction.Length == 0) return null;
                        r = r with { Primitive = "move_cardinal", Direction = direction, Ticks = N("ticks", 1) };
                    } else {
                        if (this.CaptureMoveThroughTarget().Target is not { } through
                            || through.TargetToken != S("exit_id", S("target_id"))) return null;
                        r = r with { Primitive = "move_through", ThroughTarget = through, Direction = through.Direction, Ticks = through.Ticks };
                    }
                    break;
                case "use":
                    var tool = this.CurrentUseToolTarget();
                    if (tool.Slot != N("slot", -1)) return null;
                    if (BackgroundToolTileProbe.ToolTile(player) is not { } hit || !args!.Value.TryGetProperty("tile", out var tile)
                        || tile.GetProperty("x").GetInt32() != hit.X || tile.GetProperty("y").GetInt32() != hit.Y) return null;
                    r = r with { Primitive = "use_tool", ToolTarget = tool, Ticks = Math.Max(1, N("hold_ticks", 1)) }; break;
                case "select":
                    var selections = this.CaptureToolbar().Targets.Where(t => t.Target.Slot == N("slot", -1)).ToArray();
                    if (selections.Length != 1) return null;
                    r = r with { Primitive = "select_item", SelectionTarget = selections[0].Target }; break;
                case "interact":
                    if (this.CaptureBackgroundWorldInteractionTarget().Target is not { } interaction
                        || interaction.TargetToken != S("target_id") && !(S("target") == "self" && interaction.Kind == "eat")) return null;
                    r = r with { Primitive = "world_interact", InteractionTarget = interaction, Ticks = Math.Max(1, N("hold_ticks", 1)) }; break;
                case "ui":
                    var ui = this.ReadUiPadFacts();
                    if (S("gesture") == "open_menu" && (!ui.GenericHud || Game1.CurrentEvent is not null || Game1.currentMinigame is not null)) return null;
                    string receiver = surface.Top?.ReceiverId ?? "hud";
                    if (S("receiver_id") != receiver) return null;
                    string button = S("gesture") switch { "press_a" => "A", "press_b" or "close" => "B",
                        "secondary" => "X", "advance" => ui.GenericMenu ? "Start" : "A",
                        "open_menu" => "Start", "type" => "Y", "scroll" => N("notches", 0) > 0 ? "LT" : "RT", _ => "" };
                    if (button.Length == 0) return null;
                    string context = UiPadContextTable.ContextOf(ui);
                    int order = request.VerbEnvelope!.Value.GetProperty("pins").TryGetProperty("control_order", out var ordinal) ? ordinal.GetInt32() : -1;
                    int control = ui.Comps.ElementAtOrDefault(order)?.Id ?? -1;
                    if (S("control_id") is { Length: > 0 } id && !int.TryParse(id, out control)) return null;
                    if (control >= 0 && !ui.Comps.Any(c => c.Id == control && c.Visible)) return null;
                    var extras = new Dictionary<string, object>();
                    if (ui.GenericMenu && control >= 0) extras["choice"] = control.ToString();
                    if (S("gesture") == "type") extras["text"] = S("text");
                    if (N("hold_ticks", 0) > 0 && !ui.GenericMenu) return null;
                    if (N("hold_ticks", 0) > 0) extras["value"] = N("hold_ticks", 1);
                    r = r with { Primitive = "ui_press", UiPress = new(ui.ReceiverToken!, ui.MenuType, context,
                        UiPadToken.Compute(ui.ReceiverToken!, ui.MenuType, context, ui), button, control,
                        Reserved: extras.ToDictionary(p => p.Key, p => p.Value.ToString()!), ReservedTyped: extras) }; break;
                case "transfer":
                    if (Game1.activeClickableMenu is not ItemGrabMenu menu) return null;
                    var container = this.CaptureTransferFacts(menu);
                    if (!container.GetProperty("available").GetBoolean() || S("receiver_id") != surface.Top?.ReceiverId) return null;
                    var from = args!.Value.GetProperty("from");
                    string source = from.GetProperty("area").GetString()!;
                    string destination = args.Value.GetProperty("to").GetProperty("area").GetString()!;
                    bool take = source == "container";
                    if (!(take && destination == "bag" || source == "bag" && destination == "container")) return null;
                    var slot = container.GetProperty(take ? "container_slots" : "actor_slots").EnumerateArray()
                        .FirstOrDefault(s => s.GetProperty("slot").GetInt32() == from.GetProperty("slot").GetInt32());
                    if (slot.ValueKind != JsonValueKind.Object || slot.GetProperty("item").ValueKind != JsonValueKind.Object) return null;
                    var item = slot.GetProperty("item"); int stack = item.GetProperty("stack").GetInt32();
                    int qty = S("qty") == "one" ? 1 : S("qty") == "half" ? (stack + 1) / 2 : stack;
                    r = r with { Primitive = "container_transfer", TransferTarget = new(
                        container.GetProperty("receiver_token").GetString()!, container.GetProperty("container_token").GetString()!,
                        container.GetProperty("readback_token").GetString()!, container.GetProperty("source_fact_id").GetString()!,
                        slot.GetProperty("target_token").GetString()!, from.GetProperty("slot").GetInt32(),
                        item.GetProperty("qualified_item_id").GetString()!, stack, item.GetProperty("quality").GetInt32(), stack,
                        qty == stack ? null : qty, take ? "take_out" : "quick") }; break;
                case "run_controller":
                    int seconds = N("budget_s", 1);
                    var stop = args!.Value.TryGetProperty("stop", out var limits) ? limits : default;
                    int Limit(string name, int value) => stop.ValueKind == JsonValueKind.Object && stop.TryGetProperty(name, out var limit)
                        ? limit.GetInt32() : value;
                    r = r with { Primitive = "reflex_" + S("kind"), Ticks = 1 };
                    r = S("kind") switch {
                        "fish" => r with { ReflexFish = FishReflexArgs.Default with { MaxSeconds = seconds } },
                        "combat" => r with { ReflexCombat = CombatReflexArgs.Default with { MaxSeconds = seconds, Retreat = S("mode") == "retreat",
                            HpFloorPct = Limit("hp_floor_pct", CombatReflexArgs.Default.HpFloorPct), MaxKills = Limit("max_kills", CombatReflexArgs.Default.MaxKills) } },
                        "charge" => r with { ReflexCharge = ReflexChargeTarget.ToArgs(Limit("power", 1), Math.Min(120, seconds * 60)) }, _ => r };
                    break;
                default: return null;
            }
        }
        switch (verb) {
            case "move":
                if (r.ThroughTarget is { } through) {
                    if (this.CaptureMoveThroughTarget().Target is not { } observed || !observed.Matches(through)) return null;
                    targetKind = through.Kind == "bed" ? "bed" : "door_warp";
                    nativeArgs["exit_id"] = through.TargetToken; pins["target_instance"] = through.SourceFactId;
                } else { nativeArgs["heading"] = r.Direction; nativeArgs["ticks"] = r.Ticks; }
                break;
            case "use":
                var current = this.CurrentUseToolTarget();
                if (r.ToolTarget is { } wanted && wanted != current) return null;
                if (BackgroundToolTileProbe.ToolTile(player) is not { } hit) return null;
                targetKind = "tile"; nativeArgs["slot"] = current.Slot;
                nativeArgs["tile"] = new { x = hit.X, y = hit.Y }; nativeArgs["hold_ticks"] = r.Ticks;
                pins["target_instance"] = $"tile:{player.currentLocation.NameOrUniqueName}:{hit.X},{hit.Y}";
                break;
            case "select":
                if (r.SelectionTarget is not { } selected || !this.CaptureToolbar().Targets.Any(t => t.Target == selected)) return null;
                nativeArgs["slot"] = selected.Slot; break;
            case "interact":
                if (r.InteractionTarget is not { } target) return null;
                if (EventControlAction.IsRoute(target.Kind))
                {
                    if (!EventApproachPlan.Matches(this, target)) return null;
                }
                else if (this.CaptureBackgroundWorldInteractionTarget(target.Kind).Target != target) return null;
                targetKind = request.VerbEnvelope is null && (target.Kind == AnimalHouseRoute.Kind || EventControlAction.IsRoute(target.Kind))
                    ? "legacy_compat:route" : target.Kind switch { "door" => "door_warp", "eat" => "self", _ => target.Kind };
                pins["target_instance"] = target.SourceFactId; nativeArgs["target_id"] = target.TargetToken; break;
            case "transfer":
                if (r.TransferTarget is not { } transfer) return null;
                role = transfer.Mode?.StartsWith("ship") == true ? "shipping_bin" : "container";
                nativeArgs["receiver_id"] = surface.Top?.ReceiverId; break;
            case "ui":
                var ui = this.ReadUiPadFacts();
                string direction = r.UiNav?.Dir ?? (r.Op == "menu_step" ? r.Direction : "");
                gesture = direction.Length > 0 ? "nav_dir:" + direction : r.PadButton?.Button == "Start"
                    || ui.GenericHud && r.UiPress?.Button == "Start" ? "open_menu"
                    : r.UiKeyTarget?.Key is "advance" || r.UiPress?.Button == "Start" ? "advance"
                    : r.UiPress?.Button == "Y" ? "type" : r.UiPress?.Button is "LT" or "RT" ? "scroll"
                    : r.ExitTarget is not null || r.UiPress?.Button == "B" ? "close" : "press_a";
                // ADR-23 legacy_compat: directional forms never enter the frozen v2 catalogue.
                role = direction.Length > 0 ? "legacy_compat:" + gesture : gesture == "close" ? "close"
                    : surface.Top?.Instance is ShippingMenu ? "shipping_ok"
                    : surface.Top?.Instance is LevelUpMenu level ? level.isProfessionChooser ? "profession" : "levelup_ok"
                    : gesture is "open_menu" or "type" or "scroll" ? gesture : surface.Top?.Instance is ItemListMenu ? "item_list_ok"
                    : surface.Top?.Instance is LetterViewerMenu letter ? letter.HasQuestOrSpecialOrder ? "letter_quest_close" : "close" : "advance";
                if (r.UiPress is { Button: "A" or "X" } press && surface.Top?.Instance is ShopMenu shop
                    && this.BeginNativeTrade(GenericInspect(shop).Widgets.SingleOrDefault(w => w.Id == press.SnappedId)) is { Expected: { } expectedTrade })
                    role = expectedTrade.Kind == EconomyExpect.Sell ? "shop_sell" : "shop_buy";
                if (surface.Top?.Instance is DialogueBox { isQuestion: true }) callback = Game1.currentLocation.lastQuestionKey switch {
                    "Sleep" => "sleep", "Eat" => "eat", _ => Game1.CurrentEvent is not null ? "event_question" : "unknown_callback" };
                pins["control_order"] = Math.Max(0, ui.Comps.ToList().FindIndex(c => c.Id == (r.UiPress?.SnappedId ?? ui.SnappedId)));
                pins["control_digest"] = UiPadToken.Compute(ui.ReceiverToken ?? "hud", ui.MenuType, UiPadContextTable.ContextOf(ui), ui);
                nativeArgs["receiver_id"] = surface.Top?.ReceiverId ?? "hud"; nativeArgs["gesture"] = gesture;
                break;
            case "run_controller":
                string kind = r.ReflexFish is not null ? "fish" : r.ReflexCombat is not null ? "combat" : "charge";
                nativeArgs["kind"] = kind; pins["controller_kind"] = kind; break;
            default: return null;
        }
        var held = r.TransferTarget is { } transferItem
            ? (transferItem.Mode == "take_out" && Game1.activeClickableMenu is ItemGrabMenu sourceMenu
                ? sourceMenu.ItemsToGrabMenu.actualInventory : player.Items).ElementAtOrDefault(transferItem.Slot)
            : r.SelectionTarget is { } selection ? player.Items[selection.Slot] : player.CurrentItem;
        pins["item_binding"] = new { item_id = held?.QualifiedItemId ?? "none", qty = held?.Stack ?? 0, quality = held?.Quality ?? 0 };
        if (player.CurrentTool is { } boundTool) pins["tool_binding"] = Observation.InventorySnapshotCapture.ToolToken(boundTool);
        // Crossings (walk-onto or door press) run on the seat path (TryArmCore / TryArmHoldCore), so Q14 is the seat's proof.
        bool q14 = !this.config.GamepadInputEnabled || MenuStepCommandInbox.IsSeatCrossing(r) || verb is "observe" or "select" or "ui" or "transfer";
        bool barrier = targetKind is "bed" or "door_warp" || r.InteractionTarget?.Kind == AnimalHouseRoute.Kind || role == "shipping_ok"
            || callback is "sleep" or "event_question" or "unknown_callback";
        return new(new(targetKind, player.CurrentTool is not null, HeldGiftable: held?.canBeGivenAsGift() == true,
            ControlRole: role, CallbackClass: callback), surface.Epoch, q14,
            JsonSerializer.SerializeToElement(pins), args ?? JsonSerializer.SerializeToElement(nativeArgs), r, barrier,
            role == "shop_buy" ? "shop_row" : role == "shop_sell" ? "item_slot" : callback is not null ? "response"
                : role.EndsWith("_ok") ? "ok" : role == "advance" ? "other"
                : surface.Top?.Instance is LetterViewerMenu && gesture == "press_a" ? "ok" : role);
    }
}
