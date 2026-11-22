using System.Text.Json;

namespace ArtiCore.Pure;

public static class JsonEquality
{
    // Match STJ 8 JsonNode.DeepEquals for parsed pins using only the game STJ 6 API.
    // Object order is irrelevant; array order and numeric spelling are significant.
    public static bool Equal(JsonElement left, JsonElement right)
    {
        if (left.ValueKind != right.ValueKind) return false;
        switch (left.ValueKind)
        {
            case JsonValueKind.Object:
                var properties = left.EnumerateObject().ToArray();
                return properties.Length == right.EnumerateObject().Count()
                    && properties.All(p => right.TryGetProperty(p.Name, out var value) && Equal(p.Value, value));
            case JsonValueKind.Array:
                return left.GetArrayLength() == right.GetArrayLength()
                    && left.EnumerateArray().Zip(right.EnumerateArray()).All(p => Equal(p.First, p.Second));
            case JsonValueKind.String: return left.GetString() == right.GetString();
            case JsonValueKind.Number: return left.GetRawText() == right.GetRawText();
            default: return true;
        }
    }
}
