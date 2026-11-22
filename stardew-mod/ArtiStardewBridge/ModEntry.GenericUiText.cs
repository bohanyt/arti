using System.Diagnostics;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using HarmonyLib;
using Microsoft.Xna.Framework.Graphics;
using StardewModdingAPI;
using StardewValley;
using StardewValley.BellsAndWhistles;
using StardewValley.Menus;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    bool genericTextStarted;
    void EnsureGenericUiRenderText()
    {
        if (this.genericTextStarted) return;
        this.genericTextStarted = true;
        try
        {
            GenericUiRenderedText.Enabled = () => this.config.UiPadMenusEnabled && Context.IsWorldReady
                && !Context.IsMainPlayer && !Context.IsSplitScreen;
            GenericUiRenderedText.Exact = () => this.config.GameTextDeliveryEnabled;
            var harmony = new Harmony(this.ModManifest.UniqueID + ".GenericUiText");
            foreach (MethodInfo method in typeof(SpriteBatch).GetMethods(BindingFlags.Public | BindingFlags.Instance)
                         .Where(m => m.Name == "DrawString" && m.GetParameters().Any(p => p.ParameterType == typeof(string)
                             || p.ParameterType == typeof(StringBuilder)))
                         .Concat(typeof(SpriteText).GetMethods(BindingFlags.Public | BindingFlags.Static)
                             .Where(m => m.Name == "drawString" && m.GetParameters().Any(p => p.ParameterType == typeof(string)))))
                harmony.Patch(method, prefix: new HarmonyMethod(typeof(GenericUiRenderedText), nameof(GenericUiRenderedText.Record)));
            // Native visible=true also appears on undrawn inherited controls and unused inventories.
            // Observe drawing, never draw manually or infer visibility from a menu type.
            foreach (Type type in new[] { typeof(ClickableTextureComponent), typeof(InventoryMenu) })
                foreach (MethodInfo method in type.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly)
                    .Where(m => m.Name == "draw"))
                    harmony.Patch(method, prefix: new HarmonyMethod(typeof(GenericUiRenderedText), nameof(GenericUiRenderedText.RecordControl)));
            // fix137A (ADR 237 A): a tooltip or hover box is drawn where the cursor is; its words describe no part.
            foreach (MethodInfo method in typeof(IClickableMenu).GetMethods(BindingFlags.Public | BindingFlags.Static))
                if (method.Name is "drawHoverText" or "drawToolTip")
                    harmony.Patch(method, prefix: new HarmonyMethod(typeof(GenericUiRenderedText), nameof(GenericUiRenderedText.EnterHover)),
                        finalizer: new HarmonyMethod(typeof(GenericUiRenderedText), nameof(GenericUiRenderedText.ExitHover)));
            this.Helper.Events.Display.RenderingActiveMenu += (_, _) => GenericUiRenderedText.Begin("menu");
            this.Helper.Events.Display.RenderedActiveMenu += (_, _) => GenericUiRenderedText.End();
            this.Helper.Events.Display.RenderingHud += (_, _) => GenericUiRenderedText.Begin("hud");
            this.Helper.Events.Display.RenderedHud += (_, _) => GenericUiRenderedText.End();
            this.Helper.Events.Display.RenderingWorld += (_, _) => {
                if (this.config.GameTextDeliveryEnabled && Game1.eventUp) GenericUiRenderedText.Begin("world");
            };
            this.Helper.Events.Display.RenderedWorld += (_, _) => {
                if (this.config.GameTextDeliveryEnabled) GenericUiRenderedText.EndWorld();
            };
        }
        catch (Exception exception)
        {
            this.Monitor.Log("Generic UI rendered text unavailable: " + exception.GetType().Name, LogLevel.Warn);
        }
    }
}

