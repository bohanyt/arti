using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Lossless knowledge transport through the existing snapshot/delta store. No new socket or state store.
/// Chunk manifests are published on every frame; an incomplete/new hash cannot be mistaken for current knowledge.</summary>
public sealed class WorldKnowledgeWire
{
    public const int FrameBytes = 48 * 1024;
    const int PriorityBytes = 34 * 1024;
    int priorityCursor, eventCursor;
    static readonly HashSet<string> Priority = new(StringComparer.Ordinal)
        { "world_graph", "scope", "date", "game_facts", "captured_tick", "discovery_total" };

    sealed record Encoded(string Json, string Hash, KeyValuePair<string, object>[] Parts);
    readonly Dictionary<string, Encoded> cache = new(StringComparer.Ordinal);
    readonly HashSet<string> removals = new(StringComparer.Ordinal);
    public string[] Removes => this.removals.ToArray();
    public void AckRemoves(IEnumerable<string> sent) { foreach (var key in sent) this.removals.Remove(key); }

    Encoded Encode(string key, object value)
    {
        string json = JsonSerializer.Serialize(value, ProtocolJson.Options);
        if (this.cache.TryGetValue(key, out var cached) && cached.Json == json) return cached;
        if (cached is not null) foreach (var part in cached.Parts) this.removals.Add(part.Key);
        string hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json))).ToLowerInvariant();
        var parts = new List<KeyValuePair<string, object>>();
        if (Encoding.UTF8.GetByteCount(json) > 4096)
        {
            int part = 0;
            for (int offset = 0; offset < json.Length;)
            {
                int length = Math.Min(2048, json.Length-offset);
                if (offset+length < json.Length && char.IsHighSurrogate(json[offset+length-1])) length--;
                parts.Add(new("chunk:" + hash + ":" + part++, json.Substring(offset, length)));
                offset += length;
            }
        }
        return this.cache[key] = new(json, hash, parts.ToArray());
    }

    static int Size(Dictionary<string, object> facts) => JsonSerializer.SerializeToUtf8Bytes(facts, ProtocolJson.Options).Length;

    public Dictionary<string, object> Frame(IReadOnlyDictionary<string, object> facts)
    {
        var result = new Dictionary<string, object>(StringComparer.Ordinal);
        var priority = new List<KeyValuePair<string, object>>();
        var events = new List<KeyValuePair<string, object>>();
        foreach (var fact in facts.OrderBy(p => p.Key, StringComparer.Ordinal))
        {
            var encoded = this.Encode(fact.Key, fact.Value);
            bool important = Priority.Contains(fact.Key);
            var queue = important ? priority : events;
            object root = encoded.Parts.Length == 0 ? fact.Value : new Dictionary<string, object> {
                ["chunked"] = encoded.Hash, ["parts"] = encoded.Parts.Length };
            if (important) result[fact.Key] = root;
            else queue.Add(new(fact.Key, root));
            queue.AddRange(encoded.Parts);
        }
        foreach (string stale in this.cache.Keys.Except(facts.Keys).ToArray())
        {
            foreach (var part in this.cache[stale].Parts) this.removals.Add(part.Key);
            this.removals.Add(stale);
            this.cache.Remove(stale);
        }
        void Add(List<KeyValuePair<string, object>> queue, ref int cursor, int budget)
        {
            if (queue.Count == 0) { cursor = 0; return; }
            cursor %= queue.Count;
            int visited = 0;
            while (visited++ < queue.Count)
            {
                var part = queue[cursor];
                result[part.Key] = part.Value;
                if (Size(result) > budget)
                {
                    result.Remove(part.Key);
                    break;
                }
                cursor = (cursor+1) % queue.Count;
            }
        }
        Add(priority, ref this.priorityCursor, PriorityBytes);
        Add(events, ref this.eventCursor, FrameBytes);
        return result;
    }
}
