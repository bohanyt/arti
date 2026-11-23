using System.Globalization;
using System.Numerics;
using System.Text.Json;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;

namespace ArtiHostHelper;

internal static class HostStop
{
    /// <summary>The first stop marker of this helper's folder or the run it belongs to.</summary>
    internal static string? StopMarker(string outputDirectory) => Scan(outputDirectory, beforeClick: false);

    static string? Scan(string outputDirectory, bool beforeClick)
    {
        try
        {
            if (!beforeClick && HostMarker(outputDirectory) is { } own) return own;
            string? run = Path.GetDirectoryName(Path.GetFullPath(outputDirectory));
            if (run is null) return "unreadable_run";
            if (!beforeClick && RunMarker(run) is { } marker) return marker;
            string cycles = Path.Combine(run, "runtime", "live7");
            if (Present(cycles) && CycleStop(cycles, beforeClick) is { } cycle) return cycle;
            // On the final check, stat the top-level markers after cycle discovery and the state read.
            return beforeClick ? HostMarker(outputDirectory) ?? RunMarker(run) : null;
        }
        catch { return "unreadable"; } // any doubt: inert
    }

    static readonly string[] RunMarkers = { "OPERATOR_STOP", "SUPERVISE_STOP", "LIVE7_STOP", "bridge-stop-request", "bridge-drained" };

    static string? HostMarker(string outputDirectory)
    {
        if (Present(Path.Combine(outputDirectory, "STOP"))) return "STOP";
        return Present(Path.Combine(outputDirectory, "HOST_STOP")) ? "HOST_STOP" : null;
    }

    static string? RunMarker(string run)
    {
        foreach (string marker in RunMarkers)
            if (Present(Path.Combine(run, marker))) return marker;
        return Present(Path.Combine(run, "runtime", "LIVE7_STOP")) ? "runtime/LIVE7_STOP" : null;
    }

    static string? DirectCycleMarker(string cycle)
    {
        // Only the confirmed operator-stop command writes OPERATOR_STOP. It is never a rotation or a recoverable end.
        if (Present(Path.Combine(cycle, "OPERATOR_STOP"))) return "cycle/OPERATOR_STOP";
        return Present(Path.Combine(cycle, "LIVE7_STOP")) ? "cycle/LIVE7_STOP" : null;
    }

    // The run/cycle guards in scripts/livepath/halt_authority.py, including the cycle's recovery caller's LIVE7_STOP.
    static string? CycleStop(string cycles, bool beforeClick)
    {
        string? newest = null;
        BigInteger newestId = -1;
        List<string>? finalCycles = beforeClick ? new() : null;
        foreach (string cycle in Directory.EnumerateDirectories(cycles, "c*"))
        {
            string name = Path.GetFileName(cycle);
            if (name[0] != 'c' || !BigInteger.TryParse(name.AsSpan(1), NumberStyles.None, CultureInfo.InvariantCulture, out var id))
                continue;
            if (finalCycles is not null) finalCycles.Add(cycle);
            else if (DirectCycleMarker(cycle) is { } marker) return marker;
            if (id > newestId)
            {
                newest = cycle;
                newestId = id;
            }
        }
        // The full scan retains the nested guard. The final check enumerates only numbered cycle folders.
        if (!beforeClick && Directory.EnumerateFiles(cycles, "LIVE7_STOP", SearchOption.AllDirectories).Any())
            return "cycle/LIVE7_STOP";
        string? published = newest is not null ? PublishedStopPath(newest) : null;
        if (finalCycles is not null)
            foreach (string cycle in finalCycles)
                if (DirectCycleMarker(cycle) is { } marker) return marker;
        return published is not null && (!Path.IsPathFullyQualified(published) || Present(published)) ? "cycle/state_stop" : null;
    }

    static string? PublishedStopPath(string cycle)
    {
        string state;
        try { state = HarnessFileIo.ReadShared(Path.Combine(cycle, "cycle_state.json")); }
        catch (FileNotFoundException) { return null; }
        catch (DirectoryNotFoundException) { return null; }
        using var doc = JsonDocument.Parse(state);
        if (doc.RootElement.ValueKind != JsonValueKind.Object
            || !doc.RootElement.TryGetProperty("live7_stop_file", out var value) || value.ValueKind != JsonValueKind.String)
            return null;
        return value.GetString();
    }

    // Like halt_authority.present, count any other stat failure as a stop through the caller's fail-closed catch.
    static bool Present(string path)
    {
        try { File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }

    // The caller has checked owner presence and sleep provenance. After the full scan and menu check, restat the direct
    // markers immediately before the native click. A refused click never spends this night's Yes.
    internal static bool TryClickSleep(string outputDirectory, DialogueBox menu, Point point, HelperSleep sleep,
        int day, out string? stop, Action? afterFirstScan = null)
    {
        stop = StopMarker(outputDirectory);
        if (stop is not null) return false;
        afterFirstScan?.Invoke();
        if (!ReferenceEquals(Game1.activeClickableMenu, menu) || !sleep.MayPressYes(day)) return false;
        stop = Scan(outputDirectory, beforeClick: true);
        if (stop is not null) return false;
        try { menu.receiveLeftClick(point.X, point.Y); }
        finally { sleep.TryPressYes(day); }
        return true;
    }
}
