using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>Production read/click adapter over the exact vanilla receiver. Linked against doubles in ProtocolTests.</summary>
internal sealed class NativePurchaseAnimals
{
    readonly PurchaseAnimalsMenu menu;
    readonly Farmer actor;
    readonly Func<object, string> token;
    readonly string receiver;
    readonly Func<string, string?>? shopName;
    Func<PurchaseAdoption?>? readAdoption;
    /// <param name="shopName">Native shop display name of a Data/FarmAnimals id (FarmAnimal.GetDisplayName(id, forShop: true)).</param>
    public NativePurchaseAnimals(PurchaseAnimalsMenu menu, Farmer actor, string receiver, Func<object, string> token,
        Func<string, string?>? shopName = null)
    { this.menu = menu; this.actor = actor; this.receiver = receiver; this.token = token; this.shopName = shopName; }

    // Live gap8a S26: the button hoverText is the data id ("White Chicken"); the owner names the shop's own display
    // ("Chicken"). Publish both; a lookup fault publishes no display rather than a guess.
    string? ShopName(string id)
    {
        try { return shopName?.Invoke(id) is { Length: > 0 and <= 128 } name ? name : null; }
        catch { return null; }
    }

    public bool Owns(object? m, Farmer p) => ReferenceEquals(menu, m) && ReferenceEquals(actor, p);
    public Dictionary<string, object?> ReturnState() => new()
    {
        ["receiver"] = receiver, ["viewing_target"] = menu.onFarm, ["frozen"] = menu.freeze,
        ["fading"] = Game1.IsFading(), ["warping"] = Game1.isWarping || Game1.locationRequest is not null,
        ["location"] = Game1.currentLocation?.NameOrUniqueName,
        ["menu_type"] = Game1.activeClickableMenu?.GetType().Name, ["cursor_empty"] = actor.CursorSlotItem is null,
    };

    public static TextBox? NamingBox(object? top) => top is PurchaseAnimalsMenu m
        && m.GetType() == typeof(PurchaseAnimalsMenu) && m.namingAnimal ? m.textBox : null;

    public static void NamingEnter(object top)
    {
        if (top is not PurchaseAnimalsMenu m || NamingBox(top) is not { } box
            || !ReferenceEquals(Game1.activeClickableMenu, m) || m.freeze || Game1.IsFading() || Game1.isWarping || Game1.locationRequest is not null
            || m.newAnimalHome?.GetIndoors() is not AnimalHouse house || house.isFull()
            || m.newAnimalHome.isUnderConstruction() || m.animalBeingPurchased is not { } animal
            || !animal.CanLiveIn(m.newAnimalHome) || Game1.player.Money < m.priceOfAnimal
            || Game1.textEntry is not null || Game1.keyboardDispatcher?.Subscriber is { } subscriber && !ReferenceEquals(subscriber, box)
            || Game1.player.CursorSlotItem is not null || Utility.areThereAnyOtherAnimalsWithThisName(box.Text))
            throw new InvalidOperationException("animal naming receiver changed");
        m.textBoxEnter(box);
    }
    string HomeRef(object building, object house) => token(building) + ":" + token(house);
    PurchaseAnimal Animal(FarmAnimal a) => new(a.myID.Value.ToString(), token(a), a.type.Value ?? "", a.Name,
        a.ownerID.Value.ToString(), a.homeInterior?.NameOrUniqueName ?? "");

