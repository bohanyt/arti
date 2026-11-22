using System.Globalization;
using System.Text.Json;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewValley.Network;

namespace ArtiStardewBridge;

/// <summary>
/// Live7 character creation wiring (game thread only; docs/stardew/live7/CHARACTER_CREATION.md). The table, policy,
/// executor and snapshot are pure (<see cref="CharCustomPolicy"/>, <see cref="CharCustomExecutor"/>,
/// <see cref="CharCustomSnapshot"/>); this file reads the farmhand CharacterCustomization (Source.NewFarmhand only)
/// into <see cref="CharCustomFacts"/> with field reads and pure getters, publishes the read-only ui.context
/// <c>farmer_profile</c> and the pre-world <c>join_screen.json</c>, and implements <see cref="ICharCustomPort"/> with
/// the menu's own receiveLeftClick + releaseLeftClick at a point computed from the native component's bounds (checked
/// to hit that component only) and the WS-G3 text-entry primitive on the private name / favourite TextBox. Every click
/// asserts the inactive OS-cursor guard. Never Helper.Input, never a pad Press, never Random, never an
/// activeClickableMenu assignment, never a write to a farmer field. Flag <c>CharacterCreationEnabled</c> (default OFF,
/// needs UiPadMenusEnabled). Evidence ceiling: UNIT_TESTED.
/// </summary>
public sealed partial class ModEntry
{
    private readonly Dictionary<string, string> charCustomLabelCache = new(StringComparer.Ordinal);
    private long? joinScreenLastTick;
    private bool joinScreenFaultLogged;
    private bool charCustomReadFaultLogged;

    bool CharCustomOn => this.config.Enabled && this.config.UiPadMenusEnabled && this.config.CharacterCreationEnabled;

    TextBox? CharCustomBox(CharacterCustomization menu, string field) =>
        this.Helper.Reflection.GetField<TextBox>(menu, field, required: false)?.GetValue();

    static ClickableComponent? CharCustomNamed(IEnumerable<ClickableComponent>? list, string name) =>
        list?.FirstOrDefault(c => c is not null && c.visible && string.Equals(c.name, name, StringComparison.Ordinal));

    static ColorPicker? CharCustomPicker(CharacterCustomization menu, string picker) => picker switch
    {
        CharCustomTable.PickerEye => menu.eyeColorPicker,
        CharCustomTable.PickerHair => menu.hairColorPicker,
        _ => menu.pantsColorPicker,
    };

    static ClickableComponent? CharCustomColorComponent(CharacterCustomization menu, string picker, int bar)
    {
        int id = CharCustomTable.ColorComponentId(picker, bar);
        return menu.colorPickerCCs?.FirstOrDefault(c => c is not null && c.myID == id);
    }

    static CcRect CharCustomRect(ClickableComponent c) => new(c.bounds.X, c.bounds.Y, c.bounds.Width, c.bounds.Height);

    static bool CharCustomOver(TextBox? box, int x, int y) =>
        box is not null && new Rectangle(box.X, box.Y, box.Width, box.Height).Contains(x, y);

    /// <summary>Display names of shirt / pants options (cached; a lookup fault publishes the id, never a guess).</summary>
    string CharCustomLabel(string qualified)
    {
        if (this.charCustomLabelCache.TryGetValue(qualified, out string? cached))
            return cached;
        string label;
        try { label = ItemRegistry.GetData(qualified)?.DisplayName is { Length: > 0 } name ? name : qualified; }
        catch { label = qualified; }
        if (this.charCustomLabelCache.Count < 2048)
            this.charCustomLabelCache[qualified] = label;
        return label;
    }

    /// <summary>Shirt / pants as the menu sees them: <paramref name="native"/> = shirt / pants.Value (rotateShirt /
    /// rotatePantStyle index it; a fresh farmhand has "-1" = no override), <paramref name="displayed"/> = GetShirtId() /
    /// GetPantsId() (the item shown and kept by OK).</summary>
    CharCustomCycle CharCustomClothing(IReadOnlyList<string> ids, string? native, string? displayed, string prefix) =>
        CharCustomCycle.Clothing(ids.ToArray(), native, displayed, ids.Select(id => this.CharCustomLabel(prefix + id)).ToArray());

