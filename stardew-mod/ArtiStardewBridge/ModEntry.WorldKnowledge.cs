using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using System.Text.Json;
using StardewValley;
using StardewModdingAPI.Events;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    void OnKnowledgeAssetsInvalidated(object? sender, AssetsInvalidatedEventArgs e) =>
        this.screens.Value.WorldKnowledge?.InvalidateMaps();
    long lastWorldDiscoveryFault;
    void WorldDiscoveryFault(Exception ex)
    {
        long now = Environment.TickCount64;
        if (now-this.lastWorldDiscoveryFault < 5000) return;
        this.lastWorldDiscoveryFault = now;
        this.Monitor.Log("discovery: unknown; capture " + ex.GetType().Name, LogLevel.Warn);
    }
    void ObserveWorldDiscoveries(ScreenState screen)
    {
        if (Context.IsMainPlayer || Context.IsSplitScreen) return;
        screen.WorldKnowledge ??= new NativeWorldKnowledge(this.Helper);
        try { screen.WorldKnowledge.ObserveTransient(); }
        catch (Exception ex) { this.WorldDiscoveryFault(ex); }
    }

    void OnDiscoveryInventoryChanged(object? sender, InventoryChangedEventArgs e)
    {
        if (!this.config.Enabled || !Context.IsWorldReady || !e.IsLocalPlayer || Context.IsMainPlayer || Context.IsSplitScreen) return;
        var screen = this.screens.Value;
        screen.WorldKnowledge ??= new NativeWorldKnowledge(this.Helper);
        try { screen.WorldKnowledge.Obtained(e.Added.Concat(e.QuantityChanged
            .Where(c=>c.NewSize>c.OldSize).Select(c=>c.Item))); }
        catch (Exception ex) { this.WorldDiscoveryFault(ex); }
    }

    ObservationPayload? CaptureWorldKnowledge(ScreenState screen, uint ticks)
    {
        if (!screen.Knowledge.HasSession || !Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen)
            return null;
        if (screen.KnowledgeTicks == 0 || ticks - screen.KnowledgeTicks >= 60)
        {
            screen.KnowledgeTicks = ticks;
            screen.WorldKnowledge ??= new NativeWorldKnowledge(this.Helper);
            try
            {
                var facts = screen.WorldKnowledge.Capture();
                facts["captured_tick"] = ticks;
                screen.Knowledge.Advance(screen.KnowledgeWire.Frame(facts));
            }
            catch (Exception ex)
            {
                // Unidentified data stays visible; never stale permission from the last good capture.
                screen.Knowledge.Advance(new Dictionary<string, object> { ["world_graph"] = new {
                    current = SceneIdentity.Token(Game1.currentLocation.NameOrUniqueName),
                    complete = false, places = Array.Empty<object>(), connections = Array.Empty<object>(),
                    unknown = new[] { "discovery: unknown; world capture " + ex.GetType().Name } },
                    ["captured_tick"] = ticks,
                    ["scope"] = $"{Game1.uniqueIDForThisGame}:{Game1.player.UniqueMultiplayerID}",
                    ["discovery:capture-unknown"] = new WorldDiscovery { Id="capture-unknown:"+ex.GetType().Name,
                        Date=$"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}",
                        Text="discovery: unknown; world capture "+ex.GetType().Name } });
            }
        }
        var payload = screen.Knowledge.ConsumeDirtySnapshot();
        if (payload is null) return null;
        payload = payload with { Product = ObservationProduct.Knowledge, Domain = ObservationDomain.KnowledgeWorld };
        if (screen.KnowledgeSnapshotSent)
            payload = payload with { Kind = ObservationKind.Delta, Body = new() {
                Upserts = payload.Body!.Facts, Removes = screen.KnowledgeWire.Removes.Take(64).ToArray() } };
        // Receiver lines are 64 KiB. Oversize is an explicit unavailable catalog, never a truncated whole-map claim.
        if (JsonSerializer.SerializeToUtf8Bytes(payload, ProtocolJson.Options).Length > 60 * 1024)
            payload = payload with { Available = false, Authoritative = false, Kind = ObservationKind.Error,
                Body = null, Truncation = null, Error = new() { Reason = "world_graph_oversize", Detail = "knowledge frame exceeded wire guard" } };
        return payload;
    }
}
