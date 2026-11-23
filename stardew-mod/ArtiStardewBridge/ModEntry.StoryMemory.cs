using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Minigames;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    // ADR 63: observation only, through the existing bounded occurrence ring.
    string? storyScope, storyKey, storyId, storyType, storyDate, storyLocation;
    object? storyInstance;
    Dictionary<string, string>? storyPrevious;
    long storyPoll;
    int storyLines, storyChanges;
    string? storyLastLine;

    Dictionary<string, string> ReadStoryState()
    {
        var p = Game1.player;
        var state = new Dictionary<string, string>();
        foreach (var group in p.Items.OfType<Item>().Concat(new[] { p.CursorSlotItem }.OfType<Item>())
            .Distinct<Item>(ReferenceEqualityComparer.Instance).GroupBy(i => i.TypeDefinitionId + i.itemId.Value))
            state["item:" + group.Key] = group.Sum(i => i.Stack) + " " + (ItemRegistry.GetData(group.Key)?.DisplayName ?? group.Key);
        foreach (string id in p.mailReceived) state["mail:" + id] = "added";
        foreach (string id in Game1.MasterPlayer.mailReceived) state["host_mail:" + id] = "added";
        foreach (var q in p.questLog) state["quest:" + q.id.Value] = "added";
        foreach (var pair in p.friendshipData.Pairs) state["npc_friendship:" + pair.Key] = "added";
        // Native queries over loaded native location identities; no story-ID/calendar lookup table.
        foreach (var loc in Game1.locations)
            state["accessible:" + loc.NameOrUniqueName] = Game1.isLocationAccessible(loc.Name) ? "true" : "false";
        state["key:hasRustyKey"] = p.hasRustyKey ? "true" : "false";
        return state;
    }

    void StoryRow(string phase, string? text = null, string? speaker = null, string? change = null, bool narration = false)
    {
        if (this.storyKey is null) return;
        this.RecordOccurrence("native_story", this.storyKey, new {
            phase, event_id = this.storyId ?? "UNKNOWN", event_type = this.storyType,
            date = this.storyDate, location = this.storyLocation, text = text ?? "",
            speaker = speaker ?? "UNKNOWN", narration, change = change ?? "" });
    }

    void TickStoryMemory()
    {
        if (Context.IsMainPlayer || Context.IsSplitScreen) {
            if (HostStoryRelay.CanCapture(this.config.HostStoryRelayEnabled, Context.IsMainPlayer, Context.IsSplitScreen))
                this.TickHostStoryRelay();
            return;
        }
        try {
            if (this.storyScope != this.occurrenceScope) {
                this.storyScope = this.occurrenceScope; this.storyKey = null;
                this.storyInstance = null; this.storyPrevious = null; this.storyPoll = 0;
            }
            var menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
            object? instance = (object?)Game1.CurrentEvent ?? (object?)Game1.farmEvent
                ?? (menu is LetterViewerMenu ? menu : null);
            bool edge = !ReferenceEquals(instance, this.storyInstance);
            string? line = ShownStoryLine(menu);
            long now = NowUnixMs();
            if (!edge && line == this.storyLastLine && now - this.storyPoll < 1000) return;
            Dictionary<string, string>? current = null;
            try { current = this.ReadStoryState(); } catch { /* Native state remains UNKNOWN; keep shown lines. */ }
            // Close the old episode before opening a different native instance.
            if (edge && this.storyKey is not null) {
                if (current is not null) this.StoryDiff(current);
                this.StoryRow("ended", change: this.storyChanges == 0 || current is null
                    || this.storyChanges > 32 || this.storyLines > 64 ? "UNKNOWN" : "");
                this.storyKey = null;
                // A new instance may open on this same tick: keep the carried baseline (ADR 71).
                this.storyPrevious = current is null ? null : NativeStoryFacts.Carry(this.storyPrevious, current);
            }
            if (edge && instance is not null) {
                this.storyId = Game1.CurrentEvent?.id;
                this.storyType = Game1.CurrentEvent is not null ? "Event" : instance.GetType().Name;
                this.storyDate = $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}";
                this.storyLocation = Game1.currentLocation?.NameOrUniqueName ?? "UNKNOWN";
                long observedFrame = Game1.ticks; // Value copy: never expose a game-field address to formatting.
                this.storyKey = "story:" + observedFrame; this.storyLines = this.storyChanges = 0;
                this.storyLastLine = null;
                this.StoryRow("started", change: this.storyPrevious is null ? "UNKNOWN" : "");
            }
            if (this.storyKey is not null) {
                if (current is not null) this.StoryDiff(current);
                else if (this.storyPrevious is not null || edge) this.StoryRow("gap", change: "UNKNOWN");
                if (!string.IsNullOrWhiteSpace(line) && line != this.storyLastLine && this.storyLines++ < 64) {
                    // Night events can begin before the date changes; date the text when actually shown.
                    this.storyDate = $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}";
                    this.StoryRow("line", line, (menu as DialogueBox)?.characterDialogue?.speaker?.Name,
                        narration: menu is DialogueBox { characterDialogue: null });
                }
            }
            this.storyInstance = instance; this.storyLastLine = line;
            // Inside an open instance the baseline carries keys a swapped menu view dropped (ADR 71);
            // between instances the raw snapshot is the baseline, so real removals are forgotten there.
            this.storyPrevious = current is null ? null
                : this.storyKey is not null ? NativeStoryFacts.Carry(this.storyPrevious, current) : current;
            this.storyPoll = now;
        } catch {
            // An unreadable native state is UNKNOWN, never a reason to interfere with input.
            if (NowUnixMs() - this.storyPoll >= 1000) this.StoryRow("gap", change: "UNKNOWN");
            this.storyPoll = NowUnixMs(); this.storyPrevious = null;
        }
    }

    // Shared presented-text reader: never enumerate the future event script or unseen letter pages.
    static string? ShownStoryLine(IClickableMenu? menu) => menu is DialogueBox box ? box.getCurrentString()
        : menu is LetterViewerMenu letter && letter.scale >= 1f && letter.mailMessage is { } pages
            && letter.page >= 0 && letter.page < pages.Count ? pages[letter.page] : null;

    HostStoryRelay? hostStoryRelay;
    long hostStoryFaultMs;

    // Mirrors GrandpaStory.draw's native visibility gates (1.6.15); never enumerate its future speech queue.
    (string? Text, object TextInstance, int TextPage, bool Narration) ShownGrandpaStory(GrandpaStory story)
    {
        var letter = this.Helper.Reflection.GetField<LetterViewerMenu?>(story, "letterView").GetValue();
        if (letter is not null) return (ShownStoryLine(letter), letter, letter.page, false);
        bool drawn = this.Helper.Reflection.GetField<bool>(story, "drawGrandpa").GetValue();
        if (drawn) {
            int timer = this.Helper.Reflection.GetField<int>(story, "grandpaSpeechTimer").GetValue();
            var speech = this.Helper.Reflection.GetField<Queue<string>>(story, "grandpaSpeech").GetValue();
            return (timer > 3000 && speech.Count > 0 ? speech.Peek() : null, story, speech.Count, false);
        }
        int scene = this.Helper.Reflection.GetField<int>(story, "scene").GetValue();
        return (scene == 3 ? Game1.content.LoadString("Strings\\StringsFromCSFiles:GrandpaStory.cs.12059") : null,
            story, -1, true);
    }

    void TickHostStoryRelay()
    {
        try {
            var menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
            var grandpa = Game1.currentMinigame as GrandpaStory;
            object? instance = (object?)grandpa ?? (object?)Game1.CurrentEvent ?? (object?)Game1.farmEvent
                ?? (menu is LetterViewerMenu or DialogueBox ? menu : null);
            // A title screen is not a native story scope; still close the last observed episode on exit.
            if (instance is null && !Context.IsWorldReady && this.hostStoryRelay is null) return;
            if (this.hostStoryRelay is null) {
                string configured = this.config.HostStoryRelayDirectory?.Trim() ?? "";
                if (configured.Length == 0) configured = this.config.CommandInboxDirectory?.Trim() ?? "";
                string directory = configured.Length == 0 ? Path.Combine(this.Helper.DirectoryPath, "command")
                    : Path.IsPathRooted(configured) ? configured : Path.Combine(this.Helper.DirectoryPath, configured);
                this.hostStoryRelay = new(directory, this.runtimeInstanceId);
            }
            ulong save = Game1.uniqueIDForThisGame; // Value copy; formatting must not take a protected game-field address.
            string saveId = save.ToString(System.Globalization.CultureInfo.InvariantCulture);
            long now = NowUnixMs();
            // Mabar has one separate farmhand. The HOST's online roster is
            // authoritative for whether that farmhand is in the same world.
            bool alone = !Context.IsWorldReady || Game1.getOnlineFarmers().Take(2).Count() == 1;
            this.hostStoryRelay.PublishPresence((Context.IsWorldReady || grandpa is not null) && alone, saveId, now, Game1.ticks);
            if (instance is null && !this.hostStoryRelay.HasVisibleStory) return;
            string source = grandpa is not null ? "minigame:GrandpaStory"
                : Game1.CurrentEvent is not null ? "event" : Game1.farmEvent is not null ? "farm_event"
                : menu is LetterViewerMenu ? "letter" : menu is DialogueBox ? "dialogue" : "";
            var shown = grandpa is not null ? this.ShownGrandpaStory(grandpa)
                : (Text: ShownStoryLine(menu), TextInstance: (object?)menu, TextPage: (menu as LetterViewerMenu)?.page ?? 0,
                    Narration: menu is DialogueBox { characterDialogue: null });
            this.hostStoryRelay.Capture(
                new(instance, saveId,
                    grandpa is null ? Game1.CurrentEvent?.id : null, instance?.GetType().Name ?? "UNKNOWN", source,
                    $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}", Game1.timeOfDay,
                    Game1.currentLocation?.NameOrUniqueName ?? "UNKNOWN", shown.Text,
                    grandpa is null ? (menu as DialogueBox)?.characterDialogue?.speaker?.Name : null,
                    shown.Narration, Game1.ticks, now, shown.TextInstance, shown.TextPage,
                    Game1.player?.UniqueMultiplayerID.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "UNKNOWN"));
        } catch (Exception e) {
            if (NowUnixMs() - this.hostStoryFaultMs >= 1000) {
                this.Monitor.Log("HOST story relay unavailable: " + e.Message, LogLevel.Warn);
                this.hostStoryFaultMs = NowUnixMs();
            }
        }
    }

    void StoryDiff(Dictionary<string, string> current)
    {
        if (this.storyPrevious is null) return;
        foreach (string detail in NativeStoryFacts.Additions(this.storyPrevious, current)) {
            if (this.storyChanges++ >= 32) continue;
            this.StoryRow("change", change: detail);
        }
    }
}
