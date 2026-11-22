using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    ReadyCheckRelay? nativeReadyRelay;
    long nativeReadyRelayAt;

    void SetupReadyCheckFacts()
    {
        this.Helper.Events.Multiplayer.ModMessageReceived += (_, e) =>
        {
            if (!Context.IsWorldReady || Context.IsMainPlayer || e.FromModID != "bohan.ArtiHostHelper"
                || e.Type != "native_ready_checks" || e.FromPlayerID != Game1.MasterPlayer.UniqueMultiplayerID) return;
            var relay = e.ReadAs<ReadyCheckRelay>();
            if (relay.SaveId != Game1.uniqueIDForThisGame || relay.Checks is null || relay.Checks.Length > 32
                || relay.Checks.Any(c => c.Check is null || c.Check.Length > 128 || c.Players is null || c.Players.Length > 8
                    || c.Players.Any(p => p.Name is null || p.Name.Length > 40 || p.State is not ("Ready" or "Locked" or "NotReady" or "unknown")))) return;
            this.nativeReadyRelay = relay;
            this.nativeReadyRelayAt = Environment.TickCount64;
        };
        this.Helper.Events.GameLoop.ReturnedToTitle += (_, _) => this.nativeReadyRelay = null;
    }

    Dictionary<string, object> NativeReadyFacts(string check, bool cancelable)
    {
        var facts = ReadyCheckObservation.Capture(Game1.netReady, check, Game1.netReady.GetNumberReady(check),
            Game1.netReady.GetNumberRequired(check), Game1.netReady.IsReady(check), cancelable);
        var relay = this.nativeReadyRelay;
        var peers = relay?.SaveId == Game1.uniqueIDForThisGame && Environment.TickCount64 - this.nativeReadyRelayAt <= 3000
            ? relay.Checks.FirstOrDefault(c => c.Check == check)?.Players : null;
        var local = Game1.player;
        // Names and identities come from native online farmers; readiness comes only from native state, never counts.
        facts["players"] = Game1.getOnlineFarmers().Take(8).Select(f => new ReadyFarmer(f.UniqueMultiplayerID, f.Name[..Math.Min(40, f.Name.Length)],
            f == local, f == local ? (string)facts["local_state"] : peers?.FirstOrDefault(p => p.Id == f.UniqueMultiplayerID)?.State ?? "unknown")).ToArray();
        facts["players_basis"] = peers is null ? "native_local_state; remote_unknown" : "native_local_state; host_native_ready_states";
        return facts;
    }
}
