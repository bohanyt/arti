using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Snapshot keadaan farmer yang dipotret DI GAME THREAD lalu dikirim apa
/// adanya oleh lapis async.
///
/// Sengaja MINIMAL (Gate 5): cukup untuk feedback loop motor, belum untuk
/// otak. Jangan menambah map/NPC/crop/inventory ke sini tanpa alasan motor
/// yang jelas — pemisahan MOTOR_STATE vs BRAIN_OBSERVATION dibekukan di
/// docs/stardew/OBS-0-CONTRACT.md, dan "SMAPI capability != ARTI knowledge".
/// </summary>
public sealed record StatePayload
{
    [JsonPropertyName("native")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public System.Text.Json.JsonElement? Native { get; init; }
    [JsonPropertyName("location")]
    public string Location { get; init; } = "";

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("pixel")]
    public PixelXY Pixel { get; init; } = new();

    [JsonPropertyName("facing")]
    public int Facing { get; init; }

    [JsonPropertyName("time_of_day")]
    public int TimeOfDay { get; init; }

    [JsonPropertyName("date")]
    public GameDate Date { get; init; } = new();

    [JsonPropertyName("energy")]
    public double Energy { get; init; }

    [JsonPropertyName("health")]
    public int Health { get; init; }

    [JsonPropertyName("own_state")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object>? OwnState { get; init; }

    [JsonPropertyName("menu_container")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object>? MenuContainer { get; init; }

    [JsonPropertyName("peer_sleep")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, object?>? PeerSleep { get; init; }

    [JsonPropertyName("is_player_free")]
    public bool IsPlayerFree { get; init; }

    [JsonPropertyName("can_player_move")]
    public bool CanPlayerMove { get; init; }

    /// <summary>Optional FIXB37 diagnostic facts; old clients may ignore them. Never authorizes input.</summary>
    [JsonPropertyName("player_control")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public PlayerControlDiagnostics? PlayerControl { get; init; }

    /// <summary>Nama tipe menu yang sedang terbuka; null kalau tidak ada.</summary>
    [JsonPropertyName("active_menu")]
    public string? ActiveMenu { get; init; }

    /// <summary>Naik hanya saat fingerprint UI terlihat berubah.</summary>
    [JsonPropertyName("ui_revision")]
    public long UiRevision { get; init; }

    /// <summary>
    /// Kursor UI controller. Stardew memakai lock-on antar komponen, bukan
    /// pointer bebas: stik memindahkan `currentlySnappedComponent` dari satu
    /// komponen ke komponen lain. Tanpa melaporkannya, motor menekan tombol
    /// secara buta -- persis kegagalan 29 Agu 2026 waktu `tutup_menu`
    /// men-tap B berkali-kali pada menu yang tidak pernah bereaksi.
    /// Null kalau tidak ada menu terbuka.
    /// </summary>
    [JsonPropertyName("menu")]
    public MenuInfo? Menu { get; init; }

    /// <summary>
    /// Inventaris SEPERLUNYA MOTOR. Ditambahkan 29 Agu 2026 dengan alasan
    /// motor yang jelas, bukan karena "SMAPI bisa":
    ///
    /// 1. `items_total` membuat ambil() bisa DIVERIFIKASI. Sebelum ini motor
    ///    tidak punya cara tahu barangnya benar-benar masuk tas, jadi Arti
    ///    dilarang mengumumkan "udah aku ambil" (aturan #3).
    /// 2. `active_slot` + `hotbar` diperlukan supaya motor tahu APA yang
    ///    sedang dipegang sebelum menekan tombol pakai-alat.
    /// 3. `is_tool` adalah pagar keselamatan: tombol pakai-alat di Stardew
    ///    kontekstual — memegang barang bukan-alat lalu menekannya di dekat
    ///    NPC atau shipping bin itu aksi yang TIDAK BISA DIBATALKAN.
    ///
    /// Sengaja HOTBAR SAJA (12 slot pertama, yang bisa dipilih tombol), bukan
    /// seluruh tas: sisanya tidak bisa dipakai motor tanpa membuka menu, jadi
    /// itu BRAIN_OBSERVATION, bukan MOTOR_STATE. Agregat `slots_used` dan
    /// `items_total` menutupi seluruh tas tanpa membocorkan isinya.
    ///
    /// OPSIONAL di kawat: receiver yang lebih baru tetap harus jalan dengan
    /// DLL lama yang belum mengirim ini.
    /// </summary>
    [JsonPropertyName("inventory")]
    public InventoryInfo? Inventory { get; init; }
}

public sealed record InventoryInfo
{
    /// <summary>Slot hotbar yang aktif (Farmer.CurrentToolIndex).</summary>
    [JsonPropertyName("active_slot")]
    public int ActiveSlot { get; init; }

    /// <summary>Slot terisi pada 12 slot pertama saja. Slot kosong dibuang.</summary>
    [JsonPropertyName("hotbar")]
    public IReadOnlyList<SlotItem> Hotbar { get; init; } = Array.Empty<SlotItem>();

    /// <summary>Jumlah slot terisi di SELURUH tas.</summary>
    [JsonPropertyName("slots_used")]
    public int SlotsUsed { get; init; }

    /// <summary>Jumlah butir di SELURUH tas (stack dijumlahkan).</summary>
    [JsonPropertyName("items_total")]
    public int ItemsTotal { get; init; }
}

public sealed record SlotItem
{
    [JsonPropertyName("slot")]
    public int Slot { get; init; }

    [JsonPropertyName("name")]
    public string Name { get; init; } = "";

    [JsonPropertyName("stack")]
    public int Stack { get; init; }

    /// <summary>True kalau Item ini turunan Tool (cangkul, kapak, siram, ...).</summary>
    [JsonPropertyName("is_tool")]
    public bool IsTool { get; init; }
}

public sealed record TileXY
{
    [JsonPropertyName("x")]
    public int X { get; init; }

    [JsonPropertyName("y")]
    public int Y { get; init; }
}

public sealed record PixelXY
{
    [JsonPropertyName("x")]
    public double X { get; init; }

    [JsonPropertyName("y")]
    public double Y { get; init; }
}

public sealed record GameDate
{
    [JsonPropertyName("season")]
    public string Season { get; init; } = "";

    [JsonPropertyName("day")]
    public int Day { get; init; }

    [JsonPropertyName("year")]
    public int Year { get; init; }
}

/// <summary>
/// Amplop `state`. Bentuk luarnya HARUS sama dengan hello (protocol/ev/seq/
/// player_id/screen_id) supaya receiver Python memakai gerbang validasi dan
/// SequenceGate yang sama persis.
/// </summary>
public sealed record StateEnvelope
{
    [JsonPropertyName("protocol")]
    public int Protocol { get; init; } = 1;

    [JsonPropertyName("ev")]
    public string Event { get; init; } = "state";

    [JsonPropertyName("seq")]
    public long Sequence { get; init; }

    [JsonPropertyName("player_id")]
    public long PlayerId { get; init; }

    [JsonPropertyName("screen_id")]
    public int ScreenId { get; init; }

    [JsonPropertyName("payload")]
    public StatePayload Payload { get; init; } = new();

    public static StateEnvelope Create(
        long seq,
        long playerId,
        int screenId,
        StatePayload payload
    ) => new()
    {
        Sequence = seq,
        PlayerId = playerId,
        ScreenId = screenId,
        Payload = payload,
    };
}

/// <summary>
/// Keadaan kursor UI controller pada menu yang sedang terbuka.
/// </summary>
public sealed record MenuInfo
{
    [JsonPropertyName("name")]
    public string Name { get; init; } = "";

    [JsonPropertyName("menu_type")]
    public string MenuType { get; init; } = "";

    [JsonPropertyName("page_type")]
    public string? PageType { get; init; }

    /// <summary>`myID` komponen yang sedang di-lock-on. Null = tidak ada.</summary>
    [JsonPropertyName("snap_id")]
    public int? SnapId { get; init; }

    /// <summary>Label komponen: `name`, `label`, atau `hoverText`.</summary>
    [JsonPropertyName("snap_label")]
    public string? SnapLabel { get; init; }

    [JsonPropertyName("snap_bounds")]
    public BoundsXY? SnapBounds { get; init; }

    /// <summary>`myID` tombol tutup (silang kanan atas). Null kalau menu tak punya.</summary>
    [JsonPropertyName("close_id")]
    public int? CloseId { get; init; }

    /// <summary>Semantic key komponen fokus untuk surface saat ini.</summary>
    [JsonPropertyName("focus_key")]
    public string? FocusKey { get; init; }

    [JsonPropertyName("visible_text")]
    public string? VisibleText { get; init; }

    [JsonPropertyName("speaker")]
    public string? Speaker { get; init; }

    /// <summary>
    /// Hanya komponen yang SUDAH dibangun game. Observer tidak pernah
    /// memanggil populateClickableComponentList() untuk mengisinya.
    /// </summary>
    [JsonPropertyName("components")]
    public IReadOnlyList<UiComponentInfo> Components { get; init; }
        = Array.Empty<UiComponentInfo>();
}

public sealed record UiComponentInfo
{
    [JsonPropertyName("key")]
    public string Key { get; init; } = "";

    [JsonPropertyName("native_id")]
    public int NativeId { get; init; }

    [JsonPropertyName("source")]
    public string Source { get; init; } = "";

    [JsonPropertyName("role")]
    public string Role { get; init; } = "component";

    [JsonPropertyName("name")]
    public string? Name { get; init; }

    [JsonPropertyName("label")]
    public string? Label { get; init; }

    [JsonPropertyName("bounds")]
    public BoundsXY? Bounds { get; init; }

    /// <summary>Hint native Stardew; sentinel/custom behavior tetap mungkin.</summary>
    [JsonPropertyName("neighbors")]
    public NeighborInfo Neighbors { get; init; } = new();

    [JsonPropertyName("text")]
    public string? Text { get; init; }

    [JsonPropertyName("count")]
    public int? Count { get; init; }

    [JsonPropertyName("selected")]
    public bool Selected { get; init; }
}

public sealed record NeighborInfo
{
    [JsonPropertyName("up")]
    public int? Up { get; init; }

    [JsonPropertyName("right")]
    public int? Right { get; init; }

    [JsonPropertyName("down")]
    public int? Down { get; init; }

    [JsonPropertyName("left")]
    public int? Left { get; init; }
}

public sealed record BoundsXY
{
    [JsonPropertyName("x")]
    public int X { get; init; }

    [JsonPropertyName("y")]
    public int Y { get; init; }

    [JsonPropertyName("w")]
    public int W { get; init; }

    [JsonPropertyName("h")]
    public int H { get; init; }
}
