using System.Text.RegularExpressions;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Rehearsal-only HOST console command <c>arti_host</c> (operator-staging/DESIGN_HOST_BACKGROUND_INPUT_2026-10-22.md
/// §3 Phase 2). Pure grammar + gating; ModEntry.HostConsole.cs reads the game facts and makes the ONE native call.
/// Reached only through the HOST supervisor's stdin pipe (scripts/m1_operator/host_console.py): no window, focus,
/// cursor or OS input. Gates: <c>ModConfig.RehearsalHostConsoleEnabled</c> (default OFF; only a rehearsal HOST
/// template sets it), bridge <c>Enabled</c> and <c>MenuStepCommandInboxEnabled</c> both off (HOST template shape),
/// main player, not split-screen. Never active in the real stream (the owner plays HOST by hand).
/// <list type="bullet">
/// <item><c>arti_host load &lt;SaveFolder&gt;</c>: title screen only (TitleMenu active, no world, no loader, no
/// FarmhandMenu); the folder and its save file must exist. Mirrors CoopMenu.HostFileSlot.Activate
/// (<c>multiplayerMode = 2</c>, then SaveFileSlot.Activate: <c>SaveGame.Load(slot)</c> + <c>exitActiveMenu</c>).</item>
/// <item><c>arti_host ok</c>: world ready, active menu is a LevelUpMenu (not the profession chooser) or the
/// ShippingMenu summary, and the shared ui_key classifier says ready. Calls <c>LevelUpMenu.okButtonClicked</c> or
/// <c>ShippingMenu.receiveLeftClick</c> at its own okButton centre (the game handler; no OS input).</item>
/// </list>
/// Every call logs exactly one <c>ARTI_HOST action=… outcome=…</c> line. Evidence ceiling: UNIT_TESTED.
/// </summary>
public static class HostRehearsalConsole
{
    public const string CommandName = "arti_host";
    public const string Load = "load", Ok = "ok";

    /// <summary>Stardew save folder: sanitized farmer name + "_" + game id (e.g. <c>proof1_100000002</c>).</summary>
    static readonly Regex SaveFolderPattern = new(@"\A[A-Za-z0-9]{1,40}_[0-9]{1,20}\z", RegexOptions.CultureInvariant);

    public static bool IsSaveFolderName(string? name) => name is not null && SaveFolderPattern.IsMatch(name);

    public sealed record Request(string Verb, string? SaveFolder);

    /// <summary>Exact argv grammar: <c>load &lt;SaveFolder&gt;</c> or <c>ok</c>. Anything else is refused.</summary>
    public static Request? Parse(IReadOnlyList<string>? args, out string? refusal)
    {
        refusal = null;
        string? verb = args is { Count: > 0 } ? args[0] : null;
        switch (verb)
        {
            case Load when args!.Count == 2:
                if (IsSaveFolderName(args[1]))
                    return new Request(Load, args[1]);
                refusal = "invalid_save_folder_name";
                return null;
            case Load:
                refusal = "usage:load_needs_one_save_folder";
                return null;
            case Ok when args!.Count == 1:
                return new Request(Ok, null);
            case Ok:
                refusal = "usage:ok_takes_no_arguments";
                return null;
            default:
                refusal = "unknown_verb";
                return null;
        }
    }

    /// <summary>Identity gate for every verb. Flag first: OFF means nothing else is even read. Then the bridge
    /// config: every HOST template has <c>Enabled</c> and <c>MenuStepCommandInboxEnabled</c> false, ARTI's farmhand
    /// has both true. At the title screen the farmhand is also the "main player", so a farmhand config never passes
    /// even if the flag was set there by mistake.</summary>
    public static string? GateRefusal(bool enabled, bool bridgeEnabled, bool menuStepInboxEnabled, bool mainPlayer,
        bool splitScreen) =>
        !enabled ? "flag_off"
        : bridgeEnabled ? "bridge_enabled"
        : menuStepInboxEnabled ? "menu_step_inbox_enabled"
        : !mainPlayer ? "not_main_player"
        : splitScreen ? "split_screen"
        : null;

    public sealed record TitleFacts(bool WorldReady, bool TitleMenuActive, string? SubMenuType, bool TitleGameMode,
        bool LoaderBusy, bool IsClient, bool SaveFolderExists, bool SaveFileExists);

    /// <summary>Title-screen admission for <c>load</c>. Co-op/Load submenus are the native host-slot route; the
    /// FarmhandMenu (a joining client) and every other submenu refuse.</summary>
    public static string? LoadRefusal(TitleFacts f) =>
        f.WorldReady ? "world_loaded"
        : !f.TitleMenuActive ? "not_title_screen"
        : !f.TitleGameMode ? "not_title_game_mode"
        : f.LoaderBusy ? "load_in_progress"
        : f.IsClient ? "client_mode"
        : f.SubMenuType is not (null or "CoopMenu" or "LoadGameMenu") ? "title_submenu:" + f.SubMenuType
        : !f.SaveFolderExists ? "save_folder_missing"
        : !f.SaveFileExists ? "save_file_missing"
        : null;

    /// <summary><c>ok</c> admission over the shared ui_key classification of the active menu.</summary>
    public static string? OkRefusal(bool worldReady, UiKeyState? state) =>
        !worldReady ? "world_not_ready"
        : state is null ? "no_menu"
        : state.MenuType is not ("LevelUpMenu" or "ShippingMenu") ? "unsupported_menu:" + state.MenuType
        : state.Kind == UiKeyTable.LevelUpProfession ? "profession_chooser"
        : !state.Ready ? "not_ready:" + (state.NotReadyReason ?? "unknown")
        : state.Kind is not (UiKeyTable.LevelUp or UiKeyTable.ShippingSummary) ? "unsupported_kind:" + state.Kind
        : null;

    public static string LogLine(string action, string outcome, string? detail = null) =>
        $"ARTI_HOST action={action} outcome={outcome}" + (string.IsNullOrEmpty(detail) ? "" : " " + detail);
}
