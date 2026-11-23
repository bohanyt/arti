using System.Text.Json;

namespace ArtiHostHelper;

internal static class SoloHostLaunch
{
    // Same two facts as scripts/livepath/halt_authority.py session_kind, from the helper's run.
    internal static bool Verified(string outputDirectory)
    {
        try
        {
            var run = Path.GetDirectoryName(Path.GetFullPath(outputDirectory));
            if (run is null || !File.Exists(Path.Combine(run, "runtime", "batch-host", "host-mods", "ArtiHostHelper", "config.json")))
                return false;
            using var trial = JsonDocument.Parse(HarnessFileIo.ReadShared(Path.Combine(run, "trial-config.json")));
            return trial.RootElement.ValueKind == JsonValueKind.Object
                && trial.RootElement.TryGetProperty("stardew_host_controller", out var controller)
                && controller.ValueKind == JsonValueKind.String && controller.GetString() == "harness";
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return false;
        }
    }
}