// Draw-time text, scoped to SMAPI's generic UI/HUD render boundaries; never a desktop or HOST capture.
internal static class GenericUiRenderedText
{
    internal static Func<bool> Enabled = () => false;
    internal static Func<bool> Exact = () => false;
    [ThreadStatic] static string? scope;
    [ThreadStatic] static List<string>? pending;
    [ThreadStatic] static List<PlacedText>? placed;
    [ThreadStatic] static int hoverDepth;
    static readonly GenericUiDrawVisibility MenuControls = new(), HudControls = new();
    static object? Surface => Game1.overlayMenu ?? (IClickableMenu?)Game1.emoteMenu ?? Game1.textEntry ?? Game1.activeClickableMenu;
    internal static bool? ControlVisible(object control) =>
        (Surface is null ? HudControls : MenuControls).Visible(Surface, control, Environment.TickCount64);
    sealed record Frame(int Receiver, long At, string[] Lines, PlacedText[] Placed);
    static readonly Dictionary<string, Frame> Frames = new();
    internal static void Begin(string kind)
    {
        scope = Enabled() ? kind : null;
        pending = scope is null ? null : new List<string>();
        placed = scope is null ? null : new List<PlacedText>();
        hoverDepth = 0;
        if (scope is "menu" or "hud") (scope == "menu" ? MenuControls : HudControls).Begin(scope == "menu" ? Surface : null, Environment.TickCount64);
    }
    public static void RecordControl(object __instance)
    {
        if (scope is not null && Enabled()) (scope == "menu" ? MenuControls : HudControls).Record(__instance);
    }
    public static void EnterHover() => hoverDepth++;
    public static void ExitHover() => hoverDepth = Math.Max(0, hoverDepth - 1);
    public static void Record(object[] __args)
    {
        if (scope is null || pending is null || !Enabled()) return;
        int at = Array.FindIndex(__args, arg => arg is string s && !string.IsNullOrWhiteSpace(s)
            || arg is StringBuilder b && b.Length > 0 && !string.IsNullOrWhiteSpace(b.ToString()));
        if (at < 0) return;
        string text = __args[at] is StringBuilder builder ? builder.ToString() : (string)__args[at];
        if (pending.Count < 256 && !pending.Contains(text)) pending.Add(Exact() || text.Length <= 4096 ? text : text[..4096]);
        // fix137A (ADR 237 A): where the menu's own draw put it (SpriteBatch.DrawString: its position; SpriteText.drawString:
        // x, y), so a part with no words of its own can be described by the words drawn on it. Never a tooltip.
        if (scope != "menu" || placed is null || placed.Count >= 256 || hoverDepth > 0) return;
        for (int i = at + 1; i < __args.Length; i++)
        {
            if (__args[i] is Microsoft.Xna.Framework.Vector2 position)
            { placed.Add(new PlacedText(text.Length <= 240 ? text : text[..240], (int)position.X, (int)position.Y)); return; }
            if (__args[i] is int x && i + 1 < __args.Length && __args[i + 1] is int y)
            { placed.Add(new PlacedText(text.Length <= 240 ? text : text[..240], x, y)); return; }
        }
    }
    internal static void End()
    {
        if (scope is "menu" or "hud") (scope == "menu" ? MenuControls : HudControls).End();
        if (scope is not null && pending is not null)
            Frames[scope] = new Frame(scope == "world" ? Game1.CurrentEvent is { } scene ? RuntimeHelpers.GetHashCode(scene) : 0
                : Game1.activeClickableMenu is { } menu ? RuntimeHelpers.GetHashCode(menu) : 0,
                Stopwatch.GetTimestamp(), pending.ToArray(), placed?.ToArray() ?? Array.Empty<PlacedText>());
        scope = null;
        pending = null;
        placed = null;
    }
    internal static void EndWorld() { if (scope == "world") End(); }
    /// <summary>fix137A (ADR 237 A): the menu's drawn strings with their positions, same freshness as <see cref="Current"/>.</summary>
    internal static PlacedText[] Placed(object? receiver)
    {
        int identity = receiver is null ? 0 : RuntimeHelpers.GetHashCode(receiver);
        if (!Frames.TryGetValue("menu", out Frame? frame) || frame.Receiver != identity
                || (Stopwatch.GetTimestamp() - frame.At) / (double)Stopwatch.Frequency > 2) return Array.Empty<PlacedText>();
        return frame.Placed;
    }
    internal static string[] Current(string kind, object? receiver, bool exact = false)
    {
        int identity = receiver is null ? 0 : RuntimeHelpers.GetHashCode(receiver);
        if (!Frames.TryGetValue(kind, out Frame? frame) || frame.Receiver != identity
                || (Stopwatch.GetTimestamp() - frame.At) / (double)Stopwatch.Frequency > 2) return Array.Empty<string>();
        if (exact) return frame.Lines;
        var lines = new List<string>();
        int left = 4096;
        foreach (string line in frame.Lines)
        {
            if (left <= 0) break;
            lines.Add(line.Length <= left ? line : line[..left]);
            left -= line.Length;
        }
        return lines.ToArray();
    }
}