    // ------------------------------------------------------------------ read-only facts

    /// <summary>One read of the farmhand CharacterCustomization (null with the flag off, another menu or another source).</summary>
    CharCustomFacts? ReadCharCustom(IClickableMenu? top)
    {
        if (!this.CharCustomOn || top is not CharacterCustomization menu || menu.GetType() != typeof(CharacterCustomization)
            || menu.source != CharacterCustomization.Source.NewFarmhand || Game1.player is not { } player)
            return null;
        try
        {
            return this.ReadCharCustomFacts(menu, player);
        }
        catch (Exception ex)
        {
            // Fail closed: without facts the menu is not the char_custom context and every press is refused.
            if (!this.charCustomReadFaultLogged)
                this.Monitor.Log($"char_custom facts unreadable: {ex.GetType().Name}: {ex.Message}", LogLevel.Warn);
            this.charCustomReadFaultLogged = true;
            return null;
        }
    }

    CharCustomFacts ReadCharCustomFacts(CharacterCustomization menu, Farmer player)
    {
        TextBox? name = this.CharCustomBox(menu, "nameBox");
        TextBox? favorite = this.CharCustomBox(menu, "favThingBox");
        var missing = new List<string>();
        if (name is null)
            missing.Add(CharCustomTable.Name);
        if (favorite is null)
            missing.Add(CharCustomTable.Favorite);
        if (CharCustomNamed(menu.genderButtons, "Male") is null || CharCustomNamed(menu.genderButtons, "Female") is null)
            missing.Add(CharCustomTable.Gender);
        foreach (string control in CharCustomTable.Cycles)
        {
            string native = CharCustomTable.NativeButtonName(control);
            if (CharCustomNamed(menu.leftSelectionButtons, native) is null || CharCustomNamed(menu.rightSelectionButtons, native) is null)
                missing.Add(control);
        }
        foreach (CharCustomAction action in CharCustomTable.Actions)
        {
            if (CharCustomTable.ColorOf(action.Control) is not { } color)
                continue;
            if (CharCustomPicker(menu, color.Picker) is null || CharCustomColorComponent(menu, color.Picker, color.Bar) is null
                || CharCustomColorComponent(menu, color.Picker, 0) is null)
                missing.Add(action.Control);
        }
        if (menu.okButton is null || !menu.okButton.visible)
            missing.Add(CharCustomTable.Ok);
        CharCustomColor Bars(ColorPicker? p) => p is null ? new CharCustomColor(0, 0, 0)
            : new CharCustomColor(p.hueBar.value, p.saturationBar.value, p.valueBar.value);
        var cycles = new Dictionary<string, CharCustomCycle>(StringComparer.Ordinal)
        {
            // Farmer.changeSkinColor wraps 0..23 (Farmer.cs:5438-5451); changeAccessory -1..29 (:5422-5436).
            [CharCustomTable.Skin] = new(Enumerable.Range(0, 24).Select(i => i.ToString(CultureInfo.InvariantCulture)).ToArray(),
                player.skin.Value.ToString(CultureInfo.InvariantCulture)),
            [CharCustomTable.Hair] = new(Farmer.GetAllHairstyleIndices().Select(i => i.ToString(CultureInfo.InvariantCulture)).ToArray(),
                player.hair.Value.ToString(CultureInfo.InvariantCulture)),
            // Fresh farmhand: shirt / pants.Value = "-1" (no override, the equipped 1000 / 0 items are shown; Farmer.cs:
            // 6088-6166): CharCustomCycle.Clothing publishes the shown id and marks the cycle off-list.
            [CharCustomTable.Shirt] = this.CharCustomClothing(menu.GetValidShirtIds(), player.shirt.Value, player.GetShirtId(), "(S)"),
            [CharCustomTable.Pants] = this.CharCustomClothing(menu.GetValidPantsIds(), player.pants.Value, player.GetPantsId(), "(P)"),
            [CharCustomTable.Accessory] = new(Enumerable.Range(-1, 31).Select(i => i.ToString(CultureInfo.InvariantCulture)).ToArray(),
                player.accessory.Value.ToString(CultureInfo.InvariantCulture)),
        };
        // TextBox.Update() reads the REAL mouse in UI coordinates (receiveLeftClick runs in UI mode): same read here.
        bool over;
        Game1.PushUIMode();
        try
        {
            int mx = Game1.getMouseX(), my = Game1.getMouseY();
            over = CharCustomOver(name, mx, my) || CharCustomOver(favorite, mx, my);
        }
        finally
        {
            Game1.PopUIMode();
        }
        object? subscriber = Game1.keyboardDispatcher?.Subscriber;
        return new CharCustomFacts(
            Source: menu.source.ToString(),
            Exact: ReferenceEquals(Game1.activeClickableMenu, menu) && !menu.showingCoopHelp,
            NameText: name?.Text ?? "",
            FavoriteText: favorite?.Text ?? "",
            FarmName: Game1.MasterPlayer?.farmName.Value ?? "",
            PlayerName: player.Name ?? "",
            PlayerFavorite: player.favoriteThing.Value ?? "",
            IsMale: player.IsMale,
            Cycles: cycles,
            EyeColor: Bars(menu.eyeColorPicker),
            HairColor: Bars(menu.hairColorPicker),
            PantsColor: Bars(menu.pantsColorPicker),
            CanLeave: menu.canLeaveMenu(),
            MouseOverTextBox: over,
            KeyboardOpen: Game1.textEntry is not null,
            SubscriberForeign: subscriber is not null && !ReferenceEquals(subscriber, name) && !ReferenceEquals(subscriber, favorite),
            IsCustomized: player.isCustomized.Value,
            Missing: missing,
            ShirtGameLabel: (player.GetShirtIndex() + 1).ToString(CultureInfo.InvariantCulture),
            PantsGameLabel: (player.GetPantsIndex() + 1).ToString(CultureInfo.InvariantCulture));
    }

