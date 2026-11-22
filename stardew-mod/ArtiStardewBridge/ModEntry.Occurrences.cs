using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    OccurrenceRing? occurrenceRing;
    string? occurrenceScope;
    NativeProposal? occurrenceProposal;
    object? occurrenceLevel;
    sealed class KoList { public bool Admitted; }
    readonly System.Runtime.CompilerServices.ConditionalWeakTable<ItemListMenu, KoList> koLists = new();
    HashSet<string>? occurrenceMail;
    readonly List<(ActiveAttempt Ticket, bool ChestSeen)> occurrenceTickets = new();
    void RecordOccurrence(string kind, string subject, object facts, string? attempt = null) =>
        this.occurrenceRing?.Append(kind, subject, facts, Game1.ticks,
            this.commandInbox.ExecutionSlot.ModTick, NowUnixMs(), attempt);
    void TickOccurrences()
    {
        if (!Context.IsWorldReady || Game1.player is null || this.commandInboxDir is null) return;
        string save = Game1.uniqueIDForThisGame.ToString(System.Globalization.CultureInfo.InvariantCulture);
        string scope = save + ":" + Game1.player.UniqueMultiplayerID;
        try {
            if (this.occurrenceScope != scope) {
                this.occurrenceRing = null; this.occurrenceTickets.Clear();
                this.occurrenceProposal = null; this.occurrenceLevel = null; this.occurrenceMail = null;
                this.occurrenceRing = new(this.commandInboxDir, save, Game1.player.UniqueMultiplayerID,
                    this.runtimeInstanceId, Game1.ticks, this.commandInbox.ExecutionSlot.ModTick, NowUnixMs());
                this.nightEnding = Game1.showingEndOfNightStuff || Game1.newDay;
                this.nightLedger = new(this.commandInboxDir, save, Game1.player.UniqueMultiplayerID, NativeDate, this.nightEnding);
                this.barriers = new(f => this.RecordOccurrence("barrier", "barrier", f));
                this.nightStarted = false; this.drawnSave = null; this.nightLevelIndex = 0;
                this.occurrenceScope = scope;
            }
            var proposals = this.ReadNativeProposals();
            void Proposal(NativeProposal p, string phase) => this.RecordOccurrence("host_proposal", p.proposal_id,
                new { phase, proposal_kind = p.type, p.sender_id, p.proposal_id });
            if (!proposals.unreadable && proposals.incoming != this.occurrenceProposal) {
                if (this.occurrenceProposal is { response: null } old && old.proposal_id != proposals.incoming?.proposal_id)
                    Proposal(old, "withdrawn");
                if (proposals.incoming is { } incoming) Proposal(incoming, incoming.response ?? "shown");
                this.occurrenceProposal = proposals.incoming;
            }
            var mail = Game1.mailbox.ToHashSet();
            foreach (string id in mail.Except(this.occurrenceMail ?? mail))
                this.RecordOccurrence("mail_arrived", id, new { });
            this.occurrenceMail = mail;
            if (!Context.IsMainPlayer) this.TickStoryMemory(); // HOST ticks independently before this world/inbox gate.
            this.PollNativeProgress();
            var menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
            if (menu is LevelUpMenu level && !ReferenceEquals(level, this.occurrenceLevel)) {
                if (this.nightEnding) this.Stage("night", "levelup", ++this.nightLevelIndex);
                var facts = this.ReadUiKeyFacts(level).LevelUp;
                if (facts is not null) this.RecordOccurrence("levelup_shown", "levelup", new {
                    skill = facts.Skill, level = facts.Level, profession = level.isProfessionChooser });
            }
            this.occurrenceLevel = menu is LevelUpMenu ? menu : null;
            if (menu is ItemListMenu list && this.NativeProgressionEntry(list) == "e")
                this.koLists.GetValue(list, l => {
                    var items = this.Helper.Reflection.GetField<List<Item>>(l, "itemsToList").GetValue();
                    this.occurrenceRing!.KoLosses(this.ReceiverTokenOf(l)!, items.Where(i => i is not null)
                        .Select(i => (i.QualifiedItemId, i.Stack)).ToArray(), Game1.ticks, this.commandInbox.ExecutionSlot.ModTick, NowUnixMs());
                    return new KoList();
                });
            for (int i = this.occurrenceTickets.Count - 1; i >= 0; --i) {
                var (ticket, seen) = this.occurrenceTickets[i];
                if (!seen && this.backgroundMove?.ObservedChestAttempt == ticket.AttemptId) {
                    this.occurrenceRing!.ChestEffect(this.backgroundMove.ObservedChestEffect,
                        this.commandInbox.ExecutionSlot.ModTick, NowUnixMs());
                    this.occurrenceTickets[i] = (ticket, true);
                }
                if (this.commandInbox.ExecutionSlot.RefreshClosure(ticket).GetProperty("closed").GetBoolean())
                    this.occurrenceTickets.RemoveAt(i);
            }
            if (this.occurrenceRing?.Flush() == false) this.Monitor.Log("Occurrence persistence failed; retry next tick", LogLevel.Error);
        } catch (Exception e) { this.Monitor.Log("Occurrence history unavailable: " + e.Message, LogLevel.Error); }
    }
    void RetainOccurrenceTicket(MenuStepCommandResult result)
    {
        if (this.commandInbox.ExecutionSlot.ActiveAttempt is not { } a || a.Request.RequestId != result.RequestId
            || a.ProvenClosure is not null || this.occurrenceTickets.Any(t => ReferenceEquals(t.Ticket, a))) return;
        // Same retention horizon as the ring; no second closure implementation.
        if (this.occurrenceTickets.Count == OccurrenceRing.Capacity) this.occurrenceTickets.RemoveAt(0);
        this.occurrenceTickets.Add((a, this.backgroundMove?.ObservedChestAttempt == a.AttemptId));
    }
}