    public PurchaseAnimalsFacts Read()
    {
        var m = menu;
        bool sameActor = ReferenceEquals(Game1.player, actor);
        bool active = ReferenceEquals(Game1.activeClickableMenu, m) && sameActor;
        if (!active)
            return new("return", actor.UniqueMultiplayerID.ToString(), m.TargetLocation.NameOrUniqueName, actor.Money,
                m.priceOfAnimal, m.animalBeingPurchased is { } prior ? Animal(prior) : null,
                Array.Empty<PurchaseSpecies>(), Array.Empty<PurchaseHome>(), Array.Empty<string>(), false, sameActor ? readAdoption?.Invoke() : null);
        string phase = !active ? "return" : m.freeze || Game1.IsFading() || Game1.isWarping || Game1.locationRequest is not null ? "transition"
            : m.namingAnimal ? "naming" : m.onFarm ? "home" : "species";
        var homes = new List<PurchaseHome>();
        if (m.animalBeingPurchased is { } animal && m.TargetLocation.buildings.Count <= 32)
            foreach (var b in m.TargetLocation.buildings)
            {
                if (b.GetIndoors() is not AnimalHouse h || b.isUnderConstruction() || !animal.CanLiveIn(b)
                    || h.isFull() || h.animalsThatLiveHere.Count > 64) continue;
                homes.Add(new(HomeRef(b, h), b.buildingType.Value ?? "", h.NameOrUniqueName, b.tileX.Value, b.tileY.Value,
                    b.tilesWide.Value, b.tilesHigh.Value, h.animalLimit.Value,
                    h.animalsThatLiveHere.Select(id => id.ToString()).OrderBy(id => id).ToArray()));
            }
        if (active && phase == "naming" && readAdoption is null && m.newAnimalHome is { } home
            && home.GetIndoors() is AnimalHouse house && m.animalBeingPurchased is { } pending)
        {
            string[] before = house.animalsThatLiveHere.Select(id => id.ToString()).OrderBy(id => id).ToArray();
            int money = actor.Money, price = m.priceOfAnimal;
            string building = HomeRef(home, house);
            string pendingId = pending.myID.Value.ToString();
            // #320: the debit / roster pair is frozen at the first read that passes the exact transaction boundary, so a
            // later independent wallet / roster change (shared money, another purchase) never rewrites the paid debit.
            // Identity, same-instance and home stay fresh on every read; no boundary pass => live values (uncertain).
            (int MoneyAfter, string[] After)? debit = null;
            readAdoption = () =>
            {
                if (!house.animals.TryGetValue(pending.myID.Value, out var actual))
                    return null;
                string[] after = house.animalsThatLiveHere.Select(id => id.ToString()).OrderBy(id => id).ToArray();
                bool same = ReferenceEquals(actual, pending);
                bool inHouse = ReferenceEquals(actual.homeInterior, house)
                    && ReferenceEquals(actual.currentLocation, house) && ReferenceEquals(home.GetIndoors(), house)
                    && m.TargetLocation.buildings.Contains(home);
                int now = actor.Money;
                if (debit is null && PurchaseAnimalsUi.DebitBoundary(price, money, now, before, after, pendingId, same, inHouse))
                    debit = (now, after);
                return new PurchaseAdoption(receiver, actor.UniqueMultiplayerID.ToString(), building, price, money,
                    debit?.MoneyAfter ?? now, Animal(actual), before, debit?.After ?? after, same, inHouse);
            };
        }
        var allNames = new List<string>();
        Utility.ForEachLocation(l => { allNames.AddRange(l.animals.Values.Select(a => a.displayName)); return allNames.Count <= 256; });
        var names = allNames.Take(257).ToArray();
        object? subscriber = Game1.keyboardDispatcher?.Subscriber;
        bool ready = active && !m.readOnly && !m.freeze && !Game1.IsFading() && !Game1.isWarping && Game1.locationRequest is null
            && !Game1.eventUp && Game1.textEntry is null
            && (subscriber is null || m.namingAnimal && ReferenceEquals(subscriber, m.textBox))
            && actor.CursorSlotItem is null && names.Length <= 256 && m.TargetLocation.buildings.Count <= 32;
        var rows = m.animalsToPurchase.Where(c => c.visible).Take(48).Select(c => new PurchaseSpecies(c.myID,
            c.hoverText, c.item.salePrice(), c.item is StardewValley.Object o && o.Type is null, ShopName(c.hoverText))).ToArray();
        return new(phase, actor.UniqueMultiplayerID.ToString(), m.TargetLocation.NameOrUniqueName, actor.Money,
            m.priceOfAnimal, m.animalBeingPurchased is { } a ? Animal(a) : null, rows, homes, names, ready, readAdoption?.Invoke(), m.newAnimalHome is { } selected && selected.GetIndoors() is { } selectedHouse ? HomeRef(selected, selectedHouse) : null,
            m.animalsToPurchase.FirstOrDefault(c => c.myID == m.clickedAnimalButton)?.hoverText);
    }

