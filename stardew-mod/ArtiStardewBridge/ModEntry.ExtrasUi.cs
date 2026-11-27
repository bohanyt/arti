using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;

namespace ArtiStardewBridge;

// Primitives v2 WS-G6: read-only facts of the G6 menu contexts (MineElevatorMenu, JunimoNoteMenu, MuseumMenu) for
// WS-G1's ui_inspect / ui_nav / ui_press (Protocol/UiPadExtras.cs decides), plus the museum place point. Field reads and
// pure getters only (canAcceptThisItem / isTileSuitableForMuseumPiece / highlightMethod read state); never
// performHoverAction, never a write. Flag WorldExtrasEnabled (default OFF): off = the G1 facts are unchanged.
// Evidence ceiling: UNIT_TESTED (pure table) + LOCAL_BUILT. HYPOTHESIS until the G6 live witnesses.
public sealed partial class ModEntry
{
    readonly record struct G6Component(string Kind, string Label, Item? Item);

    // WS-G6 (review MINOR 4): the one question receiver an extras press opened, by reference, with its key.
    IClickableMenu? extrasPinnedQuestion;
    string? extrasPinnedKey;
    Farmer? extrasPinnedActor;
    GameLocation? extrasPinnedLocation;
    CommandInboxIdentity? extrasPinnedIdentity;
    object? extrasPinnedTv;
    GameLocation? extrasPinnedTvLocation;
    IClickableMenu? extrasPinnedTvDialogue;
    string? extrasPinnedTvQuestionToken;

    void PinExtrasQuestion(string? extraKind)
    {
        string? key = WorldExtras.QuestionKeyOf(extraKind);
        this.extrasPinnedActor = Game1.player;
        this.extrasPinnedLocation = Game1.currentLocation;
        this.extrasPinnedIdentity = this.CurrentCommandIdentity();
        this.extrasPinnedQuestion = key is not null ? Game1.activeClickableMenu as DialogueBox : null;
        this.extrasPinnedKey = this.extrasPinnedQuestion is null ? null : key;
        this.extrasPinnedTv = key == "TV" ? Game1.currentLocation?.afterQuestion?.Target : null;
        this.extrasPinnedTvLocation = key == "TV" ? Game1.currentLocation : null;
        this.extrasPinnedTvDialogue = null;
        this.extrasPinnedTvQuestionToken = key == "TV" ? this.ReceiverTokenOf(this.extrasPinnedQuestion) : null;
    }

    bool TvDialogueCallbackPinned => this.config.WorldExtrasEnabled && UiKeyAssemblyPinned
        && this.extrasPinnedTv is StardewValley.Objects.TV
        && UiKeyInput.TvCallbackOwned(this.extrasPinnedTv, this.extrasPinnedTvLocation,
            Game1.currentLocation, Game1.afterDialogues);

    string? TvDialogueChain(IClickableMenu? menu) => menu is DialogueBox { isQuestion: false, characterDialogue: null }
        && this.TvDialogueCallbackPinned
        ? UiKeyInput.TvDialogueChainToken(menu, this.extrasPinnedTvDialogue, this.extrasPinnedTvQuestionToken) : null;

    /// <summary>The menu is the pinned extras question and the location's question key is still its key.</summary>
    bool ExtrasQuestionPinned(object? menu) => this.config.WorldExtrasEnabled && menu is not null
        && this.extrasPinnedQuestion is { } pinned && ReferenceEquals(menu, pinned)
        && (this.extrasPinnedKey is not ("Blacksmith" or "Marnie") || ReferenceEquals(Game1.player, this.extrasPinnedActor)
            && ReferenceEquals(Game1.currentLocation, this.extrasPinnedLocation)
            && this.extrasPinnedIdentity is { } identity
            && this.CurrentCommandIdentity() with { ContextGeneration = identity.ContextGeneration } == identity)
        && (this.extrasPinnedKey == "TV"
            ? Game1.currentLocation?.lastQuestionKey is null && this.extrasPinnedTv is StardewValley.Objects.TV
                && ReferenceEquals(Game1.currentLocation, this.extrasPinnedTvLocation)
                && ReferenceEquals(Game1.currentLocation?.afterQuestion?.Target, this.extrasPinnedTv)
            : string.Equals(Game1.currentLocation?.lastQuestionKey, this.extrasPinnedKey, StringComparison.Ordinal));

