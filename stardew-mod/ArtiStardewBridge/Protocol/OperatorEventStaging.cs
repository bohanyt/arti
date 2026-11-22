using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Operator files only, outside the command inbox. No protocol/catalog entry.</summary>
public static class OperatorEventStaging
{
    public const string DirectoryName = "operator-event-staging";
    public const string FestivalDirectoryName = "operator-festival-staging";
    public const int MaxBytes = 512;
    public const int MaxLifetimeMs = 30_000;
    public sealed record Receipt(string? id, bool success, string reason, string consumed_file);

    public static bool VerbAllowed(string? verb) => verb is "question" or "control" or "festival-start";
    public static string? IdentityRefusal(bool ready, bool multiplayer, bool host, bool split) =>
        !ready || !multiplayer || host || split ? "farmhand_only" : null;

    // Gate OFF or ineligible identity must not even inspect a shared pending file. Called on the game thread.
    public static Receipt? Poll(string directory, bool enabled, bool ready, bool multiplayer, bool host, bool split,
        Func<string, string?> execute, long nowMs, bool festivalStart = false, bool festivalFreeRoam = false)
    {
        if (!enabled || (festivalStart ? !ready || !multiplayer || !host || split || !festivalFreeRoam
                : IdentityRefusal(ready, multiplayer, host, split) is not null))
            return null;
        string pending = Path.Combine(directory, "request.json");
        if (!File.Exists(pending))
            return null;
        string claimed = Path.Combine(directory, $"consumed-{Guid.NewGuid():N}.json");
        AtomicFileWriter.Move(pending, claimed); // no overwrite; consume BEFORE any native mutation
        string? id = null;
        string? refusal = null;
        string? verb = null;
        try
        {
            using var stream = AtomicFileWriter.OpenRead(claimed);
            byte[] bytes = new byte[MaxBytes + 1];
            int count = 0, read;
            while (count < bytes.Length && (read = stream.Read(bytes, count, bytes.Length - count)) > 0)
                count += read;
            if (count > MaxBytes)
                refusal = "request_too_large";
            else
            {
                using var doc = JsonDocument.Parse(bytes.AsMemory(0, count));
                var root = doc.RootElement;
                var fields = root.EnumerateObject().ToArray();
                if (fields.Length != 3 || fields.Select(p => p.Name).Distinct().Count() != 3
                    || fields.Any(p => p.Name is not ("id" or "verb" or "expires_at_ms")))
                    refusal = "invalid_schema";
                else
                {
                    string? candidate = root.GetProperty("id").GetString();
                    if (candidate is not { Length: 32 } || !candidate.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f'))
                        refusal = "invalid_id";
                    else
                    {
                        // Only validated IDs may survive a later type/parse error and become filenames.
                        id = candidate;
                        verb = root.GetProperty("verb").GetString();
                        if (!VerbAllowed(verb))
                            refusal = "unknown_verb";
                        else if ((verb == "festival-start") != festivalStart)
                            refusal = "wrong_staging_instance";
                        else if (!root.GetProperty("expires_at_ms").TryGetInt64(out long expiry)
                            || expiry <= nowMs || expiry - nowMs > MaxLifetimeMs)
                            refusal = "request_expired_or_invalid_deadline";
                    }
                }
            }
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or KeyNotFoundException or FormatException)
        { refusal = "invalid_request"; }

        bool replay = false;
        if (id is not null)
        {
            string marker = Path.Combine(directory, id + ".used");
            if (File.Exists(marker))
            { refusal = "request_replay"; replay = true; }
            else
            {
                // Persistent tombstone also prevents replay across restart or receipt-write failure.
                using var used = new FileStream(marker, FileMode.CreateNew, FileAccess.Write, FileShare.None);
                used.Flush(flushToDisk: true);
            }
        }
        if (refusal is null)
        {
            try { refusal = execute(verb!); }
            catch (Exception ex) { refusal = "staging_exception:" + ex.GetType().Name; }
        }
        var receipt = new Receipt(id, refusal is null, refusal ?? "event_started", Path.GetFileName(claimed));
        string json = JsonSerializer.Serialize(receipt);
        AtomicFileWriter.Write(claimed + ".receipt.json", json);
        if (id is not null && !replay)
        {
            string final = Path.Combine(directory, id + ".receipt.json");
            string temp = claimed + ".receipt.tmp";
            AtomicFileWriter.Write(temp, json);
            AtomicFileWriter.Move(temp, final); // publish complete correlated receipt, never overwrite an earlier result
        }
        return receipt;
    }
}
