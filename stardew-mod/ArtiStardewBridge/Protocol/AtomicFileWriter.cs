using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// D9 (live batch 2, 2026-10-07): session.json / result.json are written temp + rename, and
/// Python readers occasionally hold the target during the rename, so the move fails with
/// UnauthorizedAccessException / IOException. Retry the rename a bounded number of times
/// (6 attempts, at most 100 ms total backoff) and never throw into the game tick.
/// ADR 152: write the complete temp once, then retry only publication.
/// </summary>
public static partial class AtomicFileWriter
{
    static partial void BeforePublication(string target);

    public const int Attempts = 6;
    public const int RetryDelayMs = 20;

    public readonly record struct Result(bool Ok, int Tries, Exception? Error);

    public static Result TryWrite(string target, string content, Action<int>? sleep = null, bool flushToDisk = false,
        bool overwrite = true, bool retry = true)
    {
        string temp = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
        Exception? last = null;
        int tries = 0;
        bool tempReady = false;
        int limit = retry ? Attempts : 1;
        for (int attempt = 1; attempt <= limit; attempt++)
        {
            tries = attempt;
            try
            {
                BeforePublication(target);
                if (!tempReady)
                {
                    using var stream = new FileStream(temp, FileMode.Create, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
                    var bytes = System.Text.Encoding.UTF8.GetBytes(content); stream.Write(bytes); stream.Flush(flushToDisk);
                    tempReady = true;
                }
                if (!PosixReplace(temp, target, overwrite))
                    File.Move(temp, target, overwrite);
                return new Result(true, attempt, null);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                last = ex;
                if (attempt < limit)
                {
                    try { (sleep ?? Thread.Sleep)(RetryDelayMs); } catch { }
                }
            }
            catch (Exception ex)
            {
                last = ex;
                break;
            }
        }
        try
        {
            if (File.Exists(temp))
                File.Delete(temp);
        }
        catch
        {
            // A leftover temp file is harmless; the next write overwrites it.
        }
        return new Result(false, tries, last);
    }

    public static void Move(string source, string target, bool overwrite = false)
    {
        for (int attempt = 1; ; attempt++)
        {
            try {
                if (!PosixReplace(source, target, overwrite)) File.Move(source, target, overwrite);
                return;
            }
            catch (Exception ex) when (attempt < Attempts && (ex is UnauthorizedAccessException || ex is IOException io && (io.HResult & 0xffff) is 5 or 32 or 33))
            { Thread.Sleep(RetryDelayMs); }
        }
    }

    public static FileStream OpenRead(string path)
    {
        for (int attempt = 1; ; attempt++)
        {
            try { return new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); }
            catch (Exception ex) when (attempt < Attempts && (ex is UnauthorizedAccessException || ex is IOException io && (io.HResult & 0xffff) is 5 or 32 or 33))
            { Thread.Sleep(RetryDelayMs); }
        }
    }
    public static string ReadAllText(string path, System.Text.Encoding? encoding = null)
    {
        using var stream = OpenRead(path);
        using var reader = new StreamReader(stream, encoding ?? System.Text.Encoding.UTF8);
        return reader.ReadToEnd();
    }
    public static void Write(string path, string content, bool flushToDisk = false)
    {
        var result = TryWrite(path, content, flushToDisk: flushToDisk);
        if (!result.Ok) throw result.Error ?? new IOException("IPC publication failed");
    }
    public static void AppendAllText(string path, string content)
    {
        using var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
        using var writer = new StreamWriter(stream, new System.Text.UTF8Encoding(false));
        writer.Write(content);
    }

    /// <summary>Single-writer journal append at its last acknowledged boundary; retries discard only their own torn tail.</summary>
    internal static void AppendDurable(string path, long expectedLength, byte[] record, bool prefixNewline = false)
    {
        BeforePublication(path);
        using var stream = new FileStream(path, FileMode.OpenOrCreate, FileAccess.Write, FileShare.Read | FileShare.Delete);
        if (stream.Length < expectedLength) throw new IOException("Journal shortened outside its writer");
        if (stream.Length > expectedLength) stream.SetLength(expectedLength); // Only an unacknowledged/torn tail.
        stream.Position = expectedLength;
        if (prefixNewline) stream.WriteByte((byte)'\n');
        stream.Write(record);
        stream.Flush(flushToDisk: true);
    }

    // PM-1 fix6 #8 (attempt 6: session.json write denied x11): MoveFileEx cannot replace a file any reader holds. A
    // POSIX-semantics rename can, when the reader opened it with FILE_SHARE_DELETE (the Python reader does); the reader
    // keeps its consistent old snapshot (seen in a local repro log). Unsupported here -> the old move.
    static bool PosixReplace(string temp, string target, bool overwrite = true)
    {
        if (!OperatingSystem.IsWindows()) return false;
        using SafeFileHandle handle = CreateFileW(temp, 0x00010000 /* DELETE */, 7, IntPtr.Zero, 3, 0, IntPtr.Zero);
        if (handle.IsInvalid) return false;
        string name = Path.GetFullPath(target) + "\0";
        int nameAt = 2 * IntPtr.Size + 4, size = nameAt + 2 * name.Length; // FILE_RENAME_INFO: Flags, RootDirectory, FileNameLength, FileName
        IntPtr info = Marshal.AllocHGlobal(size);
        try {
            for (int i = 0; i < size; i++) Marshal.WriteByte(info, i, 0);
            Marshal.WriteInt32(info, 0, (overwrite ? 0x1 : 0) | 0x2); // REPLACE_IF_EXISTS | POSIX_SEMANTICS
            Marshal.WriteInt32(info, 2 * IntPtr.Size, 2 * (name.Length - 1));
            Marshal.Copy(name.ToCharArray(), 0, info + nameAt, name.Length);
            return SetFileInformationByHandle(handle, 22 /* FileRenameInfoEx */, info, size);
        } finally { Marshal.FreeHGlobal(info); }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, IntPtr info, int size);
}
