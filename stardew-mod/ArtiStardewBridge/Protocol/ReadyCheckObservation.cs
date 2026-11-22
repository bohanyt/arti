using System.Reflection;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Read-only facts for every native ready barrier, independent of its check name or UI policy.</summary>
public static class ReadyCheckObservation
{
    public static Dictionary<string, object> Capture(object synchronizer, string check, int ready, int needed,
        bool allReady, bool cancelable)
    {
        // ReadySynchronizer exposes counts publicly, but local State only through the existing check.
        // GetIfExists never creates or updates a check (unlike GetOrCreate / SetLocalReady).
        string state = "unknown";
        try
        {
            object? native = synchronizer.GetType().GetMethod("GetIfExists", BindingFlags.Instance | BindingFlags.NonPublic)
                ?.Invoke(synchronizer, new object[] { check });
            state = native?.GetType().GetProperty("State")?.GetValue(native)?.ToString() ?? "unknown";
        }
        catch { } // unknown remains unknown; an open dialog or a count is not proof of local readiness
        bool? local = state is "Ready" or "Locked" ? true : state == "NotReady" ? false : null;
        return new Dictionary<string, object>
        {
            ["check"] = UiPadTable.Trim(check, 128), ["kind"] = Kind(check), ["local_state"] = state is "Ready" or "Locked" or "NotReady" ? state : "unknown",
            ["local_ready"] = local!, ["ready_count"] = ready, ["needed"] = needed, ["all_ready"] = allReady,
            ["cancelable"] = cancelable,
            ["waiting_for"] = allReady ? "none" : local == true && ready < needed ? "other_required_players" : "required_players_or_network_confirmation",
            // ClientReadyCheck receives counts, not the host's per-farmer ReadyStates map.
            ["waiting_player_ids_known"] = false,
            ["cancel_control"] = cancelable ? "cancelButton; closing cancels local readiness" : "none",
        };
    }

    public static string Kind(string check) => check switch { "sleep" => "sleep", "festivalStart" => "festival_start",
        "festivalEnd" => "festival_end", _ => "other" };

    public static string? CheckName(UiPadFacts facts) => Read(facts)?.GetProperty("check").GetString();

    static JsonElement? Read(UiPadFacts facts) =>
        facts.GenericValues?.TryGetValue("ready_check", out object? value) == true && value is not null
            ? JsonSerializer.SerializeToElement(value) : null;

    public static string? Effect(UiPadFacts before, UiPadFacts after, string? effect, out bool unreadable)
    {
        unreadable = false;
        if (Read(before) is not { } b) return effect;
        if (!b.TryGetProperty("local_ready", out var local) || local.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
        { unreadable = effect == "closed"; return unreadable ? null : effect; }
        if (local.ValueKind != JsonValueKind.True) return effect;
        if (Read(after) is not { } a || !a.TryGetProperty("check", out var check)
            || check.GetString() != b.GetProperty("check").GetString()
            || !a.TryGetProperty("local_ready", out var current) || current.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
        { unreadable = true; return null; }
        return current.ValueKind == JsonValueKind.False ? "readiness_lost" : effect;
    }
}
