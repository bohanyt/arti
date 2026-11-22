using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Bounded current receiver facts inside existing ui.context; no inventory access or mutation.</summary>
public static class ContainerUiSnapshot
{
    public const int MaxActorSlots = 48;
    public const int MaxContainerSlots = 72;
    public const int MaxBytes = 48 * 1024;

    public static Dictionary<string, object> Unavailable(string reason) =>
        new() { ["available"] = false, ["reason"] = reason };

    public static Dictionary<string, object> Build(string receiverToken, string containerToken,
        string sourceFactId, IReadOnlyList<PresentedInventorySlot> actor,
        IReadOnlyList<PresentedInventorySlot> container, PresentedItem? cursor, bool readyToClose, PresentedCloseControl? close = null,
        string? closeUnavailableReason = null, int? actorUsable = null)
    {
        if (!Token(receiverToken) || !Token(containerToken) || string.IsNullOrWhiteSpace(sourceFactId)
            || sourceFactId.Length > 512 || !SlotsValid(actor, MaxActorSlots)
            || !SlotsValid(container, MaxContainerSlots) || cursor is { IsValid: false })
            return Unavailable("invalid_or_unbounded_receiver");
        object[] Slots(IReadOnlyList<PresentedInventorySlot> slots, string side) => slots
            .OrderBy(s => s.Slot).Select(s => (object)new Dictionary<string, object> {
                ["slot"] = s.Slot, ["side"] = side, ["item"] = s.Item?.Facts()!,
                ["bounds"] = new[] { s.X, s.Y, s.Width, s.Height },
                ["target_token"] = Hash(JsonSerializer.Serialize(new object[] {
                    receiverToken, containerToken, side, s.Slot, s.X, s.Y, s.Width, s.Height, s.Item! }))
            }).ToArray();
        var facts = new Dictionary<string, object> {
            ["available"] = true, ["receiver_token"] = receiverToken,
            ["container_token"] = containerToken, ["source_fact_id"] = sourceFactId,
            ["actor_slots"] = Slots(actor, "actor"), ["container_slots"] = Slots(container, "container"),
            ["cursor"] = cursor?.Facts()!, ["ready_to_close"] = readyToClose && cursor is null,
        };
        // PM-1 fix10d (a13 d4:J2312-2319): the actor grid presents 36 buttons, but only Farmer.MaxItems of them are
        // backpack slots (InventoryMenu draws the rest locked; InventoryMenu.leftClick ignores slot >= Count). The
        // capacity term counted the 24 locked slots as free, so a take_out into a full backpack was offered.
        if (actorUsable is { } usable)
        {
            if (usable is < 1 or > MaxActorSlots) return Unavailable("invalid_or_unbounded_receiver");
            facts["actor_usable"] = usable;
        }
        if (close is { IsValid: true } && readyToClose && cursor is null)
            facts["close_target"] = close.Facts(receiverToken, containerToken, sourceFactId);
        else
            // Diagnostic (live B1W1b, 2026-10-07): say WHY no close target is published. Constant vocabulary.
            facts["close_unavailable_reason"] = cursor is not null ? "cursor_item" : !readyToClose ? "not_ready_to_close"
                : close is null ? (closeUnavailableReason is { Length: > 0 and <= 64 } r ? r : "close_unavailable")
                : close.Overlaps ? "ok_overlaps_other_control" : "close_geometry_invalid";
        string content = JsonSerializer.Serialize(facts, ProtocolJson.Options);
        facts["readback_token"] = Hash(content);
        if (JsonSerializer.SerializeToUtf8Bytes(facts, ProtocolJson.Options).Length > MaxBytes)
            return Unavailable("receiver_snapshot_too_large");
        return facts;
    }