    public IReadOnlyList<UiPadComponent> Components()
    {
        var source = (menu.allClickableComponents ?? menu.animalsToPurchase.Cast<ClickableComponent>()
            .Concat(new ClickableComponent[] { menu.okButton, menu.upArrow, menu.downArrow, menu.doneNamingButton,
                menu.randomButton, menu.textBoxCC }).ToList()).Where(c => c.visible && c.myID >= 0).ToList();
        var rows = source.Select(c => new UiPadComponent(c.myID,
            c is ClickableTextureComponent t && menu.animalsToPurchase.Contains(t) ? "animal_species" : "button",
            c is ClickableTextureComponent texture ? texture.hoverText ?? c.name ?? "" : c.name ?? "",
            Up: c.upNeighborID, Right: c.rightNeighborID, Down: c.downNeighborID, Left: c.leftNeighborID,
            X: c.bounds.X, Y: c.bounds.Y, W: c.bounds.Width, H: c.bounds.Height)).ToList();
        // PurchaseAnimalsMenu uses IClickableMenu's base automatic snap. Publish those concrete native-data edges;
        // Python never guesses sentinel neighbours or geometry. Execution still reads back the native snap.
        return rows.Select(c => {
            var native = source.First(n => n.myID == c.Id);
            int Edge(int direction, int raw) => raw != UiPadNav.AutoSnap ? raw : JunimoNavigation.Resolve(c, direction,
                rows.Where(candidate => menu.IsAutomaticSnapValid(direction, native, source.First(n => n.myID == candidate.Id))), true);
            return c with { Up = Edge(0,c.Up), Right = Edge(1,c.Right), Down = Edge(2,c.Down), Left = Edge(3,c.Left) };
        }).ToArray();
    }

    // Caller holds the existing inactive cursor guard. Only the menu's native click receives these local pixels.
    public void Click(UiPadFacts before, UiPadStep step)
    {
        var current = Read();
        if (!Owns(Game1.activeClickableMenu, Game1.player) || before.PurchaseAnimals?.Digest() != current.Digest()
            || !current.Ready) throw new InvalidOperationException("animal receiver changed");
        if (step.Button == "B")
        {
            if (menu.namingAnimal) menu.receiveGamePadButton(Microsoft.Xna.Framework.Input.Buttons.B);
            else { var c = menu.okButton.bounds.Center; menu.receiveLeftClick(c.X, c.Y); }
            return;
        }
        if (current.Phase == "species")
        {
            var c = menu.animalsToPurchase.Single(c => c.visible && c.myID == before.SnappedId).bounds.Center;
            menu.receiveLeftClick(c.X, c.Y);
            return;
        }
        var offered = current.Buildings.Single(b => b.Ref == step.G3Arg);
        // Invert PurchaseAnimalsMenu.receiveLeftClick's UI-scale -> world-tile mapping; use the actual footprint.
        for (int ty = offered.Y; ty < offered.Y + offered.Height; ty++)
            for (int tx = offered.X; tx < offered.X + offered.Width; tx++)
            {
                int x = (int)Utility.ModifyCoordinateForUIScale((tx + .5f) * Game1.tileSize - Game1.viewport.X);
                int y = (int)Utility.ModifyCoordinateForUIScale((ty + .5f) * Game1.tileSize - Game1.viewport.Y);
                // Native receiveLeftClick examines these controls before the map. Never accidentally cancel/scroll.
                if (menu.okButton.containsPoint(x, y) || menu.upArrow.containsPoint(x, y) || menu.downArrow.containsPoint(x, y)) continue;
                if (menu.TargetLocation.getBuildingAt(new Vector2(tx, ty)) is not { } native
                    || native.GetIndoors() is not { } nativeHouse || HomeRef(native, nativeHouse) != offered.Ref) continue;
                menu.receiveLeftClick(x, y);
                return;
            }
        throw new InvalidOperationException("animal building has no unobstructed native click point");
    }
}
