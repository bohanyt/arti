using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    NativePurchaseAnimals? purchaseAnimals;
    CommandInboxIdentity? purchaseAnimalsIdentity;

    bool PurchaseIdentityCurrent => this.purchaseAnimalsIdentity is { } pin
        && this.CurrentCommandIdentity() with { ContextGeneration = pin.ContextGeneration, LocationId = pin.LocationId } == pin;

    PurchaseAnimalsFacts? ReadPurchaseAnimals(IClickableMenu? top)
    {
        if (!this.ExtrasEnabled || !this.UiG3On || Game1.player is not { } player) return null;
        if (!this.PurchaseIdentityCurrent) this.purchaseAnimals = null;
        if (top is PurchaseAnimalsMenu m && m.GetType() == typeof(PurchaseAnimalsMenu))
        {
            if (this.purchaseAnimals?.Owns(m, player) != true)
            {
                this.purchaseAnimals = new(m, player, this.ReceiverTokenOf(m)!, o => this.animalTargetInstances.Single(o),
                    id => FarmAnimal.GetDisplayName(id, forShop: true));
                this.purchaseAnimalsIdentity = this.CurrentCommandIdentity();
            }
            return this.purchaseAnimals.Read();
        }
        return null;
    }

    void EnrichPurchaseAnimals(Dictionary<string, object> snapshot)
    {
        this.ReadPurchaseAnimals(Game1.activeClickableMenu);
        if (this.PurchaseIdentityCurrent && this.purchaseAnimals is { } adapter && adapter.Read().Adoption is { } ledger)
        {
            snapshot["animal_purchase"] = ledger;
            var transition = adapter.ReturnState();
            transition["return_receiver"] = Game1.activeClickableMenu is DialogueBox { isQuestion: false } box
                && box.GetType() == typeof(DialogueBox) && box.characterDialogue?.speaker?.Name == "Marnie"
                ? this.ReceiverTokenOf(box) : null;
            snapshot["animal_purchase_transition"] = transition;
        }
    }

    void ExecutePurchaseAnimals(MenuStepCommandRequest request, UiPadFacts facts, UiPadPlan plan, string receiver)
    {
        var calls = new List<string>();
        UiPadVerdict verdict;
        try
        {
            if (!PadCursorWarpGuard.Verified || !this.PurchaseIdentityCurrent) // ADR 244: no focus term
                throw new InvalidOperationException("animal click without the cursor guard");
            calls.Add("PurchaseAnimalsMenu.native_click");
            Game1.PushUIMode();
            try { this.purchaseAnimals!.Click(facts, plan.Steps.Single()); }
            finally { Game1.PopUIMode(); }
            var after = this.ReadUiPadFacts();
            string? effect = UiPadExecutor.PressEffect(facts, after);
            verdict = new(effect is not null ? UiPadVerdict.PressApplied : UiPadVerdict.PressNoEffect,
                effect is not null, effect, calls, effect ?? "no_effect", after.SnappedId, after.MenuType, after.ReceiverToken);
        }
        catch (Exception ex)
        {
            verdict = new(UiPadVerdict.PressUncertain, false, null, calls, "animal_click_fault:" + ex.GetType().Name, null, null, null);
        }
        this.FinishUiPad(request, verdict, receiver);
    }
}
