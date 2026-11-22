using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using ArtiStardewBridge.RenderTiming;
using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;

namespace ArtiS3Observer;

public sealed class ObserverConfig
{
    public bool Enabled { get; set; } = false;
    public string ExpectedBridgeSha256 { get; set; } = "";
    public string ExpectedPlayerId { get; set; } = "";
}

public sealed class ModEntry : Mod
{
    private static ModEntry? instance;
    private readonly object sync = new();
    private readonly List<object> records = new(12000);
    private ObserverConfig config = new();
    private GameRunner? runner;
    private IntPtr hwnd;
    private long playerId, started, updates, draws, presents;
    private uint marker;
    private ushort nonce;
    private bool running, attempted;
    private string? output;
    private Timer? deadline;
    private Harmony? harmony;

    public override void Entry(IModHelper helper)
    {
        this.config = helper.ReadConfig<ObserverConfig>();
        if (!this.config.Enabled) return;
        instance = this;
        helper.ConsoleCommands.Add("arti_s3_measure", "One inactive 30-second sample: arti_s3_measure <nonce 1..65535>", this.Start);
        helper.ConsoleCommands.Add("arti_s3_measure_stop", "Stop this observer; no retry in this process.", (_, _) => this.Stop("operator_stop"));
        helper.Events.GameLoop.UpdateTicked += this.Update;
        helper.Events.Display.RenderingHud += this.Paint;
        helper.Events.GameLoop.ReturnedToTitle += (_, _) => this.Stop("returned_to_title");
    }

