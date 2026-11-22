using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// Motor receipt witness only. Installed SMAPI raises UpdateTicked even after
/// catching a failed base update; an event alone cannot certify completion.
/// This finalizer observes success/skip/failure without changing game behavior.
/// </summary>
internal static class UseToolCleanupUpdateObserver
{
    static Action<Game1, bool>? completed;
    static Action<IClickableMenu?,bool>? transferCompleted;
    static Action? receiverChanged;

    internal static void InstallReceiverReceipt(string modId, Action callback)
    {
        var method = AccessTools.DeclaredMethod(typeof(IClickableMenu), nameof(IClickableMenu.SetChildMenu))
            ?? throw new MissingMethodException("IClickableMenu.SetChildMenu");
        new Harmony(modId + ".ReceiverChildReceipt").Patch(method,
            finalizer: new HarmonyMethod(typeof(UseToolCleanupUpdateObserver), nameof(ReceiverFinalizer)) { priority = Priority.Last });
        receiverChanged = callback;
    }

    static void ReceiverFinalizer() => receiverChanged?.Invoke();

    // Existing motor receipt observer, not an observation snapshot patch.
    // Count EVERY setter invocation: even nested or same-value calls invalidate
    // the one admitted synchronous R0-to-R1 transition. No prefix is needed.
    internal static void InstallTransferReceipt(string modId,Action<IClickableMenu?,bool> callback)
    {
        var setter=AccessTools.PropertySetter(typeof(Game1),nameof(Game1.activeClickableMenu))
            ?? throw new MissingMethodException("activeClickableMenu setter");
        new Harmony(modId+".TransferReceiverReceipt").Patch(setter,
            finalizer:new HarmonyMethod(typeof(UseToolCleanupUpdateObserver),nameof(TransferFinalizer))
            {priority=Priority.Last});
        transferCompleted=callback;
    }

    static void TransferFinalizer(Exception? __exception,bool __runOriginal)
    {
        try { transferCompleted?.Invoke(Game1.activeClickableMenu,__exception is null&&__runOriginal); }
        catch { try { transferCompleted?.Invoke(null,false); } catch { } }
    }

    internal static void Install(string modId, Action<Game1, bool> callback)
    {
        var method = AccessTools.DeclaredMethod(typeof(Game1), "Update", new[] { typeof(GameTime) })
            ?? throw new MissingMethodException("Game1.Update(GameTime)");
        new Harmony(modId + ".UseToolCleanupReceipt").Patch(method,
            finalizer: new HarmonyMethod(typeof(UseToolCleanupUpdateObserver), nameof(Finalizer))
            { priority = Priority.Last });
        completed = callback;
    }

    // Void: never suppress/replace an exception, skip the original, or ACK here.
    static void Finalizer(Game1 __instance, Exception? __exception, bool __runOriginal)
    {
        completed?.Invoke(__instance, __exception is null && __runOriginal);
    }
}
