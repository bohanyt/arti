using System.Text.Json;
using System.Text.Json.Serialization;
using ArtiCore.Pure;

namespace ArtiStardewBridge.Protocol;

public sealed record AdmissionRefusal(
    [property: JsonPropertyName("reason")] string Reason,
    [property: JsonPropertyName("native_code")] string NativeCode,
    [property: JsonPropertyName("detail")] string Detail,
    [property: JsonPropertyName("debt_id"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? DebtId = null);

/// <summary>Only pre-edge refusals. Executed/uncertain results and response-only replays retain their evidence.</summary>
public static class AdmissionRefusals
{
    private static readonly JsonElement Registry = LoadRegistry();
    private static JsonElement LoadRegistry()
    {
        using var stream = typeof(NativeVerbRules).Assembly.GetManifestResourceStream("refusals.json")!;
        using var doc = JsonDocument.Parse(stream); return doc.RootElement.Clone();
    }

    public static AdmissionRefusal Classify(string native, string? detail = null, string? code = null, string? debtId = null)
    {
        string arm = detail ?? "";
        if (native == "ArmRejected" && arm.StartsWith("PRECONDITION_UNAVAILABLE(", StringComparison.Ordinal) && arm.EndsWith(")", StringComparison.Ordinal))
            arm = arm.Substring(25, arm.Length - 26);
        if (native == "ArmRejected" && (Registry.GetProperty("native_map").TryGetProperty(arm, out _) || arm == "MENU_OPEN")) native = arm;
        code ??= native == "MENU_OPEN" ? "precondition_stale"
            : Registry.GetProperty("native_map").TryGetProperty(native, out var mapped) ? mapped.GetString()! : "defect-unclassified";
        return new(code, native, (detail ?? native).Length > 240 ? (detail ?? native)[..240] : detail ?? native, debtId);
    }

    public static string? Verb(MenuStepCommandRequest r) => r.VerbEnvelope?.GetProperty("verb").GetString()
        ?? LegacyFormMap.Resolve(r).Verb;

    public static MenuStepCommandResult Attach(MenuStepCommandResult result, MenuStepCommandRequest request,
        AdmissionRefusal? refusal, long epoch, long tick, bool admitted = false, object[]? debts = null, ActiveAttempt? attempt = null)
    {
        bool executed = result.Status != MenuStepCommandResult.StatusRejected
            && (result.PressCount > 0 || Verb(request) == "observe" && result.Success);
        refusal = executed ? null : refusal ?? Classify("N0_NOT_CONSUMED", result.RejectReason);
        result = result with { NotExecuted = refusal };
        var receipt = result.ReleaseReceipt; var answer = attempt?.AnswerWitness;
        object[] witnesses = answer is null ? Array.Empty<object>() : new object[] { new {
            kind = answer.Kind == "succession" ? answer.Edge == "closed" ? "receiver_closed" : "receiver_opened" : answer.Kind,
            target = request.RequestId, before = new { receiver_epoch = answer.AdmissionEpoch },
            after = answer.Kind == "profession_selected" ? (object)new { skill = answer.Skill, level = answer.Level, profession = answer.Profession }
                : new { receiver_epoch = epoch }, frame = answer.Frame } };
        if (executed && attempt is { TradeWitness: { } trade }
            && (!attempt.BracketFailed || trade is NativeTradeWitness.UnavailableFact)) witnesses = witnesses.Append(trade).ToArray();
        // # SPEC-Q ADR-17: CT pins v3 refusals for mapped legacy mutations despite R0-07 C2.
        if (Verb(request) is not { } verb || string.IsNullOrEmpty(request.RequestId) || request.Generation < 1
            || request.RequestId.Length > 128 || request.RequestId.Any(c => c is not (>= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9') && !"._:/|-".Contains(c))
            || refusal is not null && !Registry.GetProperty("codes").TryGetProperty(refusal.Reason, out _)) return result;
        return result with { RefusalEnvelope = JsonSerializer.SerializeToElement(new {
            v = "arti.verb_result/3", request_id = request.RequestId,
            attempt_id = request.VerbEnvelope?.GetProperty("attempt_id").GetString() ?? request.RequestId,
            verb, actor = request.VerbEnvelope?.GetProperty("actor").GetString() ?? "legacy", client = "inbox",
            generation = request.Generation, executed, not_executed = refusal, delivered = result.PressCount > 0,
            edges = executed ? result.PressCount ?? 0 : 0, released = receipt is not null,
            released_via = receipt?.GetProperty("via").GetString(), release_receipt = receipt,
            receiver_epoch_before = attempt?.Validity?.ReceiverEpoch ?? epoch, receiver_epoch_after = epoch,
            validity = executed && attempt?.Controller is { } controller ? controller.Receipt
                : (object)new { phase_at_end = executed ? attempt?.Validity?.EndPhase ?? "single_edge" : "admission",
                    ended_by = !executed ? "no_input" : answer is not null ? "legal_successor" : "completed", adopted = Array.Empty<object>() },
            succession = answer is { Kind: "succession" } ? new { edge = answer.Edge, authenticated = true, via = "edge", in_legal_set = true } : (object?)null,
            transition_after = (string?)null, barrier_after = (object?)null, debts_after = debts ?? Array.Empty<object>(),
            closure = result.Closure ?? JsonSerializer.SerializeToElement(new { closed = false, witness = Array.Empty<string>(), frame = (long?)null }),
            witnesses, native_lifecycle = executed ? result.Status == MenuStepCommandResult.StatusUncertain ? "uncertain" : "settled"
                : admitted ? "admitted" : "not_admitted", obs_revision_after = (long?)null,
            obs_pending = true, timing = new { admit_tick = attempt?.AdmitTick, first_edge_tick = attempt?.FirstEdgeTick,
                release_tick = receipt?.GetProperty("tick").GetUInt64(), final_tick = tick },
            faults = Array.Empty<string>()
        }) };
    }

    public static string Serialize(MenuStepCommandResult result)
    {
        if (result.RefusalEnvelope is not { } envelope
            || result.Status != MenuStepCommandResult.StatusRejected && !envelope.GetProperty("executed").GetBoolean())
            return JsonSerializer.Serialize(result, ProtocolJson.Options);
        var fields = envelope.EnumerateObject().ToDictionary(p => p.Name, p => (object?)p.Value);
        if (result.ReleaseReceipt is { } receipt) {
            fields["release_receipt"] = receipt; fields["released"] = true;
            fields["released_via"] = receipt.GetProperty("via").GetString();
        }
        if (result.Closure is { } closure) fields["closure"] = closure;
        return JsonSerializer.Serialize(fields, ProtocolJson.Options);
    }
}
