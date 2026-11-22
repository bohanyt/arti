using ArtiStardewBridge.Protocol;
namespace ArtiStardewBridge;

public sealed class ModConfig
{
    /// <summary>ADR 219: exact displayed game text on the existing ui.text stream. Trial only.</summary>
    public bool GameTextDeliveryEnabled { get; set; } = false;
    /// <summary>ADR 63: relay native story watched by the HOST to CommandInboxDirectory/host_story.jsonl.</summary>
    public bool HostStoryRelayEnabled { get; set; } = false;
    /// <summary>Optional HOST relay folder. Empty uses CommandInboxDirectory (or command/ under this mod).</summary>
    public string HostStoryRelayDirectory { get; set; } = "";

    public bool Q12BackendEdgesEnabled { get; set; } = false;
    public bool FestivalReflexEnabled { get; set; } = false;

    public bool ContainerExitInputEnabled { get; set; } = false;

    /// <summary>
    /// WS2 ui_key gate. DEFAULT OFF. Allows one leased <c>input_once</c> family
    /// <c>ui_key</c> per request: a direct menu-method call (dialogue advance, question
    /// answer/cancel, level-up confirm, profession choose, shipping summary confirm,
    /// letter close) on the game thread. No device state, Press or Suppress; works with
    /// the farmhand window inactive. Not a world-input path and not live proof.
    /// </summary>
    public bool UiKeyInputEnabled { get; set; } = false;
    public bool ContainerTransferInputEnabled { get; set; } = false;
    public bool WorldInteractionInputEnabled { get; set; } = false;
    /// <summary>
    /// WS3a (2026-10-07). BAWAAN MATI. Background world_interact also captures building / map
    /// Warp doors and spawned forage (checkAction order, plain tool in hand). Chest-only when off.
    /// </summary>
    public bool WorldInteractKindsEnabled { get; set; } = false;
    /// <summary>
    /// WS3a (2026-10-07). BAWAAN MATI. Publishes move_through_target and admits the background
    /// walk-onto warp primitive (move run + expected location transition). Needs
    /// WorldPressEnabled + BackgroundMovementEnabled + BackgroundToolUiEnabled.
    /// </summary>
    public bool MoveThroughEnabled { get; set; } = false;
    /// <summary>
    /// WS3b (2026-10-07). BAWAAN MATI. With WorldInteractKindsEnabled: background world_interact
    /// also captures npc talk (plain tool in hand) and the eat prompt (edible item in hand); with
    /// MoveThroughEnabled: move_through kind "bed" (own home). ui.context publishes eat_candidates
    /// and the ui_key question_key (Sleep / Eat).
    /// </summary>
    public bool NpcEatBedEnabled { get; set; } = false;
    public bool SelectItemInputEnabled { get; set; } = false;
    public bool Enabled { get; set; } = true;

    public int Port { get; set; } = 3017;

    /// <summary>
    /// Gerbang kandidat aksi water-once. BAWAAN MATI. Mengizinkan
    /// <c>arti_water</c> di klien SMAPI. Bukan jalur otomatis, bukan
    /// perintah transport, dan bukan bukti siram tanah kering live.
    /// </summary>
    public bool WaterOnceEnabled { get; set; } = false;

    /// <summary>
    /// Gerbang probe P5-R2 aksen (Issue #62). BAWAAN MATI: menyalakannya
    /// memberi izin SATU injeksi tombol D-pad controller lewat
    /// <c>Helper.Input.Press</c> per perintah konsol. Tidak ada jalur otomatis
    /// yang menyalakannya, tidak ada loop, dan tidak ada A/B/Start.
    /// </summary>
    public bool ControllerBootstrapProbeEnabled { get; set; } = false;

    /// <summary>
    /// Gerbang kandidat aksi menu_step P5-R3C. BAWAAN MATI. Mengizinkan
    /// <c>arti_menu_step</c>. Setup-only <c>arti_probe_menu</c> juga
    /// terbuka jika flag ini ATAU <see cref="ControllerStickHoldProbeEnabled"/>
    /// nyala. Diagnostik hold-duration tetap di gerbang terpisah. Bukan
    /// motor production.
    /// </summary>
    public bool ControllerMenuStepEnabled { get; set; } = false;

