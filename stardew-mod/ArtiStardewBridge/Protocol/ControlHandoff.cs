namespace ArtiStardewBridge.Protocol;

/// <summary>Observed control ownership only. Never a play grant or an event-specific decision.</summary>
public static class ControlHandoff
{
    public static string? Reason(PlayerControlDiagnostics f)
    {
        if ((f.EventUp || f.EventId is not null || f.FarmEventType is not null)
                && !(f.EventPlayerControl == true && f.CanPlayerMove)) return "event";
        if (f.MinigameType is not null) return "minigame";
        if (f.IsWarping || f.LocationRequestPending) return "warp";
        if (f.GlobalFade || f.FadeToBlack) return "fade";
        if (f.FreezeControls) return "freeze_controls";
        if (f.FreezePause > 0) return "freeze_pause";
        // Modal menus have their own generic UI owner. Do not relabel a menu as a forced world lock.
        if (f.ActiveMenu is null && !f.PlayerCanMove && !f.UsingTool && !f.IsEating) return "cannot_move";
        return null;
    }
}
