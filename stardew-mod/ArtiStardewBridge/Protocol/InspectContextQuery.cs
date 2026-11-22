using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// G2 read-only <c>inspect_context</c>: satu snapshot konteks native
/// terikat identitas, tanpa mutasi UI/input. Bukan store kedua; vocabulary
/// token selaras dengan kontrak G3 <c>ui.context</c> mendatang.
/// </summary>
public static class InspectContextQuery
{
    public const string Op = "inspect_context";
    public const string OutcomeInspected = "ContextInspected";
    public const int MaxLocationIdLength = 128;
    public const int MaxResultBytes = 64 * 1024;
    public const long InitialViewportTransformRevision = 1;

    static readonly string[] NotImplementedFamilies =
    {
        "ui_pointer",
        "ui_hover",
        "ui_scroll",
        "ui_drag",
        "text_edit",
        "release_all",
        "world_pointer",
        "skill_start",
    };

    public static Dictionary<string, object> BuildSnapshot(
        string runtimeInstanceId,
        long contextGeneration,
        int screenId,
        long uiRevision,
        long viewportTransformRevision,
        string locationId,
        string sessionId,
        long playerId,
        bool isPlayerFree,
        bool canPlayerMove,
        MenuInfo? menu,
        string? menuInstanceToken,
        bool worldPressEnabled,
        bool menuStepEnabled,
        bool worldReady,
        bool hasTextInputSubscriber,
        string? textInputToken = null,
        bool useToolEnabled = false
    )
    {
        bool menuActive = menu is not null;
        Dictionary<string, object> snapshot = new(StringComparer.Ordinal)
        {
            ["runtime_instance_id"] = runtimeInstanceId,
            ["context_generation"] = contextGeneration,
            ["session_id"] = sessionId,
            ["player_id"] = playerId,
            ["screen_id"] = screenId,
            ["ui_revision"] = uiRevision,
            ["viewport_transform_revision"] = viewportTransformRevision,
            ["location_id"] = locationId,
            ["mode"] = menuActive ? "menu" : "gameplay",
            ["is_player_free"] = isPlayerFree,
            ["can_player_move"] = canPlayerMove,
            ["capabilities"] = BuildCapabilities(
                worldPressEnabled,
                menuStepEnabled,
                worldReady,
                isPlayerFree,
                canPlayerMove,
                menu,
                hasTextInputSubscriber, useToolEnabled),
        };

        if (menuActive && menuInstanceToken is { Length: > 0 })
        {
            Dictionary<string, object> interactive = new(StringComparer.Ordinal)
            {
                ["receiver_token"] = ReceiverToken(menuInstanceToken)!,
                ["page_token"] = Token("pg", menuInstanceToken, menu!.PageType ?? menu.MenuType),
                ["menu_type"] = menu.MenuType,
            };
            if (menu.PageType is { Length: > 0 })
                interactive["page_type"] = menu.PageType;
            if (menu.SnapId is int snapId)
                interactive["snap_id"] = snapId;
            if (menu.FocusKey is { Length: > 0 })
            {
                interactive["focus_key"] = menu.FocusKey;
                interactive["focus_target_token"] = Token("tgt", menuInstanceToken, menu.FocusKey);
            }
            if (menu.VisibleText is { Length: > 0 })
                interactive["visible_text"] = menu.VisibleText;
            if (menu.Speaker is { Length: > 0 })
                interactive["speaker"] = menu.Speaker;
            if (textInputToken is { Length: > 0 })
                interactive["text_input_token"] = Token("txt", textInputToken);
            snapshot["interactive"] = interactive;
        }

        return snapshot;
    }

