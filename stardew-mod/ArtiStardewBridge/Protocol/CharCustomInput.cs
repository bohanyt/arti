using System.Globalization;
using System.Text;

namespace ArtiStardewBridge.Protocol;

// Live7 character creation (docs/stardew/live7/CHARACTER_CREATION.md). The farmhand variant of the vanilla
// CharacterCustomization menu (Source.NewFarmhand), which Client.setUpGame opens for a farmhand that is not customized
// yet (decomp 1.6.15 StardewValley.Network/Client.cs:226-229), becomes one ui_pad context "char_custom":
//   ui_inspect = the published "char_custom" block: every control with its current value and its options;
//   ui_press A {choice, value | text} = ONE control: the menu's own receiveLeftClick (+ releaseLeftClick) on the native
//     component (Skin / Hair / Shirt / Pants Style / Acc arrows, Male / Female, a colour-picker bar, OK), or the WS-G3
//     text-entry primitive (UiG3Executor.WriteBoxText) on the menu's private name / favourite TextBox.
// Pure and SMAPI-free; ModEntry.CharCustom.cs reads the menu into CharCustomFacts (field reads and pure getters) and
// implements ICharCustomPort. Never a direct write of a farmer field, never Random (ARTI chooses), never B / Start
// (Escape would leave an uncustomized farmhand), never an activeClickableMenu assignment.
// Decomp refs (CharacterCustomization.cs): component build 468-1140 (farmhand: no pet 612/1085, no farm types / cabins /
// skip intro 494/677/1134), Male / Female 1645-1664 (hair reset to 0 / 16), OK 1677-1787, selectionClick 1842-1979
// (Skin 1854, Hair 1861, Acc 1884, Shirt 1913, Pants Style 1920), receiveLeftClick 2018-2194 (the TextBoxes' Update()
// with the REAL mouse at 2176-2187), canLeaveMenu 2773-2784, draw syncs name / favourite into the farmer 2841-2856.
// Flag CharacterCreationEnabled (default OFF, needs UiPadMenusEnabled): off = UiPadFacts.CharCustom is null and every
// G1 table, token and snapshot is byte-identical. Evidence ceiling: UNIT_TESTED; HYPOTHESIS until the Live7 witness.

public sealed record CharCustomAction(int Choice, string Control, string Kind, string Does);

public static class CharCustomTable
{
    public const string Context = "char_custom";
    public const string MenuType = "CharacterCustomization";
    public const string SourceNewFarmhand = "NewFarmhand";
    public const string BlockKey = "char_custom";
    public const string ProfileKey = "farmer_profile";
    public const string ArgChoice = "choice", ArgValue = "value", ArgText = "text";

    /// <summary>Native clicks one cycle press may send (every click is read back; the skill repeats the press).</summary>
    public const int MaxClicksPerPress = 24;
    /// <summary>SliderBar.click: value = (int)(x / width * 100) with x inside the bar (SliderBar.cs:22-30): 0..99.</summary>
    public const int ColorMax = 99;
    /// <summary>Option lists (1.6.15: 24 skins, ~80 hairstyles, ~110 shirts, 31 accessories): bounded, never unbounded.</summary>
    public const int MaxOptions = 400;
    public const int MaxLabel = 40;

    public const string KindText = "text", KindChoice = "choice", KindCycle = "cycle", KindColor = "color", KindConfirm = "confirm";

    public const string Name = "name", Favorite = "favorite", Gender = "gender", Skin = "skin", Hair = "hair",
        Shirt = "shirt", Pants = "pants", Accessory = "accessory",
        EyeHue = "eye_hue", EyeSaturation = "eye_saturation", EyeValue = "eye_value",
        HairHue = "hair_hue", HairSaturation = "hair_saturation", HairValue = "hair_value",
        PantsHue = "pants_hue", PantsSaturation = "pants_saturation", PantsValue = "pants_value", Ok = "ok";

    public const string PickerEye = "eye", PickerHair = "hair", PickerPants = "pants";

    /// <summary>The cycle controls and the native selection-button name each one clicks (CharacterCustomization.cs:655-1080).</summary>
    public static readonly IReadOnlyList<string> Cycles = new[] { Skin, Hair, Shirt, Pants, Accessory };

    public static string NativeButtonName(string control) => control switch
    {
        Skin => "Skin",
        Hair => "Hair",
        Shirt => "Shirt",
        Pants => "Pants Style",
        Accessory => "Acc",
        _ => throw new ArgumentOutOfRangeException(nameof(control), control, "not a cycle control"),
    };

    /// <summary>gender value 1 = Male button, 2 = Female button.</summary>
    public static readonly IReadOnlyList<string> GenderOptions = new[] { "male", "female" };

    public static readonly IReadOnlyList<CharCustomAction> Actions = new CharCustomAction[]
    {
        new(0, Name, KindText, "type the farmer name into the menu's name box"),
        new(1, Favorite, KindText, "type the favourite thing into the menu's box"),
        new(2, Gender, KindChoice, "click the Male (1) or Female (2) button"),
        new(3, Skin, KindCycle, "click the Skin arrows to the option"),
        new(4, Hair, KindCycle, "click the Hair arrows to the option"),
        new(5, Shirt, KindCycle, "click the Shirt arrows to the option"),
        new(6, Pants, KindCycle, "click the Pants Style arrows to the option"),
        new(7, Accessory, KindCycle, "click the Accessory arrows to the option"),
        new(8, EyeHue, KindColor, "click the eye colour hue bar at the value"),
        new(9, EyeSaturation, KindColor, "click the eye colour saturation bar at the value"),
        new(10, EyeValue, KindColor, "click the eye colour brightness bar at the value"),
        new(11, HairHue, KindColor, "click the hair colour hue bar at the value"),
        new(12, HairSaturation, KindColor, "click the hair colour saturation bar at the value"),
        new(13, HairValue, KindColor, "click the hair colour brightness bar at the value"),
        new(14, PantsHue, KindColor, "click the pants colour hue bar at the value"),
        new(15, PantsSaturation, KindColor, "click the pants colour saturation bar at the value"),
        new(16, PantsValue, KindColor, "click the pants colour brightness bar at the value"),
        new(17, Ok, KindConfirm, "click OK: create the farmer"),
    };