    /// <summary>The created farmer, read natively (name, look, cabin, where it stands). Field reads and pure getters.</summary>
    FarmerProfile ReadFarmerProfile(Farmer p)
    {
        string homeName = p.homeLocation.Value ?? "";
        GameLocation? home = homeName.Length > 0 ? Game1.getLocationFromName(homeName) : null;
        long owner = home is Cabin cabin ? cabin.farmhandReference.UID : 0;
        Color eye = p.newEyeColor.Value, hair = p.hairstyleColor.Value, pants = p.GetPantsColor();
        return new FarmerProfile(p.Name ?? "", p.favoriteThing.Value ?? "", p.farmName.Value ?? "", p.isCustomized.Value, p.IsMale,
            p.skin.Value, p.hair.Value, p.accessory.Value, p.GetShirtId() ?? "", p.GetPantsId() ?? "",
            FarmerProfile.Hex(eye.R, eye.G, eye.B), FarmerProfile.Hex(hair.R, hair.G, hair.B), FarmerProfile.Hex(pants.R, pants.G, pants.B),
            homeName, home is Cabin, owner != 0 && owner == p.UniqueMultiplayerID, owner,
            p.currentLocation?.NameOrUniqueName ?? "", Context.IsPlayerFree, Game1.activeClickableMenu?.GetType().Name);
    }

    /// <summary>Observation only: ui.context "farmer_profile" (flag on; read-only).</summary>
    void EnrichFarmerProfile(Dictionary<string, object> snapshot)
    {
        if (!this.CharCustomOn || Game1.player is not { } player)
            return;
        try
        {
            snapshot[CharCustomTable.ProfileKey] = this.ReadFarmerProfile(player).Wire();
        }
        catch (Exception)
        {
            snapshot.Remove(CharCustomTable.ProfileKey);
        }
    }

    // ------------------------------------------------------------------ the port over the real menu

    private sealed class CharCustomGamePort : ICharCustomPort
    {
        private readonly ModEntry mod;
        private readonly CharacterCustomization menu;
        private TextBox? box;
        private IKeyboardSubscriber? previousSubscriber;