    public static Dictionary<string, object> BuildCapabilities(
        bool worldPressEnabled,
        bool menuStepEnabled,
        bool worldReady,
        bool isPlayerFree,
        bool canPlayerMove,
        MenuInfo? menu,
        bool hasTextInputSubscriber,
        bool useToolEnabled = false
    )
    {
        Dictionary<string, object> caps = new(StringComparer.Ordinal)
        {
            ["inspect_tiles"] = Available(),
            ["inspect_context"] = Available(),
            ["world_key"] = worldPressEnabled && worldReady && isPlayerFree && canPlayerMove
                && menu is null && !hasTextInputSubscriber
                ? AvailableMoveCardinal()
                : Unavailable(
                    worldPressEnabled ? "preconditions_unmet" : "backend_disabled"),
            ["ui_navigate"] = menuStepEnabled && menu is not null && menu.SnapId is not null
                ? Available()
                : Unavailable(
                    menuStepEnabled ? "no_menu_snap" : "backend_disabled"),
        };

        foreach (string family in NotImplementedFamilies)
            caps[family] = Unavailable("not_implemented");

        if (useToolEnabled && worldReady && isPlayerFree && canPlayerMove
            && menu is null && !hasTextInputSubscriber)
        {
            List<string> primitives = new();
            if (worldPressEnabled) primitives.Add("move_cardinal");
            primitives.Add("use_tool");
            caps["world_key"] = new Dictionary<string, object> {
                ["available"] = true, ["primitives"] = primitives.ToArray() };
        }

        // Source support and current admission are separate. Generic input_once
        // is never available; each actual primitive retains its independent gate.
        caps["input_once"]=new Dictionary<string,object> {
            ["implemented"]=true,["available"]=false,["reason"]="primitive_specific_admission",
            ["supported_primitives"]=new[]{"move_cardinal","use_tool","select_item","world_interact","container_transfer","container_exit","ui_key"} };
        // WS2: source support is explicit; current availability is set per receiver by EnrichUiKeyContext.
        caps["ui_key"]=new Dictionary<string,object>{["implemented"]=true,["available"]=false,
            ["reason"]="current_receiver_unavailable"};
        foreach(string family in new[]{"select_item","world_interact","container_transfer","container_exit"})
            caps[family]=new Dictionary<string,object>{["implemented"]=true,["available"]=false,["reason"]="current_backend_or_target_unavailable"};
        ((Dictionary<string,object>)caps["ui_navigate"])["backend"]="legacy_snap_probe";
        if (hasTextInputSubscriber)
            caps["text_edit"] = Unavailable("subscriber_present_backend_not_implemented");

        return caps;
    }

    /// <summary>
    /// fix89D / ADR 158: advertise the segment-guarded move_cardinal hold only while a move would take the
    /// background path (the only path with the native position guard). The focused foreground press has no
    /// guard, so Python then keeps the bounded 4-tile / 60-frame holds.
    /// </summary>
    public static void AddMoveSegment(Dictionary<string, object> snapshot, bool backgroundMovePathLive)
    {
        if (!backgroundMovePathLive || snapshot.GetValueOrDefault("capabilities") is not Dictionary<string, object> caps
            || caps.GetValueOrDefault("input_once") is not Dictionary<string, object> inputOnce)
            return;
        inputOnce["move_segment"] = new Dictionary<string, object>
        {
            ["version"] = 1, ["max_ticks"] = MoveSegmentGuard.MaxTicks, ["max_tiles"] = MoveSegmentGuard.MaxTiles,
        };
    }

    /// <summary>
    /// fix92A / ADR 162: advertise the guarded path (segment_stop + path_next) only while a move takes the
    /// background PAD path: a turn is a one-frame release of one D-pad pin and the single Press of the next,
    /// which only the pad VERIFY path proves. Otherwise Python keeps fix89D's one-segment holds.
    /// </summary>
    public static void AddMovePath(Dictionary<string, object> snapshot, bool backgroundPadMovePathLive)
    {
        if (!backgroundPadMovePathLive || snapshot.GetValueOrDefault("capabilities") is not Dictionary<string, object> caps
            || caps.GetValueOrDefault("input_once") is not Dictionary<string, object> inputOnce
            || !inputOnce.ContainsKey("move_segment"))
            return;
        inputOnce["move_path"] = new Dictionary<string, object>
        {
            ["version"] = 1, ["max_ticks"] = MovePath.MaxTicks, ["max_tiles"] = MovePath.MaxTiles,
            ["max_segments"] = MovePath.MaxSegments, ["max_segment_tiles"] = MoveSegmentGuard.MaxTiles,
        };
    }