    public static CharCustomAction? ActionOf(int choice) => choice >= 0 && choice < Actions.Count ? Actions[choice] : null;

    public static CharCustomAction? ActionOf(string control) => Actions.FirstOrDefault(a => a.Control == control);

    /// <summary>Colour control -> (picker, bar): bar 0 hue, 1 saturation, 2 value (ColorPicker.cs:28-34).</summary>
    public static (string Picker, int Bar)? ColorOf(string control) => control switch
    {
        EyeHue => (PickerEye, 0), EyeSaturation => (PickerEye, 1), EyeValue => (PickerEye, 2),
        HairHue => (PickerHair, 0), HairSaturation => (PickerHair, 1), HairValue => (PickerHair, 2),
        PantsHue => (PickerPants, 0), PantsSaturation => (PickerPants, 1), PantsValue => (PickerPants, 2),
        _ => null,
    };

    /// <summary>colorPickerCCs ids (CharacterCustomization.cs:802-824 eye 522-524, 851-873 hair 525-527, 972-994 pants 528-530).</summary>
    public static int ColorComponentId(string picker, int bar) => (picker switch
    {
        PickerEye => 522,
        PickerHair => 525,
        _ => 528,
    }) + bar;

    public static int Wrap(int index, int count) => count <= 0 ? 0 : ((index % count) + count) % count;

    public static string Trim(string? text, int max = MaxLabel) => UiPadTable.Trim(text, max);
}

/// <summary>
/// One cycle control: the native option ids in the menu's own order and the current id (labels: shirts / pants).
/// <paramref name="OffList"/> (shirt / pants only): the farmer's native value is not one of the options, e.g. the fresh
/// farmhand's <c>shirt / pants = "-1"</c> ("no override": the equipped shirtItem / pantsItem is shown). Then
/// <paramref name="Current"/> is the DISPLAYED id (Farmer.GetShirtId / GetPantsId, Farmer.cs:6088-6166) and the next arrow
/// click in EITHER direction lands on <c>Ids[0]</c> (Farmer.rotateShirt / rotatePantStyle: IndexOf == -1 -> FirstOrDefault,
/// Farmer.cs:5169-5221).
/// </summary>
public sealed record CharCustomCycle(IReadOnlyList<string> Ids, string Current, IReadOnlyList<string>? Labels = null, bool OffList = false)
{
    public int Count => this.Ids.Count;

    /// <summary>0-based position of the current (displayed) id (-1 = not in the list, e.g. a modded value).</summary>
    public int Position => IndexOf(this.Ids, this.Current);

    public static int IndexOf(IReadOnlyList<string> ids, string id)
    {
        for (int i = 0; i < ids.Count; i++)
            if (string.Equals(ids[i], id, StringComparison.Ordinal))
                return i;
        return -1;
    }

    /// <summary>
    /// The shirt / pants cycle as the vanilla menu sees it: <paramref name="ids"/> = GetValidShirtIds / GetValidPantsIds,
    /// <paramref name="native"/> = <c>farmer.shirt / pants.Value</c> (what rotateShirt / rotatePantStyle index),
    /// <paramref name="displayed"/> = GetShirtId() / GetPantsId() (what the farmer shows and keeps after OK:
    /// ConvertClothingOverrideToClothesItems only touches an override, Farmer.cs:5229-5256).
    /// </summary>
    public static CharCustomCycle Clothing(IReadOnlyList<string> ids, string? native, string? displayed, IReadOnlyList<string>? labels = null)
    {
        bool offList = IndexOf(ids, native ?? "") < 0;
        return new CharCustomCycle(ids, offList ? displayed ?? "" : native!, labels, offList);
    }
}

/// <summary>A colour picker's three SliderBar values (0..100; a click reaches 0..99).</summary>
public sealed record CharCustomColor(int Hue, int Saturation, int Value)
{
    public int Bar(int bar) => bar switch
    {
        0 => this.Hue,
        1 => this.Saturation,
        _ => this.Value,
    };
}

