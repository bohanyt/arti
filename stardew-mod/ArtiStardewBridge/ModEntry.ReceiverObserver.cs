using ArtiCore.Pure;
using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    sealed record NativeProposal(string proposal_id, string type, long sender_id, long receiver_id, string? response);
    sealed record NativeProposals(NativeProposal? outgoing, NativeProposal? incoming, bool unreadable);
    NativeProposals ReadNativeProposals()
    {
        NativeProposal? ProposalFact(Proposal? p) { if (p is null) return null;
            string type = p.proposalType.Value.ToString().ToLowerInvariant(); return new(
            this.eventObjects.GetValue(p, _ => new EventObjectId("proposal:" + this.runtimeInstanceId + ":" + ++this.nextEventObjectId)).Value,
            type is "gift" or "dance" or "trade" or "marriage" ? type : "other", p.sender.UID, p.receiver.UID,
            p.response.Value == ProposalResponse.Accepted ? "accepted" : p.response.Value == ProposalResponse.Rejected ? "declined" : null); }
        // Client.receiveServerIntroduction reads teamRoot in full before setUpGame sets readyToPlay.
        bool readable = Context.IsWorldReady && Game1.client?.readyToPlay == true
            && Game1.player?.teamRoot is { } team && ReferenceEquals(team, Game1.serverHost?.Value.teamRoot);
        return new(readable ? ProposalFact(Game1.player!.team.GetOutgoingProposal()) : null,
            readable ? ProposalFact(Game1.player!.team.GetIncomingProposal()) : null, !readable);
    }
    NativeClosureFacts? ReadNativeClosure(ActiveAttempt a)
    {
        if (!Context.IsWorldReady || Game1.player is null)
            return a.ReadOnly ? new(this.runtimeInstanceId, Game1.ticks, NowUnixMs(), false, false, false,
                false, false, false, null, null, Array.Empty<string>()) : null;
        var proposals = this.ReadNativeProposals();
        bool debt = this.backgroundMove?.HasMenuDebt(a.AttemptId) == true;
        var surface = this.CaptureReceiverFacts(false);
        bool adopted = a.Controller?.AdoptedPresenceIds.Any(id => surface.Stack.Concat(surface.Parked).Any(r => r.PresenceId == id)) == true;
        return new(this.runtimeInstanceId, surface.Frame, NowUnixMs(), debt, Game1.player.UsingTool,
            ControlHandoff.Reason(CapturePlayerControlDiagnostics()) is "fade" or "warp", a.Controller is not null && !this.ReflexBusy, adopted,
            !proposals.unreadable, proposals.outgoing?.proposal_id, proposals.incoming?.proposal_id,
            surface.Stack.Concat(surface.Parked).Select(r => r.PresenceId).ToArray());
    }
    sealed class ReceiverState
    {
        public readonly ReceiverObserver Observer = new(o => ((IClickableMenu)o).GetChildMenu(), NativeReceiverClass);
        public ReceiverSurface? Surface;
        public int Tick;
    }
    readonly StardewModdingAPI.Utilities.PerScreen<ReceiverState> receiverState = new(() => new());
    static string NativeReceiverClass(object menu) => menu is LetterViewerMenu ? "LetterViewer"
        : menu is TextEntryMenu or NamingMenu ? "text_entry" : menu.GetType().Name;

    // Q13: closed native holders, decompiled 1.6.15.24356 (the notes are not in this repository).
    ReceiverSurface CaptureReceiverFacts(bool checkController = true)
    {
        var state = receiverState.Value;
        if (state.Surface is { } cached && state.Tick == Game1.ticks) {
            if (checkController) this.commandInbox.ExecutionSlot.CheckController();
            return cached with { Authority = state.Observer.Authority };
        }
        var holders = new List<(string, object)>();
        var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);
        void Read(object? owner)
        {
            if (owner is null || !seen.Add(owner)) return;
            void Hold(string field, IClickableMenu? menu)
            { if (menu is not null) { holders.Add((owner.GetType().Name + "." + field, menu)); Read(menu); } }
            if (owner is IClickableMenu menu)
            { Hold("_parentMenu", menu.GetParentMenu()); Hold("_childMenu", menu.GetChildMenu()); }
            if (owner is GameMenu gameMenu) foreach (var page in gameMenu.pages) Read(page); // pages are not receivers
            foreach (var field in GenericUiInspection.Fields(owner.GetType()))
                if ((owner is JunimoNoteMenu && field.Name == "menuToReturnTo")
                    || (owner is CollectionsPage && field.Name == "letterviewerSubMenu")
                    || (owner is ShippingMenu && field.Name == "saveGameMenu")
                    || (owner is ChatBox && field.Name == "emojiMenu")
                    || (owner.GetType().FullName == "StardewValley.Minigames.GrandpaStory" && field.Name == "letterView"))
                    Hold(field.Name, field.GetValue(owner) as IClickableMenu);
        }
        void Root(string name, IClickableMenu? menu)
        { if (menu is not null) { holders.Add((name, menu)); Read(menu); } }
        Read(Game1.activeClickableMenu);
        foreach (var menu in Game1.nextClickableMenu ?? new()) Root("Game1.nextClickableMenu", menu);
        foreach (var menu in Game1.endOfNightMenus ?? new()) Root("Game1.endOfNightMenus", menu);
        foreach (var menu in Game1.onScreenMenus ?? Array.Empty<IClickableMenu>()) Root("Game1.onScreenMenus", menu);
        Root("Game1.overlayMenu", Game1.overlayMenu); Root("Game1.emoteMenu", Game1.emoteMenu);
        Root("Game1.chatBox", Game1.chatBox); Root("Game1.instanceTextEntry", Game1.game1?.instanceTextEntry);
        Root("TitleMenu._subMenu", TitleMenu.subMenu); Read(Game1.currentMinigame);
        var top = DeepestReceiver(Game1.activeClickableMenu);
        state.Tick = Game1.ticks;
        var surface = state.Surface = state.Observer.Capture(Game1.activeClickableMenu, holders, Game1.ticks, this.NativeProgressionEntry(top) is not null);
        if (checkController) this.commandInbox.ExecutionSlot.CheckController();
        return surface;
    }

    ControllerFrame ReadControllerFrame(MenuStepCommandRequest request, string phase)
    {
        var surface = this.CaptureReceiverFacts(false); var pin = this.CurrentCommandIdentity();
        var tool = Game1.player?.CurrentTool;
        var ui = request.Intent?.GetProperty("verb").GetString() == "ui" ? this.ReadUiPadFacts() : null;
        bool own = surface.Top?.Instance is BobberBar && tool is StardewValley.Tools.FishingRod rod && (rod.hit || rod.isReeling)
                && phase is "hook" or "reel" or "bar"
            || surface.Top?.Instance is ItemGrabMenu menu && tool is StardewValley.Tools.FishingRod
                && ReferenceEquals(menu.context, tool) && phase is "landing" or "catch_menu";
        return new(new ValidityFacts(Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
            && request.SessionId == pin.SessionId // ADR 244: focus is not a validity term
            && request.RuntimeInstanceId == pin.RuntimeInstanceId && request.ExpectedPlayerId == pin.PlayerId
            && (request.ExpectedPlayerName is not { Length: > 0 } name || name == pin.PlayerName)
            && request.ExpectedScreenId == pin.ScreenId, surface.Epoch,
            request.ExpectedLocationId == pin.LocationId && Game1.overlayMenu is null && Game1.emoteMenu is null
                && Game1.textEntry is null && Game1.currentMinigame is null && Game1.CurrentEvent is null,
            NativeControllerPhase: true), surface, tool, own, (Game1.player?.TilePoint, Game1.player?.FacingDirection),
            ui?.UiKey?.Ready ?? ui?.IsActive ?? true, // DialogueBox removes the current native queue entry at receiveLeftClick (Q13 pinned source, :438).
            surface.Top?.Instance is DialogueBox { isQuestion: false } prose ? prose.dialogues?.Count ?? 0 : ui?.PageIndex ?? 0,
            Game1.currentLocation?.lastQuestionKey, surface.Top?.Instance is LevelUpMenu level ? this.NativeProfession(level) : null);
    }

    string? NativeProgressionEntry(IClickableMenu? menu) => menu switch {
        DialogueBox { isQuestion: false } => "a",
        LetterViewerMenu l when this.InformationalLetter(l) => "b",
        ShippingMenu => "c",
        LevelUpMenu { isActive: true, informationUp: true, isProfessionChooser: false } => "d",
        ItemListMenu when this.nightLedger?.Pending.collapse == "ko" => "e", _ => null };
    bool InformationalLetter(LetterViewerMenu l) {
        var g = GenericInspect(l);
        return g.Complete && ProgressionClient.InformationalLetter(l.mailMessage?.Count ?? 0,
            l.isFromCollection, l.HasQuestOrSpecialOrder, l.itemsLeftToGrab(), Game1.player.CursorSlotItem is not null,
            g.Widgets.Where(GenericWidgetVisible).Select(w => w.Kind).SequenceEqual(new[] { "close" }));
    }
    ProfessionState NativeProfession(LevelUpMenu level)
    {
        var f = this.ReadUiKeyFacts(level).LevelUp!;
        return new(f.Skill, f.Level, level.isProfessionChooser, !level.isActive
            && !Game1.player.newLevels.Any(p => p.X == f.Skill && p.Y == f.Level),
            f.ProfessionsToChoose.ToArray(), Game1.player.professions.ToArray());
    }
    internal static IClickableMenu? DeepestReceiver(IClickableMenu? menu)
    {
        var seen = new HashSet<IClickableMenu>(ReferenceEqualityComparer.Instance);
        while (menu?.GetChildMenu() is { } child && seen.Add(menu) && !seen.Contains(child)) menu = child;
        return menu;
    }
}