    public static bool ValidateSnapshotSize(Dictionary<string, object> snapshot)
    {
        string json = JsonSerializer.Serialize(snapshot, ProtocolJson.Options);
        return SceneWire.Utf8Bytes(json) <= MaxResultBytes;
    }

    /// <summary>
    /// Envelope + blob harus satu binding. Campuran sesi/pemain/runtime/
    /// generasi/layar/lokasi/revisi gagal tertutup, bukan sukses.
    /// </summary>
    public static bool TryValidateBinding(
        Dictionary<string, object> context,
        CommandInboxIdentity identity,
        string locationId,
        string runtimeInstanceId,
        long contextGeneration,
        long uiRevision,
        long viewportTransformRevision,
        out CommandRejectReason reason
    )
    {
        if (!SameString(context, "session_id", identity.SessionId))
        {
            reason = CommandRejectReason.SessionMismatch;
            return false;
        }

        if (!SameInt64(context, "player_id", identity.PlayerId))
        {
            reason = CommandRejectReason.PlayerMismatch;
            return false;
        }

        if (!SameString(context, "runtime_instance_id", runtimeInstanceId))
        {
            reason = CommandRejectReason.BindingMismatch;
            return false;
        }

        if (!SameInt64(context, "context_generation", contextGeneration))
        {
            reason = CommandRejectReason.BindingMismatch;
            return false;
        }

        if (!SameInt32(context, "screen_id", identity.ScreenId))
        {
            reason = CommandRejectReason.ScreenMismatch;
            return false;
        }

        if (!SameString(context, "location_id", locationId)
            || !SameString(context, "location_id", identity.LocationId))
        {
            reason = CommandRejectReason.LocationMismatch;
            return false;
        }

        if (!SameInt64(context, "ui_revision", uiRevision)
            || !SameInt64(context, "viewport_transform_revision", viewportTransformRevision))
        {
            reason = CommandRejectReason.BindingMismatch;
            return false;
        }

        reason = CommandRejectReason.None;
        return true;
    }

    static Dictionary<string, object> Available() => new(StringComparer.Ordinal)
    {
        ["available"] = true,
    };

    static Dictionary<string, object> AvailableMoveCardinal() => new(StringComparer.Ordinal)
    {
        ["available"] = true,
        ["primitives"] = new[] { "move_cardinal" },
    };

    static Dictionary<string, object> Unavailable(string reason) => new(StringComparer.Ordinal)
    {
        ["available"] = false,
        ["reason"] = reason,
    };

    /// <summary>
    /// The ONE receiver-token namespace published in ui.context
    /// (<c>interactive.receiver_token</c>, <c>container.receiver_token</c>) and
    /// therefore the one every input verdict, transfer/exit readback and Python
    /// target arg must use. A raw tracker serial (<c>mi5</c>) is not a receiver token.
    /// </summary>
    public static string? ReceiverToken(string? menuInstanceToken) =>
        menuInstanceToken is { Length: > 0 } ? Token("rcv", menuInstanceToken) : null;

    static string Token(string prefix, params string?[] parts)
    {
        List<string> bagian = new() { prefix };
        foreach (string? part in parts)
        {
            if (part is { Length: > 0 })
                bagian.Add(part);
        }
        return string.Join(':', bagian);
    }

    static bool SameString(Dictionary<string, object> context, string key, string expected)
    {
        return context.TryGetValue(key, out object? value)
            && value is string text
            && string.Equals(text, expected, StringComparison.Ordinal);
    }

    static bool SameInt64(Dictionary<string, object> context, string key, long expected)
    {
        if (!context.TryGetValue(key, out object? value) || value is null)
            return false;
        return value switch
        {
            long number => number == expected,
            int number => number == expected,
            JsonElement el when el.ValueKind == JsonValueKind.Number
                && el.TryGetInt64(out long parsed) => parsed == expected,
            _ => false,
        };
    }

    static bool SameInt32(Dictionary<string, object> context, string key, int expected)
    {
        if (!context.TryGetValue(key, out object? value) || value is null)
            return false;
        return value switch
        {
            int number => number == expected,
            long number => number == expected,
            JsonElement el when el.ValueKind == JsonValueKind.Number
                && el.TryGetInt32(out int parsed) => parsed == expected,
            _ => false,
        };
    }
}

