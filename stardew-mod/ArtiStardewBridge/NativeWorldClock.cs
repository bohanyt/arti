using StardewValley;
using StardewValley.Network;
using Netcode;
using System.Reflection;

namespace ArtiStardewBridge;

/// <summary>The clock used by observer facts and their admission checks.</summary>
internal static class NativeWorldClock
{
    private static readonly FieldInfo Clock = typeof(NetWorldState).GetField("timeOfDay", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingFieldException(typeof(NetWorldState).FullName, "timeOfDay");
    internal static int World => ((NetInt)Clock.GetValue(Game1.netWorldState.Value)!).Value;
    // NetWorldState.WriteToGame1 copies this field into the client's local
    // clock. Event continuations can temporarily write the local field first.
    // The host's live clock is authoritative on host; synced world on clients.
    internal static int Now => Game1.IsMasterGame ? Game1.timeOfDay : World;
}
