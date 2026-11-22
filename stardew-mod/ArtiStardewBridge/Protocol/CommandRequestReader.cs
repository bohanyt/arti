namespace ArtiStardewBridge.Protocol;

/// <summary>Claim an exact completed publication before reading it. The persistent
/// exclusive consume lock serializes readers; a failed unadmitted claim survives
/// retries/restarts. Retirement never deletes the public writer pathname.</summary>
public static class CommandRequestReader
{
    public const int Attempts = 4;

    internal static bool IsSharingViolation(IOException error)
    {
        int code = error.HResult & 0xffff;
        return code is 32 or 33 || OperatingSystem.IsLinux() && code == 11; // EAGAIN from exclusive flock
    }
    public static bool TryConsume(string path, out string raw, Action<int>? retryDelay = null)
    {
        raw = "";
        string claimed = path + ".claimed";
        for (int attempt = 0; attempt < Attempts; attempt++)
        {
            try
            {
                // Never remove this lock file: every reader must lock the same inode.
                using var owner = new FileStream(path + ".consume.lock", FileMode.OpenOrCreate,
                    FileAccess.ReadWrite, FileShare.None);
                // Recover an unadmitted claim first. Move is same-volume, atomic and
                // non-replacing. New writers may now publish B without affecting A.
                if (!File.Exists(claimed)) AtomicFileWriter.Move(path, claimed);
                string complete;
                using (var stream = AtomicFileWriter.OpenRead(claimed))
                using (var reader = new StreamReader(stream)) complete = reader.ReadToEnd();
                File.Delete(claimed);
                raw = complete; // only a fully read AND retired claim may be admitted
                return true;
            }
            catch (FileNotFoundException) { return false; }
            catch (DirectoryNotFoundException) { return false; }
            catch (IOException ex) when (IsSharingViolation(ex) && attempt + 1 < Attempts)
            { if (retryDelay is null) Thread.Sleep(5); else retryDelay(attempt); }
        }
        return false;
    }
}