/// <summary>
/// Monotonic <c>context_generation</c> selama hidup
/// <c>runtime_instance_id</c>. Naik saat lokasi atau receiver interaktif
/// berganti. ReturnedToTitle menginvalidasi binding lama TANPA mendaur ulang
/// generasi ke 1. Perubahan HUD/informasi di receiver yang sama tidak
/// membuat generasi baru.
/// </summary>
public sealed class ContextGenerationTracker
{
    private string? locationId;
    private string? menuInstanceToken;
    private long generation = 1;

    public long Current => this.generation;

    public long Observe(string locationId, string? menuInstanceToken)
    {
        if (this.locationId is null)
        {
            this.locationId = locationId;
            this.menuInstanceToken = menuInstanceToken;
            return this.generation;
        }

        if (!string.Equals(this.locationId, locationId, StringComparison.Ordinal)
            || !string.Equals(this.menuInstanceToken, menuInstanceToken, StringComparison.Ordinal))
        {
            this.locationId = locationId;
            this.menuInstanceToken = menuInstanceToken;
            this.generation++;
        }

        return this.generation;
    }

    /// <summary>
    /// Batas sesi/title: binding lama tidak berlaku, serial generasi tetap
    /// naik. Jangan set <c>generation = 1</c> selama runtime instance hidup.
    /// </summary>
    public void Reset()
    {
        this.locationId = null;
        this.menuInstanceToken = null;
        this.generation++;
    }
}

/// <summary>
/// Token instance receiver interaktif (halaman <c>GameMenu</c> atau menu
/// aktif, sama dengan yang dibaca CaptureMenu). Naik hanya saat referensi
/// receiver berubah (buka/tutup/ganti halaman), bukan saat ui_revision naik
/// karena teks/fokus HUD. Reset sesi menginvalidasi binding tanpa mendaur
/// ulang serial ke <c>mi1</c>.
/// </summary>
public sealed class MenuInstanceTokenTracker
{
    private object? activeReceiver;
    private int serial;
    private string? currentToken;

    public string? Observe(object? receiver)
    {
        if (receiver is null)
        {
            this.activeReceiver = null;
            this.currentToken = null;
            return null;
        }

        if (!ReferenceEquals(receiver, this.activeReceiver))
        {
            this.activeReceiver = receiver;
            this.serial++;
            this.currentToken = $"mi{this.serial}";
        }

        return this.currentToken;
    }

    /// <summary>
    /// Batas sesi/title: receiver lama tidak berlaku. Serial tetap monotonic
    /// selama runtime instance hidup.
    /// </summary>
    public void Reset()
    {
        this.activeReceiver = null;
        this.currentToken = null;
    }
}

/// <summary>
/// Token opaque untuk subscriber/receiver text-input aktual
/// (<c>Game1.keyboardDispatcher.Subscriber</c>), bukan salinan token menu.
/// Serial monotonic selama <c>runtime_instance_id</c> hidup; ganti
/// subscriber atau hilang-lalu-muncul lagi membuat token baru. Reset sesi
/// menginvalidasi receiver lama tanpa mendaur ulang <c>ti1</c>. Perubahan
/// HUD/informasi di subscriber yang sama tidak membuat token baru.
/// </summary>
public sealed class TextInputTokenTracker
{
    private object? activeSubscriber;
    private int serial;
    private string? currentToken;

    public string? Observe(object? subscriber)
    {
        if (subscriber is null)
        {
            this.activeSubscriber = null;
            this.currentToken = null;
            return null;
        }

        if (!ReferenceEquals(subscriber, this.activeSubscriber))
        {
            this.activeSubscriber = subscriber;
            this.serial++;
            this.currentToken = $"ti{this.serial}";
        }

        return this.currentToken;
    }

    /// <summary>
    /// Batas sesi/title: subscriber lama tidak berlaku. Serial tetap
    /// monotonic selama runtime instance hidup.
    /// </summary>
    public void Reset()
    {
        this.activeSubscriber = null;
        this.currentToken = null;
    }
}
