using System.Diagnostics;
using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// WS2 <c>ui_key</c> wiring (game thread only). Pure admission/verdict logic lives in
/// <see cref="UiKeyInput"/> / <see cref="UiKeyClassifier"/>; this file only reads game
/// objects into facts and performs the ONE whitelisted direct method call.
/// Menus get no input dispatch while the window is inactive (Game1.updateActiveMenu), so
/// this path never touches device state: no Helper.Input press/suppress, no history,
/// no active-menu assignment. A source guard (UiKeyInputTests) pins those properties.
/// Evidence ceiling: UNIT_TESTED.
/// </summary>
public sealed partial class ModEntry
{
    static readonly Guid UiKeyPinnedMvid = new("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf");

    sealed class UiKeySettleState
    {
        public MenuStepCommandRequest Request;
        public UiKeyInput Input = null!;
        public IClickableMenu Original = null!;
        public UiKeyTarget Target = null!;
        public UiKeyState State = null!;
        public Stopwatch Clock = null!;
        public long PlayerId;
        public string? ActorBefore;
    }

    private Action<IClickableMenu?, bool>? uiKeyTransition;
    private UiKeySettleState? uiKeySettle;
    private string? uiKeyUncertainReceiver;

    static bool UiKeyAssemblyPinned => typeof(Game1).Module.ModuleVersionId == UiKeyPinnedMvid;

    /// <summary>Admission term. No IsActive term: the call works with the window inactive.</summary>
    bool UiKeyReady => UiKeyRoute.Ready(this.config.Enabled, this.config.MenuStepCommandInboxEnabled,
        this.config.UiKeyInputEnabled, this.transferObserverReady, Context.IsMainPlayer, Context.IsSplitScreen,
        Context.IsWorldReady, Game1.keyboardDispatcher?.Subscriber is not null);

    static bool UiKeyWhitelistedType(IClickableMenu menu) =>
        menu.GetType() == typeof(DialogueBox) || menu.GetType() == typeof(LevelUpMenu)
        || menu.GetType() == typeof(ShippingMenu) || menu.GetType() == typeof(LetterViewerMenu);

    /// <summary>Sat gift: the native identity of the multiplayer gift prompt. The pending Gift proposal for this farmer
    /// AND the location's afterQuestion closure is FarmerTeam's own, capturing that very proposal (never text).</summary>
    static GiftPinFacts ReadGiftPin(IClickableMenu? menu, out Proposal? proposal)
    {
        proposal = Game1.player?.team?.GetIncomingProposal();
        var box = menu as DialogueBox;
        object? target = Game1.currentLocation?.afterQuestion?.Target;
        Type? owner = Game1.currentLocation?.afterQuestion?.Method.DeclaringType;
        Proposal? pending = proposal;
        bool owned = target is not null && pending is not null
            && (owner == typeof(FarmerTeam) || owner?.DeclaringType == typeof(FarmerTeam))
            && target.GetType().GetFields(System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic)
                .Any(f => f.FieldType == typeof(Proposal) && ReferenceEquals(f.GetValue(target), pending));
        return new GiftPinFacts(box?.isQuestion == true, box?.characterDialogue is not null,
            box is not null && ReferenceEquals(box, Game1.activeClickableMenu), proposal is not null,
            proposal?.proposalType.Value == ProposalType.Gift, proposal?.canceled.Value == true, proposal?.gift.Value is not null,
            proposal is not null && ReferenceEquals(proposal.receiver.Value, Game1.player), owned,
            box?.responses is { } rs ? rs.Select(r => r?.responseKey ?? "").ToArray() : Array.Empty<string>());
    }

    static bool GiftPinned(IClickableMenu? menu) => GiftPin.Holds(ReadGiftPin(menu, out _));

    static string UiKeyTrim(string? text, int max = UiKeyTable.MaxText) =>
        string.IsNullOrEmpty(text) ? "" : text.Length <= max ? text : text[..max];

