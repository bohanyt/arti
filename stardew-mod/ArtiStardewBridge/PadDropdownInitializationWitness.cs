using ArtiStardewBridge.Protocol;
using HarmonyLib;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// FIXB46: witness only OptionsDropDown(string,int,int,int) initialization, case 38.
/// The pinned 1.6.15 constructor calls Options.setDropDownToProperValue, which reloads
/// startup_preferences.gamepadMode (Options.cs:2006-2011). Its paired Harmony __state
/// captures the before binding/values and is consumed immediately on return. No
/// transpiler, event/timing inference or persistent menu permission is involved.
/// Installation follows PadCursorWarpGuard's flag-gated Harmony pattern and the
/// existing ResolvePadPin/BackgroundControlInputHooks game-MVID gate.
/// </summary>
internal static class PadDropdownInitializationWitness
{
    private static Func<PadModeAdapter?> adapter = static () => null;
    public static bool Installed { get; private set; }

    public static void Install(string modId, Func<PadModeAdapter?> getAdapter, IMonitor monitor)
    {
        adapter = getAdapter;
        var harmony = new Harmony(modId + ".PadDropdownInitializationWitness");
        try
        {
            if (typeof(Game1).Module.ModuleVersionId != BackgroundMoveRuntime.PinnedGameMvid)
                throw new InvalidOperationException("dropdown_game_mvid");
            var constructor = AccessTools.Constructor(typeof(OptionsDropDown),
                new[] { typeof(string), typeof(int), typeof(int), typeof(int) })
                ?? throw new MissingMethodException("OptionsDropDown(string,int,int,int)");
            var interactive = AccessTools.Method(typeof(Options), nameof(Options.setGamepadMode), new[] { typeof(string) })
                ?? throw new MissingMethodException("Options.setGamepadMode(string)");
            harmony.Patch(constructor,
                prefix: new HarmonyMethod(typeof(PadDropdownInitializationWitness), nameof(ConstructorPrefix)) { priority = Priority.Last },
                postfix: new HarmonyMethod(typeof(PadDropdownInitializationWitness), nameof(ConstructorPostfix)) { priority = Priority.First });
            harmony.Patch(interactive,
                prefix: new HarmonyMethod(typeof(PadDropdownInitializationWitness), nameof(InteractivePrefix)) { priority = Priority.First });
            Installed = true;
            monitor.Log("PAD dropdown initialization witness installed (case 38; synchronous one-shot).", LogLevel.Info);
        }
        catch (Exception ex)
        {
            Installed = false;
            harmony.UnpatchAll(harmony.Id);
            monitor.Log($"PAD dropdown witness NOT installed; pad path disabled: {ex.GetType().Name}: {ex.Message}", LogLevel.Error);
        }
    }

    private static void ConstructorPrefix(int whichOption, out PadModeAdapter.DropdownWitness? __state)
    {
        __state = null;
        PadModeAdapter? target = null;
        try
        {
            if (Installed)
            {
                target = adapter();
                __state = target?.BeginDropdownInitialization(whichOption);
            }
        }
        catch (Exception ex)
        {
            Installed = false; // also fail closed if adapter resolution itself failed
            target?.OnDropdownCallbackFault("constructor_prefix", ex);
        }
    }

    private static void ConstructorPostfix(PadModeAdapter.DropdownWitness? __state)
    {
        PadModeAdapter? target = null;
        try
        {
            if (Installed)
            {
                target = adapter();
                target?.EndDropdownInitialization(__state);
            }
        }
        catch (Exception ex)
        {
            Installed = false;
            target?.OnDropdownCallbackFault("constructor_postfix", ex);
        }
    }

    // Initialization assigns the mode directly. setGamepadMode is a separate owner
    // write path (Options.changeDropDownOption case 38) and invalidates any witness.
    private static void InteractivePrefix(Options __instance)
    {
        PadModeAdapter? target = null;
        try
        {
            if (Installed && ReferenceEquals(__instance, Game1.options))
            {
                target = adapter();
                target?.OnInteractiveModeWrite();
            }
        }
        catch (Exception ex)
        {
            Installed = false;
            target?.OnDropdownCallbackFault("interactive_prefix", ex);
        }
    }
}
