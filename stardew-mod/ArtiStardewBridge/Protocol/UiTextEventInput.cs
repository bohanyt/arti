using System.Globalization;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G3 (PLAN 1.2 rows NamingMenu / TextBox / NumberSelectionMenu, event, ReadyCheckDialog, chat;
// 6.1 D3-D7; WS-G3 brief): text / number entry, event dialogue + allowlisted event questions, ReadyCheck policy,
// festival direct entries and the gated chat send. Rides the WS-G1 ui_press family (no new inbox family): the
// payload is a ui_press reserved arg (text / value / choice / festival + slot + item), consumed ONLY by the G3
// contexts below; every other context keeps refusing reserved args. chat_send is its own world_key primitive
// (ChatSendTarget) because the chat box is not a menu.
// Flag TextEventMenusEnabled (default OFF, needs UiPadMenusEnabled). Flag off = UiPadFacts.G3 is null and every
// table / token / snapshot below is byte-identical to WS-G1. SMAPI-free; ModEntry.UiG3.cs reads the game.
// Decomp refs: TextBox.cs:191-266 (RecieveTextInput / RecieveCommandInput), NamingMenu.cs:85 (textBoxEnter),
// NumberSelectionMenu.cs:159-232, ReadyCheckDialog.cs:6-80, Event.cs:10862 (skipEvent), 12806-13056
// (answerDialogue), DialogueBox.cs:355-400, ChatBox.cs:110 (textBoxEnter(string)).
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the G3 live witnesses.

public static class UiG3Table
{
    public const string CtxNaming = "naming", CtxNumber = "number", CtxReadyCheck = "ready_check";

    public const string ArgText = "text", ArgValue = "value", ArgChoice = "choice", ArgFestival = "festival",
        ArgSlot = "slot", ArgItem = "item";

    /// <summary>Reserved ui_press args G3 consumes (added to UiPadTable.ReservedArgs).</summary>
    public static readonly IReadOnlyList<string> ReservedArgs = new[] { ArgChoice, ArgFestival, ArgSlot, ArgItem };

    /// <summary>Owner-supplied names: NamingMenu has no textLimit (TextBox.limitWidth trims by font width instead).</summary>
    public const int NameMaxLength = 12;
    public const int ChatMaxLength = 100;
    public const int MaxChoices = 8;

    // G3 actions (the single G3 step of a plan; executed by UiG3Executor, never by the G1 mirror executor).
    public const string ActTextEnter = "text_enter", ActNumberOk = "number_ok", ActNumberStep = "number_step",
        ActNumberCancel = "number_cancel", ActEventAdvance = "event_advance", ActEventAnswer = "event_answer",
        ActReadyConfirm = "ready_confirm", ActReadyCancel = "ready_cancel", ActFestivalSlot = "festival_slot";

    // Festival direct entries (festival_action{kind} = ui_press A {festival: kind[, slot, item]}).
    public const string FestFortune = "fortune", FestSecretSanta = "secret_santa_gift", FestLuauSoup = "luau_soup",
        FestGrange = "grange_place", FestStarBet = "star_tokens_bet", FestStarBuy = "star_tokens_buy";

    public static readonly IReadOnlyList<string> FestivalKinds = new[]
        { FestFortune, FestSecretSanta, FestLuauSoup, FestGrange, FestStarBet, FestStarBuy };

    /// <summary>Menu-typed G3 contexts (checked before eventUp: a NamingMenu / NumberSelectionMenu / ReadyCheck can
    /// open during an event or festival, Event.cs:13042 / 12975 / 12099).</summary>
    public static string? MenuContext(string menuType) => menuType switch
    {
        "NamingMenu" => CtxNaming,
        "NumberSelectionMenu" => CtxNumber,
        "ReadyCheckDialog" => CtxReadyCheck,
        _ => null,
    };

    public static bool IsG3Context(string context) =>
        context is CtxNaming or CtxNumber or CtxReadyCheck or UiPadTable.CtxEvent;

    /// <summary>Reserved args each G3 context consumes (anything else is refused before any call).</summary>
    public static IReadOnlyCollection<string> ArgsFor(string context) => context switch
    {
        CtxNaming => new[] { UiPadTable.ReservedText },
        CtxNumber => new[] { UiPadTable.ReservedValue },
        UiPadTable.CtxEvent => new[] { ArgChoice, ArgFestival, ArgSlot, ArgItem },
        _ => Array.Empty<string>(),
    };

    static string Deny(string why) => UiPadTable.Denied + ":" + why;
    static string None(string why) => UiPadTable.NoMeaning + ":" + why;

    static UiPadPlan G3(string button, string does, string action, string? arg = null) =>
        UiPadPlan.Ok(button, does, new UiPadStep(UiPadStepKind.G3, button, G3Action: action, G3Arg: arg));

    /// <summary>
    /// The G3 cell for <paramref name="button"/>. <paramref name="reserved"/> null = describe (ui_inspect row: the
    /// payload is named, not required); non-null = execution (a missing payload is refused, zero calls).
    /// </summary>
    public static UiPadPlan Translate(string button, UiPadFacts f, string context, IReadOnlyDictionary<string, string>? reserved)
    {
        UiG3Facts g = f.G3!;
        if (reserved is not null)
            foreach (string key in reserved.Keys)
                if (!ArgsFor(context).Contains(key, StringComparer.Ordinal))
                    return UiPadPlan.No(button, Deny("reserved_arg_g2_g3:" + key));
        return context switch
        {
            CtxNaming => Naming(button, g, reserved),
            CtxNumber => Number(button, g, reserved),
            CtxReadyCheck => ReadyCheck(button, g),
            _ => Event(button, f, g, reserved),
        };
    }

    // ------------------------------------------------------------------ naming / text box

    static UiPadPlan Naming(string button, UiG3Facts g, IReadOnlyDictionary<string, string>? reserved)
    {
        if (button != UiPadTable.BtnA)
            return UiPadPlan.No(button, None("naming"));
        if (g.Naming is not { } box)
            return UiPadPlan.No(button, Deny("naming_facts_unavailable"));
        if (box.KeyboardOpen)
            return UiPadPlan.No(button, Deny("on_screen_keyboard_open"));
        if (box.SubscriberForeign)
            // Another text box (e.g. the chat box) owns the keyboard: never steal it.
            return UiPadPlan.No(button, Deny("subscriber_foreign"));
        if (reserved is null)
            return G3(button, "enter the name {text} then OK", ActTextEnter);
        if (!reserved.TryGetValue(UiPadTable.ReservedText, out string? text))
            return UiPadPlan.No(button, Deny("text_required"));
        if (UiG3Policy.NameRefusal(text, box.MinLength, box.TextLimit) is { } why)
            return UiPadPlan.No(button, Deny(why));
        return G3(button, "enter the name then OK", ActTextEnter, text);
    }

    // ------------------------------------------------------------------ number selection

    static UiPadPlan Number(string button, UiG3Facts g, IReadOnlyDictionary<string, string>? reserved)
    {
        if (g.Number is not { } n)
            return UiPadPlan.No(button, Deny("number_facts_unavailable"));
        if (n.KeyboardOpen)
            return UiPadPlan.No(button, Deny("on_screen_keyboard_open"));
        if (n.MouseOverBox && button is UiPadTable.BtnA or UiPadTable.BtnB)
            // receiveLeftClick ends with numberSelectedBox.Update(): with the real mouse over the box in gamepad
            // mode it opens the on-screen keyboard (TextBox.cs:128-147). The farmhand cursor cannot be moved.
            return UiPadPlan.No(button, Deny("mouse_over_number_box"));
        switch (button)
        {
            case UiPadTable.BtnA:
            {
                if (reserved is null)
                    return G3(button, "set {value} (optional) then OK", ActNumberOk);
                string? raw = reserved.TryGetValue(UiPadTable.ReservedValue, out string? v) ? v : null;
                UiG3NumberPlan plan = UiG3Policy.Number(raw, n);
                return plan.Refusal is { } why ? UiPadPlan.No(button, Deny(why))
                    : G3(button, "set the number then OK", ActNumberOk, plan.Value.ToString(CultureInfo.InvariantCulture));
            }
            case UiPadTable.BtnLT:
            case UiPadTable.BtnRT:
            {
                int target = n.Current + (button == UiPadTable.BtnRT ? 1 : -1);
                UiG3NumberPlan plan = UiG3Policy.Number(target.ToString(CultureInfo.InvariantCulture), n);
                if (plan.Refusal is { } why)
                    return UiPadPlan.No(button, Deny(why));
                if (plan.Value == n.Current)
                    return UiPadPlan.No(button, None("number_at_bound"));
                return G3(button, button == UiPadTable.BtnRT ? "number +1" : "number -1", ActNumberStep,
                    plan.Value.ToString(CultureInfo.InvariantCulture));
            }
            case UiPadTable.BtnB:
                return G3(button, "cancel", ActNumberCancel);
            default:
                return UiPadPlan.No(button, None("number"));
        }
    }

    // ------------------------------------------------------------------ ready check (owner D4)

    static UiPadPlan ReadyCheck(string button, UiG3Facts g)
    {
        if (g.ReadyCheck is not { } r)
            return UiPadPlan.No(button, Deny("ready_check_facts_unavailable"));
        UiG3ReadyDecision d = UiG3Policy.ReadyCheck(r, g.FestivalGoal);
        return button switch
        {
            UiPadTable.BtnA => d.ConfirmRefusal is { } why ? UiPadPlan.No(button, Deny(why))
                : G3(button, "confirm ready (the dialog already readies the local player each tick)", ActReadyConfirm),
            UiPadTable.BtnB => r.Cancelable ? G3(button, "cancel readiness and close waiting dialog", ActReadyCancel)
                : UiPadPlan.No(button, Deny("ready_check_not_cancelable")),
            _ => UiPadPlan.No(button, None("ready_check")),
        };
    }

    // ------------------------------------------------------------------ event / festival

    static UiPadPlan Event(string button, UiPadFacts f, UiG3Facts g, IReadOnlyDictionary<string, string>? reserved)
    {
        if (button == UiPadTable.BtnSelect)
            return UiPadPlan.No(button, Deny("event_skip_denied")); // owner D6: never skip (Event.cs:10862)
        if (f.MinigameActive)
            return UiPadPlan.No(button, Deny("minigame_active"));
        if (g.Event is not { } ev)
            return UiPadPlan.No(button, Deny("event_facts_unavailable"));
        if (button != UiPadTable.BtnA)
            return UiPadPlan.No(button, None("event"));
        string? festival = reserved is not null && reserved.TryGetValue(ArgFestival, out string? k) ? k : null;
        if (festival is not null)
            return Festival(button, f, g, ev, festival, reserved!);
        if (reserved is not null && (reserved.ContainsKey(ArgSlot) || reserved.ContainsKey(ArgItem)))
            return UiPadPlan.No(button, Deny("slot_item_need_festival"));
        if (f.MenuType != "DialogueBox" || ev.Dialogue is not { } d)
            return UiPadPlan.No(button, Deny(g.FestivalMenu is not null ? "festival_menu_needs_festival_kind" : "event_menu_unsupported"));
        if (!d.Ready)
            return UiPadPlan.No(button, Deny("not_ready:" + (d.NotReadyReason ?? "unknown")));
        string? choiceRaw = reserved is not null && reserved.TryGetValue(ArgChoice, out string? c) ? c : null;
        if (!d.IsQuestion)
            return choiceRaw is not null ? UiPadPlan.No(button, Deny("choice_without_question"))
                : G3(button, "advance the event dialogue", ActEventAdvance);
        UiG3QuestionClass cls = UiG3Policy.EventQuestion(d);
        if (!cls.Allowed)
            return UiPadPlan.No(button, Deny("event_question_refused:" + cls.Class));
        if (reserved is null)
            return G3(button, "answer the event question ({choice} index, else the snapped response)", ActEventAnswer);
        int index;
        if (choiceRaw is not null)
        {
            if (!UiG3Policy.TryIndex(choiceRaw, out index) || index >= d.Choices.Count)
                return UiPadPlan.No(button, Deny("choice_out_of_range"));
        }
        else if (f.SnappedId is int snapped && snapped >= 0 && snapped < d.Choices.Count)
            index = snapped;
        else
            return UiPadPlan.No(button, Deny("nothing_snapped"));
        if (UiG3Policy.QuickBranchRefusal(d, index) is { } branch)
            return UiPadPlan.No(button, Deny(branch));
        return G3(button, "answer the event question", ActEventAnswer, index.ToString(CultureInfo.InvariantCulture));
    }

    static UiPadPlan Festival(string button, UiPadFacts f, UiG3Facts g, UiEventFacts ev, string kind,
        IReadOnlyDictionary<string, string> reserved)
    {
        if (!FestivalKinds.Contains(kind, StringComparer.Ordinal))
            return UiPadPlan.No(button, Deny("festival_kind_unknown"));
        if (kind is FestStarBet or FestStarBuy)
            return UiPadPlan.No(button, Deny("betting_denied")); // betStarTokens / buyStarTokens: refused by default
        if (kind == FestGrange)
            // The grange is a StorageContainer drag (pick, place one, return the rest: Event.cs:12178-12210); P2 tail.
            return UiPadPlan.No(button, Deny("grange_deferred_p2"));
        if (!ev.Festival)
            return UiPadPlan.No(button, Deny("not_festival"));
        if (reserved.ContainsKey(ArgChoice))
            return UiPadPlan.No(button, Deny("festival_choice_fixed"));
        // Festival questions: the festival kind answers its own question key with Yes (index 0) under the policy.
        if (f.MenuType == "DialogueBox" && ev.Dialogue is { IsQuestion: true } d)
        {
            if (reserved.ContainsKey(ArgSlot) || reserved.ContainsKey(ArgItem))
                return UiPadPlan.No(button, Deny("slot_item_on_question"));
            if (!d.Ready)
                return UiPadPlan.No(button, Deny("not_ready:" + (d.NotReadyReason ?? "unknown")));
            if (d.AfterQuestionCallback || d.NpcQuestion)
                return UiPadPlan.No(button, Deny("festival_question_mismatch"));
            string? expected = kind switch { FestFortune => "fortuneTeller", FestSecretSanta => "secretSanta", _ => null };
            if (expected is null || d.QuestionKey != expected)
                return UiPadPlan.No(button, Deny("festival_question_mismatch"));
            if (kind == FestFortune)
            {
                if (g.FortuneReadThisYear)
                    return UiPadPlan.No(button, Deny("fortune_already_read"));
                if (g.Money < UiG3Policy.FortunePrice)
                    return UiPadPlan.No(button, Deny("money_short"));
            }
            if (d.Choices.Count < 1)
                return UiPadPlan.No(button, Deny("choice_out_of_range"));
            return G3(button, kind == FestFortune ? "pay 100g for the fortune (Yes)" : "give a secret gift (Yes)", ActEventAnswer, "0");
        }
        // Festival item menus (ItemGrabMenu with the event's own behaviour): one click on the owner-pinned slot.
        string? menuKind = g.FestivalMenu;
        if (menuKind is null || menuKind != kind || kind is not (FestSecretSanta or FestLuauSoup))
            return UiPadPlan.No(button, Deny("festival_menu_mismatch"));
        if (!reserved.TryGetValue(ArgSlot, out string? slotRaw) || !UiG3Policy.TryIndex(slotRaw, out int slot)
            || !reserved.TryGetValue(ArgItem, out string? item) || string.IsNullOrWhiteSpace(item))
            return UiPadPlan.No(button, Deny("slot_item_required"));
        if (slot >= g.Inventory.Count || g.Inventory[slot] is not { } held)
            return UiPadPlan.No(button, Deny("slot_empty"));
        if (!string.Equals(held.QualifiedItemId, item, StringComparison.Ordinal))
            return UiPadPlan.No(button, Deny("slot_item_mismatch"));
        if (!held.Highlighted)
            return UiPadPlan.No(button, Deny("item_not_accepted_here"));
        if (!g.ItemAllowlist.Contains(item, StringComparer.Ordinal))
            return UiPadPlan.No(button, Deny("item_policy")); // owner D10: none until the owner names the item
        return G3(button, "offer the pinned slot item", ActFestivalSlot, slot.ToString(CultureInfo.InvariantCulture));
    }
}

// ---------------------------------------------------------------------- FIXB31 event skip (owner decision 2026-10-12)

/// <summary>Read-only facts of Game1.CurrentEvent for the skip policy (sampled on the game thread).</summary>
public sealed record EventSkipFacts(bool EventUp, bool FarmEvent, bool Minigame, bool Festival, bool Wedding, bool Skippable,
    bool Skipped, bool PlayerControl, bool MenuOpen, string? MenuType, bool QuestionPending);

/// <summary>
/// FIXB31: when ARTI may skip the current event (pad_button Select = the vanilla gamepad Back branch, Game1.cs:4859 /
/// 13414). Only a plain cutscene: never a festival, a wedding, a player-control sequence, a minigame, a farm (night)
/// event, an event the game marks unskippable, or while a question / any non-dialogue menu is pending (owner D6 stays
/// for those). Pure.
/// </summary>
public static class EventSkipPolicy
{
    public const string Detail = PadContextTable.EventSkipDetail;

    /// <summary>The refusal, or null when the skip is allowed.</summary>
    public static string? Refusal(EventSkipFacts f)
    {
        if (!f.EventUp) return "no_event";
        if (f.FarmEvent) return "farm_event";
        if (f.Minigame) return "minigame";
        if (f.Festival) return "festival";
        if (f.Wedding) return "wedding";
        if (f.PlayerControl) return "player_control";
        if (f.QuestionPending) return "question_pending";
        if (f.MenuOpen && f.MenuType != "DialogueBox") return "menu_open:" + (f.MenuType ?? "unknown");
        if (!f.Skippable) return "not_skippable";
        if (f.Skipped) return "already_skipped";
        return null;
    }
}

// ---------------------------------------------------------------------- facts (read by ModEntry.UiG3.cs)

/// <summary>NamingMenu text box (TextBox.cs; NamingMenu.cs:28-74).</summary>
public sealed record UiTextBoxFacts(string Title, string Text, bool Selected, bool SubscriberIsBox, bool SubscriberForeign,
    int TextLimit = -1, bool NumbersOnly = false, int MinLength = 1, bool KeyboardOpen = false);

/// <summary>NumberSelectionMenu (protected fields read by reflection, NumberSelectionMenu.cs:20-36).</summary>
public sealed record UiNumberFacts(string Message, int Min, int Max, int Price, int Current, int Money, string BoxText,
    bool KeyboardOpen = false, bool MouseOverBox = false);

/// <summary>ReadyCheckDialog (ReadyCheckDialog.cs:6-80; Game1.netReady).</summary>
public sealed record UiReadyCheckFacts(string CheckName, int ReadyCount, int Needed, bool Cancelable, bool InitiatedByArti);

/// <summary>Native festival command provenance, bound to the exact dialogue receiver.</summary>
public sealed record FestivalDialogueOwner(string LifecycleId, int CommandIndex, string CommandHash, string DialogueId);

/// <summary>The event DialogueBox (ui_key's own dialogue facts) + where an answer goes.</summary>
/// <param name="QuickBranches">quickQuestion only: the "(break)" branch each answer splices into the script
/// (Event.cs:12817-12823; index = answer), read from eventCommands[CurrentCommand]; null when unreadable.</param>
public sealed record UiEventDialogueFacts(bool IsQuestion, bool NpcQuestion, string? QuestionKey, bool AfterQuestionCallback,
    bool Ready, string? NotReadyReason, IReadOnlyList<string> Choices, string Text, bool Transitioning, int DialoguesCount,
    int BrokenUpCount, int SelectedResponse, IReadOnlyList<string>? QuickBranches = null,
    IReadOnlyList<string?>? ResponseKeys = null, string? NpcIdentity = null, string? QuestionIdentity = null,
    int NpcDialogueIndex = -1, FestivalDialogueOwner? FestivalOwner = null);

/// <summary>Game1.CurrentEvent (Event.cs:3776-3844).</summary>
public sealed record UiEventFacts(string EventId, bool Festival, string? FestivalId, string? Command, bool Skippable,
    bool PlayerControl, int PreviousAnswer, UiEventDialogueFacts? Dialogue = null, int CommandIndex = -1,
    string? EventInstance = null);

public sealed record UiSlotItem(string QualifiedItemId, int Stack, bool Highlighted);

public sealed record UiG3Facts(
    bool Enabled,
    UiTextBoxFacts? Naming = null,
    UiNumberFacts? Number = null,
    UiReadyCheckFacts? ReadyCheck = null,
    UiEventFacts? Event = null,
    string? FestivalMenu = null,
    int Money = 0,
    bool FortuneReadThisYear = false,
    IReadOnlyList<UiSlotItem?>? InventorySlots = null,
    IReadOnlyList<string>? ItemAllowlistIds = null,
    bool FestivalGoal = false)
{
    public IReadOnlyList<UiSlotItem?> Inventory => this.InventorySlots ?? Array.Empty<UiSlotItem?>();

    public IReadOnlyList<string> ItemAllowlist => this.ItemAllowlistIds ?? Array.Empty<string>();

    /// <summary>Binding digest of the G3 state (ready counts excluded: they move while ARTI only waits).</summary>
    public string Digest()
    {
        StringBuilder t = new("g3");
        if (this.Naming is { } n)
            t.Append("|n=").Append(n.Text).Append(',').Append(n.Selected ? 1 : 0).Append(',').Append(n.SubscriberIsBox ? 1 : 0);
        if (this.Number is { } m)
            t.Append("|m=").Append(m.Current).Append(',').Append(m.Min).Append(',').Append(m.Max).Append(',').Append(m.Price);
        if (this.ReadyCheck is { } r)
            t.Append("|r=").Append(r.CheckName).Append(',').Append(r.Cancelable ? 1 : 0).Append(',').Append(r.InitiatedByArti ? 1 : 0);
        if (this.Event is { } e)
        {
            t.Append("|e=").Append(e.EventId).Append(',').Append(e.Command).Append(',').Append(e.CommandIndex)
                .Append(',').Append(e.Festival).Append(',').Append(e.PlayerControl).Append(',').Append(e.PreviousAnswer);
            t.Append("|event_instance=").Append(System.Text.Json.JsonSerializer.Serialize(e.EventInstance));
            if (e.Dialogue is { } d)
                t.Append("|d=").Append(d.IsQuestion ? 1 : 0).Append(',').Append(d.QuestionKey).Append(',')
                    .Append(UiKeyTargetToken.Hash(d.Text)[..16]).Append(',').Append(d.DialoguesCount).Append(',')
                    .Append(d.BrokenUpCount).Append(',').Append(string.Join("\u001f", d.Choices))
                    .Append(',').Append(d.Ready).Append(',').Append(d.Transitioning).Append(',').Append(d.NpcQuestion)
                    .Append(',').Append(d.AfterQuestionCallback).Append(',').Append(d.NotReadyReason)
                    .Append(',').Append(d.QuickBranches is null ? "-" : string.Join("\u001f", d.QuickBranches))
                    // JSON preserves exact native keys, their order and boundaries (no label trimming).
                    .Append("|question=").Append(JsonSerializer.Serialize(new
                    {
                        d.ResponseKeys, d.NpcIdentity, d.QuestionIdentity, d.NpcDialogueIndex, d.FestivalOwner,
                    }));
        }
        if (this.FestivalMenu is { } fm)
        {
            t.Append("|f=").Append(fm);
            foreach (UiSlotItem? s in this.Inventory)
                t.Append(',').Append(s?.QualifiedItemId).Append('x').Append(s?.Stack ?? 0);
        }
        return t.ToString();
    }
}

// ---------------------------------------------------------------------- policies (pure)

public readonly record struct UiG3NumberPlan(int Value, bool Clamped, string? Refusal);

public readonly record struct UiG3QuestionClass(bool Allowed, string Class);

public readonly record struct UiG3ReadyDecision(string Kind, string Mode, string? ConfirmRefusal);

public static class UiG3Policy
{
    public const int FortunePrice = 100; // Event.cs:12909-12916 (Money -= 100, mail "fortuneTeller"+year)

    public static bool TryIndex(string raw, out int index) =>
        int.TryParse(raw, NumberStyles.None, CultureInfo.InvariantCulture, out index) && index >= 0 && index < 1000;

    /// <summary>Owner-supplied name: 1..12 chars (and the box's textLimit / minLength), letters / digits / space / ' - .</summary>
    public static string? NameRefusal(string? text, int minLength, int textLimit)
    {
        if (string.IsNullOrEmpty(text))
            return "text_required";
        if (text.Length > UiG3Table.NameMaxLength || (textLimit > 0 && text.Length > textLimit))
            return "text_too_long";
        if (text.Length < Math.Max(1, minLength))
            return "text_too_short";
        if (text.Trim() != text || text.Contains("  ", StringComparison.Ordinal))
            return "text_whitespace";
        foreach (char c in text)
            if (!(char.IsLetterOrDigit(c) || c is ' ' or '\'' or '-' or '.'))
                return "text_charset";
        return null;
    }

    /// <summary>Chat text (owner D5): plain text only, never a "/" command (ChatBox.cs:110-123 runs commands).</summary>
    public static string? ChatRefusal(string? text)
    {
        if (string.IsNullOrEmpty(text))
            return "text_required";
        if (text.Length > UiG3Table.ChatMaxLength)
            return "text_too_long";
        if (text.Trim() != text)
            return "text_whitespace";
        if (text[0] == '/')
            return "chat_command_denied";
        foreach (char c in text)
            if (char.IsControl(c) || char.IsSurrogate(c))
                return "text_charset";
        return null;
    }

    /// <summary>
    /// The number to set: parsed (digits only), clamped to [min, max] and to what the money buys (price &gt; 0:
    /// the OK click itself never checks money, NumberSelectionMenu.cs:183-192). Null raw = keep the current value.
    /// </summary>
    public static UiG3NumberPlan Number(string? raw, UiNumberFacts n)
    {
        int value;
        if (raw is null)
            value = n.Current;
        else if (!int.TryParse(raw, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out value))
            return new UiG3NumberPlan(0, false, "value_not_integer");
        if (n.Min > n.Max)
            return new UiG3NumberPlan(0, false, "number_range_invalid");
        int max = n.Max;
        if (n.Price > 0)
        {
            int affordable = n.Money / n.Price;
            if (affordable < n.Min)
                return new UiG3NumberPlan(0, false, "unaffordable");
            max = Math.Min(max, affordable);
        }
        int clamped = Math.Clamp(value, n.Min, max);
        return new UiG3NumberPlan(clamped, clamped != value, null);
    }

    public static string ReadyCheckKind(string checkName) => checkName switch
    {
        "sleep" => "sleep",
        "festivalStart" or "festivalEnd" => "festival",
        _ when checkName.StartsWith("MainEvent_", StringComparison.Ordinal) => "main_event",
        _ => "event_command",
    };

    /// <summary>
    /// Owner D4: ARTI confirms only a sleep check it started itself (the Sleep question it answered Yes);
    /// festival start/end and main events only under an owner festival goal; everything else is wait-only.
    /// ADR 64: cancel follows native cancelability. The dialog readies the local player every update anyway
    /// (ReadyCheckDialog.cs:73, inactive window included): ARTI is ready on ANY check shown, the D4 confirm adds
    /// nothing and "wait" does not mean "not ready". The real protection is refusing the questions that open those
    /// dialogs (Sleep only under the owner's sleep objective; festival keys refused / festival_action only).
    /// </summary>
    public static UiG3ReadyDecision ReadyCheck(UiReadyCheckFacts r, bool festivalGoal)
    {
        string kind = ReadyCheckKind(r.CheckName);
        string? refusal = kind switch
        {
            "sleep" => r.InitiatedByArti ? null : "ready_check_wait_only:sleep_not_started_by_arti",
            "festival" or "main_event" => festivalGoal ? null : "ready_check_wait_only:festival_goal_required",
            _ => "ready_check_wait_only:event_ready_check",
        };
        return new UiG3ReadyDecision(kind, refusal is null ? "confirm" : "wait", refusal);
    }

    static readonly Dictionary<string, (bool Allowed, string Class)> EventKeys = new(StringComparer.Ordinal)
    {
        ["quickQuestion"] = (true, "script_branch"),       // Event.cs:12817-12823 splices the chosen answer's commands
        ["null"] = (true, "no_op_key"),                    // event-script convention: only previousAnswerChoice is set
        ["shaneCliffs"] = (true, "scene_branch"),          // 12837: rewrites Shane's next line
        ["haleyDarkRoom"] = (true, "scene_branch"),        // 12885: fork decorate / leave
        ["bandFork"] = (true, "scene_branch"),             // 12951: which song
        ["chooseCharacter"] = (true, "scene_branch"),      // 13013: warrior / healer scene fork
        ["spirits_eve_shortcut"] = (true, "festival_shortcut"), // 12987: in-festival warp
        ["fortuneTeller"] = (false, "festival_action_only"),     // 12909: costs 100g -> festival_action{fortune}
        ["secretSanta"] = (false, "festival_action_only"),       // 12871: gift menu -> festival_action
        ["fishingGame"] = (false, "timed_minigame"),
        ["slingshotGame"] = (false, "timed_minigame"),
        ["wheelBet"] = (false, "betting_denied"),
        ["StarTokenShop"] = (false, "betting_denied"),
        ["starTokenShop"] = (false, "festival_shop_g2"),
        ["shaneLoan"] = (false, "money_transfer"),         // 13000: Money -= 3000
        ["cave"] = (false, "permanent_choice"),            // farm cave (host action)
        ["pet"] = (false, "permanent_choice"),             // pet adoption / rejection (13037-13060)
    };

    /// <summary>Owner D7 event question allowlist (everything unreviewed or permanent is refused).</summary>
    public static UiG3QuestionClass EventQuestion(UiEventDialogueFacts d)
    {
        if (d.Choices.Count < 1 || d.Choices.Count > UiG3Table.MaxChoices)
            return new UiG3QuestionClass(false, "choices_unsupported");
        if (d.NpcQuestion)
            // $q / $r inside an event "speak" (characterDialogue.chooseResponse): friendship-scale effects only.
            return new UiG3QuestionClass(true, "npc_event_response");
        if (d.AfterQuestionCallback)
            // DialogueBox.cs:388: with an afterQuestion delegate the answer goes to a location callback, not the event.
            return new UiG3QuestionClass(false, "location_callback");
        string key = d.QuestionKey ?? "";
        if (key.StartsWith("fork", StringComparison.Ordinal) && key.Length > 4
            && int.TryParse(key[4..], NumberStyles.None, CultureInfo.InvariantCulture, out _))
            return new UiG3QuestionClass(true, "script_branch"); // Event.cs:12809-12815 toggles specialEventVariable1
        return EventKeys.TryGetValue(key, out var row) ? new UiG3QuestionClass(row.Allowed, row.Class)
            : new UiG3QuestionClass(false, "unreviewed_event_key");
    }

    /// <summary>Script commands an answer must never splice in (money / items / mail / festival prizes).</summary>
    public static readonly IReadOnlyList<string> QuickBranchDeniedCommands = new[]
        { "money", "removeitem", "additem", "mail", "addmailreceived", "awardfestivalprize" };

    /// <summary>
    /// quickQuestion answers splice the chosen "(break)" branch into the event script (Event.cs:12817-12823): refused
    /// when that branch (commands split by '\\') carries a money / item / mail / prize command, or cannot be read.
    /// </summary>
    public static string? QuickBranchRefusal(UiEventDialogueFacts d, int index)
    {
        if (d.NpcQuestion || d.QuestionKey != "quickQuestion")
            return null;
        if (d.QuickBranches is not { } branches || index < 0 || index >= branches.Count)
            return "quick_question_branch_unreadable";
        foreach (string command in branches[index].Split('\\'))
        {
            string verb = command.Trim().Split(' ')[0].ToLowerInvariant();
            if (QuickBranchDeniedCommands.Contains(verb))
                return "quick_question_branch_denied:" + verb;
        }
        return null;
    }

    /// <summary>Readiness of the event DialogueBox (same facts and order as ui_key's classifier, eventUp allowed).</summary>
    public static (bool Ready, string? Reason) DialogueReadiness(UiKeyDialogueFacts d) =>
        d.Transitioning || d.QuestionPauseTimer > 0 ? (false, "transitioning")
        : !d.TextFullyRevealed ? (false, "typing")
        : d.SafetyTimer > 0 ? (false, "safety_timer")
        : d.IsQuestion && !d.OptionsShown ? (false, "options_not_shown")
        : (true, null);
}

// ---------------------------------------------------------------------- execution (pure, over a port)

/// <summary>
/// The text-entry primitive's box half (WS-G3), shared by the NamingMenu row and the Live7 character creation
/// (CharCustomExecutor): the menu's own TextBox as the keyboard subscriber, RecieveTextInput, read back.
/// </summary>
public interface IUiTextBoxPort
{
    bool SubscriberIsBox();
    /// <summary>Remembers the current Game1.keyboardDispatcher.Subscriber and sets the menu's own box.</summary>
    void SetSubscriberToBox();
    /// <summary>Puts the remembered subscriber back (only while the box is still the subscriber).</summary>
    void RestoreSubscriber();
    void ClearBoxText();
    void ReceiveText(string text);
    string BoxText();
    /// <summary>Clears TextBox._showKeyboard (set whenever Selected turns true): a later TextBox.Update() with the real
    /// mouse over the box would otherwise open TextEntryMenu (TextBox.cs:139-145), e.g. after a rejected name.</summary>
    void ClearKeyboardRequest();
}

/// <summary>The game side of a G3 step (ModEntry.UiG3.cs over the real menu; a recording fake in tests).</summary>
public interface IUiG3Port : IUiTextBoxPort
{
    UiPadFacts Read();

    /// <summary>NamingMenu.textBoxEnter(textBox) (NamingMenu.cs:85): the OK path without the click's TextBox.Update().</summary>
    void NamingEnter();

    // number
    void SetNumber(int value);
    void NumberOk();
    void NumberCancel();

    // event
    void EventAdvance();
    void EventAnswer(int index);

    // ready check
    void ReadyConfirm();
    void ReadyCancel() => throw new NotSupportedException("ready cancel port unavailable");

    // festival menus
    void FestivalSlotClick(int slot);
    /// <summary>Game1.player.Items[slot] now (the proof that the offered item left the inventory).</summary>
    UiSlotItem? InventorySlot(int slot);

    /// <summary>Game1.textEntry is open (the on-screen keyboard); CloseTextEntry is the cleanup.</summary>
    bool KeyboardOpen();
    void CloseTextEntry();
}

public static class UiG3Executor
{
    static UiPadVerdict V(string outcome, bool applied, string? effect, List<string> calls, string reason, UiPadFacts? after,
        string? readback = null) =>
        new(outcome, applied, effect, calls.ToArray(), reason, after?.SnappedId, after is { MenuOpen: true } ? after.MenuType : null,
            after is { MenuOpen: true } ? after.ReceiverToken : null, readback);

    static bool Gone(UiPadFacts before, UiPadFacts after) =>
        !after.MenuOpen || after.MenuType != before.MenuType || after.ReceiverToken != before.ReceiverToken;

    /// <summary>Runs one allowed G3 plan; never throws (a fault after a call = uncertain).</summary>
    public static UiPadVerdict Run(IUiG3Port port, UiPadFacts before, UiPadPlan plan)
    {
        if (!plan.IsG3)
            throw new InvalidOperationException("G3 executor needs an allowed G3 plan");
        UiPadStep step = plan.Steps[0];
        var calls = new List<string>();
        try
        {
            return step.G3Action switch
            {
                UiG3Table.ActTextEnter => TextEnter(port, before, step.G3Arg!, calls),
                UiG3Table.ActNumberOk => NumberOk(port, before, int.Parse(step.G3Arg ?? before.G3?.Number?.Current.ToString(CultureInfo.InvariantCulture) ?? "0", CultureInfo.InvariantCulture), calls),
                UiG3Table.ActNumberStep => NumberStep(port, before, int.Parse(step.G3Arg!, CultureInfo.InvariantCulture), calls),
                UiG3Table.ActNumberCancel => Closing(port, before, calls, "NumberSelectionMenu.cancel(click)", p => p.NumberCancel(), "closed"),
                UiG3Table.ActEventAdvance => EventAdvance(port, before, calls),
                UiG3Table.ActEventAnswer => EventAnswer(port, before, int.Parse(step.G3Arg!, CultureInfo.InvariantCulture), calls),
                UiG3Table.ActReadyConfirm => ReadyConfirm(port, before, calls),
                UiG3Table.ActReadyCancel => Closing(port, before, calls, "ReadyCheckDialog.cancelButton(click)", p => p.ReadyCancel(), "readiness_cancelled"),
                UiG3Table.ActFestivalSlot => FestivalSlot(port, before, int.Parse(step.G3Arg!, CultureInfo.InvariantCulture), calls),
                _ => throw new InvalidOperationException("unknown G3 action " + step.G3Action),
            };
        }
        catch (Exception ex)
        {
            UiPadFacts? now = null;
            try { now = port.Read(); } catch { }
            return V(calls.Count == 0 ? UiPadVerdict.PressNoEffect : UiPadVerdict.PressUncertain, false, null, calls,
                "call_fault:" + ex.GetType().Name, now);
        }
    }

    /// <summary>
    /// Text into the menu's own box: subscriber set only if it is not the box already and ALWAYS restored (finally),
    /// text cleared then RecieveTextInput(string) (TextBox.cs:226 appends), read back, and OK only on an exact match.
    /// </summary>
    static UiPadVerdict TextEnter(IUiG3Port port, UiPadFacts before, string text, List<string> calls)
    {
        string read = WriteBoxText(port, text, calls);
        if (!string.Equals(read, text, StringComparison.Ordinal))
            // Width trim / dirty-word filter changed it: never confirm a name the owner did not give.
            return V(UiPadVerdict.PressUncertain, false, null, calls, "text_readback_mismatch", port.Read(), read);
        calls.Add("NamingMenu.textBoxEnter(textBox)");
        port.NamingEnter();
        UiPadFacts after = port.Read();
        if (port.KeyboardOpen())
        {
            calls.Add("Game1.closeTextEntry()");
            port.CloseTextEntry();
            return V(UiPadVerdict.PressUncertain, false, null, calls, "on_screen_keyboard_opened", after, read);
        }
        if (before.PurchaseAnimals?.Animal is { } animal)
        {
            var adoption = after.PurchaseAnimals?.Adoption;
            return adoption is not null && PurchaseAnimalsUi.ExactAdoption(adoption, before.PurchaseAnimals!, text)
                ? V(UiPadVerdict.PressApplied, true, "animal_adopted", calls, "native_adoption_pending_return", after, read)
                : V(UiPadVerdict.PressUncertain, false, null, calls, "animal_adoption_unverified", after, read);
        }
        if (Gone(before, after))
            return V(UiPadVerdict.PressApplied, true, "named", calls, "naming_menu_closed", after, read);
        // Rejected name (e.g. textBoxEnter's minLength check, NamingMenu.cs:85-98): the menu stays open with our text;
        // the keyboard request is cleared again so no later click pops TextEntryMenu. Uncertain (receiver latched).
        try
        {
            calls.Add("TextBox._showKeyboard=false");
            port.ClearKeyboardRequest();
        }
        catch (Exception)
        {
            calls.Add("fault:clear_keyboard_request");
        }
        return V(UiPadVerdict.PressUncertain, false, null, calls, "naming_menu_still_open", after, read);
    }

    /// <summary>
    /// The text-entry primitive's box half: subscriber set only if it is not the box already and ALWAYS restored
    /// (finally), text cleared then RecieveTextInput(string) (TextBox.cs:226 appends), the keyboard request cleared,
    /// and the box text read back. Shared by the NamingMenu row (TextEnter) and the Live7 character creation.
    /// </summary>
    public static string WriteBoxText(IUiTextBoxPort port, string text, List<string> calls)
    {
        bool swapped = false;
        string read;
        try
        {
            if (!port.SubscriberIsBox())
            {
                calls.Add("keyboardDispatcher.Subscriber=box");
                port.SetSubscriberToBox();
                swapped = true;
            }
            calls.Add("TextBox.Text=\"\"");
            port.ClearBoxText();
            calls.Add("TextBox.RecieveTextInput(text)");
            port.ReceiveText(text);
            read = port.BoxText();
        }
        finally
        {
            if (swapped)
            {
                calls.Add("keyboardDispatcher.Subscriber=restore");
                port.RestoreSubscriber();
            }
            try
            {
                calls.Add("TextBox._showKeyboard=false");
                port.ClearKeyboardRequest();
            }
            catch (Exception)
            {
                calls.Add("fault:clear_keyboard_request");
            }
        }
        return read;
    }

    static UiPadVerdict NumberOk(IUiG3Port port, UiPadFacts before, int value, List<string> calls)
    {
        int current = before.G3?.Number?.Current ?? int.MinValue;
        if (value != current)
        {
            calls.Add("numberSelectedBox.Text+currentValue=" + value);
            port.SetNumber(value);
            UiPadFacts mid = port.Read();
            if (mid.G3?.Number?.Current != value)
                return V(UiPadVerdict.PressUncertain, false, null, calls, "number_readback_mismatch", mid);
        }
        calls.Add("NumberSelectionMenu.receiveLeftClick(okButton)");
        port.NumberOk();
        UiPadFacts after = port.Read();
        if (port.KeyboardOpen())
        {
            calls.Add("Game1.closeTextEntry()");
            port.CloseTextEntry();
            return V(UiPadVerdict.PressUncertain, false, null, calls, "on_screen_keyboard_opened", after);
        }
        string readback = value.ToString(CultureInfo.InvariantCulture);
        if (Gone(before, after))
            return V(UiPadVerdict.PressApplied, true, "number_selected", calls, "number_menu_closed", after, readback);
        return value != current
            ? V(UiPadVerdict.PressUncertain, false, null, calls, "number_set_ok_not_closed", after, readback)
            : V(UiPadVerdict.PressNoEffect, false, null, calls, "ok_not_accepted", after, readback);
    }

    static UiPadVerdict NumberStep(IUiG3Port port, UiPadFacts before, int value, List<string> calls)
    {
        calls.Add("numberSelectedBox.Text+currentValue=" + value);
        port.SetNumber(value);
        UiPadFacts after = port.Read();
        int? now = after.G3?.Number?.Current;
        string readback = (now ?? 0).ToString(CultureInfo.InvariantCulture);
        return now == value ? V(UiPadVerdict.PressApplied, true, "number_changed", calls, "number:" + value, after, readback)
            : now == before.G3?.Number?.Current ? V(UiPadVerdict.PressNoEffect, false, null, calls, "number_unchanged", after, readback)
            : V(UiPadVerdict.PressUncertain, false, null, calls, "number_readback_mismatch", after, readback);
    }

    static UiPadVerdict Closing(IUiG3Port port, UiPadFacts before, List<string> calls, string call, Action<IUiG3Port> act, string effect)
    {
        calls.Add(call);
        act(port);
        UiPadFacts after = port.Read();
        if (port.KeyboardOpen())
        {
            calls.Add("Game1.closeTextEntry()");
            port.CloseTextEntry();
            return V(UiPadVerdict.PressUncertain, false, null, calls, "on_screen_keyboard_opened", after);
        }
        return Gone(before, after) ? V(UiPadVerdict.PressApplied, true, effect, calls, "menu_closed", after)
            : V(UiPadVerdict.PressNoEffect, false, null, calls, "menu_still_open", after);
    }

    static UiPadVerdict EventAdvance(IUiG3Port port, UiPadFacts before, List<string> calls)
    {
        calls.Add("DialogueBox.receiveLeftClick(event)");
        port.EventAdvance();
        UiPadFacts after = port.Read();
        if (Gone(before, after))
            return V(UiPadVerdict.PressApplied, true, after.MenuOpen ? "replaced" : "closed", calls, "dialogue_closed", after);
        UiEventDialogueFacts? b = before.G3?.Event?.Dialogue, a = after.G3?.Event?.Dialogue;
        if (b is null || a is null)
            return V(UiPadVerdict.PressUncertain, false, null, calls, "dialogue_facts_lost", after);
        bool moved = a.Text != b.Text || a.DialoguesCount != b.DialoguesCount || a.BrokenUpCount != b.BrokenUpCount
            || a.Transitioning != b.Transitioning || a.IsQuestion != b.IsQuestion;
        return moved ? V(UiPadVerdict.PressApplied, true, "advanced", calls, "dialogue_advanced", after)
            : V(UiPadVerdict.PressNoEffect, false, null, calls, "dialogue_unchanged", after);
    }

    static UiPadVerdict EventAnswer(IUiG3Port port, UiPadFacts before, int index, List<string> calls)
    {
        UiEventDialogueFacts? b = before.G3?.Event?.Dialogue;
        string label = b is not null && index < b.Choices.Count ? b.Choices[index] : "";
        calls.Add("DialogueBox.selectedResponse=" + index + "+receiveLeftClick(event)");
        port.EventAnswer(index);
        UiPadFacts after = port.Read();
        string readback = index.ToString(CultureInfo.InvariantCulture) + ":" + UiPadTable.Trim(label, 64);
        UiEventDialogueFacts? a = after.G3?.Event?.Dialogue;
        // DialogueBox.cs:375-383: every answered question starts the transition (transitioning = true) and the
        // event-key path sets Event.previousAnswerChoice (Event.cs:12808) before anything else.
        bool keyPath = b is { NpcQuestion: false, AfterQuestionCallback: false };
        bool sameEvent = before.G3?.Event?.EventInstance == after.G3?.Event?.EventInstance;
        bool previous = !keyPath || (sameEvent && after.G3?.Event?.PreviousAnswer == index);
        bool transitioned = Gone(before, after) || (a is not null && a.Transitioning && !(b?.Transitioning ?? true));
        if (transitioned && previous)
            return V(UiPadVerdict.PressApplied, true, "answered", calls, "question_answered", after, readback);
        if (!transitioned && (after.G3?.Event?.PreviousAnswer ?? -1) == (before.G3?.Event?.PreviousAnswer ?? -1))
            return V(UiPadVerdict.PressNoEffect, false, null, calls, "question_unchanged", after);
        return V(UiPadVerdict.PressUncertain, false, null, calls, "answer_partial_proof", after, readback);
    }

    static UiPadVerdict ReadyConfirm(IUiG3Port port, UiPadFacts before, List<string> calls)
    {
        calls.Add("netReady.SetLocalReady(checkName,true)");
        port.ReadyConfirm();
        UiPadFacts after = port.Read();
        int was = before.G3?.ReadyCheck?.ReadyCount ?? 0, now = after.G3?.ReadyCheck?.ReadyCount ?? 0;
        string readback = now + "/" + (after.G3?.ReadyCheck?.Needed ?? before.G3?.ReadyCheck?.Needed ?? 0);
        if (Gone(before, after))
            return V(UiPadVerdict.PressApplied, true, "ready_check_done", calls, "ready_check_closed", after, readback);
        return now > was ? V(UiPadVerdict.PressApplied, true, "ready", calls, "ready_count_up", after, readback)
            : V(UiPadVerdict.PressNoEffect, false, null, calls, "already_ready", after, readback);
    }

    static UiPadVerdict FestivalSlot(IUiG3Port port, UiPadFacts before, int slot, List<string> calls)
    {
        UiSlotItem? item = before.G3 is { } g && slot < g.Inventory.Count ? g.Inventory[slot] : null;
        calls.Add("ItemGrabMenu.receiveLeftClick(inventory slot " + slot + ")");
        port.FestivalSlotClick(slot);
        UiPadFacts after = port.Read();
        string readback = item?.QualifiedItemId ?? "";
        UiSlotItem? left = port.InventorySlot(slot);
        bool consumed = item is not null && (left is null || left.QualifiedItemId != item.QualifiedItemId || left.Stack < item.Stack);
        if (Gone(before, after) && consumed)
            return V(UiPadVerdict.PressApplied, true, "festival_item_given", calls, "menu_closed_item_taken", after, readback);
        if (Gone(before, after) || consumed)
            // The menu closed without an inventory change readable here (or the reverse): never claim the gift.
            return V(UiPadVerdict.PressUncertain, false, null, calls, Gone(before, after) ? "menu_closed_item_unchanged" : "item_taken_menu_open",
                after, readback);
        return V(UiPadVerdict.PressNoEffect, false, null, calls, "festival_menu_unchanged", after, readback);
    }
}

// ---------------------------------------------------------------------- ui_inspect publication

public static class UiG3Snapshot
{
    /// <summary>The "g3" block inside ui_pad for a G3 context (null when G3 is off or the context is not G3).</summary>
    public static Dictionary<string, object>? Block(UiPadFacts f, string context)
    {
        if (f.G3 is not { Enabled: true } g || !UiG3Table.IsG3Context(context))
            return null;
        var block = new Dictionary<string, object>(StringComparer.Ordinal);
        if (context == UiG3Table.CtxNaming && g.Naming is { } n)
            block["text_entry"] = new Dictionary<string, object>
            {
                ["title"] = UiPadTable.Trim(n.Title), ["text"] = UiPadTable.Trim(n.Text, 64), ["min_length"] = Math.Max(1, n.MinLength),
                ["max_length"] = n.TextLimit > 0 ? Math.Min(n.TextLimit, UiG3Table.NameMaxLength) : UiG3Table.NameMaxLength,
                ["keyboard_open"] = n.KeyboardOpen,
            };
        if (context == UiG3Table.CtxNumber && g.Number is { } m)
            block["number"] = new Dictionary<string, object>
            {
                ["message"] = UiPadTable.Trim(m.Message), ["min"] = m.Min, ["max"] = m.Max, ["value"] = m.Current,
                ["price"] = m.Price, ["money"] = m.Money,
            };
        if (context == UiG3Table.CtxReadyCheck && g.ReadyCheck is { } r)
            block["ready_check"] = ReadyCheck(r, g.FestivalGoal);
        if (context == UiPadTable.CtxEvent && g.Event is { } e)
            block["event"] = Event(e, g);
        return block;
    }

    public static Dictionary<string, object> ReadyCheck(UiReadyCheckFacts r, bool festivalGoal)
    {
        UiG3ReadyDecision d = UiG3Policy.ReadyCheck(r, festivalGoal);
        var block = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["kind"] = d.Kind, ["check"] = UiPadTable.Trim(r.CheckName, 64), ["ready_count"] = r.ReadyCount, ["needed"] = r.Needed,
            ["cancelable"] = r.Cancelable, ["initiated_by_arti"] = r.InitiatedByArti, ["policy"] = d.Mode,
            // ReadyCheckDialog.update re-readies the local player every tick, inactive window included
            // (ReadyCheckDialog.cs:73): ARTI is "ready" on ANY check shown; "wait" never means "not ready".
            ["auto_ready"] = true,
        };
        if (d.ConfirmRefusal is { } why)
            block["wait_reason"] = why;
        return block;
    }

    public static Dictionary<string, object> Event(UiEventFacts e, UiG3Facts g)
    {
        var block = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["id"] = UiPadTable.Trim(e.EventId, 64), ["festival"] = e.Festival, ["skippable"] = e.Skippable,
            ["skip"] = "denied", ["player_control"] = e.PlayerControl,
            ["command_index"] = e.CommandIndex,
            ["previous_answer"] = e.PreviousAnswer,
        };
        if (e.FestivalId is { } fid)
            block["festival_id"] = UiPadTable.Trim(fid, 64);
        if (e.Command is { } cmd)
            block["command"] = UiPadTable.Trim(cmd, 32);
        if (g.FestivalMenu is { } menu)
            block["festival_menu"] = menu;
        if (e.Dialogue is { } d)
        {
            var dialogue = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["kind"] = d.IsQuestion ? (d.NpcQuestion ? "npc_question" : "question") : "dialogue",
                ["text"] = UiPadTable.Trim(d.Text, UiPadTable.MaxBodyText), ["ready"] = d.Ready,
            };
            if (d.NotReadyReason is { } why)
                dialogue["wait_reason"] = why;
            if (!d.IsQuestion && d.FestivalOwner is { } owner)
                dialogue["festival_owner"] = new Dictionary<string, object> {
                    ["lifecycle_id"] = owner.LifecycleId, ["command_index"] = owner.CommandIndex,
                    ["command_hash"] = owner.CommandHash, ["dialogue_id"] = owner.DialogueId };
            if (d.IsQuestion)
            {
                UiG3QuestionClass cls = UiG3Policy.EventQuestion(d);
                if (d.QuestionKey is { } key)
                    dialogue["question_key"] = UiPadTable.Trim(key, 64);
                dialogue["question_class"] = cls.Class;
                dialogue["answerable"] = cls.Allowed;
                dialogue["selected_response"] = d.SelectedResponse;
                dialogue["choices"] = d.Choices.Take(UiG3Table.MaxChoices)
                    .Select((c, i) => new Dictionary<string, object> { ["index"] = i, ["label"] = UiPadTable.Trim(c) }).ToArray();
            }
            block["dialogue"] = dialogue;
        }
        return block;
    }
}