    UiKeyMenuFacts ReadUiKeyFacts(IClickableMenu menu)
    {
        var env = new UiKeyEnvironment(Game1.eventUp, Game1.currentMinigame is not null, Game1.farmEvent is not null,
            Game1.keyboardDispatcher?.Subscriber is not null,
            menu.GetChildMenu() is not null || menu.GetParentMenu() is not null || !ReferenceEquals(menu, Game1.activeClickableMenu),
            UiKeyAssemblyPinned);
        string type = menu.GetType().Name;
        if (!UiKeyWhitelistedType(menu))
            return new UiKeyMenuFacts(type, env);
        switch (menu)
        {
            case DialogueBox box:
            {
                string current = box.getCurrentString() ?? "";
                var responses = box.isQuestion && box.responses is { } rs
                    ? rs.Take(UiKeyTable.MaxOptions + 1)
                        .Select(r => new UiKeyResponse(UiKeyTrim(r?.responseKey), UiKeyTrim(r?.responseText))).ToList()
                    : new List<UiKeyResponse>();
                bool gift = GiftPinned(box); // Sat gift: native identity only; a vanilla key never spells it
                string? lastKey = Game1.currentLocation?.lastQuestionKey;
                return new UiKeyMenuFacts(type, env, Dialogue: new UiKeyDialogueFacts(box.isQuestion,
                    box.characterDialogue is not null, box.transitioning,
                    box.characterIndexInDialogue >= current.Length - 1, box.safetyTimer, box.questionFinishPauseTimer,
                    current, box.dialogues?.Count ?? 0, box.characterDialoguesBrokenUp?.Count ?? 0, responses,
                    box.responseCC is not null,
                    box.isQuestion && box.characterDialogue is null
                        ? (gift ? UiKeyTable.GiftQuestionKey
                            : this.extrasPinnedKey == "TV" && this.ExtrasQuestionPinned(box) ? "TV"
                            : lastKey == UiKeyTable.GiftQuestionKey ? null : lastKey) : null,
                    this.ExtrasQuestionPinned(box), gift)); // WS-G6: only the question ARTI's extras press opened
            }
            case LevelUpMenu level:
            {
                int skill = this.Helper.Reflection.GetField<int>(level, "currentSkill").GetValue();
                int lvl = this.Helper.Reflection.GetField<int>(level, "currentLevel").GetValue();
                var choose = this.Helper.Reflection.GetField<List<int>>(level, "professionsToChoose").GetValue();
                return new UiKeyMenuFacts(type, env, LevelUp: new UiKeyLevelUpFacts(level.isActive, level.informationUp,
                    level.isProfessionChooser, level.CanReceiveInput(), level.readyToClose(), skill, lvl,
                    choose?.ToArray() ?? Array.Empty<int>(), level.hasUpdatedProfessions,
                    choose is not null && choose.Any(p => Game1.player.professions.Contains(p))));
            }
            case ShippingMenu shipping:
            {
                int intro = this.Helper.Reflection.GetField<int>(shipping, "introTimer").GetValue();
                object? save = this.Helper.Reflection.GetField<object?>(shipping, "saveGameMenu").GetValue();
                bool outro = this.Helper.Reflection.GetField<bool>(shipping, "outro").GetValue();
                bool saved = this.Helper.Reflection.GetField<bool>(shipping, "savedYet").GetValue();
                var ok = shipping.okButton;
                var c = ok.bounds.Center;
                bool clear = ok.containsPoint(c.X, c.Y)
                    && !shipping.categories.Any(cat => cat.visible && cat.containsPoint(c.X, c.Y))
                    && shipping.upperRightCloseButton?.containsPoint(c.X, c.Y) != true;
                return new UiKeyMenuFacts(type, env, Shipping: new UiKeyShippingFacts(shipping.currentPage, intro <= 0,
                    save is not null, outro, saved, clear, new[] { ok.bounds.X, ok.bounds.Y, ok.bounds.Width, ok.bounds.Height }));
            }
            case LetterViewerMenu letter:
                return new UiKeyMenuFacts(type, env, Letter: new UiKeyLetterFacts(letter.isFromCollection, letter.isMail,
                    letter.scale, letter.readyToClose(), letter.page, letter.itemsLeftToGrab(), letter.HasQuestOrSpecialOrder));
        }
        return new UiKeyMenuFacts(type, env);
    }