    /// <summary>
    /// Gerbang probe P5-R3C analog hold-duration (diagnostik). BAWAAN MATI.
    /// Tetap terpisah supaya menu_step bisa dinyalakan tanpa hold-duration.
    /// </summary>
    public bool ControllerStickHoldProbeEnabled { get; set; } = false;

    /// <summary>
    /// TEST-ONLY console/file staging, independent of every probe/input gate. DEFAULT OFF.
    /// question/control require the farmhand; festival-start requires HOST at a reviewed free-roam
    /// festival (separate file directory). Native/owner/STOP checks remain. Turn OFF after staging.
    /// </summary>
    public bool OperatorEventStagingEnabled { get; set; } = false;

    /// <summary>
    /// REHEARSAL-ONLY HOST console command <c>arti_host load &lt;SaveFolder&gt;|ok</c> (DESIGN_HOST_BACKGROUND_INPUT
    /// Phase 2), fed through the HOST supervisor's stdin pipe. DEFAULT OFF; only a rehearsal HOST template sets it.
    /// Refuses on a farmhand (main player only); witness_run prepare-run refuses it in the farmhand config.
    /// Never on in the real stream: there the owner plays HOST by hand.
    /// </summary>
    public bool RehearsalHostConsoleEnabled { get; set; } = false;

    /// <summary>
    /// TEST-ONLY virtual-gamepad probe (a probe branch, #278). Default OFF. When on, the mod
    /// patches InputState.SetMousePosition (observe; optional inactive-only block) and polls
    /// &lt;CommandInboxDirectory&gt;/../probe-gamepad/trigger.json. Never enable outside a probe.
    /// </summary>
    public bool GamepadProbeEnabled { get; set; } = false;

    /// <summary>
    /// TEST-ONLY reflex probe ops (Primitives v2 WS-G4/G5 prep: old_pad, charge_hold, combat_swing, pet_a; PRB-1/3/8).
    /// Default OFF. Only effective together with <see cref="GamepadProbeEnabled"/> (it runs inside that op engine).
    /// <c>old_pad</c> writes <c>Game1.oldPadState</c> only while the captured BobberBar is the deepest menu of the
    /// inactive farmhand (PLAN §1.5 c) and writes neutral on every exit. Never enable outside a probe block.
    /// </summary>
    public bool ReflexProbeEnabled { get; set; } = false;

    /// <summary>
    /// Gerbang kotak masuk perintah lokal P5-R3D. BAWAAN MATI. Menyalakannya
    /// mengizinkan mod membaca SATU berkas permintaan (`menu_step`,
    /// `water_once`, `world_press`, atau query read-only `inspect_tiles`)
    /// dan menulis satu berkas hasil.
    /// Sengaja terpisah dari
    /// <see cref="ControllerMenuStepEnabled"/>,
    /// <see cref="WaterOnceEnabled"/>, dan
    /// <see cref="WorldPressEnabled"/>: kotak masuk plus gerbang op yang
    /// diminta harus nyala sebelum aksi menyentuh dunia, jadi menyalakan
    /// kotak masuk saja tidak pernah cukup untuk mutasi. <c>inspect_tiles</c>
    /// read-only memakai gerbang kotak masuk ini tanpa gerbang motor.
    ///
    /// Bukan transport production (itu P7, sesudah checkpoint P6).
    /// </summary>
    public bool MenuStepCommandInboxEnabled { get; set; } = false;

    /// <summary>
    /// Gerbang op <c>world_press</c> di kotak masuk P5-R3D. BAWAAN MATI.
    /// Sengaja terpisah dari <see cref="MenuStepCommandInboxEnabled"/>:
    /// kotak masuk saja tidak pernah cukup untuk menekan binding gerak.
    /// Console <c>arti_probe_press</c> tetap memakai probe yang sama; gerbang
    /// ini hanya mengizinkan jalur berkas Python.
    /// </summary>
    public bool WorldPressEnabled { get; set; } = false;

    /// <summary>Independent #208 equipped-tool caller gate. Native validation required before activation.</summary>
    public bool UseToolInputEnabled { get; set; } = false;

