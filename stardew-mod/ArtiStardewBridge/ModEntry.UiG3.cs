using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// Primitives v2 WS-G3 wiring (game thread only): naming / number / ready-check / event contexts on the WS-G1
/// ui_press path, the read-only ui.context blocks <c>ui_event</c> / <c>ready_check</c>, and the gated
/// <c>chat_send</c>. Tables, policies, executor and verdicts are pure (<see cref="UiG3Table"/>,
/// <see cref="UiG3Policy"/>, <see cref="UiG3Executor"/>); this file reads the game into <see cref="UiG3Facts"/>
/// (field reads and pure getters only) and implements <see cref="IUiG3Port"/> with the decomp's own calls:
/// TextBox.RecieveTextInput + NamingMenu.textBoxEnter, NumberSelectionMenu OK / cancel clicks with
/// numberSelectedBox.Text and currentValue set together, DialogueBox.receiveLeftClick under eventUp (the
/// vanilla route to Event.answerDialogue, DialogueBox.cs:387), netReady.SetLocalReady, ItemGrabMenu.receiveLeftClick
/// on the owner-pinned slot, ChatBox.textBoxEnter(string). Never Helper.Input, never a pad Press, never an
/// activeClickableMenu assignment, never Event.skipEvent. Flags <c>TextEventMenusEnabled</c> / <c>ChatSendEnabled</c>
/// (default OFF). Evidence ceiling: UNIT_TESTED.
/// </summary>
public sealed partial class ModEntry
{
    // Independent uses of the existing instance tracker: never share a tracker across NPC and dialogue reads.
    private readonly MenuInstanceTokenTracker uiG3NpcIdentity = new();
    private readonly MenuInstanceTokenTracker uiG3QuestionIdentity = new();
    private readonly MenuInstanceTokenTracker uiG3EventIdentity = new();

    bool UiG3On => this.config.UiPadMenusEnabled && this.config.TextEventMenusEnabled;

    bool ChatSendReady => ChatSendRoute.Ready(this.config.Enabled, this.config.MenuStepCommandInboxEnabled,
        this.config.ChatSendEnabled, this.transferObserverReady, Context.IsMainPlayer, Context.IsSplitScreen, Context.IsWorldReady); // ADR 244

    /// <summary>session.json text_event_menus_enabled / chat_send_enabled (omitted with each flag off).</summary>
    private CommandInboxSession WithUiG3SessionFacts(CommandInboxSession session)
    {
        if (this.UiG3On)
            session = session with { TextEventMenusEnabled = this.UiPadReady };
        if (this.config.ChatSendEnabled)
            session = session with { ChatSendEnabled = this.ChatSendReady };
        return session;
    }


    // ------------------------------------------------------------------ read-only facts