    /// <summary>Observation only: capability + bounded <c>ui_key</c> block + day transition facts.</summary>
    void EnrichUiKeyContext(Dictionary<string, object> snapshot)
    {
        try
        {
            snapshot["day_transition"] = this.NativeDayTransition();
            if (snapshot.GetValueOrDefault("capabilities") is not Dictionary<string, object> caps)
                return;
            bool ready = this.UiKeyReady;
            IClickableMenu? menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
            string? receiver = this.ReceiverTokenOf(menu);
            string? published = (snapshot.GetValueOrDefault("interactive") as Dictionary<string, object>)
                ?.GetValueOrDefault("receiver_token") as string;
            if (menu is null || receiver is null || receiver != published)
            {
                caps[UiKeyTable.Primitive] = new Dictionary<string, object>
                    { ["implemented"] = true, ["available"] = false, ["reason"] = ready ? "no_menu" : "backend_disabled" };
                return;
            }
            UiKeyState state = UiKeyClassifier.Classify(this.ReadUiKeyFacts(menu));
            bool latched = this.uiKeyUncertainReceiver == receiver;
            bool available = ready && state.Ready && !latched && this.uiKeySettle is null;
            caps[UiKeyTable.Primitive] = new Dictionary<string, object>
            {
                ["implemented"] = true, ["available"] = available,
                ["reason"] = !ready ? "backend_disabled" : latched ? "uncertain_latched"
                    : this.uiKeySettle is not null ? "busy" : state.NotReadyReason ?? "ready",
            };
            var block = new Dictionary<string, object>
            {
                ["receiver_token"] = receiver, ["menu_type"] = state.MenuType, ["ready"] = state.Ready && !latched,
                ["wait_only"] = state.WaitOnly,
            };
            if (state.Kind is { } kind)
            {
                block["kind"] = kind;
                block["keys"] = UiKeyTable.KeysFor(kind)
                    .Select(k => new Dictionary<string, object> { ["key"] = k, ["target_token"] = state.TokenFor(receiver, k) })
                    .ToArray();
            }
            // WS3b: which allowlisted location question this is (Sleep / Eat), so the runtime answers its own.
            if (state.Kind == "question" && menu is DialogueBox { isQuestion: true, characterDialogue: null }
                && state.QuestionKey == UiKeyTable.GiftQuestionKey && GiftPin.Holds(ReadGiftPin(menu, out Proposal? gp))
                && gp?.gift.Value is Item giftItem)
            {
                // Sat gift: facts only (ARTI chooses); bag room is the game's own admission term.
                block["question_key"] = UiKeyTable.GiftQuestionKey;
                block["gift"] = new Dictionary<string, object>
                {
                    ["item_qid"] = UiKeyTrim(giftItem.QualifiedItemId, 64), ["stack"] = giftItem.Stack,
                    ["item_name"] = UiKeyTrim(giftItem.DisplayName, 64),
                    ["sender_name"] = UiKeyTrim(gp.sender.Value?.Name, 64),
                    ["bag_has_room"] = Game1.player.couldInventoryAcceptThisItem(giftItem),
                };
            }
            else if (state.Kind == "question" && menu is DialogueBox { isQuestion: true, characterDialogue: null }
                && UiKeyTable.IsAllowedQuestionKey(state.QuestionKey, this.ExtrasQuestionPinned(menu))) // + WS-G6 pinned
                block["question_key"] = this.extrasPinnedKey == "TV" && this.ExtrasQuestionPinned(menu) ? "TV" : Game1.currentLocation.lastQuestionKey;
            if (latched) block["not_ready_reason"] = "uncertain_latched";
            else if (state.NotReadyReason is { } why) block["not_ready_reason"] = why;
            if (state.Options is { Count: > 0 } options)
                block["options"] = options.Select((o, i) => new Dictionary<string, object>
                    { ["index"] = i, ["label"] = o.Label, ["response_key"] = o.ResponseKey }).ToArray();
            if (state.Professions is { } professions)
                block["professions"] = professions.Select((p, i) => new Dictionary<string, object>
                    { ["index"] = i, ["profession_id"] = p,
                      ["name"] = UiKeyTrim(LevelUpMenu.getProfessionTitleFromNumber(p), 64) }).ToArray();
            if (state.Skill is { } skill) block["skill"] = skill;
            if (state.Level is { } level) block["level"] = level;
            if (state.Letter is { } letter)
                block["letter"] = new Dictionary<string, object>
                    { ["page"] = letter.Page, ["has_items"] = letter.HasItems, ["has_quest"] = letter.HasQuest, ["is_mail"] = letter.IsMail };
            snapshot[UiKeyTable.Primitive] = block;
        }
        catch (Exception)
        {
            if (snapshot.GetValueOrDefault("capabilities") is Dictionary<string, object> caps)
                caps[UiKeyTable.Primitive] = new Dictionary<string, object>
                    { ["implemented"] = true, ["available"] = false, ["reason"] = "read_failed" };
            snapshot.Remove(UiKeyTable.Primitive);
        }
    }

