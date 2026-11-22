using System.Reflection;
using System.Runtime.CompilerServices;
using HarmonyLib;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

/// <summary>Read-only provenance of boxes created by reviewed native festival commands.
/// Merely being under eventUp is never ownership. No input or game-state writes.</summary>
internal static class FestivalDialogueWitness
{
    internal sealed record Call(Event Event, string FestivalId, GameLocation Location, Farmer Player, string[] Script,
        int Index, string Raw, int ReceiverIndex, IClickableMenu? Before);
    private sealed record Receipt(Call Call, object? Dialogue, object? Speaker, string Id);
    private static readonly ConditionalWeakTable<DialogueBox, Receipt> receipts = new();
    private static Func<bool> enabled = () => false;
    private static long serial;

    internal static void Install(string modId, Func<bool> isEnabled, IMonitor monitor)
    {
        enabled = () => false;
        try
        {
            if (typeof(Game1).Module.ModuleVersionId != new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf"))
                throw new InvalidOperationException("game_mvid");
            var harmony = new Harmony(modId + ".FestivalDialogueWitness");
            foreach (string method in new[] { "Speak", "Message", "Cutscene" })
                harmony.Patch((typeof(Event).GetNestedType("DefaultCommands") ?? throw new MissingMemberException("DefaultCommands"))
                    .GetMethods(BindingFlags.Public | BindingFlags.Static).Single(m => m.Name == method && m.GetParameters().Length == 3),
                    prefix: new HarmonyMethod(typeof(FestivalDialogueWitness), nameof(Before)),
                    finalizer: new HarmonyMethod(typeof(FestivalDialogueWitness), nameof(After)));
            enabled = isEnabled;
            monitor.Log("FESTIVAL native dialogue provenance witness installed (read-only).", LogLevel.Info);
        }
        catch (Exception ex)
        {
            monitor.Log("FESTIVAL dialogue automation unavailable: " + ex.GetType().Name, LogLevel.Warn);
        }
    }

    private static void Before(Event __0, MethodBase __originalMethod, out Call? __state)
    {
        __state = null;
        try { if (enabled()) __state = Begin(__0, __originalMethod.Name); }
        catch { /* Fail closed; never interrupt vanilla. */ }
    }

    private static void After(Call? __state, Exception? __exception)
    {
        try { if (__exception is null) Complete(__state); }
        catch { /* A missing receipt withholds automatic input. */ }
    }

    // Event.cs:155-196,1027-1036,2055-2073. Speak/message keep the command index;
    // winner Cutscene creates Lewis's box and then advances exactly one command.
    internal static Call? Begin(Event ev, string method)
    {
        if (!Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen || !Game1.eventUp
            || !ReferenceEquals(Game1.CurrentEvent, ev) || !ReferenceEquals(Game1.currentLocation?.currentEvent, ev)
            || ev is not { isFestival: true, eventSwitched: true, playerControlSequence: false, festivalTimer: <= 0 }
            || ev.id is not ("festival_spring13" or "festival_winter8") || Game1.player is not { } player
            || !ReferenceEquals(player.currentLocation, Game1.currentLocation)
            || ev.CurrentCommand < 0 || ev.CurrentCommand >= ev.eventCommands.Length
            || Game1.activeClickableMenu is not null || Game1.dialogueUp) return null;
        string raw = ev.eventCommands[ev.CurrentCommand];
        bool winner = method == "Cutscene" && raw == "cutscene "
            + (ev.id == "festival_spring13" ? "eggHuntWinner" : "iceFishingWinner");
        if (!(method == "Speak" && raw.StartsWith("speak Lewis ", StringComparison.Ordinal))
            && !(method == "Message" && raw.StartsWith("message ", StringComparison.Ordinal)) && !winner) return null;
        return new(ev, ev.id, Game1.currentLocation!, player, ev.eventCommands, ev.CurrentCommand, raw,
            ev.CurrentCommand + (winner ? 1 : 0), Game1.activeClickableMenu);
    }

    internal static void Complete(Call? call)
    {
        if (call is null || !ScopeValid(call) || Game1.activeClickableMenu is not DialogueBox box
            || box.GetType() != typeof(DialogueBox) || ReferenceEquals(call.Before, box) || box.isQuestion
            || !Game1.dialogueUp || call.Event.CurrentCommand != call.ReceiverIndex) return;
        // Speak and winner boxes must carry the actual Event actor, not an overworld NPC.
        if (!call.Raw.StartsWith("message ", StringComparison.Ordinal)
            && (box.characterDialogue?.speaker is not { } speaker || speaker.Name != "Lewis"
                || !call.Event.actors.Any(a => ReferenceEquals(a, speaker)))) return;
        receipts.Remove(box);
        receipts.Add(box, new(call, box.characterDialogue, box.characterDialogue?.speaker,
            "fd:" + Interlocked.Increment(ref serial)));
    }

    private static bool ScopeValid(Call c) => Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
        && Game1.eventUp && ReferenceEquals(Game1.CurrentEvent, c.Event)
        && ReferenceEquals(Game1.currentLocation, c.Location) && ReferenceEquals(c.Location.currentEvent, c.Event)
        && ReferenceEquals(Game1.player, c.Player) && ReferenceEquals(c.Player.currentLocation, c.Location)
        && c.Event.id == c.FestivalId && c.Event.isFestival && c.Event.eventSwitched
        && !c.Event.playerControlSequence && c.Event.festivalTimer <= 0
        && ReferenceEquals(c.Event.eventCommands, c.Script) && c.Script[c.Index] == c.Raw;

    internal static FestivalDialogueOwner? Read(Event ev, IClickableMenu? top, string lifecycleId)
    {
        if (top is not DialogueBox box || !ReferenceEquals(top, Game1.activeClickableMenu) || box.isQuestion || !Game1.dialogueUp
            || !receipts.TryGetValue(box, out var r) || !ReferenceEquals(r.Call.Event, ev)
            || !ScopeValid(r.Call) || ev.CurrentCommand != r.Call.ReceiverIndex
            || !ReferenceEquals(r.Dialogue, box.characterDialogue) || !ReferenceEquals(r.Speaker, box.characterDialogue?.speaker))
            return null;
        return new(lifecycleId, r.Call.Index, UiKeyTargetToken.Hash(r.Call.Raw), r.Id);
    }
}