    /// <summary>
    /// Gerbang V4 tahap-1 gerak latar (#278). BAWAAN MATI. Hanya untuk
    /// <c>input_once</c> <c>world_key</c>/<c>move_cardinal</c> ber-lease dari
    /// kotak masuk yang SAMA, saat jendela farmhand tidak aktif. Tetap butuh
    /// <see cref="MenuStepCommandInboxEnabled"/> dan <see cref="WorldPressEnabled"/>.
    /// Mati = tidak ada hook, latch, maupun thread watchdog yang dipasang;
    /// perilaku foreground tidak berubah. Bukan use_tool/UI/menu/memancing/tempur.
    /// </summary>
    public bool BackgroundMovementEnabled { get; set; } = false;

    /// <summary>
    /// V4 Stage 2 (#278/5994543545, 2.1 5994935998, 2.2 5995067151). DEFAULT OFF.
    /// Requires <see cref="BackgroundMovementEnabled"/> (the same runtime, hook and
    /// watchdog). While the farmhand window is inactive: <c>input_once</c>
    /// <c>use_tool</c> (exact Axe/Pickaxe, 1..14 ticks; also needs
    /// <see cref="UseToolInputEnabled"/>) and <c>world_interact</c> (one pinned chest,
    /// one tick; needs the existing WorldInteractionInputEnabled gate) through one
    /// verified retained SMAPI press of one pinned keyboard key, plus
    /// container_transfer / container_exit on that chest's open menu. ADR 244: with it on,
    /// the same verified path serves her requests whether the window is active or not.
    /// Off = the foreground paths and every IsActive gate are exactly as before.
    /// Not: minimized, cold start/join, fishing, combat, charged tools.
    /// </summary>
    public bool BackgroundToolUiEnabled { get; set; } = false;

    /// <summary>
    /// WS1 primitives v1 (farm tools). DEFAULT OFF. Only meaningful with
    /// <see cref="BackgroundToolUiEnabled"/>. While the farmhand is inactive, the
    /// background <c>use_tool</c> hold additionally admits the basic unenchanted Hoe,
    /// the basic unenchanted non-bottomless WateringCan and the basic Scythe
    /// <c>(W)47</c>, each at most 4 ticks, and the gather window carries the 3x3
    /// <c>tiles</c> facts (tillable / water_refill / villager). Off = Axe/Pickaxe only,
    /// exactly as before. The faced tool-tile guard applies to every tool kind.
    /// Not: upgraded/enchanted hoe or can, golden/iridium scythe, weapons.
    /// </summary>
    public bool BackgroundFarmToolsEnabled { get; set; } = false;

    /// <summary>
    /// Default-OFF S3 timing option for a separate windowed, non-minimized
    /// multiplayer farmhand. Removes only MonoGame's inactive sleep; does not
    /// grant input, change focus/IsActive, or guarantee rendered/captured FPS.
    /// Read at startup. Disable and restart, or use arti_render_timing_off.
    /// </summary>
    public bool FarmhandInactiveRenderingEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G0. DEFAULT OFF. Needs <see cref="BackgroundMovementEnabled"/> and
    /// <see cref="BackgroundToolUiEnabled"/>; the cursor-hidden gate also needs
    /// <see cref="FarmhandInactiveRenderingEnabled"/> (else every pad request refuses
    /// CURSOR_NOT_HIDDEN:draw_path_off). While the farmhand window is inactive, the background
    /// world requests use the virtual-gamepad retained-press path instead of the keyboard latch:
    /// use_tool = pad X, world_interact = pad A, move_cardinal = one left-stick hold (VERIFY role).
    /// ARTI owns Options.gamepadMode = ForceOn (+ gamepadControls, snappyMenus off) for the whole
    /// session, window active or not (ADR 244), and gives it back at lease end / STOP,
    /// ReturnedToTitle, a SaveGameMenu (night save) and dispose, pinning
    /// Game1._oldGamepadConnectedState so no "controller" message / pause menu appears. Also
    /// installs the OS-cursor guard (InputState.SetMousePosition; both window states, ADR 244) and the pad panic
    /// (arti_pad_panic, command-dir pad_panic.json). The legacy controller probes refuse while on; refused
    /// together with GamepadProbeEnabled. Off = the v1 behaviour: keyboard latch, CursorInert, v1 input
    /// normalization, no pad key in session.json / ui.context, no Harmony patch, legacy drivers built; the
    /// shared code (HelperPressPort, hold plan, press session, release read) takes its unchanged keyboard
    /// branch (UNIT_TESTED, not a byte-level claim). HYPOTHESIS until live.
    /// </summary>
    public bool GamepadInputEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G0 slice 2 (P2). DEFAULT OFF. With <see cref="GamepadInputEnabled"/>: admits the
    /// leased input_once world_key primitive <c>pad_button</c> (LB / RB = Farmer.shiftToolbar, Start =
    /// GameMenu, Y = GameMenu crafting tab) as one direct call mirroring the vanilla branch; refused by
    /// the PadContextTable outside the world row. UNIT_TESTED; HYPOTHESIS until live.
    /// </summary>
    public bool PadButtonInputEnabled { get; set; } = false;