    UiKeyProbe? ProbeUiKey(IClickableMenu original, UiKeyState state, UiKeyTarget target, long playerId)
    {
        if (!Context.IsWorldReady || Game1.player is null || Game1.player.UniqueMultiplayerID != playerId
            || Context.IsMainPlayer || Context.IsSplitScreen)
            return null;
        IClickableMenu? current = CurrentInteractiveMenu(Game1.activeClickableMenu);
        string? token = this.ReceiverTokenOf(current);
        string? facts = current is not null && ReferenceEquals(current, original)
            ? UiKeyClassifier.Classify(this.ReadUiKeyFacts(current)).KindFacts : null;
        bool active = true, pending = false, canReceive = false;
        int professionCount = 0;
        if (original is LevelUpMenu level)
        {
            active = level.isActive;
            pending = Game1.player.newLevels.Any(p => p.X == state.Skill && p.Y == state.Level);
            if (target.ProfessionId is { } id)
                professionCount = Game1.player.professions.Contains(id) ? 1 : 0;
        }
        if (original is ShippingMenu shipping)
            canReceive = shipping.CanReceiveInput();
        string? actor = original is LetterViewerMenu
            ? ContainerExitReadback.Contents(Game1.player.Items.Select(PresentedItemOf), 48) : null;
        return new UiKeyProbe(token, current?.GetType().Name, facts, active, pending, professionCount, canReceive, actor,
            this.TvDialogueChain(current));
    }

    /// <summary>The ONE whitelisted direct call. Never Helper.Input, never a key/click history write.</summary>
    void InvokeUiKey(IClickableMenu menu, UiKeyTarget target, UiKeyState state)
    {
        bool ownedTv = this.TvDialogueChain(menu) is not null
            || (this.extrasPinnedKey == "TV" && this.ExtrasQuestionPinned(menu)
                && target.Key == UiKeyTable.Answer && target.OptionIndex is int i
                && state.Options?[i].ResponseKey is "Weather" or "Fortune");
        this.commandInbox.ExecutionSlot.Bracket(() => {
            this.InvokeUiKeyCore(menu, target, state);
            if (this.commandInbox.ExecutionSlot.ActiveAttempt is { } a) a.Delivered = true;
        });
        if (ownedTv)
            this.extrasPinnedTvDialogue = this.TvDialogueCallbackPinned
                && Game1.activeClickableMenu is DialogueBox { isQuestion: false, characterDialogue: null } next ? next : null;
    }