// ---------------------------------------------------------------------- chat_send (owner D5, default OFF)

/// <summary>input_once family world_key primitive "chat_send", args {text}: Game1.chatBox.textBoxEnter(text).</summary>
public readonly record struct ChatSendTarget(string Text)
{
    public const string Primitive = "chat_send";
    public const string ArgText = "text";

    public bool IsValid => UiG3Policy.ChatRefusal(this.Text) is null;

    public static bool TryParse(JsonElement args, out ChatSendTarget target)
    {
        target = default;
        if (args.ValueKind != JsonValueKind.Object)
            return false;
        int count = 0;
        string? text = null;
        foreach (JsonProperty p in args.EnumerateObject())
        {
            count++;
            if (!string.Equals(p.Name, ArgText, StringComparison.Ordinal) || p.Value.ValueKind != JsonValueKind.String)
                return false;
            text = p.Value.GetString();
        }
        if (count != 1 || text is null)
            return false;
        target = new ChatSendTarget(text);
        return target.IsValid;
    }

    public Dictionary<string, object> Args() => new(StringComparer.Ordinal) { [ArgText] = this.Text };
}

/// <summary>One chat_send verdict (a direct call, no device state: cleanup always confirmed).</summary>
public sealed record ChatSendVerdict(string Outcome, bool Applied, int InvokeCount, string? Detail)
{
    public const string Sent = "ChatSent", NoEffect = "ChatSendNoEffect", Uncertain = "ChatSendUncertain";