        public CharCustomGamePort(ModEntry mod, CharacterCustomization menu)
        {
            this.mod = mod;
            this.menu = menu;
        }

        public UiPadFacts Read() => this.mod.ReadUiPadFacts();

        public long UnblockedWarps => PadCursorWarpGuard.Counters.UnblockedInactive;

        private TextBox Box => this.box ?? throw new CharCustomGuardException("no character text box bound");

        private static void AssertWarpGuarded()
        {
            if (!PadCursorWarpGuard.Verified) // ADR 244: the guard blocks snap warps with the window active too
                throw new CharCustomGuardException("char_custom call without the OS-cursor guard");
        }

        private void AssertReceiver()
        {
            if (!ReferenceEquals(Game1.activeClickableMenu, this.menu))
                throw new CharCustomGuardException("char_custom receiver changed");
        }

        // text box (the WS-G3 text-entry primitive's box half)

        public void UseTextBox(string control)
        {
            this.AssertReceiver();
            this.box = this.mod.CharCustomBox(this.menu, control == CharCustomTable.Name ? "nameBox" : "favThingBox")
                ?? throw new CharCustomGuardException("character text box missing");
        }

        public bool TextFits(string text)
        {
            TextBox box = this.Box;
            // TextBox.Text trims while MeasureString(text).X > Width - 21 (TextBox.cs:47-63).
            return !box.limitWidth || box.Font.MeasureString(text).X <= box.Width - 21;
        }

        public bool SubscriberIsBox() => ReferenceEquals(Game1.keyboardDispatcher.Subscriber, this.Box);

        public void SetSubscriberToBox()
        {
            this.AssertReceiver();
            TextBox box = this.Box;
            this.previousSubscriber = Game1.keyboardDispatcher.Subscriber;
            Game1.keyboardDispatcher.Subscriber = box; // KeyboardDispatcher.cs:28-49 selects it
        }

        public void RestoreSubscriber()
        {
            TextBox box = this.Box;
            if (ReferenceEquals(Game1.keyboardDispatcher.Subscriber, box))
                Game1.keyboardDispatcher.Subscriber = this.previousSubscriber;
            this.previousSubscriber = null;
        }

        public void ClearBoxText() => this.Box.Text = "";

        public void ReceiveText(string text) => this.Box.RecieveTextInput(text);

        public string BoxText() => this.Box.Text ?? "";

        public void ClearKeyboardRequest() =>
            this.mod.Helper.Reflection.GetField<bool>(this.Box, "_showKeyboard").SetValue(false);

        // native clicks

        /// <summary>Every rectangle CharacterCustomization.receiveLeftClick tests for the farmhand source (:2047-2175).</summary>
        private List<(string Key, CcRect Rect)> ClickRects()
        {
            var rects = new List<(string, CcRect)>();
            foreach (ClickableComponent c in this.menu.genderButtons.Where(c => c is not null && c.visible))
                rects.Add(("gender:" + c.name, CharCustomRect(c)));
            foreach (ClickableComponent c in this.menu.leftSelectionButtons.Where(c => c is not null && c.visible))
                rects.Add(("left:" + c.name, CharCustomRect(c)));
            foreach (ClickableComponent c in this.menu.rightSelectionButtons.Where(c => c is not null && c.visible))
                rects.Add(("right:" + c.name, CharCustomRect(c)));
            if (this.menu.okButton is { visible: true } ok)
                rects.Add(("ok", CharCustomRect(ok)));
            if (this.menu.randomButton is { visible: true } random)
                rects.Add(("random", CharCustomRect(random)));
            foreach (string picker in new[] { CharCustomTable.PickerEye, CharCustomTable.PickerHair, CharCustomTable.PickerPants })
            {
                if (CharCustomPicker(this.menu, picker) is null)
                    continue;
                for (int bar = 0; bar < 3; bar++)
                    if (CharCustomColorComponent(this.menu, picker, bar) is { } cc)
                        rects.Add(("picker:" + picker, CharCustomRect(cc)));
            }
            return rects;
        }