    /// <summary>The G6 label of a non-slot component of the three menus (null = not a G6 component).</summary>
    static G6Component? G6ComponentKind(IClickableMenu owner, ClickableComponent c)
    {
        switch (owner)
        {
            case MineElevatorMenu elevator when elevator.elevators.Contains(c):
                return new G6Component(UiPadExtrasTable.KindFloor, c.name ?? "", null);
            case JunimoNoteMenu junimo:
                if (c is Bundle bundle && junimo.bundles.Contains(bundle))
                    return new G6Component(UiPadExtrasTable.KindBundle,
                        !string.IsNullOrWhiteSpace(bundle.label) ? bundle.label : bundle.name ?? "", null);
                if (c is ClickableTextureComponent slot && junimo.ingredientSlots.Contains(slot))
                    return new G6Component(UiPadExtrasTable.KindIngredient, slot.item?.DisplayName ?? "empty bundle slot", slot.item);
                if (ReferenceEquals(c, junimo.backButton))
                    return new G6Component(UiPadExtrasTable.KindBack, "back", null);
                if (ReferenceEquals(c, junimo.purchaseButton))
                    return new G6Component(UiPadExtrasTable.KindPurchase, "purchase", null);
                if (ReferenceEquals(c, junimo.presentButton))
                    return new G6Component(UiPadExtrasTable.KindPresent, "rewards", null);
                if (ReferenceEquals(c, junimo.areaNextButton) || ReferenceEquals(c, junimo.areaBackButton))
                    return new G6Component(UiPadExtrasTable.KindArea, "area page", null);
                return null;
            case MuseumMenu museum when ReferenceEquals(c, museum.okButton):
                return new G6Component(UiPadExtrasTable.KindOk, "OK", null);
            default:
                return null;
        }
    }

    UiPadExtrasFacts ReadUiPadExtrasFacts(IClickableMenu top)
    {
        var facts = new UiPadExtrasFacts(TimeOfDay: Game1.timeOfDay, CurrentMineLevel: Game1.CurrentMineLevel);
        switch (top)
        {
            case LetterViewerMenu letter:
                return facts with { LetterQuest = ReadLetterQuest(letter, Game1.player) }; // W1 gap B4 (ModEntry.QuestCapture.cs)
            case JunimoNoteMenu junimo:
            {
                if (junimo.scrambledText)
                    return facts with { JunimoScrambled = true, JunimoCanClick = JunimoNoteMenu.canClick };
                Item? held = junimo.heldItem;
                Bundle? page = junimo.currentPageBundle;
                var accepting = new List<int>();
                if (junimo.specificBundlePage && held is not null && page is not null)
                    foreach (ClickableTextureComponent slot in junimo.ingredientSlots)
                        if (slot.item is null && page.canAcceptThisItem(held, slot))
                            accepting.Add(slot.myID);
                BundlePageFacts? bundleFacts = null;
                if (junimo.specificBundlePage && page is not null && page.ingredients.Count <= 16)
                {
                    // CommunityCenter.bundles is this same world-state field (CommunityCenter.cs:104).
                    // Read the actual net array independently of the menu-local ingredient descriptions.
                    Game1.netWorldState.Value.Bundles.FieldDict.TryGetValue(page.bundleIndex, out var persistent);
                    var ingredients = page.ingredients.Select((ingredient, index) =>
                    {
                        var matches = new List<int>();
                        // Flavored-item matching invokes an item query; leave those unsupported rather than run RNG in a reader.
                        if (ingredient.preservesId is null && !ingredient.completed && page.depositsAllowed)
                            foreach (ClickableComponent slot in junimo.inventory.inventory)
                                if (int.TryParse(slot.name, out int n) && n >= 0 && n < junimo.inventory.actualInventory.Count
                                    && junimo.inventory.actualInventory[n] is { } item && item.Stack >= ingredient.stack
                                    && page.IsValidItemForThisIngredientDescription(item, ingredient)) matches.Add(slot.myID);
                        return new BundleIngredientFacts(index, ingredient.category?.ToString() ?? ItemRegistry.QualifyItemId(ingredient.id) ?? ingredient.id,
                            ingredient.stack, ingredient.quality, ingredient.completed, matches,
                            junimo.ingredientList.FirstOrDefault(c => c.myID == 1000 + index)?.hoverText ?? ingredient.id ?? "category",
                            PersistentCompleted: persistent is not null && index < persistent.Length ? persistent[index] : null);
                    }).ToArray();
                    bundleFacts = new BundlePageFacts(page.bundleIndex, page.label ?? page.name, page.depositsAllowed,
                        ingredients, junimo.ingredientSlots.Where(s => s.item is null).Select(s => s.myID).ToArray(), accepting);
                }
                return facts with
                {
                    JunimoSpecificPage = junimo.specificBundlePage,
                    JunimoCanClick = JunimoNoteMenu.canClick,
                    JunimoScrambled = junimo.scrambledText,
                    JunimoPartialDonation = junimo.partialDonationItem is not null,
                    JunimoBundleBusy = junimo.specificBundlePage && page is { completionTimer: > 0 },
                    AcceptingSlots = accepting,
                    BundlePage = bundleFacts,
                };
            }
            case MuseumMenu museum:
            {
                bool holdingPiece;
                try { holdingPiece = this.Helper.Reflection.GetField<bool>(museum, "holdingMuseumPiece").GetValue(); }
                catch { holdingPiece = true; } // unreadable => the strictest reading (refused)
                var donatable = new List<int>();
                InventoryMenu inventory = museum.inventory;
                foreach (ClickableComponent slot in inventory.inventory)
                {
                    if (!int.TryParse(slot.name, out int index) || index < 0 || index >= inventory.actualInventory.Count)
                        continue;
                    Item? item = inventory.actualInventory[index];
                    if (item is not null && inventory.highlightMethod(item) && museum.Museum.isItemSuitableForDonation(item))
                        donatable.Add(slot.myID);
                }
                return facts with
                {
                    MuseumReady = museum.state == 1 && museum.fadeTimer <= 0 && !museum.menuMovingDown,
                    MuseumHoldingPiece = holdingPiece,
                    MuseumFreeTile = museum.heldItem is not null && MuseumPlacePoint(museum) is not null,
                    DonatableSlots = donatable,
                };
            }
            default:
                return facts;
        }
    }