    /// <summary>WS-G0 slice 2 (P2 sub-flag). DEFAULT OFF. pad_button Select = QuestLog (journal).</summary>
    public bool PadSelectJournalEnabled { get; set; } = false;

    /// <summary>
    /// FIXB31 (owner decision 2026-10-12). DEFAULT OFF. With <see cref="PadButtonInputEnabled"/>: during a plain cutscene
    /// (skippable, not a festival / wedding, no question or other menu pending, no player-control sequence, no
    /// minigame) pad_button Select skips the event as ONE direct call mirroring the vanilla gamepad Back branch
    /// (Game1.cs:4859 / 13414: skipped = true; skipEvent(); freezeControls = false). Off: the event row is unchanged
    /// (every button NoMeaning:event). UNIT_TESTED; HYPOTHESIS until live.
    /// </summary>
    public bool EventSkipEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G1. DEFAULT OFF. Needs <see cref="GamepadInputEnabled"/> (the OS-cursor guard) and
    /// <see cref="MenuStepCommandInboxEnabled"/>. Publishes ui.context "ui_pad" (ui_inspect: the active menu's
    /// context, snapped component, labelled components, what each button does; read-only field reads, never
    /// performHoverAction) and admits the leased input_once primitives <c>ui_nav</c> (applyMovementKey on the
    /// snap owner; denylisted neighbours refused first) and <c>ui_press</c> (the vanilla dispatcher's calls
    /// mirrored as direct calls on the snapped component; ui_key's own call for the v1 contexts) while the
    /// farmhand window is inactive. Off = ui.context / session.json / inbox byte-for-byte unchanged.
    /// UNIT_TESTED; HYPOTHESIS until the G1 live witness.
    /// </summary>
    public bool UiPadMenusEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G3. DEFAULT OFF. Needs <see cref="UiPadMenusEnabled"/>. Adds the ui_press contexts
    /// <c>naming</c> (NamingMenu: <c>A{text}</c> through the menu's own TextBox + textBoxEnter), <c>number</c>
    /// (NumberSelectionMenu: <c>A{value}</c> clamped, LT/RT +-1, B cancel), <c>ready_check</c> (owner D4 policy:
    /// confirm only a sleep check ARTI started, never cancel) and <c>event</c> (dialogue advance, owner-D7
    /// allowlisted event questions <c>A{choice}</c>, festival entries <c>A{festival[,slot,item]}</c>; skip never, D6),
    /// plus the read-only ui.context blocks <c>ui_event</c> and <c>ready_check</c>. Off = byte-identical to WS-G1.
    /// UNIT_TESTED; HYPOTHESIS until the G3 live witnesses.
    /// </summary>
    public bool TextEventMenusEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G3, owner D5. DEFAULT OFF. Admits the input_once world_key primitive <c>chat_send</c>
    /// (ChatBox.textBoxEnter(text): plain text only, never a "/" command, never while the chat box is open).
    /// </summary>
    public bool ChatSendEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G3, owner D10 item policy for festival entries (secret santa gift, luau soup): the
    /// qualified item ids ARTI may hand over. DEFAULT EMPTY = every festival item entry refused (item_policy).
    /// </summary>
    public List<string> FestivalItemAllowlist { get; set; } = new();

    /// <summary>
    /// Primitives v2 WS-G2: the economy rows on top of the G1 menu layer (needs <see cref="UiPadMenusEnabled"/>):
    /// ShopMenu buy {qty} / sell one / half / stack, CraftingPage craft {qty} (GameMenu tab and standalone cooking),
    /// Clint's upgrade shop (only with <see cref="ItemPolicyConfig.AllowToolUpgrade"/>), and the container_transfer
    /// modes quick / take_out (chest, needs <see cref="ContainerTransferInputEnabled"/>) and ship_one / ship_stack
    /// (shipping bin, inactive farmhand). Every sell / ship / buy / craft is gated by <see cref="EconomyItemPolicy"/>.
    /// Off = ui.context / session.json / inbox unchanged. UNIT_TESTED; HYPOTHESIS until the G2 live witnesses.
    /// </summary>
    public bool EconomyMenusEnabled { get; set; } = false;