    /// <summary>One read of the G3 state (null with the flag off). Field reads and pure getters only.</summary>
    UiG3Facts? ReadUiG3Facts(IClickableMenu? top)
    {
        if (!this.UiG3On || Game1.player is not { } player)
            return null;
        UiTextBoxFacts? naming = null;
        UiNumberFacts? number = null;
        UiReadyCheckFacts? ready = null;
        string? festivalMenu = null;
        List<UiSlotItem?>? slots = null;
        bool keyboard = Game1.textEntry is not null;
        object? subscriber = Game1.keyboardDispatcher?.Subscriber;
        switch (top)
        {
            case PurchaseAnimalsMenu pm when pm.GetType() == typeof(PurchaseAnimalsMenu) && pm.namingAnimal && pm.textBox is { } animalBox:
                naming = new UiTextBoxFacts("animal name", animalBox.Text ?? "", animalBox.Selected, ReferenceEquals(subscriber, animalBox),
                    subscriber is not null && !ReferenceEquals(subscriber, animalBox), animalBox.textLimit, animalBox.numbersOnly, 1, keyboard);
                break;
            case NamingMenu nm when nm.GetType() == typeof(NamingMenu) && nm.textBox is { } box:
                naming = new UiTextBoxFacts(nm.title ?? "", box.Text ?? "", box.Selected, ReferenceEquals(subscriber, box),
                    subscriber is not null && !ReferenceEquals(subscriber, box), box.textLimit, box.numbersOnly, nm.minLength, keyboard);
                break;
            case NumberSelectionMenu ns when ns.GetType() == typeof(NumberSelectionMenu):
            {
                TextBox nbox = this.Helper.Reflection.GetField<TextBox>(ns, "numberSelectedBox").GetValue();
                bool over;
                Game1.PushUIMode();
                try { over = new Rectangle(nbox.X, nbox.Y, nbox.Width, nbox.Height).Contains(Game1.getMouseX(), Game1.getMouseY()); }
                finally { Game1.PopUIMode(); }
                number = new UiNumberFacts(this.Helper.Reflection.GetField<string>(ns, "message").GetValue() ?? "",
                    this.Helper.Reflection.GetField<int>(ns, "minValue").GetValue(),
                    this.Helper.Reflection.GetField<int>(ns, "maxValue").GetValue(),
                    this.Helper.Reflection.GetField<int>(ns, "price").GetValue(),
                    this.Helper.Reflection.GetField<int>(ns, "currentValue").GetValue(), player.Money, nbox.Text ?? "", keyboard, over);
                break;
            }
            case ReadyCheckDialog rc when rc.GetType() == typeof(ReadyCheckDialog):
            {
                string name = rc.checkName ?? "";
                ready = new UiReadyCheckFacts(name, Game1.netReady.GetNumberReady(name), Game1.netReady.GetNumberRequired(name),
                    rc.isCancelable(), name == "sleep" && this.nightLedger?.HasIntent == true && player.timeWentToBed.Value != 0);
                break;
            }
            case ItemGrabMenu grab when grab.GetType() == typeof(ItemGrabMenu) && Game1.CurrentEvent is { isFestival: true } fest
                                        && ReferenceEquals(grab.behaviorFunction?.Target, fest):
                festivalMenu = grab.behaviorFunction!.Method.Name switch
                {
                    "chooseSecretSantaGift" => UiG3Table.FestSecretSanta,
                    "clickToAddItemToLuauSoup" => UiG3Table.FestLuauSoup,
                    _ => null,
                };
                if (festivalMenu is not null)
                    slots = player.Items.Select(i => i is null ? null
                        : new UiSlotItem(i.QualifiedItemId, i.Stack, grab.inventory?.highlightMethod?.Invoke(i) ?? false)).ToList();
                break;
        }
        return new UiG3Facts(true, naming, number, ready, this.ReadUiEventFacts(top), festivalMenu, player.Money,
            player.mailReceived.Contains("fortuneTeller" + Game1.year), slots,
            this.config.FestivalItemAllowlist?.Where(s => !string.IsNullOrWhiteSpace(s)).ToArray());
    }

    /// <summary>Game1.CurrentEvent under eventUp (+ its DialogueBox through ui_key's own read). Read-only.</summary>
    UiEventFacts? ReadUiEventFacts(IClickableMenu? top)
    {
        if (!Game1.eventUp || Game1.CurrentEvent is not { } ev || Game1.farmEvent is not null
            || !ReferenceEquals(ev, Game1.currentLocation?.currentEvent))
            return null;
        string? command = null;
        if (ev.eventCommands is { Length: > 0 } cmds)
        {
            string raw = cmds[Math.Clamp(ev.CurrentCommand, 0, cmds.Length - 1)] ?? "";
            int space = raw.IndexOf(' ');
            command = space > 0 ? raw[..space] : raw;
        }
        int previous = -1;
        try { previous = this.Helper.Reflection.GetField<int>(ev, "previousAnswerChoice").GetValue(); }
        catch { previous = -1; }
        UiEventDialogueFacts? dialogue = null;
        if (top is DialogueBox box && box.GetType() == typeof(DialogueBox)
            && this.ReadUiKeyFacts(box).Dialogue is { } d)
        {
            (bool ok, string? why) = UiG3Policy.DialogueReadiness(d);
            IReadOnlyList<string>? branches = null;
            if (d.QuestionKey == "quickQuestion" && ev.eventCommands is { Length: > 0 } all)
            {
                // The same command answerDialogue splices from (Event.cs:12819: eventCommands[min(len-1, CurrentCommand)]).
                string current = all[Math.Min(all.Length - 1, ev.CurrentCommand)] ?? "";
                int space = current.IndexOf(' ');
                string[] parts = space < 0 ? Array.Empty<string>() : current[(space + 1)..].Split("(break)");
                branches = parts.Length > 1 ? parts.Skip(1).ToArray() : null;
            }
            dialogue = new UiEventDialogueFacts(d.IsQuestion, d.IsQuestion && d.HasCharacterDialogue, d.QuestionKey,
                d.IsQuestion && !d.HasCharacterDialogue && Game1.currentLocation?.afterQuestion is not null, ok, why,
                d.Responses.Select(r => r.Label).ToArray(), d.CurrentString, box.transitioning, d.DialoguesCount, d.BrokenUpCount,
                box.selectedResponse, branches,
                box.responses?.Take(UiKeyTable.MaxOptions + 1).Select(r => r?.responseKey).ToArray(),
                this.uiG3NpcIdentity.Observe(box.characterDialogue?.speaker),
                this.uiG3QuestionIdentity.Observe(box.characterDialogue),
                box.characterDialogue?.currentDialogueIndex ?? -1,
                this.ReadFestivalDialogueOwner(ev, box));
        }
        return new UiEventFacts(ev.id ?? "", ev.isFestival, ev.isFestival ? ev.id : null, command, ev.skippable,
            ev.playerControlSequence, previous, dialogue, ev.CurrentCommand, this.uiG3EventIdentity.Observe(ev));
    }