    /// <summary>
    /// The UI point of a free suitable museum tile (visible viewport, interior first) that the menu's own placement
    /// branch accepts (MuseumMenu.receiveLeftClick: y bound, not over the inventory or OK button). null = none.
    /// fix54G: one picker (Protocol/MuseumSpot) for the WS-G6 MuseumPlace step and the generic museum spot.
    /// </summary>
    static MuseumSpot.Spot? MuseumPlaceSpot(MuseumMenu museum)
    {
        LibraryMuseum hall = museum.Museum;
        if (hall is null)
            return null;
        xTile.Dimensions.Rectangle view = Game1.viewport;
        int limit = Game1.viewport.Height - (museum.height - (IClickableMenu.borderWidth + IClickableMenu.spaceToClearTopBorder + 192));
        return MuseumSpot.Pick(view.X, view.Y, view.Width, view.Height, hall.isTileSuitableForMuseumPiece,
            Utility.ModifyCoordinateForUIScale, Utility.ModifyCoordinateFromUIScale,
            (x, y) => y >= limit || museum.inventory.getInventoryPositionOfClick(x, y) >= 0
                || museum.okButton?.containsPoint(x, y) == true);
    }

    static Point? MuseumPlacePoint(MuseumMenu museum) => MuseumPlaceSpot(museum) is { } spot ? new Point(spot.UiX, spot.UiY) : null;

    /// <summary>
    /// fix54G (G1): under the generic any-menu UI the museum floor is no component, so a picked item could never be
    /// placed. Publish the one free tile the native predicates admit as an accepts-item control; pressing it is the
    /// ordinary generic left click (receiveLeftClick at its Center), the same call as the WS-G6 MuseumPlace step. Only
    /// while the menu holds an item it would accept (isItemSuitableForDonation) and is not fading. Credit stays with
    /// NATIVE_PROGRESS museum_donation (museumPieces + inventory delta), never with this press.
    /// </summary>
    static void AddMuseumSpot(GenericGraph graph)
    {
        if (graph.Dispatch is not MuseumMenu museum || museum.heldItem is not { } held || museum.fadeTimer > 0
            || museum.Museum is not { } hall || !hall.isItemSuitableForDonation(held) || MuseumPlaceSpot(museum) is not { } spot)
            return;
        var (x, y, w, h) = MuseumSpot.Bounds(spot, (int)Utility.ModifyCoordinateForUIScale(MuseumSpot.Tile));
        var bounds = new Rectangle(x, y, w, h);
        graph.Widgets.Add(new GenericWidget(graph.Widgets.Count, MuseumSpot.FieldPath, new ClickableComponent(bounds, MuseumSpot.Name),
            null, bounds, MuseumSpot.Label(spot, held.DisplayName), "button", null, museum, AcceptsItem: true));
    }
}