        private void ClickAt(string key, CcRect target, int x, int y)
        {
            AssertWarpGuarded();
            this.AssertReceiver();
            if (!CharCustomGeometry.HitsOnly(x, y, target, this.ClickRects().Where(r => r.Key != key).Select(r => r.Rect)))
                throw new CharCustomGuardException("char_custom click point is not unique to " + key);
            Game1.PushUIMode();
            try
            {
                // One vanilla mouse click on the menu: press then release (CharacterCustomization.cs:2018, 2573).
                this.menu.receiveLeftClick(x, y);
                this.menu.releaseLeftClick(x, y);
            }
            finally
            {
                Game1.PopUIMode();
            }
        }

        public void ClickArrow(string control, int direction)
        {
            string native = CharCustomTable.NativeButtonName(control);
            ClickableComponent button = CharCustomNamed(direction < 0 ? this.menu.leftSelectionButtons : this.menu.rightSelectionButtons, native)
                ?? throw new CharCustomGuardException("selection arrow missing: " + native);
            CcRect rect = CharCustomRect(button);
            this.ClickAt((direction < 0 ? "left:" : "right:") + native, rect, rect.CenterX, rect.CenterY);
        }

        public void ClickGender(bool male)
        {
            string native = male ? "Male" : "Female";
            ClickableComponent button = CharCustomNamed(this.menu.genderButtons, native)
                ?? throw new CharCustomGuardException("gender button missing: " + native);
            CcRect rect = CharCustomRect(button);
            this.ClickAt("gender:" + native, rect, rect.CenterX, rect.CenterY);
        }

        public void ClickColor(string picker, int bar, int value)
        {
            ColorPicker colors = CharCustomPicker(this.menu, picker) ?? throw new CharCustomGuardException("colour picker missing: " + picker);
            ClickableComponent target = CharCustomColorComponent(this.menu, picker, bar)
                ?? throw new CharCustomGuardException("colour bar missing: " + picker + bar);
            // colorPickerCCs[hue] sits at the picker origin (CharacterCustomization.cs:800-802 / 849-851 / 970-972).
            ClickableComponent origin = CharCustomColorComponent(this.menu, picker, 0)
                ?? throw new CharCustomGuardException("colour picker origin missing: " + picker);
            SliderBar slider = bar switch
            {
                0 => colors.hueBar,
                1 => colors.saturationBar,
                _ => colors.valueBar,
            };
            int offset = CharCustomGeometry.SliderOffset(value, slider.bounds.Width)
                ?? throw new CharCustomGuardException("colour value not reachable by a click: " + value);
            int x = origin.bounds.X + slider.bounds.X + offset;
            int y = origin.bounds.Y + slider.bounds.Center.Y;
            // ColorPicker.click maps through its own bounds and bar (ColorPicker.cs:43-63): both must contain the point.
            if (!colors.containsPoint(x, y) || !slider.bounds.Contains(x - origin.bounds.X, y - origin.bounds.Y))
                throw new CharCustomGuardException("colour click point outside the native bar");
            this.ClickAt("picker:" + picker, CharCustomRect(target), x, y);
        }

        public void ClickOk()
        {
            ClickableTextureComponent ok = this.menu.okButton ?? throw new CharCustomGuardException("OK button missing");
            CcRect rect = CharCustomRect(ok);
            this.ClickAt("ok", rect, rect.CenterX, rect.CenterY);
        }

        public bool KeyboardOpen() => Game1.textEntry is not null;

        public void CloseTextEntry() => Game1.closeTextEntry();

        public FarmerProfile Profile() => this.mod.ReadFarmerProfile(Game1.player);
    }

    // ------------------------------------------------------------------ execution