    void InvokeUiKeyCore(IClickableMenu menu, UiKeyTarget target, UiKeyState state)
    {
        switch (menu)
        {
            case DialogueBox box when target.Kind == UiKeyTable.Dialogue:
                this.Monitor.Log("ui_key INVOKE method=DialogueBox.receiveLeftClick", LogLevel.Info);
                box.receiveLeftClick(0, 0);
                return;
            case DialogueBox box when target.Kind is UiKeyTable.Question or UiKeyTable.NpcQuestion:
            {
                int index = target.Key == UiKeyTable.Cancel ? box.responses.Length - 1 : target.OptionIndex!.Value;
                if (index < 0 || index >= box.responses.Length)
                    throw new InvalidOperationException("ui_key response index out of range");
                // Sat gift: press-time re-check of the native identity (the classifier ran this tick; never trust it twice).
                if (state.QuestionKey == UiKeyTable.GiftQuestionKey
                    && (!GiftPinned(box) || !UiKeyTable.GiftResponse(box.responses[index]?.responseKey)))
                    throw new InvalidOperationException("ui_key gift identity changed");
                this.Monitor.Log($"ui_key INVOKE method=DialogueBox.receiveLeftClick selectedResponse={index}", LogLevel.Info);
                box.selectedResponse = index;
                if (box.responseCC is { } cc && index < cc.Count)
                    box.currentlySnappedComponent = cc[index];
                box.receiveLeftClick(0, 0);
                this.ObserveSleepAnswer(box, state.QuestionKey);
                return;
            }
            case LevelUpMenu level when target.Kind == UiKeyTable.LevelUp && !level.isProfessionChooser:
                this.Monitor.Log("ui_key INVOKE method=LevelUpMenu.okButtonClicked", LogLevel.Info);
                level.okButtonClicked();
                return;
            case ShippingMenu shipping when target.Kind == UiKeyTable.ShippingSummary:
            {
                var center = shipping.okButton.bounds.Center;
                this.Monitor.Log("ui_key INVOKE method=ShippingMenu.receiveLeftClick(okButton.center)", LogLevel.Info);
                shipping.receiveLeftClick(center.X, center.Y);
                return;
            }
            case LetterViewerMenu letterMenu when target.Kind == UiKeyTable.Letter && !letterMenu.isFromCollection:
                this.Monitor.Log("ui_key INVOKE method=LetterViewerMenu.exitThisMenu", LogLevel.Info);
                letterMenu.exitThisMenu(letterMenu.ShouldPlayExitSound());
                return;
        }
        throw new InvalidOperationException("ui_key menu/kind mismatch");
    }

    void StartProfessionFrame(UiKeyTarget target, UiKeyState state)
    {
        if (GenericMenuSurface is not LevelUpMenu { isProfessionChooser: true } menu || !state.Ready
            || target.Key != UiKeyTable.Choose || target.OptionIndex is not (0 or 1)
            || state.TokenFor(this.ReceiverTokenOf(menu)!, target.Key) != target.TargetToken
            || state.Professions?[target.OptionIndex.Value] != target.ProfessionId)
            throw new InvalidOperationException("profession_target_changed");
        var component = target.OptionIndex == 0 ? menu.leftProfession : menu.rightProfession;
        int id = GenericInspect(menu).Widgets.Single(w => ReferenceEquals(w.Component, component)).Id;
        this.StartGenericUiFrame("left", id, null);
    }
    void RejectUiKey(MenuStepCommandRequest request, CommandInboxIdentity pin, string reason)
    {
        this.Monitor.Log($"ui_key REJECT request_id={request.RequestId} reason={reason}", LogLevel.Warn);
        this.WriteCommandResult(this.commandInbox.CompleteRejected(request,
            reason == "UiKeyBusy" ? CommandRejectReason.CommandBusy : CommandRejectReason.BindingMismatch, pin, NowUnixMs()));
    }