    static bool Token(string value) => !string.IsNullOrWhiteSpace(value) && value.Length <= 128;
    static string Hash(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
    static bool SlotsValid(IReadOnlyList<PresentedInventorySlot> slots, int max) =>
        slots.Count is > 0 && slots.Count <= max && slots.Select(s => s.Slot).Distinct().Count() == slots.Count
        && slots.All(s => s.Slot >= 0 && s.Slot < max && s.Width > 0 && s.Height > 0
            && s.X >= 0 && s.Y >= 0 && s.X <= 16384 && s.Y <= 16384 && s.Width <= 16384 && s.Height <= 16384
            && s.Item is not { IsValid: false });
}

public sealed record PresentedInventorySlot(int Slot, int X, int Y, int Width, int Height, PresentedItem? Item);
public sealed record PresentedItem(string QualifiedItemId, int Stack, int Quality, int MaxStack = 0)
{
    public bool IsValid => !string.IsNullOrWhiteSpace(QualifiedItemId) && QualifiedItemId.Length <= 128
        && Stack is > 0 and <= 999999 && Quality is 0 or 1 or 2 or 4
        && MaxStack is >=0 and <=999999;
    public Dictionary<string, object> Facts() {
        var facts=new Dictionary<string,object> {
            ["qualified_item_id"] = QualifiedItemId, ["stack"] = Stack, ["quality"] = Quality };
        if(MaxStack>0)facts["max_stack"]=MaxStack;
        return facts;
    }
}

/// <summary>Geometry of the actual ordinary chest check-button. No inferred focus.</summary>
public sealed record PresentedCloseControl(int X, int Y, int Width, int Height,
    int ViewportWidth, int ViewportHeight, bool Visible, bool NativeHit,
    IReadOnlyList<int[]> OtherBounds)
{
    /// <summary>The OK button overlaps another presented control (for example a visible colour picker).</summary>
    public bool Overlaps => OtherBounds.Any(b => b.Length == 4 && b[2] > 0 && b[3] > 0
        && X < (long)b[0] + b[2] && b[0] < (long)X + Width && Y < (long)b[1] + b[3] && b[1] < (long)Y + Height);

    public bool IsValid => Visible && NativeHit && ViewportWidth is >0 and <=16384
        && ViewportHeight is >0 and <=16384 && X>=0 && Y>=0 && Width>0 && Height>0
        && (long)X+Width<=ViewportWidth && (long)Y+Height<=ViewportHeight
        && OtherBounds.Count<=160 && OtherBounds.All(b => b.Length==4
            && b[2]>=0 && b[3]>=0 && Math.Abs((long)b[0])<=16384 && Math.Abs((long)b[1])<=16384
            && b[2]<=16384 && b[3]<=16384
            && !(b[2]>0 && b[3]>0 && X<(long)b[0]+b[2] && b[0]<(long)X+Width
                && Y<(long)b[1]+b[3] && b[1]<(long)Y+Height));
    public Dictionary<string,object> Facts(string receiver,string container,string source) => new() {
        ["kind"]="ordinary_chest_close", ["bounds"]=new[]{X,Y,Width,Height},
        ["viewport"]=new[]{ViewportWidth,ViewportHeight},
        ["target_token"]=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
            JsonSerializer.Serialize(new object[]{receiver,container,source,X,Y,Width,Height,
                ViewportWidth,ViewportHeight,"ordinary_chest_close"})))).ToLowerInvariant()
    };
}

/// <summary>
/// Pure close-target gate (WS2 B8 fix, live B1W1b): the first refusing term names the reason. A VISIBLE chest
/// colour picker is no longer a refusal: its bounds join the OK button's overlap check
/// (<see cref="WithColorPicker"/>), so only a picker that actually overlaps OK keeps the target unpublished.
/// Decompiled 1.6.15: the picker sits ABOVE the menu (<c>ItemGrabMenu.cs:283/767</c>, y = menu y - 64 - 2*border),
/// and an OK click outside its colour row leaves <c>colorSelection</c> unchanged (<c>DiscreteColorPicker.cs:62-79</c>),
/// so the colour re-assignment in <c>ItemGrabMenu.receiveLeftClick</c> (<c>:911-917</c>) is a no-op while the
/// chest colour already equals the selection (that term stays a refusal).
/// </summary>
public static class ContainerCloseGate
{
    public static string? FirstRefusal(IEnumerable<(bool Refused, string Reason)> terms)
    {
        foreach (var (refused, reason) in terms)
            if (refused) return reason;
        return null;
    }

    public static IReadOnlyList<int[]> WithColorPicker(IReadOnlyList<int[]> others, bool pickerPresent, bool pickerVisible,
        int[] pickerBounds) => pickerPresent && pickerVisible ? others.Append(pickerBounds).ToArray() : others;
}