/// <summary>Read-only facts of the farmhand CharacterCustomization (one tick; field reads and pure getters only).</summary>
public sealed record CharCustomFacts(
    string Source,
    bool Exact,
    string NameText,
    string FavoriteText,
    string FarmName,
    string PlayerName,
    string PlayerFavorite,
    bool IsMale,
    IReadOnlyDictionary<string, CharCustomCycle> Cycles,
    CharCustomColor EyeColor,
    CharCustomColor HairColor,
    CharCustomColor PantsColor,
    bool CanLeave,
    bool MouseOverTextBox,
    bool KeyboardOpen,
    bool SubscriberForeign,
    bool IsCustomized,
    IReadOnlyList<string>? Missing = null,
    string? ShirtGameLabel = null,
    string? PantsGameLabel = null)
{
    public CharCustomColor Picker(string picker) => picker switch
    {
        CharCustomTable.PickerEye => this.EyeColor,
        CharCustomTable.PickerHair => this.HairColor,
        _ => this.PantsColor,
    };

    /// <summary>draw() copies the boxes into the farmer every frame (CharacterCustomization.cs:2841-2848).</summary>
    public bool Synced => string.Equals(this.PlayerName, this.NameText, StringComparison.Ordinal)
        && string.Equals(this.PlayerFavorite, this.FavoriteText, StringComparison.Ordinal);

    public IReadOnlyList<string> MissingControls => this.Missing ?? Array.Empty<string>();

    public string Digest()
    {
        var text = new StringBuilder();
        text.Append(this.Source).Append('|').Append(this.Exact ? 1 : 0).Append('|').Append(this.NameText).Append('|')
            .Append(this.FavoriteText).Append('|').Append(this.FarmName).Append('|').Append(this.PlayerName).Append('|')
            .Append(this.PlayerFavorite).Append('|').Append(this.IsMale ? 'm' : 'f');
        foreach (string key in CharCustomTable.Cycles)
        {
            text.Append('|').Append(key).Append(':');
            if (this.Cycles.TryGetValue(key, out CharCustomCycle? cycle))
            {
                text.Append(cycle.Current).Append(':').Append(cycle.Count).Append(':')
                    .Append(UiKeyTargetToken.Hash(string.Join("\u001f", cycle.Ids)));
                if (cycle.OffList)
                    // "-1" (equipped item shown) -> Ids[0] override can keep the same displayed id: still a state change.
                    text.Append(":off");
            }
        }
        foreach (CharCustomColor c in new[] { this.EyeColor, this.HairColor, this.PantsColor })
            text.Append('|').Append(c.Hue).Append(',').Append(c.Saturation).Append(',').Append(c.Value);
        text.Append('|').Append(this.CanLeave ? 1 : 0).Append(this.MouseOverTextBox ? 1 : 0).Append(this.KeyboardOpen ? 1 : 0)
            .Append(this.SubscriberForeign ? 1 : 0).Append(this.IsCustomized ? 1 : 0).Append('|')
            .Append(string.Join(",", this.MissingControls));
        return UiKeyTargetToken.Hash(text.ToString())[..32];
    }
}

/// <summary>Native readback of the farmer (the created character): published as ui.context "farmer_profile".</summary>
public sealed record FarmerProfile(
    string Name,
    string FavoriteThing,
    string FarmName,
    bool IsCustomized,
    bool IsMale,
    int Skin,
    int Hair,
    int Accessory,
    string ShirtId,
    string PantsId,
    string EyeColor,
    string HairColor,
    string PantsColor,
    string HomeLocation,
    bool HomeIsCabin,
    bool CabinOwned,
    long CabinOwnerId,
    string Location,
    bool PlayerFree,
    string? MenuType)
{
    public Dictionary<string, object?> Wire() => new(StringComparer.Ordinal)
    {
        ["name"] = CharCustomTable.Trim(this.Name, 32),
        ["favorite_thing"] = CharCustomTable.Trim(this.FavoriteThing, 32),
        ["farm_name"] = CharCustomTable.Trim(this.FarmName, 32),
        ["is_customized"] = this.IsCustomized,
        ["gender"] = this.IsMale ? "male" : "female",
        ["skin"] = this.Skin,
        ["hair"] = this.Hair,
        ["accessory"] = this.Accessory,
        ["shirt_id"] = CharCustomTable.Trim(this.ShirtId, 32),
        ["pants_id"] = CharCustomTable.Trim(this.PantsId, 32),
        ["eye_color"] = this.EyeColor,
        ["hair_color"] = this.HairColor,
        ["pants_color"] = this.PantsColor,
        ["home_location"] = CharCustomTable.Trim(this.HomeLocation, 64),
        ["home_is_cabin"] = this.HomeIsCabin,
        ["cabin_owned"] = this.CabinOwned,
        ["cabin_owner_id"] = this.CabinOwnerId.ToString(CultureInfo.InvariantCulture),
        ["location"] = CharCustomTable.Trim(this.Location, 64),
        ["player_free"] = this.PlayerFree,
        ["menu_type"] = this.MenuType,
    };

    /// <summary>Compact readback string for a press verdict (UiPadVerdict.Readback).</summary>
    public string Readback() => System.Text.Json.JsonSerializer.Serialize(this.Wire());

    public static string Hex(int r, int g, int b) =>
        "#" + r.ToString("X2", CultureInfo.InvariantCulture) + g.ToString("X2", CultureInfo.InvariantCulture)
        + b.ToString("X2", CultureInfo.InvariantCulture);
}

/// <summary>A component rectangle in menu (UI) coordinates.</summary>
public readonly record struct CcRect(int X, int Y, int W, int H)
{
    public bool Contains(int x, int y) => x >= this.X && x < this.X + this.W && y >= this.Y && y < this.Y + this.H;

    public int CenterX => this.X + this.W / 2;

    public int CenterY => this.Y + this.H / 2;
}

/// <summary>Click-point arithmetic over native component rectangles (never a coordinate literal).</summary>
public static class CharCustomGeometry
{
    /// <summary>
    /// The offset inside a SliderBar of <paramref name="width"/> that its own click maps to <paramref name="value"/>
    /// (SliderBar.click: value = (int)((float)x / (float)width * 100f), SliderBar.cs:22-30), or null when no pixel does.
    /// </summary>
    public static int? SliderOffset(int value, int width)
    {
        if (width <= 0 || value < 0 || value > 100)
            return null;
        int start = (value * width + 99) / 100;
        for (int x = Math.Max(0, start - 1); x <= Math.Min(width - 1, start + 2); x++)
            if (SliderValue(x, width) == value)
                return x;
        return null;
    }