    /// <summary>WS-G2 owner item policy (PLAN 6.1 D10): sell / ship allowlists, price caps, never-list, buy cap.</summary>
    public ItemPolicyConfig EconomyItemPolicy { get; set; } = new();

    /// <summary>
    /// Primitives v2 WS-G4. DEFAULT OFF. With <see cref="GamepadInputEnabled"/> (pad path configured): admits the
    /// leased input_once world_key primitive <c>reflex_fish</c> {max_s, cast_power, treasure, hook_retries} while the
    /// farmhand is inactive, a FishingRod is the current tool, ARTI faces water whose cast landing tile is fishable,
    /// the inventory has a free slot and no menu is open; publishes ui.context <c>fishing</c> (landing, water_edges)
    /// and capability <c>reflex_fish</c>. The reflex casts (pad X hold to the castingPower target), hooks (1-tick pad
    /// X), drives the BobberBar through the one guarded Game1.oldPadState X writer and dismisses the catch (1-tick pad
    /// X): one verdict FishCaught / FishEscaped / NoBite / Aborted(reason). UNIT_TESTED; HYPOTHESIS until live.
    /// </summary>
    public bool ReflexFishingEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G6. DEFAULT OFF. With GamepadInputEnabled (pad path) + WorldInteractKindsEnabled: the world
    /// extras world_interact kinds (pet, animal_pet / _milk / _shear, horse_mount / _dismount, shake_tree, harvest_crop,
    /// machine_collect / _insert, hay_hopper, ladder_down, shaft, mine_elevator, bus, minecart, place_item), the
    /// ui.context extras_targets list, the G6 menu contexts (mine_elevator / junimo_note / museum, with
    /// UiPadMenusEnabled) and the Shaft / Bus / pagedResponse question keys for ui_key. Off = every v1 / G0 / G1
    /// observation, digest and refusal unchanged. UNIT_TESTED; HYPOTHESIS until the G6 live witnesses.
    /// </summary>
    public bool WorldExtrasEnabled { get; set; } = false;

    /// <summary>Generic ACTION/USE buttons with observed world deltas. Uses the existing input owner.</summary>
    public bool GenericWorldInputEnabled { get; set; } = false;

    /// <summary>
    /// WS-G6 owner policy allow_profession_menu. DEFAULT OFF: every extras kind whose XP gain would cross Farming
    /// (or another skill's) level 5 / 10 is refused (profession_menu_policy:level_N), because the night's profession
    /// chooser is not answerable by the runtime (owner D8).
    /// </summary>
    public bool ExtrasAllowProfessionMenu { get; set; } = false;

    /// <summary>
    /// WS-G6 owner policy (review MAJOR 2). DEFAULT OFF: every ladder / shaft descent into or inside the Skull Cavern
    /// (mine level 121+; shafts exist only there) is refused (skull_cavern_policy). Regular Mines ladders also need
    /// health &gt;= 50 % max and stamina &gt;= 20; a shaft also needs health &gt;= 55 (its fall costs up to 45 HP).
    /// </summary>
    public bool ExtrasAllowSkullCavern { get; set; } = false;

    /// <summary>
    /// WS-G6 machine drop-in item policy (review MINOR 3): qualified ids an X drop-in may consume. DEFAULT: the four ores
    /// (furnace) + WS-G2's basic forage / crop allowlist. Other items: refused by WS-G2's never-dispose list, else admitted
    /// only at or below <see cref="ExtrasMachineInputPriceCap"/>; tools / quest / special / equipment never.
    /// </summary>
    // Same values as WorldExtras.DefaultMachineInputAllowlist (pinned by WorldExtrasTests); spelled out here so ModConfig.cs
    // stays compilable in test projects that do not compile Protocol/WorldExtras.cs.
    public List<string> ExtrasMachineInputAllowlist { get; set; } =
        new(new[] { "(O)378", "(O)380", "(O)384", "(O)386" }.Concat(ItemPolicyConfig.DefaultAllowlist));

