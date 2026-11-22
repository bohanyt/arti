using ArtiStardewBridge.Protocol;
using StardewValley;
using StardewValley.Objects;
using StardewValley.Tools;
using SObject = StardewValley.Object;

namespace ArtiStardewBridge.Observation;

/// <summary>
/// Adapter read-only OBS-3A. HARUS dipanggil dari game thread.
///
/// Membaca Farmer.Items + equipment publik, membangun
/// <see cref="InventorySnapshotInput"/> imutabel, lalu menyerahkan ke
/// builder Protocol. Tidak mutasi tas, tidak equip, tidak klik, tidak
/// menu handler.
///
/// Project ProtocolTests hanya mengompilasi Protocol/**/*.cs — PASS C#
/// BUKAN bukti berkas ini kompil terhadap Stardew.
/// </summary>
public static class InventorySnapshotCapture
{
    public static InventorySnapshotInput Read(Farmer farmer)
    {
        IList<Item> items = farmer.Items;
        InventorySlotInput[] slots = new InventorySlotInput[items.Count];
        for (int i = 0; i < items.Count; i++)
            slots[i] = new InventorySlotInput { Slot = i, Item = View(items[i]) };

        return new InventorySnapshotInput
        {
            ActiveSlot = farmer.CurrentToolIndex,
            Slots = slots,
            Equipment = new[]
            {
                Equip(InventoryEquipmentLocation.Hat, farmer.hat?.Value),
                Equip(InventoryEquipmentLocation.Shirt, farmer.shirtItem?.Value),
                Equip(InventoryEquipmentLocation.Pants, farmer.pantsItem?.Value),
                Equip(InventoryEquipmentLocation.Boots, farmer.boots?.Value),
                Equip(InventoryEquipmentLocation.LeftRing, farmer.leftRing?.Value),
                Equip(InventoryEquipmentLocation.RightRing, farmer.rightRing?.Value),
            },
        };
    }

    static InventoryEquipmentInput Equip(string location, Item? item) =>
        new() { Location = location, Item = View(item) };

    static InventoryItemView? View(Item? item)
    {
        if (item is null)
            return null;

        string qid = item.QualifiedItemId ?? "";
        if (string.IsNullOrWhiteSpace(qid))
        {
            string raw = item.ItemId ?? "";
            if (!string.IsNullOrWhiteSpace(raw))
            {
                string qualified = ItemRegistry.QualifyItemId(raw);
                qid = string.IsNullOrWhiteSpace(qualified) ? raw : qualified;
            }
        }

        string? toolSubtype = null;
        int? upgrade = null;
        string? weaponSubtype = null;
        int? waterLeft = null, waterMax = null;
        int? attachmentSlots = null;
        IReadOnlyList<InventoryAttachmentView>? attachments = null;
        string? instanceToken = null;
        if (item is MeleeWeapon weapon)
        {
            toolSubtype = nameof(MeleeWeapon);
            upgrade = weapon.UpgradeLevel;
            weaponSubtype = WeaponSubtype(weapon);
        }
        else if (item is Tool tool)
        {
            toolSubtype = tool.GetType().Name;
            upgrade = tool.UpgradeLevel;
            if (tool is WateringCan can)
            {
                // WS1: WaterLeft already reports waterCanMax for a bottomless can.
                waterLeft = can.WaterLeft;
                waterMax = can.waterCanMax;
            }
            if (tool.AttachmentSlotsCount > 0)
            {
                // S31: native attachment slots (FishingRod bait slot 0, tackle 1+), explicit empty, plus a
                // lifetime instance token. Read only: never attach/detach here.
                attachmentSlots = tool.AttachmentSlotsCount;
                attachments = Attachments(tool, attachmentSlots.Value);
                instanceToken = ToolTokens.Token(tool);
            }
        }

        int quality = 0;
        if (item is SObject obj)
            quality = obj.Quality;

        return new InventoryItemView
        {
            QualifiedItemId = qid,
            Name = item.Name,
            DisplayName = item.DisplayName,
            Stack = item.Stack,
            Quality = quality,
            Category = item.Category,
            TypeDefinitionId = item.TypeDefinitionId ?? "",
            ToolSubtype = toolSubtype,
            ToolUpgradeLevel = upgrade,
            WeaponSubtype = weaponSubtype,
            ToolWaterLeft = waterLeft,
            ToolWaterMax = waterMax,
            ToolAttachmentSlots = attachmentSlots,
            Attachments = attachments,
            ToolInstanceToken = instanceToken,
        };
    }

    // S31: one weak token per tool instance for the life of the process (all screens share it).
    static readonly InventoryInstanceTokens ToolTokens = new();
    internal static string ToolToken(Tool tool) => ToolTokens.Token(tool);

    static IReadOnlyList<InventoryAttachmentView> Attachments(Tool tool, int count)
    {
        int bounded = Math.Min(count, InventoryObservationCaps.Attachments);
        InventoryAttachmentView[] views = new InventoryAttachmentView[bounded];
        for (int i = 0; i < bounded; i++)
        {
            SObject? attached = i < tool.attachments.Length ? tool.attachments[i] : null;
            if (attached is null)
            {
                views[i] = new InventoryAttachmentView { Slot = i };
                continue;
            }
            views[i] = new InventoryAttachmentView
            {
                Slot = i,
                QualifiedItemId = attached.QualifiedItemId ?? "",
                Stack = attached.Stack,
                Quality = attached.Quality,
                Category = attached.Category,
                // Tackle wear only (FishingRod.maxTackleUses applies to category -22).
                Uses = attached.Category == SObject.tackleCategory ? attached.uses.Value : null,
            };
        }
        return views;
    }

    static string WeaponSubtype(MeleeWeapon weapon)
    {
        int type = weapon.type.Value;
        return type switch
        {
            MeleeWeapon.dagger => "dagger",
            MeleeWeapon.club => "club",
            MeleeWeapon.defenseSword => "sword",
            _ => "weapon",
        };
    }
}