    public static int SliderValue(int x, int width) => (int)((float)x / (float)width * 100f);

    /// <summary>receiveLeftClick tests every list in turn (CharacterCustomization.cs:2047-2175): the point must be inside the
    /// target and inside no other clickable rectangle.</summary>
    public static bool HitsOnly(int x, int y, CcRect target, IEnumerable<CcRect> others) =>
        target.Contains(x, y) && !others.Any(r => r.Contains(x, y));
}

public static class CharCustomPolicy
{
    static string Deny(string why) => UiPadTable.Denied + ":" + why;

    static string None(string why) => UiPadTable.NoMeaning + ":" + why;

    /// <summary>Receiver-level gates (refused before any call).</summary>
    public static string? Gate(CharCustomFacts f) =>
        !f.Exact ? Deny("char_custom_receiver_changed")
        : !string.Equals(f.Source, CharCustomTable.SourceNewFarmhand, StringComparison.Ordinal) ? Deny("char_custom_source:" + f.Source)
        : f.IsCustomized ? Deny("already_customized")
        : f.KeyboardOpen ? Deny("on_screen_keyboard_open")
        : f.SubscriberForeign ? Deny("subscriber_foreign")
        : null;

    static UiPadPlan Plan(CharCustomAction action, string? arg) => UiPadPlan.Ok(UiPadTable.BtnA, action.Does,
        new UiPadStep(UiPadStepKind.CharCustom, UiPadTable.BtnA, G3Action: action.Control, G3Arg: arg));

    static bool TryInt(string? raw, out int value) =>
        int.TryParse(raw, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out value);

    /// <summary>
    /// The cell for <paramref name="button"/>. <paramref name="reserved"/> null = describe (the ui_inspect row); non-null =
    /// execution: {choice} + exactly the payload that control takes, or a refusal with zero calls.
    /// </summary>
    public static UiPadPlan Translate(string button, UiPadFacts f, IReadOnlyDictionary<string, string>? reserved)
    {
        if (f.CharCustom is not { } cc)
            return UiPadPlan.No(button, Deny("char_custom_facts_unavailable"));
        if (button != UiPadTable.BtnA)
            // B / Start / Y map to the menu key (Utility.cs:7036): exiting would leave an uncustomized farmhand.
            return UiPadPlan.No(button, button is UiPadTable.BtnB or UiPadTable.BtnStart or UiPadTable.BtnY
                ? Deny("char_custom_no_exit") : None("char_custom_only_a"));
        if (Gate(cc) is { } gate)
            return UiPadPlan.No(button, gate);
        if (reserved is null)
            return UiPadPlan.Ok(button, "set ONE character control: choice (+ value / text), see char_custom.actions");
        if (!reserved.TryGetValue(CharCustomTable.ArgChoice, out string? rawChoice) || !UiG3Policy.TryIndex(rawChoice, out int choice)
            || CharCustomTable.ActionOf(choice) is not { } action)
            return UiPadPlan.No(button, Deny("choice_required"));
        string[] allowed = action.Kind switch
        {
            CharCustomTable.KindText => new[] { CharCustomTable.ArgChoice, CharCustomTable.ArgText },
            CharCustomTable.KindConfirm => new[] { CharCustomTable.ArgChoice },
            _ => new[] { CharCustomTable.ArgChoice, CharCustomTable.ArgValue },
        };
        string? foreign = reserved.Keys.OrderBy(k => k, StringComparer.Ordinal).FirstOrDefault(k => !allowed.Contains(k));
        if (foreign is not null)
            return UiPadPlan.No(button, Deny("reserved_arg:" + foreign));
        if (cc.MissingControls.Contains(action.Control))
            return UiPadPlan.No(button, Deny("control_unavailable:" + action.Control));
        // Every click ends with nameBox.Update() / favThingBox.Update() on the REAL mouse (CharacterCustomization.cs:
        // 2176-2187, TextBox.cs:128-145): with it over a box the box is selected and, in pad mode, the on-screen
        // keyboard opens. The farmhand cursor cannot be moved (inactive guard): refuse the click instead.
        if (action.Kind != CharCustomTable.KindText && cc.MouseOverTextBox)
            return UiPadPlan.No(button, Deny("mouse_over_text_box"));
        switch (action.Kind)
        {
            case CharCustomTable.KindText:
            {
                if (!reserved.TryGetValue(CharCustomTable.ArgText, out string? text))
                    return UiPadPlan.No(button, Deny("text_required"));
                if (UiG3Policy.NameRefusal(text, 1, -1) is { } why)
                    return UiPadPlan.No(button, Deny(why));
                string current = action.Control == CharCustomTable.Name ? cc.NameText : cc.FavoriteText;
                if (string.Equals(current, text, StringComparison.Ordinal))
                    return UiPadPlan.No(button, None("already_set"));
                return Plan(action, text);
            }
            case CharCustomTable.KindChoice:
            {
                if (!reserved.TryGetValue(CharCustomTable.ArgValue, out string? raw) || !TryInt(raw, out int value) || value is < 1 or > 2)
                    return UiPadPlan.No(button, Deny("value_out_of_range"));
                // Male / Female also reset the hairstyle (0 / 16, CharacterCustomization.cs:1645-1664): never click the
                // already selected one.
                if (cc.IsMale == (value == 1))
                    return UiPadPlan.No(button, None("already_set"));
                return Plan(action, value.ToString(CultureInfo.InvariantCulture));
            }
            case CharCustomTable.KindCycle:
            {
                if (!cc.Cycles.TryGetValue(action.Control, out CharCustomCycle? cycle) || cycle.Count == 0
                    || cycle.Count > CharCustomTable.MaxOptions)
                    return UiPadPlan.No(button, Deny("options_unavailable:" + action.Control));
                // Off the list (fresh farmhand shirt / pants "-1"): the next click lands on Ids[0] whatever is shown, so
                // every option is reachable even when the shown id is not an option.
                if (cycle.Position < 0 && !cycle.OffList)
                    return UiPadPlan.No(button, Deny("current_option_unknown:" + action.Control));
                if (!reserved.TryGetValue(CharCustomTable.ArgValue, out string? raw) || !TryInt(raw, out int value)
                    || value < 1 || value > cycle.Count)
                    return UiPadPlan.No(button, Deny("value_out_of_range"));
                // Shown = chosen: OK keeps the shown item (an off-list farmer keeps its equipped shirtItem / pantsItem).
                if (value - 1 == cycle.Position)
                    return UiPadPlan.No(button, None("already_set"));
                // The executor walks to this native id (a shirt / pants list may shrink once the start item is left).
                return Plan(action, cycle.Ids[value - 1]);
            }
            case CharCustomTable.KindColor:
            {
                (string picker, int bar) = CharCustomTable.ColorOf(action.Control)!.Value;
                if (!reserved.TryGetValue(CharCustomTable.ArgValue, out string? raw) || !TryInt(raw, out int value)
                    || value < 0 || value > CharCustomTable.ColorMax)
                    return UiPadPlan.No(button, Deny("value_out_of_range"));
                if (cc.Picker(picker).Bar(bar) == value)
                    return UiPadPlan.No(button, None("already_set"));
                return Plan(action, value.ToString(CultureInfo.InvariantCulture));
            }
            default:
            {
                // OK needs the name, farm and favourite (canLeaveMenu, :2773-2784) AND the farmer already showing the boxes'
                // text (draw syncs it, :2841-2848): OK itself copies nameBox.Text.Trim() (:1696), so an unsynced read is
                // only a wait, never a different name.
                if (UiG3Policy.NameRefusal(cc.NameText, 1, -1) is { } badName)
                    return UiPadPlan.No(button, Deny("name_invalid:" + badName));
                if (UiG3Policy.NameRefusal(cc.FavoriteText, 1, -1) is { } badFavorite)
                    return UiPadPlan.No(button, Deny("favorite_invalid:" + badFavorite));
                if (!cc.Synced)
                    return UiPadPlan.No(button, Deny("not_synced"));
                if (!cc.CanLeave)
                    return UiPadPlan.No(button, Deny("ok_not_ready"));
                return Plan(action, null);
            }
        }
    }
}

