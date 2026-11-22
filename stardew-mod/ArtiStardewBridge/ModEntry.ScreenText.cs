using ArtiStardewBridge.Protocol;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    // Game thread, farmhand/screen identity already checked by AdvanceUiContextNative.
    // Only active presentations: never read future dialogue scripts, mailbox data,
    // chat, naming/text-entry fields, or arbitrary reflected string fields.
    IReadOnlyList<UiTextTickSample> CaptureScreenText(UiTextTickSample sample, bool exact = false)
    {
        if (this.config.GameTextDeliveryEnabled && !exact) return this.CapturePresentedGameText(sample);
        var result = new List<UiTextTickSample>();
        IClickableMenu? menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
        try
        {
            switch (menu)
            {
                case DialogueBox box:
                    sample = sample with { Family = Game1.eventUp ? "event_dialogue"
                        : this.TvDialogueCallbackPinned ? "tv" : "dialogue",
                        Source = "DialogueBox.getCurrentString", Text = box.getCurrentString() ?? "" };
                    break;
                case LetterViewerMenu letter:
                    sample = sample with { Family = "letter", Source = "LetterViewerMenu.currentPage",
                        Text = letter.scale >= 1f && letter.mailMessage is { } pages
                            && letter.page >= 0 && letter.page < pages.Count ? pages[letter.page] : "",
                        Options = Array.Empty<Dictionary<string, object>>(), Labels = Array.Empty<string>() };
                    break;
                case LevelUpMenu level:
                    sample = sample with { Family = "level_up", Source = "LevelUpMenu.presentedFields",
                        Text = this.ReadLevelScreenText(level), Labels = Array.Empty<string>() };
                    break;
                case ShippingMenu shipping:
                    sample = sample with { Family = "shipping_summary", Source = "ShippingMenu.visibleTotals",
                        Text = this.ReadShippingScreenText(shipping), Labels = Array.Empty<string>() };
                    break;
                case ShopMenu shop:
                    sample = sample with { Text = string.Join("\n", new[] { shop.boldTitleText,
                        Game1.options.showMerchantPortraits ? shop.potraitPersonDialogue : null }
                        .Where(s => !string.IsNullOrWhiteSpace(s))), Source = "ShopMenu.visibleLabels" };
                    break;
            }
        }
        catch (Exception)
        {
            // An unsupported game field must never break native input/capture.
            sample = sample with { CaptureComplete = false };
        }
        if (menu is not (DialogueBox or LetterViewerMenu or LevelUpMenu or ShippingMenu or ShopMenu)
            && Game1.keyboardDispatcher?.Subscriber is not null)
            sample = sample with { DeferredFamily = true, Text = "", Options = Array.Empty<Dictionary<string, object>>(), Labels = Array.Empty<string>() };
        result.Add(exact ? sample : ScreenText.Bounded(sample));
        if (Game1.displayHUD)
        {
            var messages = Game1.hudMessages.Where(m => m.timeLeft > 0 && m.transparency > 0)
                .Take(9).Select(m => m.message + (m.number > 1 ? " x" + m.number : "")).ToArray();
            if (messages.Length > 0) result.Add(ScreenText.Hud(sample, messages));
        }
        return result;
    }

    T ScreenField<T>(object menu, string name) => this.Helper.Reflection.GetField<T>(menu, name).GetValue();

    string ReadLevelScreenText(LevelUpMenu level)
    {
        if (!level.informationUp || this.ScreenField<int>(level, "timerBeforeStart") > 0) return "";
        var lines = new List<string> { this.ScreenField<string>(level, "title") };
        if (level.isProfessionChooser)
        {
            lines.Add(Game1.content.LoadString("Strings\\UI:LevelUp_ChooseProfession"));
            lines.AddRange(this.ScreenField<List<string>>(level, "leftProfessionDescription").Take(this.config.GameTextDeliveryEnabled ? int.MaxValue : 8));
            lines.AddRange(this.ScreenField<List<string>>(level, "rightProfessionDescription").Take(this.config.GameTextDeliveryEnabled ? int.MaxValue : 8));
        }
        else
        {
            lines.AddRange(this.ScreenField<List<string>>(level, "extraInfoForLevel").Take(this.config.GameTextDeliveryEnabled ? int.MaxValue : 8));
            foreach (CraftingRecipe recipe in this.ScreenField<List<CraftingRecipe>>(level, "newCraftingRecipes").Take(this.config.GameTextDeliveryEnabled ? int.MaxValue : 16))
            {
                string kind = Game1.content.LoadString("Strings\\UI:LearnedRecipe_" + (recipe.isCookingRecipe ? "cooking" : "crafting"));
                lines.Add(Game1.content.LoadString("Strings\\UI:LevelUp_NewRecipe", kind, recipe.DisplayName));
            }
        }
        return string.Join("\n", lines);
    }

    string ReadShippingScreenText(ShippingMenu shipping)
    {
        // Totals are final displayed values only once the intro has finished.
        if (this.ScreenField<int>(shipping, "introTimer") > 0
            || this.ScreenField<object?>(shipping, "saveGameMenu") is not null) return "";
        var lines = new List<string>();
        if (shipping.currentPage == -1)
        {
            var totals = this.ScreenField<List<int>>(shipping, "categoryTotals");
            for (int i = 0; i < Math.Min(6, totals.Count); i++)
                lines.Add(shipping.getCategoryName(i) + ": " + totals[i] + "g");
        }
        return string.Join("\n", lines);
    }
}
