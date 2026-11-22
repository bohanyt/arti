using ArtiStardewBridge.Protocol;
namespace ArtiHostHelper;

/// <summary>Bounded sharing-contention retries for harness files, never gameplay operations.</summary>
internal static class HarnessFileIo
{
    internal static readonly int[] RetryDelaysMs = { 10, 25, 50, 100, 200 };

    internal static bool IsTransient(Exception ex) =>
        (ex is IOException || ex is UnauthorizedAccessException)
        && (ex.HResult & 0xffff) is 5 or 32 or 33; // access denied, sharing/lock violation

    internal static void Retry(string operation, string path, Action action, Action<int>? delay = null)
    {
        for (int attempt = 0; ; attempt++)
        {
            try { action(); return; }
            catch (Exception ex) when (IsTransient(ex) && attempt < RetryDelaysMs.Length)
            {
                (delay ?? (ms => Thread.Sleep(ms)))(RetryDelaysMs[attempt]);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                throw new IOException($"HOST file I/O failed: operation={operation} path={path} attempts={attempt + 1}", ex);
            }
        }
    }

    /// <summary>ADR 242: the HOST helper alone consumes a harness token file (STARTUP_RELEASE). The harness
    /// creates it once and never rewrites it. Its writer briefly keeps the new name open with DELETE access
    /// (POSIX rename, then the temp name's unlink); a read that meets that handle, or a missing file, is
    /// "not released yet" for this tick, never a HOST stop. Any other I/O failure still throws.</summary>
    internal static bool TryReadToken(string path, string expected, Func<string, string>? read = null)
    {
        if (!File.Exists(path)) return false;
        try { return (read ?? ReadShared)(path).Trim() == expected; }
        catch (Exception ex) when (IsTransient(ex) || ex is FileNotFoundException || ex is DirectoryNotFoundException)
        { return false; }
    }

    /// <summary>Read without excluding a writer's rename/delete handle (FileShare.Read alone does).</summary>
    internal static string ReadShared(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete);
        using var reader = new StreamReader(stream);
        return reader.ReadToEnd();
    }

    internal static void AtomicWrite(string path, string contents, Action<int>? delay = null)
    {
        var result = AtomicFileWriter.TryWrite(path, contents, delay);
        if (!result.Ok)
            throw new IOException($"HOST file I/O failed: operation=replace path={path} attempts={result.Tries}", result.Error);
    }

    internal static void Append(string path, string contents)
    {
        // A partial append is never replayed.
        try { AtomicFileWriter.AppendAllText(path, contents); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { throw new IOException($"HOST file I/O failed: operation=append path={path}", ex); }
    }
}