/// <summary>
/// Thrown by an <see cref="ICharCustomPort"/> guard BEFORE any native call (OS-cursor guard, receiver changed, component
/// missing, click point not unique, value not reachable): nothing reached the menu, so the press is a no-effect refusal,
/// never an uncertain verdict that would latch the receiver.
/// </summary>
public sealed class CharCustomGuardException : InvalidOperationException
{
    public CharCustomGuardException(string reason) : base("char_custom guard: " + reason) => this.Reason = reason;

    public string Reason { get; }
}

/// <summary>The game side of a char_custom step (ModEntry.CharCustom.cs over the real menu; a simulating fake in tests).
/// Click methods run every guard first and throw <see cref="CharCustomGuardException"/> before the native call.</summary>
public interface ICharCustomPort : IUiTextBoxPort
{
    UiPadFacts Read();

    /// <summary>OS-cursor guard counter: SetMousePosition calls that went through while inactive.</summary>
    long UnblockedWarps { get; }

    /// <summary>Binds the text-box members to the menu's private name / favourite TextBox.</summary>
    void UseTextBox(string control);

    /// <summary>The bound box keeps <paramref name="text"/> whole: TextBox.Text trims by font width (TextBox.cs:47-63).</summary>
    bool TextFits(string text);

    /// <summary>One receiveLeftClick + releaseLeftClick on a cycle control's left (-1) or right (+1) arrow.</summary>
    void ClickArrow(string control, int direction);

    void ClickGender(bool male);

    /// <summary>One click on a colour-picker bar at the pixel its SliderBar maps to <paramref name="value"/>.</summary>
    void ClickColor(string picker, int bar, int value);

    void ClickOk();

    bool KeyboardOpen();

    void CloseTextEntry();

    FarmerProfile Profile();
}

public static class CharCustomExecutor
{
    static bool SameReceiver(UiPadFacts before, UiPadFacts after) =>
        after.MenuOpen && after.MenuType == before.MenuType && after.ReceiverToken == before.ReceiverToken
        && after.CharCustom is not null;