    private static string Hash(Assembly assembly) => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(assembly.Location)));

    private void Start(string command, string[] args)
    {
        lock (this.sync)
        {
            if (this.attempted) { this.Monitor.Log("S3 observer permits one attempt per process.", LogLevel.Warn); return; }
            this.attempted = true;
            try
            {
                if (args.Length != 1 || !ushort.TryParse(args[0], out this.nonce) || this.nonce == 0)
                    throw new InvalidOperationException("nonce must be 1..65535");
                var bridge = AppDomain.CurrentDomain.GetAssemblies().Single(a => a.GetName().Name == "ArtiStardewBridge");
                if (this.config.ExpectedBridgeSha256.Length != 64 || !Hash(bridge).Equals(this.config.ExpectedBridgeSha256, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("bridge hash mismatch/unpinned");
                if (Hash(typeof(Game).Assembly) != "92E5423A5D002B399DE4369E483577007274C5634745F5414FD508981B7494DE"
                    || Hash(typeof(Game1).Assembly) != "7F1E5B8E58D2758B78570BA771BBEB03D33522F62188BF6C32EDF0CF626DEAEE"
                    || Hash(typeof(Mod).Assembly) != "5E4C51BB3CD6616F5ADCB804769DBB7C6872B2478FAB786FB4CA00C72C7456BF")
                    throw new InvalidOperationException("installed stack mismatch; re-inspect before instrumentation");
                if (!long.TryParse(this.config.ExpectedPlayerId, out this.playerId))
                    throw new InvalidOperationException("player identity unpinned");
                this.runner = GameRunner.instance;
                if (!this.ReadIdentity(out this.hwnd, out _, out string error)) throw new InvalidOperationException(error);
                using var process = Process.GetCurrentProcess();
                string exe = process.MainModule?.FileName ?? throw new InvalidOperationException("process path unavailable");
                if (!Path.GetFileName(exe).Equals("StardewModdingAPI.exe", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("unexpected process executable");
                string directory = Path.Combine(this.Helper.DirectoryPath, "evidence");
                Directory.CreateDirectory(directory);
                this.output = Path.Combine(directory, $"s3-{DateTime.UtcNow:yyyyMMddTHHmmssfffZ}-{this.nonce}.jsonl");
                this.harmony = new Harmony(this.ModManifest.UniqueID);
                MethodInfo draw = typeof(GameRunner).GetMethod("Draw", BindingFlags.Instance | BindingFlags.NonPublic)!;
                this.harmony.Patch(draw, finalizer: new HarmonyMethod(typeof(ModEntry), nameof(AfterDraw)));
                MethodInfo endDraw = typeof(Game).GetMethod("EndDraw", BindingFlags.Instance | BindingFlags.NonPublic)!;
                this.harmony.Patch(endDraw, finalizer: new HarmonyMethod(typeof(ModEntry), nameof(AfterPresent)));
                this.started = Stopwatch.GetTimestamp();
                this.records.Add(new { kind = "identity", qpc = this.started, frequency = Stopwatch.Frequency,
                    utc = DateTime.UtcNow, nonce = this.nonce, pid = Environment.ProcessId,
                    processStartUtc = process.StartTime.ToUniversalTime(), executable = exe, hwnd = this.hwnd.ToInt64(),
                    sdlWindow = this.runner!.Window.Handle.ToInt64(), playerId = this.playerId,
                    bridgeSha256 = Hash(bridge), observerSha256 = Hash(typeof(ModEntry).Assembly),
                    gameSha256 = Hash(typeof(Game1).Assembly), monoGameSha256 = Hash(typeof(Game).Assembly),
                    smapiSha256 = Hash(typeof(Mod).Assembly),
                    inactiveSleepMs = this.runner.InactiveSleepTime.TotalMilliseconds,
                    fixedStep = this.runner.IsFixedTimeStep, targetMs = this.runner.TargetElapsedTime.TotalMilliseconds,
                    width = Game1.graphics.GraphicsDevice.PresentationParameters.BackBufferWidth,
                    height = Game1.graphics.GraphicsDevice.PresentationParameters.BackBufferHeight,
                    markerX = 16, markerY = 16, markerCell = 8, markerSpace = "RenderingHud SpriteBatch coordinates",
                    musicVolume = Game1.options.musicVolumeLevel, soundVolume = Game1.options.soundVolumeLevel,
                    ambientVolume = Game1.options.ambientVolumeLevel });
                this.running = true;
                this.deadline = new Timer(_ => this.Stop("wall_deadline"), null, TimeSpan.FromSeconds(30), Timeout.InfiniteTimeSpan);
                this.Monitor.Log($"S3 recording one 30-second sample to {this.output}; no input sent.", LogLevel.Info);
            }
            catch (Exception ex)
            {
                this.harmony?.UnpatchAll(this.ModManifest.UniqueID);
                this.Monitor.Log("S3 PRECONDITION_UNAVAILABLE: " + ex.Message, LogLevel.Error);
            }
        }
    }

    private bool ReadIdentity(out IntPtr window, out IntPtr foreground, out string error)
    {
        window = foreground = IntPtr.Zero; error = "inactive farmhand/window identity unavailable";
        if (this.runner is null || !ReferenceEquals(this.runner, GameRunner.instance)
            || !Context.IsWorldReady || !Context.IsMultiplayer || Context.IsMainPlayer || Context.IsSplitScreen
            || this.runner.gameInstances.Count != 1 || Game1.player.UniqueMultiplayerID != this.playerId
            || this.runner.IsActive || Game1.graphics.IsFullScreen || Game1.options.fullscreen
            || Game1.options.windowedBorderlessFullscreen
            || this.runner.Window.GetType().FullName != "Microsoft.Xna.Framework.SdlGameWindow"
            || !SdlWindowBinding.TryGetVisibleWindow(this.runner.Window.Handle, out window)) return false;
        foreground = SdlWindowBinding.GetForegroundWindow();
        if (foreground == IntPtr.Zero || foreground == window || (this.hwnd != IntPtr.Zero && this.hwnd != window)) return false;
        return true;
    }

    private void Update(object? sender, UpdateTickedEventArgs args) => this.Sample("update");

    private static Exception? AfterDraw(GameRunner __instance, Exception? __exception, bool __runOriginal)
    {
        if (instance is { } self && ReferenceEquals(__instance, self.runner))
        {
            if (__exception is null && __runOriginal) self.Sample("draw");
            else self.Stop("draw_failed_or_skipped");
        }
        return __exception;
    }

    // Diagnostic-only finalizer. Preserve the exception; a failed/skipped EndDraw
    // is never counted as a successful present return. No broad behavior patch.
    private static Exception? AfterPresent(Game __instance, Exception? __exception, bool __runOriginal)
    {
        if (instance is { } self && ReferenceEquals(__instance, self.runner))
        {
            if (__exception is null && __runOriginal) self.Sample("present");
            else self.Stop("present_failed_or_skipped");
        }
        return __exception;
    }

    private void Sample(string kind)
    {
        lock (this.sync)
        {
            if (!this.running) return;
            try
            {
                long now = Stopwatch.GetTimestamp();
                if (now - this.started >= 30 * Stopwatch.Frequency) { this.Stop("wall_deadline"); return; }
                if (this.records.Count >= 20000) { this.Stop("buffer_limit"); return; }
                if (!this.ReadIdentity(out var window, out var foreground, out string error)) { this.Stop(error); return; }
                if (kind == "update") this.updates++;
                if (kind == "draw") this.draws++;
                if (kind == "present") this.presents++;
                this.records.Add(new { kind, qpc = now, marker = this.marker, updates = this.updates, draws = this.draws,
                    presents = this.presents, worldTicks = Game1.ticks, hwnd = window.ToInt64(), foreground = foreground.ToInt64(),
                    active = this.runner!.IsActive, minimized = SdlWindowBinding.IsIconic(window),
                    inactiveSleepMs = this.runner.InactiveSleepTime.TotalMilliseconds });
            }
            catch (Exception ex) { this.Stop("observation_error:" + ex.GetType().Name); }
        }
    }

    private void Paint(object? sender, RenderingHudEventArgs args)
    {
        lock (this.sync)
        {
            if (!this.running) return;
            try
            {
                if (!this.ReadIdentity(out _, out _, out string error)) { this.Stop(error); return; }
                ulong bits = MarkerCodec.Encode(this.nonce, ++this.marker);
                args.SpriteBatch.Draw(Game1.staminaRect, new Rectangle(12, 12, 72, 72), Color.Magenta);
                for (int cell = 0; cell < 64; cell++)
                    args.SpriteBatch.Draw(Game1.staminaRect, new Rectangle(16 + cell % 8 * 8, 16 + cell / 8 * 8, 8, 8),
                        ((bits >> (63 - cell)) & 1) == 1 ? Color.White : Color.Black);
            }
            catch (Exception ex) { this.Stop("marker_error:" + ex.GetType().Name); }
        }
    }

    private void Stop(string reason)
    {
        lock (this.sync)
        {
            if (!this.running) return;
            this.running = false;
            this.deadline?.Dispose();
            this.records.Add(new { kind = "stop", qpc = Stopwatch.GetTimestamp(), reason,
                updates = this.updates, draws = this.draws, presents = this.presents, marker = this.marker });
            try
            {
                string report = string.Join(Environment.NewLine, this.records.Select(record => JsonSerializer.Serialize(record))) + Environment.NewLine;
                var written = ArtiStardewBridge.Protocol.AtomicFileWriter.TryWrite(this.output!, report, overwrite: false);
                if (!written.Ok) throw written.Error ?? new IOException("S3 report publication failed");
            }
            catch (Exception ex) { this.Monitor.Log("FAULT S3 witness write FAILED: " + ex.GetType().Name, LogLevel.Error); }
            this.Monitor.Log("S3 observer stopped: " + reason + "; counts are not a live acceptance verdict.", LogLevel.Info);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            this.Stop("disposed");
            this.harmony?.UnpatchAll(this.ModManifest.UniqueID);
            instance = null;
        }
        base.Dispose(disposing);
    }
}