    /// <summary>Observation only: ui_event (any event, menu or not) and ready_check (ReadyCheckDialog up).</summary>
    void EnrichUiG3Context(Dictionary<string, object> snapshot)
    {
        if (!this.UiG3On)
            return;
        this.EnrichPurchaseAnimals(snapshot);
        try
        {
            UiG3Facts? g = this.ReadUiG3Facts(Game1.activeClickableMenu);
            if (g?.Event is { } ev)
                snapshot["ui_event"] = UiG3Snapshot.Event(ev, g);
            if (g?.ReadyCheck is { } rc)
            {
                var readiness = UiG3Snapshot.ReadyCheck(rc, g.FestivalGoal);
                foreach (var fact in this.NativeReadyFacts(rc.CheckName, rc.Cancelable)) readiness[fact.Key] = fact.Value;
                snapshot["ready_check"] = readiness;
            }
        }
        catch (Exception)
        {
            snapshot.Remove("ui_event");
            snapshot.Remove("ready_check");
        }
    }

    // ------------------------------------------------------------------ the port over the real menu

    private sealed class UiG3GamePort : IUiG3Port
    {
        private readonly ModEntry mod;
        private readonly IClickableMenu top;
        private IKeyboardSubscriber? previousSubscriber;

        public UiG3GamePort(ModEntry mod, IClickableMenu top)
        {
            this.mod = mod;
            this.top = top;
        }

        public UiPadFacts Read() => this.mod.ReadUiPadFacts();

        private TextBox Box => NativePurchaseAnimals.NamingBox(this.top)
            ?? (this.top as NamingMenu)?.textBox ?? throw new InvalidOperationException("no naming text box");

        private static void AssertWarpGuarded()
        {
            // Menu clicks may re-snap (ItemGrabMenu.cs:996-1006 snapCursorToCurrentSnappedComponent): guard first.
            if (!PadCursorWarpGuard.Verified) // ADR 244: the guard blocks snap warps with the window active too
                throw new InvalidOperationException("ui_g3 call without the OS-cursor guard");
        }

        private static void InUiMode(Action call)
        {
            Game1.PushUIMode();
            try { call(); }
            finally { Game1.PopUIMode(); }
        }

        public bool SubscriberIsBox() => ReferenceEquals(Game1.keyboardDispatcher.Subscriber, this.Box);

        public void SetSubscriberToBox()
        {
            TextBox box = this.Box;
            this.previousSubscriber = Game1.keyboardDispatcher.Subscriber;
            Game1.keyboardDispatcher.Subscriber = box; // KeyboardDispatcher.cs:28-49 selects it
        }

        public void RestoreSubscriber()
        {
            TextBox box = this.Box;
            if (ReferenceEquals(Game1.keyboardDispatcher.Subscriber, box))
                Game1.keyboardDispatcher.Subscriber = this.previousSubscriber;
            this.previousSubscriber = null;
        }

