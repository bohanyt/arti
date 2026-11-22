using System.Globalization;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>R0-07 §7.1 envelope and immutable digest projection. Verb-specific pins/dispatch
/// remain with A2/A7; this reader does not choose a row or translate a verb.</summary>
public static class VerbRequest
{
    private static readonly string[] ImmutableKeys = {
        "protocol", "op", "v", "request_id", "request_seq", "runtime_instance_id", "session_id", "owner_id",
        "generation", "expected_player_id", "expected_screen_id", "attempt_id", "actor", "verb", "args", "pins"
    };

    public static bool TryParse(string raw, out MenuStepCommandRequest request, out CommandRejectReason reason)
    {
        request = default;
        reason = CommandRejectReason.MalformedJson;
        if (raw.Length > MenuStepCommandInbox.MaxRequestBytes) return false;
        try
        {
            using var document = JsonDocument.Parse(raw);
            var root = document.RootElement;
            var keys = ImmutableKeys.Concat(new[] { "control_seq", "deadline_unix_ms", "payload_sha256" }).ToHashSet();
            if (root.ValueKind != JsonValueKind.Object || root.EnumerateObject().Count() != keys.Count
                || root.EnumerateObject().Any(p => !keys.Remove(p.Name)) || keys.Count != 0) return false;
            string S(string key) => root.GetProperty(key).GetString() ?? "";
            long N(string key) => root.GetProperty(key).GetInt64();
            if (N("protocol") != 1 || S("op") != "verb" || S("v") != "arti.verb_request/1"
                || N("request_seq") < 1 || N("generation") < 1 || N("control_seq") < 1
                || N("expected_screen_id") < 0 || N("deadline_unix_ms") < 0
                || new[] { "request_id", "runtime_instance_id", "session_id", "owner_id", "attempt_id" }.Any(k => !IsId(S(k)))
                || S("actor") is not ("model" or "activity" or "safety" or "legacy")
                || S("verb") is not ("observe" or "move" or "use" or "select" or "interact" or "transfer" or "ui" or "run_controller")
                || root.GetProperty("args").ValueKind != JsonValueKind.Object
                || root.GetProperty("pins").ValueKind != JsonValueKind.Object) return false;
            if (!root.GetProperty("pins").TryGetProperty("row_key", out var rowKey)
                || rowKey.ValueKind != JsonValueKind.String
                || !ArtiCore.Pure.NativeVerbRules.ValidRowKey(rowKey.GetString()!)) return false;
            if (!ArtiCore.Pure.NativeSchemas.Valid("verb", System.Text.Json.Nodes.JsonNode.Parse(
                JsonSerializer.Serialize(new { verb = S("verb"), args = root.GetProperty("args"), pins = root.GetProperty("pins") }))!)) return false;
            var envelope = root.Clone();
            if (S("payload_sha256") != ImmutableDigest(envelope))
            { reason = CommandRejectReason.JournalIdentityMismatch; return false; }
            request = new MenuStepCommandRequest(S("request_id"), S("session_id"), N("expected_player_id"), null,
                "", 0, 0, N("deadline_unix_ms"), "verb", ExpectedScreenId: checked((int)N("expected_screen_id")),
                RuntimeInstanceId: S("runtime_instance_id"), OwnerId: S("owner_id"), Generation: N("generation"),
                ControlSeq: N("control_seq"), RequestSeq: N("request_seq"), VerbEnvelope: envelope);
            reason = CommandRejectReason.None;
            return true;
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException or KeyNotFoundException or FormatException or OverflowException)
        { return false; }
    }

    public static string ImmutableDigest(JsonElement envelope)
    {
        var text = new StringBuilder("{");
        foreach (string key in ImmutableKeys.OrderBy(k => k, StringComparer.Ordinal))
        { WriteString(text, key); text.Append(':'); WriteCanonical(text, envelope.GetProperty(key)); text.Append(','); }
        text.Length--; text.Append('}');
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text.ToString()))).ToLowerInvariant();
    }

    private static bool IsId(string value) => value.Length is >= 1 and <= 128
        && value.All(c => c is >= 'A' and <= 'Z' or >= 'a' and <= 'z' or >= '0' and <= '9' or '.' or '_' or ':' or '/' or '|' or '-');

    // C8 is Python json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False).
    // JSON encoders for HTML escape extra Unicode; numeric source spelling is not canonical.
    private static void WriteString(StringBuilder text, string value)
    {
        text.Append('"');
        foreach (char c in value)
            text.Append(c switch { '"' => "\\\"", '\\' => "\\\\", '\b' => "\\b", '\f' => "\\f", '\n' => "\\n", '\r' => "\\r", '\t' => "\\t",
                < ' ' => "\\u" + ((int)c).ToString("x4"), _ => c.ToString() });
        text.Append('"');
    }

    private static string CanonicalNumber(JsonElement value)
    {
        string raw = value.GetRawText();
        if (!raw.Contains('.') && !raw.Contains('e') && !raw.Contains('E'))
            return BigInteger.Parse(raw, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture);
        double number = value.GetDouble();
        if (!double.IsFinite(number)) throw new JsonException("Non-finite payload number");
        string text = number.ToString("R", CultureInfo.InvariantCulture).ToLowerInvariant();
        int e = text.IndexOf('e');
        if (e >= 0) return text[..e] + "e" + int.Parse(text[(e + 1)..], CultureInfo.InvariantCulture).ToString("+00;-00", CultureInfo.InvariantCulture);
        if (Math.Abs(number) >= 1e16)
        {
            string sign = number < 0 ? "-" : "", digits = text.TrimStart('-');
            string mantissa = digits.TrimEnd('0');
            if (mantissa.Length > 1) mantissa = mantissa.Insert(1, ".");
            return sign + mantissa + "e+" + (digits.Length - 1).ToString(CultureInfo.InvariantCulture);
        }
        return text.Contains('.') ? text : text + ".0";
    }

    internal static void WriteCanonical(StringBuilder text, JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Object)
        {
            text.Append('{');
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in value.EnumerateObject().OrderBy(p => p.Name, Comparer<string>.Create(CompareKeys)))
            {
                if (!names.Add(property.Name)) throw new JsonException("Duplicate payload key");
                WriteString(text, property.Name); text.Append(':'); WriteCanonical(text, property.Value); text.Append(',');
            }
            if (names.Count > 0) text.Length--; text.Append('}');
        }
        else if (value.ValueKind == JsonValueKind.Array)
        {
            text.Append('[');
            foreach (var item in value.EnumerateArray()) { WriteCanonical(text, item); text.Append(','); }
            if (value.GetArrayLength() > 0) text.Length--; text.Append(']');
        }
        else if (value.ValueKind == JsonValueKind.String) WriteString(text, value.GetString()!);
        else text.Append(value.ValueKind == JsonValueKind.Number ? CanonicalNumber(value) : value.GetRawText());
    }

    private static int CompareKeys(string left, string right)
    {
        var a = left.EnumerateRunes().Select(r => r.Value).ToArray();
        var b = right.EnumerateRunes().Select(r => r.Value).ToArray();
        for (int i = 0; i < Math.Min(a.Length, b.Length); i++)
            if (a[i] != b[i]) return a[i].CompareTo(b[i]);
        return a.Length.CompareTo(b.Length);
    }
}