    void ExecuteUiKey(MenuStepCommandRequest request, bool inactive)
    {
        this.ApplyActionOwnerRecords();
        CommandInboxIdentity pin = this.CurrentCommandIdentity();
        IClickableMenu? menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
        string? receiver = this.ReceiverTokenOf(menu);
        UiKeyTarget? target = request.UiKeyTarget;
        this.Monitor.Log($"arti_command ui_key ADMIT request_id={request.RequestId} receiver={receiver} "
            + $"type={menu?.GetType().Name} kind={target?.Kind} key={target?.Key} inactive={inactive}", LogLevel.Info);
        string? refuse = !this.UiKeyReady || !UiKeyAssemblyPinned ? "UiKeyNotReady"
            : target is null ? "UiKeyUnsupported"
            : this.uiKeySettle is not null || this.commandInbox.ExecutionSlot.Blocks(request) ? "UiKeyBusy"
            : this.actionOwner.ReleaseRequested || this.actionOwner.StopLatched ? "UiKeyNotReady"
            : !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(request, pin, Context.IsWorldReady,
                Context.IsPlayerFree, Context.CanPlayerMove, out _) ? "UiKeyTargetChanged"
            : !this.actionOwner.CanAdmitLeasedInputOnce(pin, NowUnixMs(), request.RuntimeInstanceId, request.OwnerId,
                request.Generation, request.ControlSeq, out _) ? "UiKeyNotReady"
            : menu is null || receiver is null || !UiKeyWhitelistedType(menu) || menu.GetType().Name != target.MenuType
                ? "UiKeyTargetChanged"
            : this.uiKeyUncertainReceiver == receiver ? "UiKeyNotReady"
            : null;
        if (refuse is not null)
        {
            this.RejectUiKey(request, pin, refuse);
            return;
        }
        IClickableMenu original = menu!;
        UiKeyState state;
        UiKeyProbe? before;
        try
        {
            state = UiKeyClassifier.Classify(this.ReadUiKeyFacts(original));
            before = this.ProbeUiKey(original, state, target!, pin.PlayerId);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"ui_key facts unreadable: {ex.GetType().Name}", LogLevel.Warn);
            this.RejectUiKey(request, pin, "UiKeyUnsupported");
            return;
        }
        if (before is null)
        {
            this.RejectUiKey(request, pin, "UiKeyTargetChanged");
            return;
        }
        if (target!.Kind == UiKeyTable.LevelUpProfession)
        {
            try { this.StartProfessionFrame(target, state); }
            catch { this.RejectUiKey(request, pin, "UiKeyTargetChanged"); }
            return;
        }
        var input = new UiKeyInput();
        string? previous = receiver;
        this.uiKeyTransition = (next, ok) =>
        {
            string? nextToken = this.ReceiverTokenOf(CurrentInteractiveMenu(next));
            input.ObserveReceiverTransition(previous, nextToken);
            previous = nextToken;
        };
        UiKeyVerdict? verdict;
        try
        {
            verdict = input.Start(target!, state, receiver, true, before,
                () => this.InvokeUiKey(original, target!, state),
                () => this.ProbeUiKey(original, state, target!, pin.PlayerId));
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"ui_key executor fault: {ex.GetType().Name}", LogLevel.Error);
            verdict = input.Invoked
                ? new UiKeyVerdict(UiKeyVerdict.OutcomeUncertain, false, null, 1, false, "UnverifiedReceiverTransition",
                    null, null, null, "executor_fault")
                : new UiKeyVerdict(UiKeyVerdict.OutcomeRejected, false, null, 0, false, "NotInvoked", null, null, null, "UiKeyUnsupported");
        }
        if (verdict is { Rejected: true })
        {
            this.uiKeyTransition = null;
            this.RejectUiKey(request, pin, verdict.Reason);
            return;
        }
        var settle = new UiKeySettleState
        {
            Request = request, Input = input, Original = original, Target = target!, State = state,
            Clock = Stopwatch.StartNew(), PlayerId = pin.PlayerId, ActorBefore = before.ActorContents,
        };
        if (verdict is not null)
        {
            this.FinishUiKey(settle, verdict);
            return;
        }
        this.uiKeySettle = settle; // inbox stays inflight; TickUiKeySettle observes once per tick
    }

    /// <summary>Every UpdateTicked (both branches), before TickCommandInbox. Monotonic deadline.</summary>
    void TickUiKeySettle()
    {
        if (this.uiKeySettle is not { } settle)
        {
            if (this.uiKeyUncertainReceiver is { } latched
                && this.ReceiverTokenOf(CurrentInteractiveMenu(Game1.activeClickableMenu)) != latched)
            {
                this.Monitor.Log("ui_key uncertain latch cleared (receiver changed)", LogLevel.Info);
                this.uiKeyUncertainReceiver = null;
            }
            else if (this.uiKeyUncertainReceiver is { } stable && this.uiWatchdog.KeyUncertain.Due(!this.UiMotorUnsafe
                    && this.uiMotor is null && this.uiPadSettle is null && this.commandInbox.ExecutionSlot.ActiveAttempt is null, NowUnixMs())
                && this.uiWatchdog.KeyUncertain.Resolved(stable, UiPadToken.StateDigest(this.ReadUiPadFacts()), NowUnixMs()))
            {
                this.Monitor.Log("ui_key uncertain latch cleared (re-observed stable, input released)", LogLevel.Info);
                this.uiKeyUncertainReceiver = null;
            }
            return;
        }
        if (!this.commandInbox.HasInflight || this.commandInbox.InflightRequestId != settle.Request.RequestId)
        {
            this.Monitor.Log($"ui_key settle abandoned (inbox slot changed) request_id={settle.Request.RequestId}", LogLevel.Warn);
            this.uiKeySettle = null;
            this.uiKeyTransition = null;
            this.uiKeyUncertainReceiver = this.ReceiverTokenOf(settle.Original);
            return;
        }
        if (Game1.paused || Game1.HostPaused)
        {
            settle.Clock.Stop();
            return; // no probe/transition timeout while native UI progress is paused
        }
        settle.Clock.Start();
        UiKeyProbe? probe;
        try { probe = this.ProbeUiKey(settle.Original, settle.State, settle.Target, settle.PlayerId); }
        catch { probe = null; }
        UiKeyVerdict? verdict = settle.Input.Observe(probe, settle.Clock.ElapsedMilliseconds);
        if (verdict is not null)
            this.FinishUiKey(settle, verdict);
    }

    void FinishUiKey(UiKeySettleState settle, UiKeyVerdict verdict)
    {
        this.uiKeySettle = null;
        this.uiKeyTransition = null;
        if (verdict.Uncertain)
            this.uiKeyUncertainReceiver = settle.Target.ReceiverToken;
        long? generation = null;
        try
        {
            if (this.config.Enabled && Context.IsWorldReady)
                this.AdvanceUiContextNative(this.screens.Value);
            generation = this.CurrentCommandIdentity().ContextGeneration;
        }
        catch (Exception) { generation = null; }
        string? actorAfter = settle.Original is LetterViewerMenu && Game1.player is not null
            ? ContainerExitReadback.Contents(Game1.player.Items.Select(PresentedItemOf), 48) : null;
        this.Monitor.Log($"ui_key VERDICT request_id={settle.Request.RequestId} outcome={verdict.Outcome} "
            + $"effect={verdict.Effect} result_receiver={verdict.ResultReceiverToken} reason={verdict.Reason} "
            + $"settle_ms={settle.Clock.ElapsedMilliseconds}", verdict.Uncertain ? LogLevel.Warn : LogLevel.Info);
        this.WriteCommandResult(this.commandInbox.CompleteUiKey(verdict, this.CurrentCommandIdentity(), NowUnixMs(),
            generation, settle.ActorBefore, actorAfter));
    }
}