    /// <summary>Runs one allowed char_custom plan; never throws (a fault after a call = uncertain).</summary>
    public static UiPadVerdict Run(ICharCustomPort port, UiPadFacts before, UiPadPlan plan)
    {
        if (!plan.Allowed || plan.Steps.Count != 1 || plan.Steps[0].Kind != UiPadStepKind.CharCustom)
            throw new InvalidOperationException("char_custom executor needs an allowed char_custom plan");
        UiPadStep step = plan.Steps[0];
        CharCustomAction action = CharCustomTable.ActionOf(step.G3Action ?? "")
            ?? throw new InvalidOperationException("unknown char_custom control " + step.G3Action);
        var calls = new List<string>();
        long warps = port.UnblockedWarps;
        UiPadVerdict verdict;
        try
        {
            verdict = action.Kind switch
            {
                CharCustomTable.KindText => Text(port, before, action.Control, step.G3Arg!, calls),
                CharCustomTable.KindChoice => GenderClick(port, before, step.G3Arg == "1", calls),
                CharCustomTable.KindCycle => Cycle(port, before, action.Control, step.G3Arg!, calls),
                CharCustomTable.KindColor => Color(port, before, action.Control,
                    int.Parse(step.G3Arg!, CultureInfo.InvariantCulture), calls),
                _ => Confirm(port, before, calls),
            };
        }
        catch (CharCustomGuardException guard)
        {
            // A port guard refused before any native call: nothing changed, no receiver latch.
            UiPadFacts? now = null;
            try { now = port.Read(); } catch { }
            verdict = V(UiPadVerdict.PressNoEffect, false, null, calls, "guard_refused:" + guard.Reason, now);
        }
        catch (Exception ex)
        {
            UiPadFacts? now = null;
            try { now = port.Read(); } catch { }
            verdict = V(calls.Count == 0 ? UiPadVerdict.PressNoEffect : UiPadVerdict.PressUncertain, false, null, calls,
                "call_fault:" + ex.GetType().Name, now);
        }
        long after;
        try { after = port.UnblockedWarps; } catch { after = warps + 1; }
        if (after != warps)
            // The OS cursor moved while inactive: never claim a clean effect (global stop rule, PLAN 6.2).
            verdict = verdict with { Outcome = UiPadVerdict.PressUncertain, Applied = false, Reason = "os_warp_unblocked" };
        return verdict;
    }

    /// <summary>One native click: recorded once the port's guards passed and the menu call started. A guard refusal
    /// propagates unrecorded (no effect); any other fault records the call (the menu may have changed: uncertain).</summary>
    static void NativeClick(List<string> calls, string call, Action click)
    {
        try
        {
            click();
        }
        catch (CharCustomGuardException)
        {
            throw;
        }
        catch
        {
            calls.Add(call);
            throw;
        }
        calls.Add(call);
    }

    static UiPadVerdict V(string outcome, bool applied, string? effect, List<string> calls, string reason, UiPadFacts? after,
        string? readback = null) =>
        new(outcome, applied, effect, calls.ToArray(), reason, after?.SnappedId, after is { MenuOpen: true } ? after.MenuType : null,
            after is { MenuOpen: true } ? after.ReceiverToken : null, readback);

    /// <summary>A click that opened the on-screen keyboard is cleaned up and never claimed.</summary>
    static UiPadVerdict? Keyboard(ICharCustomPort port, List<string> calls, UiPadFacts after)
    {
        if (!port.KeyboardOpen())
            return null;
        calls.Add("Game1.closeTextEntry()");
        port.CloseTextEntry();
        return V(UiPadVerdict.PressUncertain, false, null, calls, "on_screen_keyboard_opened", after);
    }

    static UiPadVerdict Text(ICharCustomPort port, UiPadFacts before, string control, string text, List<string> calls)
    {
        port.UseTextBox(control);
        if (!port.TextFits(text))
            // TextBox.Text would trim it (never a name ARTI did not choose): zero calls.
            return V(UiPadVerdict.PressNoEffect, false, null, calls, "text_too_wide", before);
        string read = UiG3Executor.WriteBoxText(port, text, calls);
        UiPadFacts after = port.Read();
        if (Keyboard(port, calls, after) is { } open)
            return open;
        if (!string.Equals(read, text, StringComparison.Ordinal))
            return V(UiPadVerdict.PressUncertain, false, null, calls, "text_readback_mismatch", after, read);
        string? boxNow = after.CharCustom is { } cc ? control == CharCustomTable.Name ? cc.NameText : cc.FavoriteText : null;
        if (!SameReceiver(before, after) || !string.Equals(boxNow, text, StringComparison.Ordinal))
            return V(UiPadVerdict.PressUncertain, false, null, calls, "text_receiver_readback_mismatch", after, read);
        return V(UiPadVerdict.PressApplied, true, "changed", calls, "text_set:" + control, after, control + "=" + read);
    }

    static UiPadVerdict GenderClick(ICharCustomPort port, UiPadFacts before, bool male, List<string> calls)
    {
        NativeClick(calls, "receiveLeftClick(" + (male ? "Male" : "Female") + ")", () => port.ClickGender(male));
        UiPadFacts after = port.Read();
        if (Keyboard(port, calls, after) is { } open)
            return open;
        if (!SameReceiver(before, after))
            return V(UiPadVerdict.PressUncertain, false, null, calls, "receiver_changed", after);
        CharCustomFacts cc = after.CharCustom!;
        string readback = "gender=" + (cc.IsMale ? "male" : "female") + ";hair="
            + (cc.Cycles.TryGetValue(CharCustomTable.Hair, out CharCustomCycle? hair) ? hair.Position + 1 : 0);
        return cc.IsMale == male
            ? V(UiPadVerdict.PressApplied, true, "changed", calls, "gender_set", after, readback)
            : V(UiPadVerdict.PressUncertain, false, null, calls, "gender_readback_mismatch", after, readback);
    }