    public bool IsUncertain => this.Outcome == Uncertain;

    /// <summary>
    /// ChatBox.receiveChatMessage appends the local echo (ChatBox.cs:123, 571-586; the list is capped, so the proof is
    /// a NEW last message object whose text, whitespace-insensitive (Game1.parseText wraps), contains the sent text).
    /// </summary>
    public static ChatSendVerdict From(bool newLastMessage, string? lastAfter, string text, bool threw)
    {
        if (threw)
            return new ChatSendVerdict(Uncertain, false, 1, "invoke_threw");
        if (!newLastMessage)
            return new ChatSendVerdict(NoEffect, false, 1, "no_chat_echo");
        static string Flat(string s) => new(s.Where(c => !char.IsWhiteSpace(c)).ToArray());
        return lastAfter is not null && Flat(lastAfter).Contains(Flat(text), StringComparison.Ordinal)
            ? new ChatSendVerdict(Sent, true, 1, "chat_echo_observed")
            : new ChatSendVerdict(Uncertain, false, 1, "chat_echo_mismatch");
    }
}

/// <summary>Admission composition for chat_send (no window-activity term: the call needs no input dispatch).</summary>
public static class ChatSendRoute
{
    /// <summary>
    /// ADR 244: no window-focus term. The observed cause stays a refusal: the owner typing in the chat box
    /// (<see cref="Refusal"/>, chatBoxActive).
    /// </summary>
    public static bool Ready(bool configEnabled, bool inboxEnabled, bool chatSendEnabled, bool observerReady, bool isMainPlayer,
        bool isSplitScreen, bool worldReady) =>
        configEnabled && inboxEnabled && chatSendEnabled && observerReady && !isMainPlayer && !isSplitScreen && worldReady;

    /// <summary>Refused before the call: owner D5 flag, text policy, the owner typing in the chat box, an open menu.</summary>
    public static string? Refusal(bool chatSendEnabled, string text, bool chatBoxPresent, bool chatBoxActive, bool menuOpen) =>
        !chatSendEnabled ? "chat_disabled_d5"
        : UiG3Policy.ChatRefusal(text) is { } why ? why
        : !chatBoxPresent ? "chat_box_unavailable"
        : chatBoxActive ? "chat_box_in_use"
        : menuOpen ? "menu_open"
        : null;
}
