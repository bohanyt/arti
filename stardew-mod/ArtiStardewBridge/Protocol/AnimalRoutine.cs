namespace ArtiStardewBridge.Protocol;

/// <summary>Immutable native tool-product identity at publication/admission/Press.</summary>
public sealed record AnimalProductFingerprint(string Qid, int Quality, int Quantity);

/// <summary>FIXB60: native hay A admission and exact per-tile effect; no game mutation here.</summary>
public static class AnimalRoutine
{
    public const int HousesMax = 16, AnimalsMax = 64, ProductsMax = 64, TroughsMax = 64;

    public static string? HayPlaceMiss(bool animalHouse, bool trough, bool occupied, string? heldQid,
        bool eventUp, bool ownTile, bool mounted, bool sitting, bool occupant, int timeOfDay) =>
        !animalHouse || !trough ? "not_animal_trough" : heldQid != "(O)178" ? "hay_required"
        : eventUp ? "event_active" : ownTile ? "grab_tile_is_own_tile" : mounted ? "mounted"
        : sitting ? "sitting" : occupied ? "trough_occupied" : occupant ? "occupant_at_tile"
        : timeOfDay >= WorldExtras.HardStopTime ? "time_rule:hard_stop" : null;

    public static bool HayPlaced(bool wasHay, bool isHay, int beforeCount, int afterCount) =>
        !wasHay && isHay && beforeCount - afterCount == 1;

    public static bool SameTarget(string? originalToken, string? currentToken) =>
        !string.IsNullOrEmpty(originalToken) && string.Equals(originalToken, currentToken, StringComparison.Ordinal);
}