    static UiPadVerdict Cycle(ICharCustomPort port, UiPadFacts before, string control, string targetId, List<string> calls)
    {
        UiPadFacts now = before;
        for (int n = 0; ; n++)
        {
            CharCustomCycle cycle = now.CharCustom!.Cycles[control];
            int position = cycle.Position;
            int target = CharCustomCycle.IndexOf(cycle.Ids, targetId);
            string readback = control + "=" + (position + 1) + "/" + cycle.Count + ":" + cycle.Current;
            if ((position < 0 && !cycle.OffList) || target < 0)
                return n == 0
                    ? V(UiPadVerdict.PressNoEffect, false, null, calls, "cycle_target_unavailable", now, readback)
                    : V(UiPadVerdict.PressUncertain, false, null, calls, "cycle_target_lost", now, readback);
            if (position == target)
                return V(UiPadVerdict.PressApplied, true, "changed", calls, "cycle_reached", now, readback);
            if (n >= CharCustomTable.MaxClicksPerPress)
                return V(UiPadVerdict.PressApplied, true, "changed", calls, "cycle_partial:" + n, now, readback);
            int count = cycle.Count;
            int direction;
            string expected;
            if (cycle.OffList)
            {
                // rotateShirt / rotatePantStyle from a value off the list: either arrow sets Ids[0] (Farmer.cs:5169-5221).
                direction = 1;
                expected = cycle.Ids[0];
            }
            else
            {
                int forward = CharCustomTable.Wrap(target - position, count), backward = CharCustomTable.Wrap(position - target, count);
                direction = forward <= backward ? 1 : -1;
                expected = cycle.Ids[CharCustomTable.Wrap(position + direction, count)];
            }
            try
            {
                NativeClick(calls, "receiveLeftClick(" + CharCustomTable.NativeButtonName(control) + (direction > 0 ? " right)" : " left)"),
                    () => port.ClickArrow(control, direction));
            }
            catch (CharCustomGuardException guard) when (n > 0)
            {
                // Earlier clicks of this press were applied and read back; the guard stopped the next one before the menu.
                return V(UiPadVerdict.PressApplied, true, "changed", calls, "cycle_stopped:" + guard.Reason, now, readback);
            }
            UiPadFacts after = port.Read();
            if (Keyboard(port, calls, after) is { } open)
                return open;
            if (!SameReceiver(before, after))
                return V(UiPadVerdict.PressUncertain, false, null, calls, "receiver_changed", after);
            string got = after.CharCustom!.Cycles.TryGetValue(control, out CharCustomCycle? next) ? next.Current : "";
            if (!string.Equals(got, expected, StringComparison.Ordinal))
                return V(UiPadVerdict.PressUncertain, false, null, calls, "cycle_readback_mismatch:" + expected + "->" + got, after,
                    control + "=" + got);
            now = after;
        }
    }

    static UiPadVerdict Color(ICharCustomPort port, UiPadFacts before, string control, int value, List<string> calls)
    {
        (string picker, int bar) = CharCustomTable.ColorOf(control)!.Value;
        int old = before.CharCustom!.Picker(picker).Bar(bar);
        NativeClick(calls, "receiveLeftClick(" + picker + " picker bar " + bar + " @" + value + ")", () => port.ClickColor(picker, bar, value));
        UiPadFacts after = port.Read();
        if (Keyboard(port, calls, after) is { } open)
            return open;
        if (!SameReceiver(before, after))
            return V(UiPadVerdict.PressUncertain, false, null, calls, "receiver_changed", after);
        int got = after.CharCustom!.Picker(picker).Bar(bar);
        string readback = control + "=" + got;
        return got == value ? V(UiPadVerdict.PressApplied, true, "changed", calls, "color_set", after, readback)
            : got == old ? V(UiPadVerdict.PressNoEffect, false, null, calls, "color_unchanged", after, readback)
            : V(UiPadVerdict.PressUncertain, false, null, calls, "color_readback_mismatch", after, readback);
    }

    static UiPadVerdict Confirm(ICharCustomPort port, UiPadFacts before, List<string> calls)
    {
        CharCustomFacts cc = before.CharCustom!;
        NativeClick(calls, "receiveLeftClick(OK)", port.ClickOk);
        UiPadFacts after = port.Read();
        if (Keyboard(port, calls, after) is { } open)
            return open;
        if (SameReceiver(before, after))
            return UiPadToken.StateDigest(after) == UiPadToken.StateDigest(before)
                ? V(UiPadVerdict.PressNoEffect, false, null, calls, "ok_not_accepted", after)
                : V(UiPadVerdict.PressUncertain, false, null, calls, "ok_menu_still_open", after);
        FarmerProfile profile = port.Profile();
        bool created = profile.IsCustomized
            && string.Equals(profile.Name, cc.NameText.Trim(), StringComparison.Ordinal)
            && string.Equals(profile.FavoriteThing, cc.FavoriteText.Trim(), StringComparison.Ordinal)
            && profile.IsMale == cc.IsMale;
        return created
            ? V(UiPadVerdict.PressApplied, true, "created", calls, "farmer_created", after, profile.Readback())
            : V(UiPadVerdict.PressUncertain, false, null, calls, "created_unverified", after, profile.Readback());
    }
}

/// <summary>ui_inspect publication of the "char_custom" block (values, options, actions; no coordinates).</summary>
public static class CharCustomSnapshot
{
    static Dictionary<string, object> CycleBlock(CharCustomCycle cycle, bool withOptions, string? gameLabel)
    {
        var block = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["value"] = cycle.Position + 1,
            ["count"] = cycle.Count,
            ["id"] = CharCustomTable.Trim(cycle.Current, 32),
        };
        if (gameLabel is not null)
            block["game_label"] = gameLabel;
        if (cycle.OffList)
            block["off_list"] = true; // id = the shown item; the next arrow click lands on option 1