    /// <summary>ui_press on the char_custom context (admission already done by ExecuteUiPad): one pure-executor run.</summary>
    void ExecuteCharCustom(MenuStepCommandRequest request, UiPadFacts facts, UiPadPlan plan, IClickableMenu top, string receiver)
    {
        UiPadVerdict verdict;
        try
        {
            verdict = top is CharacterCustomization menu && menu.GetType() == typeof(CharacterCustomization)
                ? CharCustomExecutor.Run(new CharCustomGamePort(this, menu), facts, plan)
                // Nothing was called: a refusal, never an uncertain verdict that latches the receiver.
                : new UiPadVerdict(UiPadVerdict.PressNoEffect, false, null, Array.Empty<string>(),
                    "guard_refused:receiver_not_char_custom", null, null, null);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"char_custom executor fault: {ex.GetType().Name}: {ex.Message}", LogLevel.Error);
            verdict = new UiPadVerdict(UiPadVerdict.PressUncertain, false, null, Array.Empty<string>(),
                "executor_fault:" + ex.GetType().Name, null, null, null);
        }
        this.Monitor.Log($"char_custom request_id={request.RequestId} step={plan.Steps.FirstOrDefault()} outcome={verdict.Outcome} "
            + $"reason={verdict.Reason} readback={verdict.Readback ?? "-"}", verdict.Uncertain ? LogLevel.Warn : LogLevel.Info);
        this.FinishUiPad(request, verdict, receiver);
    }

    // ------------------------------------------------------------------ the join screen (pre-world, read-only)

    /// <summary>Title-screen join facts (TitleMenu -> FarmhandMenu): field reads only.</summary>
    static JoinScreenFacts ReadJoinScreen()
    {
        IClickableMenu? active = Game1.activeClickableMenu;
        bool title = active is TitleMenu;
        IClickableMenu? sub = title ? TitleMenu.subMenu : active;
        var farmhands = sub as FarmhandMenu;
        var slots = new List<JoinSlotFacts>();
        if (farmhands is not null)
        {
            List<LoadGameMenu.MenuSlot> all = farmhands.MenuSlots ?? new List<LoadGameMenu.MenuSlot>();
            for (int i = 0; i < all.Count && i < JoinScreenFacts.MaxSlots; i++)
            {
                int shown = i - farmhands.currentItemIndex;
                CcRect? button = shown >= 0 && shown < farmhands.slotButtons.Count ? CharCustomRect(farmhands.slotButtons[shown]) : null;
                if (all[i] is FarmhandMenu.FarmhandSlot slot)
                    slots.Add(new JoinSlotFacts(i, true, slot.Farmer?.isCustomized.Value == true, slot.Farmer?.Name ?? "",
                        slot.BelongsToAnotherPlayer(), button));
                else
                    slots.Add(new JoinSlotFacts(i, false, false, "", false, button));
            }
        }
        Client? client = farmhands?.client;
        return new JoinScreenFacts(title, sub?.GetType().Name, farmhands is not null, client is not null,
            farmhands?.gettingFarmhands == true, farmhands?.approvingFarmhand == true, client?.timedOut == true, client?.connectionMessage,
            farmhands?.loading == true, farmhands?.currentItemIndex ?? 0, slots, Game1.options?.uiScale ?? 1f,
            Game1.uiViewport.Width, Game1.uiViewport.Height);
    }

    /// <summary>
    /// Every UpdateTicked while the world is NOT ready (flag on): join_screen.json in the command folder, at most every
    /// 30 ticks, so the operator join can confirm the FarmhandMenu slot natively. Never an input, never a menu call.
    /// </summary>
    void TickJoinScreenFacts()
    {
        if (!this.CharCustomOn || this.commandInboxDir is null || Context.IsWorldReady)
            return;
        long ticks = Game1.ticks;
        if (this.joinScreenLastTick is long last && ticks >= last && ticks - last < 30)
            return;
        this.joinScreenLastTick = ticks;
        try
        {
            string json = JsonSerializer.Serialize(ReadJoinScreen().Wire(NowUnixMs(), ticks), ProtocolJson.Options);
            AtomicFileWriter.TryWrite(Path.Combine(this.commandInboxDir, JoinScreenFacts.FileName), json);
        }
        catch (Exception ex)
        {
            if (!this.joinScreenFaultLogged)
                this.Monitor.Log($"join_screen facts unavailable: {ex.GetType().Name}: {ex.Message}", LogLevel.Warn);
            this.joinScreenFaultLogged = true;
        }
    }
}
