using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    // Called after lifecycle cleanup on title return, or after the old inbox
    // slot has been interrupted on a native save/player/screen transition.
    private void RotateCommandSession()
    {
        this.commandSessionId = Guid.NewGuid().ToString("N");
        this.commandWorldIdentity = null;
        this.contextGenerationTracker.Reset();
        this.menuInstanceTokenTracker.Reset();
        this.textInputTokenTracker.Reset();
        this.commandInbox.ResetForNewSession();
        this.actionOwner.ResetForNewSession();
        this.lastPublishedSession = null;
        this.RestartActionOwnerPoller();
    }

    private CommandInboxIdentity RefreshCommandWorldIdentity(CommandInboxIdentity identity, bool worldReady, long now)
    {
        // A transient missing native identity is not evidence of a new world.
        if (!worldReady || identity.PlayerId == 0 || string.IsNullOrWhiteSpace(identity.SaveId)
            || identity.RuntimeInstanceId != this.runtimeInstanceId || identity.SessionId != this.commandSessionId)
            return identity;
        if (this.commandWorldIdentity is { } previous && (previous.SaveId != identity.SaveId
            || previous.PlayerId != identity.PlayerId || previous.ScreenId != identity.ScreenId))
        {
            MenuStepCommandResult? interrupted = this.commandInbox.CompleteInterrupted("CommandWorldChanged", identity, now);
            if (interrupted is not null) this.WriteCommandResult(interrupted);
            this.RotateCommandSession();
            identity = identity with { SessionId = this.commandSessionId, ContextGeneration = this.contextGenerationTracker.Current };
        }
        this.commandWorldIdentity = identity;
        return identity;
    }

    private bool BindCommandJournalScope(CommandInboxIdentity identity, bool worldReady) =>
        worldReady && !this.commandInbox.HasInflight
        && identity.RuntimeInstanceId == this.runtimeInstanceId && identity.SessionId == this.commandSessionId
        && this.commandInbox.BindJournalScope(identity);
}
