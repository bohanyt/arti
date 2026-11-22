namespace ArtiCore.Pure;

public sealed record NativeAnswerWitness(string AttemptId, string Kind, long AdmissionEpoch, bool Authenticated,
    bool InLegalSet, string? Edge = null, int? From = null, int? To = null, long Frame = 0, int? Skill = null, int? Level = null, int? Profession = null);
public sealed record ProfessionState(int Skill, int Level, bool Choosing, bool Committed, int[] Options, int[] Owned);

/// <summary>A delivered callback/native-update bracket; snapshots alone never create an AW.</summary>
public sealed class ReceiverAnswerWindow
{
    readonly string attempt;
    readonly ReceiverSurface admission;
    readonly AnswerRule rule;
    readonly bool proseAdvance;
    bool open, gate;
    int page;
    ProfessionState? profession;
    public ReceiverAnswerWindow(NativeVerbRules rules, string attempt, ReceiverSurface admission, string role, string gesture)
    { this.attempt = attempt; this.admission = admission; rule = rules.AnswerRow(admission.Top?.MenuType ?? "hud", role, gesture);
      proseAdvance = admission.Top?.MenuType == "DialogueBox" && role == "other" && gesture == "advance"; }
    public bool Begin(ReceiverSurface before, bool canReceiveInput, int pageIndex, ProfessionState? profession = null)
    {
        if (open || before.Epoch != admission.Epoch || !ReferenceEquals(before.Top?.Instance, admission.Top?.Instance)) return false;
        open = true; gate = canReceiveInput; page = pageIndex; this.profession = profession; return true;
    }
    public NativeAnswerWitness? End(ReceiverSurface after, bool canReceiveInput, int pageIndex, bool delivered,
        bool completed, string? questionKey = null, ProfessionState? profession = null)
    {
        bool bracket = open; open = false;
        if (!bracket || !completed || !delivered || rule.AnswerClass != "terminal" || rule.Row is not { } row) return null;
        if (admission.Top?.MenuType == "LevelUpMenu" && row.GetProperty("key").GetProperty("control_role").GetString() == "profession"
            && this.profession is { Choosing: true, Options.Length: 2 } p && profession is { Committed: true, Choosing: false } q
            && p.Skill == q.Skill && p.Level == q.Level && p.Options.SequenceEqual(q.Options)
            && !p.Options.Intersect(p.Owned).Any() && q.Options.Intersect(q.Owned).ToArray() is { Length: 1 } selected
            && after.Epoch == admission.Epoch && ReferenceEquals(after.Top?.Instance, admission.Top.Instance))
            return new(attempt, "profession_selected", admission.Epoch, true, true, Frame: after.Frame,
                Skill: p.Skill, Level: p.Level, Profession: selected[0]);
        if (after.Epoch != admission.Epoch)
        {
            if (after.Epoch != admission.Epoch + 1) return null; // no witness across an intervening foreign epoch
            string edge = after.Top is null ? "closed" : admission.Top is null
                || after.Stack.Any(layer => ReferenceEquals(layer.Instance, admission.Top.Instance)) ? "opened" : "replaced";
            bool legal = row.GetProperty("legal_successors").EnumerateArray().Any(s =>
                s.GetProperty("edge").GetString() == edge && s.GetProperty("receiver_class").GetString() == (after.Top?.MenuType ?? "hud")
                && s.GetProperty("via").GetString() == "edge"
                && (!s.TryGetProperty("question_key", out var q) || q.GetString() == questionKey));
            return legal ? new(attempt, "succession", admission.Epoch, true, true, edge, Frame: after.Frame) : null;
        }
        if (!ReferenceEquals(after.Top?.Instance, admission.Top?.Instance)) return null;
        if (gate && !canReceiveInput && admission.Top?.MenuType == "ShippingMenu" && rule.PostAnswer == "outro")
            return new(attempt, "ok_accepted", admission.Epoch, true, true, Frame: after.Frame);
        return proseAdvance && page != pageIndex ? new(attempt, "page_advanced", admission.Epoch, true, true, From: page, To: pageIndex, Frame: after.Frame) : null;
    }
}