        public void ClearBoxText() => this.Box.Text = "";

        public void ReceiveText(string text) => this.Box.RecieveTextInput(text);

        public string BoxText() => this.Box.Text ?? "";

        public void ClearKeyboardRequest() =>
            this.mod.Helper.Reflection.GetField<bool>(this.Box, "_showKeyboard").SetValue(false);

        public void NamingEnter()
        {
            AssertWarpGuarded();
            if (this.top is PurchaseAnimalsMenu pm && pm.GetType() == typeof(PurchaseAnimalsMenu) && pm.namingAnimal)
            {
                InUiMode(() => NativePurchaseAnimals.NamingEnter(pm));
                return;
            }
            NamingMenu menu = this.top as NamingMenu ?? throw new InvalidOperationException("not a NamingMenu");
            InUiMode(() => menu.textBoxEnter(menu.textBox));
        }

        private NumberSelectionMenu Number => this.top as NumberSelectionMenu ?? throw new InvalidOperationException("not a NumberSelectionMenu");

        public void SetNumber(int value)
        {
            NumberSelectionMenu menu = this.Number;
            // update() re-parses the text into currentValue every tick (NumberSelectionMenu.cs:216-223): set both.
            this.mod.Helper.Reflection.GetField<TextBox>(menu, "numberSelectedBox").GetValue().Text = value.ToString();
            this.mod.Helper.Reflection.GetField<int>(menu, "currentValue").SetValue(value);
        }

        public void NumberOk()
        {
            AssertWarpGuarded();
            NumberSelectionMenu menu = this.Number;
            Point c = menu.okButton.bounds.Center;
            InUiMode(() => menu.receiveLeftClick(c.X, c.Y));
        }

        public void NumberCancel()
        {
            AssertWarpGuarded();
            NumberSelectionMenu menu = this.Number;
            Point c = menu.cancelButton.bounds.Center;
            InUiMode(() => menu.receiveLeftClick(c.X, c.Y));
        }

        private DialogueBox Dialogue => this.top as DialogueBox ?? throw new InvalidOperationException("not a DialogueBox");

        public void EventAdvance()
        {
            AssertWarpGuarded();
            this.Dialogue.receiveLeftClick(0, 0); // ui_key's own advance call, eventUp allowed here
        }

        public void EventAnswer(int index)
        {
            AssertWarpGuarded();
            if (Game1.CurrentEvent is null || !ReferenceEquals(Game1.CurrentEvent, Game1.currentLocation?.currentEvent))
                throw new InvalidOperationException("event question receiver changed");
            DialogueBox box = this.Dialogue;
            if (box.responses is not { } responses || index < 0 || index >= responses.Length)
                throw new InvalidOperationException("event answer index out of range");
            box.selectedResponse = index;
            if (box.responseCC is { } cc && index < cc.Count)
                box.currentlySnappedComponent = cc[index];
            box.receiveLeftClick(0, 0); // -> currentEvent.answerDialogue(lastQuestionKey, index) (DialogueBox.cs:387)
        }

        public void ReadyConfirm()
        {
            ReadyCheckDialog dialog = this.top as ReadyCheckDialog ?? throw new InvalidOperationException("not a ReadyCheckDialog");
            Game1.netReady.SetLocalReady(dialog.checkName, ready: true); // what its update() does each tick (:73)
        }

        public void ReadyCancel()
        {
            var dialog = this.top as ReadyCheckDialog ?? throw new InvalidOperationException("not a ReadyCheckDialog");
            if (!dialog.isCancelable()) throw new InvalidOperationException("ready check not cancelable");
            var center = dialog.cancelButton.bounds.Center;
            InUiMode(() => dialog.receiveLeftClick(center.X, center.Y));
        }

        public void FestivalSlotClick(int slot)
        {
            AssertWarpGuarded();
            ItemGrabMenu grab = this.top as ItemGrabMenu ?? throw new InvalidOperationException("not an ItemGrabMenu");
            ClickableComponent cc = grab.inventory.inventory.FirstOrDefault(c => c.name == slot.ToString())
                ?? throw new InvalidOperationException("slot component missing");
            Point center = cc.bounds.Center;
            InUiMode(() => grab.receiveLeftClick(center.X, center.Y));
        }