    /// <summary>WS-G6: unit sell price cap for a drop-in item outside the allowlist (0 = allowlist only). DEFAULT 50.</summary>
    public int ExtrasMachineInputPriceCap { get; set; } = 50;

    /// <summary>
    /// Primitives v2 WS-G5. DEFAULT OFF. With <see cref="GamepadInputEnabled"/> (pad path configured): admits the leased
    /// input_once world_key primitive <c>reflex_combat</c> {max_s, hp_floor_pct, max_kills, retreat} while the farmhand is
    /// inactive, a MeleeWeapon (owner 2026-10-23: the scythe counts) is the current tool, a monster is within 8 tiles, health is above the floor and
    /// the time rule allows it (Mines / Skull Cavern: start 09:00-20:00; never at/after 01:00); publishes ui.context
    /// <c>combat</c> (monsters, health, exit tile) and capability <c>reflex_combat</c>. The reflex faces (1-frame D-pad),
    /// approaches (short D-pad holds) and swings (1-frame pad X at a 24-tick cadence, only while not UsingTool); pad A is
    /// never pressed. Verdict MonsterKilled(n) / Retreated(reason) / Downed / NoTarget / Aborted(reason). UNIT_TESTED;
    /// HYPOTHESIS until live.
    /// </summary>
    public bool ReflexCombatEnabled { get; set; } = false;

    /// <summary>
    /// Primitives v2 WS-G5. DEFAULT OFF. With <see cref="GamepadInputEnabled"/>: admits <c>reflex_charge</c>
    /// {target_level 1-4, max_ticks 45-200} with an upgraded Hoe / Watering Can (Axe and Pickaxe never charge), a valid faced
    /// tile and a safe area; holds pad X until toolPower reaches the target (capped by upgradeLevel + Reaching), releases and
    /// reads the area back. Verdict ChargeReached(n) / ChargeAborted(reason). UNIT_TESTED; HYPOTHESIS until live.
    /// </summary>
    public bool ReflexChargeEnabled { get; set; } = false;

    /// <summary>
    /// Live7 character creation (docs/stardew/live7/CHARACTER_CREATION.md). DEFAULT OFF. Needs
    /// <see cref="UiPadMenusEnabled"/>. Adds the ui_pad context <c>char_custom</c> for the farmhand variant of the vanilla
    /// CharacterCustomization (Source.NewFarmhand, opened by Client.setUpGame for an unclaimed cabin): the published
    /// <c>char_custom</c> block (every control's value and options) and <c>ui_press A {choice, value | text}</c> = one native
    /// receiveLeftClick on the control's own component (arrows, Male / Female, colour bars, OK) or the WS-G3 text-entry
    /// primitive on its name / favourite TextBox; plus the read-only ui.context <c>farmer_profile</c> and, while the world
    /// is not ready, the read-only <c>join_screen.json</c> (FarmhandMenu slots) in the command folder. Off = ui.context,
    /// session.json and the inbox byte-for-byte unchanged. UNIT_TESTED; HYPOTHESIS until the Live7 witness.
    /// </summary>
    public bool CharacterCreationEnabled { get; set; } = false;

    /// <summary>
    /// Unchanged-snapshot refresh interval (ms) for <c>progress.inventory</c> and
    /// <c>ui.context</c>. Both domains are change-driven; the Python runtime treats
    /// them as fresh only for 2 s after receipt. When nothing changed for this long
    /// since the last acknowledged send, the same facts are re-sent at the SAME
    /// <c>domain_revision</c> (never incremented), so only the receiver's receipt
    /// time moves. Default 1000. 0 (or negative) disables the refresh and restores
    /// the old send-on-change-only behaviour. Not a new cadence: it rides the
    /// existing 10 Hz observer tick and is checked only when nothing changed.
    /// </summary>
    public int ObservationRefreshIntervalMs { get; set; } = 1000;

    /// <summary>
    /// Folder kotak masuk perintah. Kosong = <c>command/</c> di dalam folder
    /// mod. Path relatif dihitung dari folder mod; path absolut dipakai apa
    /// adanya supaya lab bisa memisahkan profile ARTI dari profile Bohan.
    /// </summary>
    public string CommandInboxDirectory { get; set; } = "";
}
