using System.Text.Json.Serialization;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;
using StardewValley.Locations;

namespace ArtiHostHelper;

/// <summary>
/// The HOST's own facts behind <see cref="HostAccepting.Ok"/>, read only where barrier.json is already
/// written (every 60 ticks). Read-only game queries, plain loops, no closure. It never throws into the game loop: the
/// whole read is one try, and any exception reads as not accepting.
/// </summary>
public sealed class HostAcceptingFacts
{
    [JsonPropertyName("server_up")] public bool ServerUp { get; init; }
    [JsonPropertyName("connections")] public int Connections { get; init; }
    [JsonPropertyName("farmers")] public int Farmers { get; init; }
    [JsonPropertyName("own_cabin")] public bool OwnCabin { get; init; }
    [JsonPropertyName("own_online")] public bool OwnOnline { get; init; }
    [JsonPropertyName("slot_free")] public bool SlotFree { get; init; }
    [JsonPropertyName("ok")] public bool Ok { get; init; }
    [JsonPropertyName("error")] public string? Error { get; init; }

    public static HostAcceptingFacts Read(long farmhandId)
    {
        try
        {
            if (!Context.IsWorldReady) return new HostAcceptingFacts { Error = "world_not_ready" };
            bool serverUp = Game1.IsServer && Game1.server is not null;
            int connections = serverUp ? Game1.server!.connectionsCount : 0;
            int farmers = Game1.otherFarmers.Count;
            bool ownOnline = false;
            foreach (Farmer farmer in Game1.getOnlineFarmers())
                if (farmer.UniqueMultiplayerID == farmhandId) ownOnline = true;
            bool ownCabin = false;
            foreach (Building building in Game1.getFarm().buildings)
                if (building.indoors.Value is Cabin cabin && cabin.OwnerId == farmhandId) ownCabin = true;
            bool slotFree = HostAccepting.SlotFree(ownCabin, ownOnline);
            return new HostAcceptingFacts { ServerUp = serverUp, Connections = connections, Farmers = farmers,
                OwnCabin = ownCabin, OwnOnline = ownOnline, SlotFree = slotFree,
                Ok = HostAccepting.Ok(serverUp, connections, farmers, slotFree) };
        }
        catch (Exception ex) { return new HostAcceptingFacts { Error = ex.GetType().Name }; } // any doubt: not accepting
    }
}