        public UiSlotItem? InventorySlot(int slot) =>
            Game1.player?.Items is { } items && slot >= 0 && slot < items.Count && items[slot] is { } item
                ? new UiSlotItem(item.QualifiedItemId, item.Stack, false) : null;

        public bool KeyboardOpen() => Game1.textEntry is not null;

        public void CloseTextEntry() => Game1.closeTextEntry();
    }

    // ------------------------------------------------------------------ execution

    /// <summary>ui_press on a G3 context (admission already done by ExecuteUiPad): one pure-executor run.</summary>
    void ExecuteUiG3(MenuStepCommandRequest request, UiPadFacts facts, UiPadPlan plan, IClickableMenu top, string receiver)
    {
        var port = new UiG3GamePort(this, top);
        UiPadVerdict verdict;
        try
        {
            verdict = UiG3Executor.Run(port, facts, plan);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"ui_g3 executor fault: {ex.GetType().Name}: {ex.Message}", LogLevel.Error);
            verdict = new UiPadVerdict(UiPadVerdict.PressUncertain, false, null, Array.Empty<string>(),
                "executor_fault:" + ex.GetType().Name, null, null, null);
        }
        this.FinishUiPad(request, verdict, receiver);
    }

    /// <summary>
    /// chat_send (owner D5, default OFF): one ChatBox.textBoxEnter(text) — the chat box's own send path without
    /// opening it (no keyboard subscriber change). "/" commands refused before the call.
    /// </summary>
    void ExecuteChatSend(MenuStepCommandRequest request)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        string text = request.ChatSend?.Text ?? "";
        ChatBox? chat = Game1.chatBox;
        string? refuse = !this.ChatSendReady ? "ChatSendNotReady"
            : request.ChatSend is null ? "ChatSendUnsupported"
            : this.commandInbox.ExecutionSlot.Blocks(request) ? "ChatSendBusy"
            : this.actionOwner.ReleaseRequested || this.actionOwner.StopLatched ? "ChatSendNotReady"
            : !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, pin, Context.IsWorldReady,
                Context.IsPlayerFree, Context.CanPlayerMove, out _) ? "ChatSendTargetChanged"
            : !this.actionOwner.CanAdmitLeasedInputOnce(pin, NowUnixMs(), request.RuntimeInstanceId, request.OwnerId,
                request.Generation, request.ControlSeq, out _) ? "ChatSendNotReady"
            : ChatSendRoute.Refusal(this.config.ChatSendEnabled, text, chat is not null, chat?.isActive() == true,
                Game1.activeClickableMenu is not null) is { } why
                ? "CHAT_DENIED:" + why
            : null;
        this.Monitor.Log($"arti_command chat_send request_id={request.RequestId} length={text.Length} refuse={refuse ?? "-"}", LogLevel.Info);
        if (refuse is not null)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(request,
                refuse == "ChatSendBusy" ? CommandRejectReason.CommandBusy : CommandRejectReason.ArmRejected, pin, NowUnixMs(),
                refuse == "ChatSendBusy" ? null : BgReason.PreconditionUnavailable(refuse)));
            return;
        }
        ChatMessage? lastBefore = chat!.messages.LastOrDefault();
        bool threw = false;
        try
        {
            chat.textBoxEnter(text);
        }
        catch (Exception ex)
        {
            threw = true;
            this.Monitor.Log($"chat_send fault: {ex.GetType().Name}", LogLevel.Error);
        }
        ChatMessage? lastAfter = chat.messages.LastOrDefault();
        string? lastText = lastAfter is null ? null : string.Concat(lastAfter.message.Select(s => s.message ?? ""));
        ChatSendVerdict verdict = ChatSendVerdict.From(lastAfter is not null && !ReferenceEquals(lastAfter, lastBefore), lastText, text, threw);
        this.Monitor.Log($"arti_command chat_send VERDICT request_id={request.RequestId} outcome={verdict.Outcome} detail={verdict.Detail}",
            verdict.IsUncertain ? LogLevel.Warn : LogLevel.Info);
        this.WriteCommandResult(this.commandInbox.CompleteChatSend(verdict, this.CurrentCommandIdentity(), NowUnixMs()));
    }
}