        if (withOptions)
            block["options"] = cycle.Ids.Take(CharCustomTable.MaxOptions).Select((id, i) => new Dictionary<string, object>
            {
                ["value"] = i + 1,
                ["id"] = CharCustomTable.Trim(id, 32),
                ["label"] = CharCustomTable.Trim(cycle.Labels is { } labels && i < labels.Count ? labels[i] : id),
            }).ToArray();
        return block;
    }

    static Dictionary<string, object> ColorBlock(CharCustomColor c) => new(StringComparer.Ordinal)
    {
        ["hue"] = c.Hue,
        ["saturation"] = c.Saturation,
        ["value"] = c.Value,
    };

    public static Dictionary<string, object> Build(CharCustomFacts f)
    {
        var block = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["source"] = f.Source,
            ["farm_name"] = CharCustomTable.Trim(f.FarmName, 32),
            ["name"] = new Dictionary<string, object> { ["text"] = CharCustomTable.Trim(f.NameText, 32), ["max_length"] = UiG3Table.NameMaxLength },
            ["favorite"] = new Dictionary<string, object> { ["text"] = CharCustomTable.Trim(f.FavoriteText, 32), ["max_length"] = UiG3Table.NameMaxLength },
            ["gender"] = new Dictionary<string, object>
            {
                ["value"] = f.IsMale ? 1 : 2,
                ["label"] = f.IsMale ? "male" : "female",
                ["options"] = CharCustomTable.GenderOptions.ToArray(),
            },
            ["eye_color"] = ColorBlock(f.EyeColor),
            ["hair_color"] = ColorBlock(f.HairColor),
            ["pants_color"] = ColorBlock(f.PantsColor),
            ["color_max"] = CharCustomTable.ColorMax,
            ["synced"] = f.Synced,
            ["can_leave"] = f.CanLeave,
            ["mouse_over_text_box"] = f.MouseOverTextBox,
            ["keyboard_open"] = f.KeyboardOpen,
            ["subscriber_foreign"] = f.SubscriberForeign,
            ["max_clicks_per_press"] = CharCustomTable.MaxClicksPerPress,
            ["actions"] = CharCustomTable.Actions.Select(a => new Dictionary<string, object>
            {
                ["choice"] = a.Choice, ["control"] = a.Control, ["kind"] = a.Kind,
            }).ToArray(),
        };
        foreach (string key in CharCustomTable.Cycles)
            if (f.Cycles.TryGetValue(key, out CharCustomCycle? cycle))
                block[key] = CycleBlock(cycle, key is CharCustomTable.Shirt or CharCustomTable.Pants,
                    key == CharCustomTable.Shirt ? f.ShirtGameLabel : key == CharCustomTable.Pants ? f.PantsGameLabel : null);
        if (f.MissingControls.Count > 0)
            block["missing"] = f.MissingControls.ToArray();
        if (CharCustomPolicy.Gate(f) is { } gate)
            block["wait_reason"] = gate;
        return block;
    }
}

// ---------------------------------------------------------------------- the join screen (read-only, pre-world)

/// <summary>One FarmhandMenu slot as the farmhand's title screen shows it (FarmhandMenu.cs:14-104).</summary>
public sealed record JoinSlotFacts(int Index, bool FarmhandSlot, bool Customized, string Name, bool BelongsToAnotherPlayer,
    CcRect? Button);

/// <summary>
/// Pre-world facts of the farmhand's join screen (TitleMenu -> CoopMenu -> FarmhandMenu), written as join_screen.json in
/// the command folder while the world is not ready (flag CharacterCreationEnabled). Lets the operator join confirm the
/// slot list natively (an unclaimed "New Farmhand" slot, FarmhandMenu.cs:66-75, or ARTI's named farmhand) instead of a
/// reference crop that shows a save-specific name. Field reads only.
/// </summary>
public sealed record JoinScreenFacts(bool TitleMenu, string? SubMenu, bool FarmhandMenu, bool HasClient, bool GettingFarmhands,
    bool ApprovingFarmhand, bool TimedOut, string? Message, bool Loading, int CurrentItemIndex, IReadOnlyList<JoinSlotFacts> Slots,
    float UiScale, int ViewportWidth, int ViewportHeight)
{
    public const string FileName = "join_screen.json";
    public const string Schema = "arti_join_screen.v1";
    public const int MaxSlots = 16;

    public Dictionary<string, object?> Wire(long utcMs, long ticks) => new(StringComparer.Ordinal)
    {
        ["schema"] = Schema,
        ["utc_ms"] = utcMs,
        ["ticks"] = ticks,
        ["title_menu"] = this.TitleMenu,
        ["sub_menu"] = this.SubMenu,
        ["farmhand_menu"] = this.FarmhandMenu,
        ["has_client"] = this.HasClient,
        ["getting_farmhands"] = this.GettingFarmhands,
        ["approving_farmhand"] = this.ApprovingFarmhand,
        ["timed_out"] = this.TimedOut,
        ["message"] = this.Message is null ? null : UiPadTable.Trim(this.Message, 120),
        ["loading"] = this.Loading,
        ["current_item_index"] = this.CurrentItemIndex,
        ["ui_scale"] = Math.Round(this.UiScale, 4),
        ["ui_viewport"] = new[] { this.ViewportWidth, this.ViewportHeight },
        ["slots"] = this.Slots.Take(MaxSlots).Select(s => new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["index"] = s.Index,
            ["farmhand_slot"] = s.FarmhandSlot,
            ["customized"] = s.Customized,
            ["name"] = CharCustomTable.Trim(s.Name, 32),
            ["belongs_to_another_player"] = s.BelongsToAnotherPlayer,
            ["button"] = s.Button is { } b ? new[] { b.X, b.Y, b.W, b.H } : null,
        }).ToArray(),
    };
}
