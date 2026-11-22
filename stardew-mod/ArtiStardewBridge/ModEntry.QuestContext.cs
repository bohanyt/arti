using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

// W1 gap B4 (S28 surat_quest): the read-only ui.context quest facts. Not linked into ProtocolTests (receiver tokens and
// the character scan are ModEntry-wide); the fact readers it calls are (ModEntry.QuestCapture.cs).
public sealed partial class ModEntry
{
    /// <summary>
    /// ui.context "quests" = the local actor's quest log (active and completed-with-reward entries only: the log itself,
    /// never a generated future request), newest first, bounded; a non-completed delivery quest carries its target NPC's
    /// current native location (a travel hint: on a farmhand an inactive location may be stale, the faced capture decides).
    /// ui.context "letter_quest" = the quest the open mail letter presents, with that letter's receiver token.
    /// </summary>
    void EnrichQuestContext(Dictionary<string, object> snapshot)
    {
        snapshot.Remove("quests");
        snapshot.Remove("letter_quest");
        if (!this.ExtrasEnabled || !Context.IsWorldReady || Game1.player is not Farmer player)
            return;
        try
        {
            snapshot["quests"] = this.ReadQuestFacts(player, QuestTargetLocation).Select(q => (object)q.Wire()).ToArray();
            if (Game1.activeClickableMenu is LetterViewerMenu letter && ReadLetterQuest(letter, player) is { } presented)
                snapshot["letter_quest"] = presented.Wire(this.ReceiverTokenOf(CurrentInteractiveMenu(letter)));
        }
        catch (Exception)
        {
            snapshot.Remove("quests");
            snapshot.Remove("letter_quest");
        }
    }

    static string QuestTargetLocation(string npcName) =>
        Game1.getCharacterFromName(npcName)?.currentLocation?.Name ?? "";
}
