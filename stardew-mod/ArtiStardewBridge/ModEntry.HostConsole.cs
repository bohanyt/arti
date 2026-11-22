using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// Rehearsal-only HOST console command <c>arti_host</c> (grammar + gating in <see cref="HostRehearsalConsole"/>).
/// SMAPI runs console commands on the game thread. Gate: <see cref="ModConfig.RehearsalHostConsoleEnabled"/>
/// (default OFF) + bridge Enabled/MenuStepCommandInboxEnabled both off (never a farmhand config) + main player
/// + not split-screen, checked before any game state is read. Only native game
/// methods: no Helper.Input, no OS input, no cursor, no window focus. Each call logs one ARTI_HOST line.
/// Evidence ceiling: UNIT_TESTED until a live witness (HOST template with the flag, another window in front).
/// </summary>
public sealed partial class ModEntry
{
    void SetUpHostRehearsalConsole(IModHelper helper) =>
        helper.ConsoleCommands.Add(HostRehearsalConsole.CommandName,
            "REHEARSAL-ONLY HOST command (requires RehearsalHostConsoleEnabled; main player only): "
                + "arti_host load <SaveFolder> (title screen) | arti_host ok (LevelUpMenu OK / ShippingMenu OK)",
            this.OnHostRehearsalConsoleCommand);

    void OnHostRehearsalConsoleCommand(string name, string[] args)
    {
        string action = args.Length > 0 && args[0] is HostRehearsalConsole.Load or HostRehearsalConsole.Ok ? args[0] : "invalid";
        void Log(string outcome, string? detail, LogLevel level = LogLevel.Info) =>
            this.Monitor.Log(HostRehearsalConsole.LogLine(action, outcome, detail), level);
        string? refusal = HostRehearsalConsole.GateRefusal(this.config.RehearsalHostConsoleEnabled, this.config.Enabled,
            this.config.MenuStepCommandInboxEnabled, Context.IsMainPlayer, Context.IsSplitScreen);
        HostRehearsalConsole.Request? request = null;
        if (refusal is null)
            request = HostRehearsalConsole.Parse(args, out refusal);
        if (refusal is not null || request is null)
        {
            Log("refused", "reason=" + (refusal ?? "invalid_request"), LogLevel.Warn);
            return;
        }
        try
        {
            if (request.Verb == HostRehearsalConsole.Load)
                this.HostConsoleLoad(request.SaveFolder!, Log);
            else
                this.HostConsoleOk(Log);
        }
        catch (Exception ex)
        {
            Log("error", "exception=" + ex.GetType().Name + " message=" + ex.Message.Replace('\n', ' '), LogLevel.Error);
        }
    }

    void HostConsoleLoad(string saveFolder, Action<string, string?, LogLevel> log)
    {
        string folder = Path.Combine(Constants.SavesPath, saveFolder);
        IClickableMenu? sub = Game1.activeClickableMenu is TitleMenu ? TitleMenu.subMenu : null;
        var facts = new HostRehearsalConsole.TitleFacts(
            WorldReady: Context.IsWorldReady,
            TitleMenuActive: Game1.activeClickableMenu is TitleMenu,
            SubMenuType: sub?.GetType().Name,
            TitleGameMode: Game1.gameMode == Game1.titleScreenGameMode,
            LoaderBusy: Game1.currentLoader is not null,
            IsClient: Game1.IsClient,
            SaveFolderExists: Directory.Exists(folder),
            SaveFileExists: File.Exists(Path.Combine(folder, saveFolder)));
        // Type names are compared exactly, so a subclass (e.g. FarmhandMenu : LoadGameMenu) never passes.
        if (HostRehearsalConsole.LoadRefusal(facts) is { } refusal)
        {
            log("refused", $"reason={refusal} save={saveFolder}", LogLevel.Warn);
            return;
        }
        // Native host-slot route (1.6.15 decomp): CoopMenu.HostFileSlot.Activate sets multiplayerMode = 2 (server)
        // then LoadGameMenu.SaveFileSlot.Activate runs SaveGame.Load(slotName) and Game1.exitActiveMenu().
        Game1.multiplayerMode = 2;
        SaveGame.Load(saveFolder);
        Game1.exitActiveMenu();
        log("invoked", $"method=HostFileSlot.Activate(mirror) save={saveFolder} game_mode={Game1.gameMode} "
            + $"multiplayer_mode={Game1.multiplayerMode}", LogLevel.Info);
    }

    void HostConsoleOk(Action<string, string?, LogLevel> log)
    {
        IClickableMenu? menu = Game1.activeClickableMenu;
        UiKeyState? state = Context.IsWorldReady && menu is not null ? UiKeyClassifier.Classify(this.ReadUiKeyFacts(menu)) : null;
        if (HostRehearsalConsole.OkRefusal(Context.IsWorldReady, state) is { } refusal)
        {
            log("refused", $"reason={refusal} menu={menu?.GetType().Name ?? "none"}", LogLevel.Warn);
            return;
        }
        string method;
        switch (menu)
        {
            case LevelUpMenu level when !level.isProfessionChooser && state!.Kind == UiKeyTable.LevelUp:
                method = "LevelUpMenu.okButtonClicked";
                level.okButtonClicked();
                break;
            case ShippingMenu shipping when state!.Kind == UiKeyTable.ShippingSummary:
            {
                var center = shipping.okButton.bounds.Center;
                method = "ShippingMenu.receiveLeftClick(okButton.center)";
                shipping.receiveLeftClick(center.X, center.Y);
                break;
            }
            default:
                log("refused", $"reason=menu_kind_mismatch menu={menu?.GetType().Name}", LogLevel.Warn);
                return;
        }
        log("invoked", $"method={method} kind={state!.Kind} menu_after={Game1.activeClickableMenu?.GetType().Name ?? "none"}",
            LogLevel.Info);
    }
}
