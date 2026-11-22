using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// WS2 <c>ui_key</c>: one direct menu-method call on the game thread for a small
/// whitelist of menus (dialogue advance, question answer/cancel, level-up confirm,
/// profession choose, shipping summary confirm, letter close). Menus receive no
/// keyboard/mouse/gamepad dispatch while the window is inactive
/// (<c>Game1.updateActiveMenu</c> returns before input dispatch), so no device state,
/// Press or Suppress is ever involved. SMAPI-free; ModEntry only reads game objects
/// into <see cref="UiKeyMenuFacts"/> / <see cref="UiKeyProbe"/> and passes delegates.
/// Evidence ceiling: UNIT_TESTED (HYPOTHESIS until a live witness).
/// </summary>
public static class UiKeyTable
{
    public const string Primitive = "ui_key";
    public const string Dialogue = "dialogue", Question = "question", NpcQuestion = "npc_question",
        LevelUp = "level_up", LevelUpProfession = "level_up_profession",
        ShippingSummary = "shipping_summary", Letter = "letter";
    public const string Advance = "advance", Answer = "answer", Cancel = "cancel",
        Confirm = "confirm", Choose = "choose", Close = "close";
    public const int MaxOptions = 8;
    public const int MaxProfessionId = 29;
    public const int MaxText = 128;

    /// <summary>
    /// Location questions (kind <c>question</c>) are answerable only for these
    /// <c>GameLocation.lastQuestionKey</c> values: bed "Sleep" (<c>GameLocation.cs:3765</c>) and eat/drink
    /// "Eat" (<c>Game1.cs:11468</c>). Everything else (JojaSignUp, dogStatue, evil shrines, ...) can be a
    /// permanent save mutation and is refused <c>unsupported_question</c>.
    /// </summary>
    public static readonly string[] AllowedQuestionKeys = { "Sleep", "Eat" };

    /// <summary>
    /// WS-G6 (review MINOR 4): the shaft / bus / minecart question keys (<see cref="WorldExtras.QuestionKeys"/>) are
    /// answerable ONLY for the very DialogueBox an ARTI extras press opened (<paramref name="extrasPinned"/>: the mod pins
    /// that receiver by reference at the verified QuestionOpened verdict). Vanilla reuses "pagedResponse"
    /// (ShowPagedResponses) for the phone, building placement and the animal-purchase location: those stay refused.
    /// </summary>
    public static bool IsAllowedQuestionKey(string? key, bool extrasPinned = false, bool giftPinned = false) =>
        key is not null && (AllowedQuestionKeys.Contains(key) || (extrasPinned && WorldExtras.QuestionKeys.Contains(key))
            || (giftPinned && key == GiftQuestionKey));

    /// <summary>
    /// Sat gift (2026-10-24): the multiplayer gift prompt (<c>FarmerTeam.handleIncomingProposal</c>, ProposalType.Gift:
    /// <c>createQuestionDialogue(GivenGift, createYesNoResponses(), FarmerTeam closure)</c>) has no lastQuestionKey. The mod
    /// publishes this synthetic key ONLY while the native identity holds (<see cref="GiftPin.Holds"/>); answers are the
    /// vanilla "Yes" / "No" response keys.
    /// </summary>
    public const string GiftQuestionKey = "GiftPlayerItem";
    public static bool GiftResponse(string? responseKey) => responseKey is "Yes" or "No";

    static readonly Dictionary<string, (string Menu, string[] Keys)> Kinds = new(StringComparer.Ordinal)
    {
        [Dialogue] = ("DialogueBox", new[] { Advance }),
        [Question] = ("DialogueBox", new[] { Answer, Cancel }),
        [NpcQuestion] = ("DialogueBox", new[] { Answer }),
        [LevelUp] = ("LevelUpMenu", new[] { Confirm }),
        [LevelUpProfession] = ("LevelUpMenu", new[] { Choose }),
        [ShippingSummary] = ("ShippingMenu", new[] { Confirm }),
        [Letter] = ("LetterViewerMenu", new[] { Close }),
    };

    /// <summary>Never invoked: observed and advertised <c>wait_only</c>.</summary>
    public static readonly string[] WaitOnlyTypes =
        { "SaveGameMenu", "ReadyCheckDialog", "NamingMenu", "ShopMenu", "GameMenu", "ItemGrabMenu" };

    public static bool IsKind(string? kind) => kind is not null && Kinds.ContainsKey(kind);
    public static string? MenuOf(string kind) => Kinds.TryGetValue(kind, out var v) ? v.Menu : null;
    public static IReadOnlyList<string> KeysFor(string? kind) =>
        kind is not null && Kinds.TryGetValue(kind, out var v) ? v.Keys : Array.Empty<string>();
    public static bool IsAllowed(string? menuType, string? kind, string? key) =>
        kind is not null && Kinds.TryGetValue(kind, out var v) && v.Menu == menuType && v.Keys.Contains(key);
    public static bool IsWaitOnly(string? menuType) => menuType is not null && WaitOnlyTypes.Contains(menuType);

    /// <summary>Kinds whose effect is proven synchronously right after the call.</summary>
    public static bool IsSyncKind(string kind) =>
        kind is LevelUp or LevelUpProfession or ShippingSummary or Letter;

    /// <summary>Exact arg key set per key (no extras).</summary>
    public static string[] ArgKeys(string key) => key switch
    {
        Answer => new[] { "receiver_token", "menu_type", "kind", "key", "target_token", "option_index" },
        Choose => new[] { "receiver_token", "menu_type", "kind", "key", "target_token", "option_index", "profession_id" },
        _ => new[] { "receiver_token", "menu_type", "kind", "key", "target_token" },
    };
}

public sealed record UiKeyTarget(string ReceiverToken, string MenuType, string Kind, string Key,
    string TargetToken, int? OptionIndex = null, int? ProfessionId = null)
{
    public const string Primitive = UiKeyTable.Primitive;

    public bool IsValid =>
        new[] { ReceiverToken, MenuType, Kind, Key, TargetToken }.All(s => !string.IsNullOrWhiteSpace(s) && s.Length <= UiKeyTable.MaxText)
        && ReceiverToken.StartsWith("rcv:", StringComparison.Ordinal)
        && UiKeyTargetToken.IsShape(TargetToken)
        && UiKeyTable.IsAllowed(MenuType, Kind, Key)
        && Key switch
        {
            UiKeyTable.Answer => OptionIndex is >= 0 and < UiKeyTable.MaxOptions && ProfessionId is null,
            UiKeyTable.Choose => OptionIndex is 0 or 1 && ProfessionId is >= 0 and <= UiKeyTable.MaxProfessionId,
            _ => OptionIndex is null && ProfessionId is null,
        };

    public Dictionary<string, object> Args()
    {
        var args = new Dictionary<string, object>
        {
            ["receiver_token"] = ReceiverToken, ["menu_type"] = MenuType, ["kind"] = Kind,
            ["key"] = Key, ["target_token"] = TargetToken,
        };
        if (OptionIndex is { } option) args["option_index"] = option;
        if (ProfessionId is { } profession) args["profession_id"] = profession;
        return args;
    }

    public static bool TryParse(JsonElement args, out UiKeyTarget? target)
    {
        target = null;
        if (args.ValueKind != JsonValueKind.Object) return false;
        var props = args.EnumerateObject().ToList();
        if (props.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != props.Count) return false;
        if (!args.TryGetProperty("key", out var keyElement) || keyElement.ValueKind != JsonValueKind.String) return false;
        string key = keyElement.GetString()!;
        string[] keys = UiKeyTable.ArgKeys(key);
        if (props.Count != keys.Length || props.Any(p => !keys.Contains(p.Name))) return false;
        string[] texts = { "receiver_token", "menu_type", "kind", "key", "target_token" };
        if (texts.Any(k => args.GetProperty(k).ValueKind != JsonValueKind.String)) return false;
        int? option = null, profession = null;
        if (keys.Contains("option_index"))
        {
            var o = args.GetProperty("option_index");
            if (o.ValueKind != JsonValueKind.Number || !o.TryGetInt32(out int v)) return false;
            option = v;
        }
        if (keys.Contains("profession_id"))
        {
            var p = args.GetProperty("profession_id");
            if (p.ValueKind != JsonValueKind.Number || !p.TryGetInt32(out int v)) return false;
            profession = v;
        }
        target = new UiKeyTarget(args.GetProperty("receiver_token").GetString()!, args.GetProperty("menu_type").GetString()!,
            args.GetProperty("kind").GetString()!, key, args.GetProperty("target_token").GetString()!, option, profession);
        return target.IsValid;
    }
}

public static class UiKeyTargetToken
{
    public static string Compute(string receiverToken, string menuType, string kind, string key, string kindFacts)
    {
        string text = string.Join("|", receiverToken, menuType, kind, key, kindFacts);
        return "uk:" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant()[..32];
    }

    public static string Hash(string? text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text ?? ""))).ToLowerInvariant();

    public static bool IsShape(string? token) =>
        token is { Length: 35 } && token.StartsWith("uk:", StringComparison.Ordinal)
        && token[3..].All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');
}

// ---- Raw facts read by ModEntry on the game thread (one tick, one receiver) ----

public sealed record UiKeyEnvironment(bool EventUp = false, bool MinigameActive = false, bool FarmEventActive = false,
    bool TextSubscriber = false, bool HasChildOrParent = false, bool AssemblyPinned = true);

public sealed record UiKeyResponse(string ResponseKey, string Label);

public sealed record UiKeyDialogueFacts(bool IsQuestion, bool HasCharacterDialogue, bool Transitioning,
    bool TextFullyRevealed, int SafetyTimer, int QuestionPauseTimer, string CurrentString, int DialoguesCount,
    int BrokenUpCount, IReadOnlyList<UiKeyResponse> Responses, bool OptionsShown, string? QuestionKey = null,
    bool ExtrasPinned = false, bool GiftPinned = false);

/// <summary>Sat gift: the pure native-identity terms of the multiplayer gift prompt (read by the mod each frame).</summary>
public sealed record GiftPinFacts(bool IsQuestion, bool HasCharacterDialogue, bool IsActiveMenu, bool ProposalPending,
    bool ProposalIsGift, bool ProposalCanceled, bool GiftPresent, bool ReceiverIsPlayer, bool CallbackOwnedByFarmerTeam,
    IReadOnlyList<string> ResponseKeys);

public static class GiftPin
{
    /// <summary>All terms, else not the gift prompt (any other question keeps its own rules / unsupported_question).</summary>
    public static bool Holds(GiftPinFacts f) =>
        f.IsQuestion && !f.HasCharacterDialogue && f.IsActiveMenu && f.ProposalPending && f.ProposalIsGift
        && !f.ProposalCanceled && f.GiftPresent && f.ReceiverIsPlayer && f.CallbackOwnedByFarmerTeam
        && f.ResponseKeys is { Count: 2 } keys && keys[0] == "Yes" && keys[1] == "No";
}

public sealed record UiKeyLevelUpFacts(bool IsActive, bool InformationUp, bool IsProfessionChooser,
    bool CanReceiveInput, bool ReadyToClose, int Skill, int Level, IReadOnlyList<int> ProfessionsToChoose,
    bool HasUpdatedProfessions, bool AnyOptionOwned = false);

public sealed record UiKeyShippingFacts(int CurrentPage, bool IntroDone, bool SaveMenuActive, bool Outro,
    bool SavedYet, bool OkGeometryClear, int[] OkBounds);

public sealed record UiKeyLetterFacts(bool IsFromCollection, bool IsMail, float Scale, bool ReadyToClose,
    int Page, bool HasItems, bool HasQuest);

public sealed record UiKeyMenuFacts(string MenuType, UiKeyEnvironment Env, UiKeyDialogueFacts? Dialogue = null,
    UiKeyLevelUpFacts? LevelUp = null, UiKeyShippingFacts? Shipping = null, UiKeyLetterFacts? Letter = null);

/// <summary>Classified current receiver state: kind, readiness and the token facts.</summary>
public sealed record UiKeyState(string MenuType, string? Kind, bool Ready, string? NotReadyReason, bool WaitOnly,
    string KindFacts, int OptionCount = 0, IReadOnlyList<UiKeyResponse>? Options = null,
    IReadOnlyList<int>? Professions = null, int? Skill = null, int? Level = null, UiKeyLetterFacts? Letter = null,
    string? QuestionKey = null)
{
    public string TokenFor(string receiverToken, string key) =>
        UiKeyTargetToken.Compute(receiverToken, MenuType, Kind ?? "", key, KindFacts);
}

public static class UiKeyClassifier
{
    public static UiKeyState Classify(UiKeyMenuFacts f, bool genericUi = false)
    {
        string menu = f.MenuType;
        UiKeyState NotReady(string? kind, string reason, string facts = "", bool waitOnly = false) =>
            new(menu, kind, false, reason, waitOnly, facts);
        if (f.Env.EventUp || f.Env.MinigameActive || f.Env.FarmEventActive)
            return NotReady(null, "event_active", waitOnly: true);
        if (UiKeyTable.IsWaitOnly(menu))
            return NotReady(null, "wait_only", waitOnly: true);
        if (f.Env.TextSubscriber)
            return NotReady(null, "text_input", waitOnly: true);
        if (f.Env.HasChildOrParent)
            return NotReady(null, "unsupported_menu");
        UiKeyState state = menu switch
        {
            "DialogueBox" when f.Dialogue is { } d => Dialogue(menu, d, genericUi),
            "LevelUpMenu" when f.LevelUp is { } l => LevelUp(menu, l),
            "ShippingMenu" when f.Shipping is { } s => Shipping(menu, s),
            "LetterViewerMenu" when f.Letter is { } t => Letter(menu, t),
            _ => NotReady(null, "unsupported_menu"),
        };
        if (!f.Env.AssemblyPinned && state.Kind is not null)
            state = state with { Ready = false, NotReadyReason = "assembly_unpinned" };
        return state;
    }

    static UiKeyState Dialogue(string menu, UiKeyDialogueFacts d, bool genericUi)
    {
        string kind = !d.IsQuestion ? UiKeyTable.Dialogue : d.HasCharacterDialogue ? UiKeyTable.NpcQuestion : UiKeyTable.Question;
        string hash = UiKeyTargetToken.Hash(d.CurrentString);
        bool gift = d.GiftPinned && d.QuestionKey == UiKeyTable.GiftQuestionKey;
        string facts = d.IsQuestion
            ? (d.QuestionKey == "TV" ? "tv|" : gift ? "gift|" : "") + "r=" + string.Join("\u001f", d.Responses.Select(r => r.ResponseKey)) + "|h=" + hash
            : "h=" + hash + "|d=" + d.DialoguesCount + "|b=" + d.BrokenUpCount;
        var options = d.IsQuestion ? d.Responses.Take(UiKeyTable.MaxOptions).ToArray() : Array.Empty<UiKeyResponse>();
        string? reason = !genericUi && kind == UiKeyTable.Question && !UiKeyTable.IsAllowedQuestionKey(d.QuestionKey, d.ExtrasPinned, gift) ? "unsupported_question"
            : d.Transitioning || d.QuestionPauseTimer > 0 ? "transitioning"
            : !d.TextFullyRevealed ? "typing"
            : d.SafetyTimer > 0 ? "safety_timer"
            : d.IsQuestion && (!d.OptionsShown) ? "options_not_shown"
            : d.IsQuestion && (d.Responses.Count < 1 || d.Responses.Count > UiKeyTable.MaxOptions) ? "unsupported_menu"
            : null;
        return new(menu, kind, reason is null, reason, false, facts, options.Length, options, QuestionKey: d.QuestionKey);
    }

    static UiKeyState LevelUp(string menu, UiKeyLevelUpFacts l)
    {
        string kind = l.IsProfessionChooser ? UiKeyTable.LevelUpProfession : UiKeyTable.LevelUp;
        string facts = l.IsProfessionChooser
            ? $"s={l.Skill}|l={l.Level}|p={string.Join(",", l.ProfessionsToChoose)}"
            : $"s={l.Skill}|l={l.Level}";
        string? reason = !l.IsActive || !l.InformationUp ? "outro"
            : !l.CanReceiveInput ? "timer_before_start"
            : !l.ReadyToClose ? "not_ready_to_close"
            : l.IsProfessionChooser && (!l.HasUpdatedProfessions || l.ProfessionsToChoose.Count != 2
                || l.ProfessionsToChoose.Any(p => p < 0 || p > UiKeyTable.MaxProfessionId)) ? "options_not_shown"
            : l.IsProfessionChooser && l.AnyOptionOwned ? "profession_already_owned"
            : null;
        return new(menu, kind, reason is null, reason, false, facts,
            Professions: l.IsProfessionChooser ? l.ProfessionsToChoose.ToArray() : null, Skill: l.Skill, Level: l.Level);
    }

    static UiKeyState Shipping(string menu, UiKeyShippingFacts s)
    {
        string facts = "summary|" + string.Join(",", s.OkBounds);
        string? reason = s.Outro || s.SavedYet ? "outro"
            : s.SaveMenuActive ? "saving"
            : !s.IntroDone ? "intro"
            : s.CurrentPage != -1 ? "category_page"
            : !s.OkGeometryClear || s.OkBounds.Length != 4 ? "ok_geometry_unclear"
            : null;
        return new(menu, UiKeyTable.ShippingSummary, reason is null, reason, false, facts);
    }

    static UiKeyState Letter(string menu, UiKeyLetterFacts t)
    {
        string facts = $"p={t.Page}|m={(t.IsMail ? 1 : 0)}|i={(t.HasItems ? 1 : 0)}|q={(t.HasQuest ? 1 : 0)}";
        string? reason = t.IsFromCollection ? "unsupported_menu"
            : t.Scale < 1f ? "opening"
            : !t.ReadyToClose ? "not_ready_to_close"
            : null;
        return new(menu, t.IsFromCollection ? null : UiKeyTable.Letter, reason is null, reason, false, facts, Letter: t);
    }
}

/// <summary>
/// One read of the current interactive receiver plus the pinned original menu's
/// kind-specific sync facts. <c>MenuActive</c>/<c>LevelPending</c>/<c>ProfessionCount</c>
/// are read on the ORIGINAL LevelUpMenu object; <c>CanReceiveInput</c> on the original ShippingMenu.
/// </summary>
public sealed record UiKeyProbe(string? ReceiverToken, string? MenuType, string? KindFacts,
    bool MenuActive = true, bool LevelPending = false, int ProfessionCount = 0, bool CanReceiveInput = false,
    string? ActorContents = null, string? TvDialogueChain = null);

public sealed record UiKeyVerdict(string Outcome, bool Applied, string? Effect, int InvokeCount, bool Settled,
    string ReceiverTransitionStatus, string? ResultReceiverToken, string? ResultMenuType, int? ProfessionId,
    string Reason, long SettleMs = 0)
{
    public const string OutcomeApplied = "UiKeyApplied", OutcomeNoEffect = "UiKeyNoEffect",
        OutcomeUncertain = "UiKeyUncertain", OutcomeRejected = "UiKeyRejected";
    public bool Uncertain => this.Outcome == OutcomeUncertain;
    public bool Rejected => this.Outcome == OutcomeRejected;
}

/// <summary>
/// Single-use executor. <see cref="Start"/> revalidates and invokes at most once;
/// a sync kind may finish immediately; otherwise <see cref="Observe"/> is fed one
/// probe per tick until a verdict or the monotonic deadline. A consumed instance
/// never invokes again (no replay).
/// </summary>
public sealed class UiKeyInput
{
    public const int AsyncSettleMs = 1500;
    public const int SyncReceiverWaitMs = 1000;

    bool consumed, invoked, done, syncApplied;
    int transitions;
    UiKeyTarget? target;
    UiKeyProbe? before;
    string? firstTransitionFrom;
    string? firstTransitionTo, secondTransitionFrom, secondTransitionTo;

    public bool Invoked => this.invoked;
    public bool Done => this.done;
    public int Transitions => this.transitions;

    // Shared with the native reader so protocol replay tests the ownership proof,
    // including the delegate method, rather than manufacturing a TV chain flag.
    public static bool TvCallbackOwned(object? tv, object? location, object? currentLocation, Delegate? callback) =>
        tv is not null && location is not null && ReferenceEquals(location, currentLocation)
        && ReferenceEquals(callback?.Target, tv) && callback.Method.Name == "proceedToNextScene";

    // A chain ID is captured while the question is current. Re-observing that
    // historical object would change the single-current-receiver token tracker.
    public static string? TvDialogueChainToken(object? menu, object? pinnedDialogue, string? questionToken) =>
        menu is not null && ReferenceEquals(menu, pinnedDialogue) ? questionToken : null;

    /// <summary>Harmony setter finalizer delegate: one call per receiver change.</summary>
    public void ObserveReceiverTransition(string? from, string? to)
    {
        if (!this.invoked || this.done || from == to) return;
        this.transitions++;
        if (this.transitions == 1) { this.firstTransitionFrom = from; this.firstTransitionTo = to; }
        if (this.transitions == 2) { this.secondTransitionFrom = from; this.secondTransitionTo = to; }
    }

    static UiKeyVerdict Reject(string reason) =>
        new(UiKeyVerdict.OutcomeRejected, false, null, 0, false, "NotInvoked", null, null, null, reason);

    public UiKeyVerdict? Start(UiKeyTarget wanted, UiKeyState? current, string? currentReceiver, bool eligible,
        UiKeyProbe before, Action invoke, Func<UiKeyProbe?> read)
    {
        if (this.consumed) return Reject("UiKeyReplay");
        this.consumed = true;
        if (!eligible) return Reject("UiKeyNotReady");
        if (!wanted.IsValid || current is null || current.Kind is null) return Reject("UiKeyUnsupported");
        if (currentReceiver != wanted.ReceiverToken || current.MenuType != wanted.MenuType || current.Kind != wanted.Kind
            || before.ReceiverToken != wanted.ReceiverToken)
            return Reject("UiKeyTargetChanged");
        if (!UiKeyTable.IsAllowed(current.MenuType, current.Kind, wanted.Key)) return Reject("UiKeyUnsupported");
        if (!current.Ready) return Reject("UiKeyNotReady");
        if (current.TokenFor(wanted.ReceiverToken, wanted.Key) != wanted.TargetToken) return Reject("UiKeyTargetChanged");
        if (wanted.Key == UiKeyTable.Answer && (wanted.OptionIndex is not { } i || i >= current.OptionCount))
            return Reject("UiKeyTargetChanged");
        if (current.QuestionKey == "TV" && (wanted.Key != UiKeyTable.Answer
                || current.Options?[wanted.OptionIndex!.Value].ResponseKey is not ("Weather" or "Fortune" or "(Leave)")))
            return Reject("UiKeyUnsupported");
        if (current.QuestionKey == "Marnie" && (wanted.Key != UiKeyTable.Answer
                || current.Options?[wanted.OptionIndex!.Value].ResponseKey is not ("Purchase" or "Leave")))
            return Reject("UiKeyUnsupported");
        if (current.QuestionKey == UiKeyTable.GiftQuestionKey && (wanted.Key != UiKeyTable.Answer
                || !UiKeyTable.GiftResponse(current.Options?[wanted.OptionIndex!.Value].ResponseKey)))
            return Reject("UiKeyUnsupported");
        if (current.QuestionKey == "Blacksmith" && (wanted.Key != UiKeyTable.Answer
                || current.Options?[wanted.OptionIndex!.Value].ResponseKey is not ("Upgrade" or "Leave")))
            return Reject("UiKeyUnsupported");
        if (wanted.Key == UiKeyTable.Choose
            && (current.Professions is not { Count: 2 } ps || wanted.OptionIndex is not { } c || ps[c] != wanted.ProfessionId))
            return Reject("UiKeyTargetChanged");
        this.target = wanted;
        this.before = before;
        this.invoked = true;
        try { invoke(); }
        catch { this.done = true; return this.Finish(UiKeyVerdict.OutcomeUncertain, null, null, "invoke_threw", 0); }
        UiKeyProbe? after;
        try { after = read(); } catch { after = null; }
        if (after is null) { this.done = true; return this.Finish(UiKeyVerdict.OutcomeUncertain, null, null, "identity_lost", 0); }
        if (!UiKeyTable.IsSyncKind(wanted.Kind)) return null; // async: keep inflight
        return this.SyncVerdict(after);
    }

    UiKeyVerdict? SyncVerdict(UiKeyProbe after)
    {
        UiKeyTarget t = this.target!;
        UiKeyProbe b = this.before!;
        switch (t.Kind)
        {
            case UiKeyTable.LevelUp:
            {
                bool inactive = !after.MenuActive, removed = !after.LevelPending;
                if (inactive && removed) { this.syncApplied = true; return null; }
                this.done = true;
                return inactive || removed
                    ? this.Finish(UiKeyVerdict.OutcomeUncertain, after, null, "partial_sync_proof", 0)
                    : this.Finish(UiKeyVerdict.OutcomeNoEffect, after, null, "no_sync_proof", 0);
            }
            case UiKeyTable.LevelUpProfession:
            {
                bool inactive = !after.MenuActive, removed = !after.LevelPending,
                    granted = after.ProfessionCount == b.ProfessionCount + 1;
                if (inactive && removed && granted) { this.syncApplied = true; return null; }
                this.done = true;
                return inactive || removed || after.ProfessionCount != b.ProfessionCount
                    ? this.Finish(UiKeyVerdict.OutcomeUncertain, after, null, "partial_sync_proof", 0)
                    : this.Finish(UiKeyVerdict.OutcomeNoEffect, after, null, "no_sync_proof", 0);
            }
            case UiKeyTable.ShippingSummary:
            {
                this.done = true;
                if (this.transitions > 0 || after.ReceiverToken != b.ReceiverToken)
                    return this.Finish(UiKeyVerdict.OutcomeUncertain, after, null, "unexpected_receiver_change", 0);
                return b.CanReceiveInput && !after.CanReceiveInput
                    ? this.Finish(UiKeyVerdict.OutcomeApplied, after, "shipping_confirmed", "outro_started", 0)
                    : this.Finish(UiKeyVerdict.OutcomeNoEffect, after, null, "no_sync_proof", 0);
            }
            default: // letter
            {
                this.done = true;
                if (after.ReceiverToken == b.ReceiverToken)
                    return this.Finish(UiKeyVerdict.OutcomeNoEffect, after, null, "receiver_unchanged", 0);
                if (this.transitions < 1)
                    return this.Finish(UiKeyVerdict.OutcomeUncertain, after, null, "transition_unobserved", 0);
                return this.Finish(UiKeyVerdict.OutcomeApplied, after, after.ReceiverToken is null ? "closed" : "replaced",
                    "receiver_changed", 0);
            }
        }
    }

    /// <summary>One settle probe (null = identity lost). Returns the verdict once, then null forever.</summary>
    public UiKeyVerdict? Observe(UiKeyProbe? probe, long elapsedMs)
    {
        if (!this.invoked || this.done) return null;
        UiKeyTarget t = this.target!;
        UiKeyProbe b = this.before!;
        if (this.syncApplied)
        {
            // Success already rests on the synchronous proof; only report the receiver change.
            string effect = t.Kind == UiKeyTable.LevelUp ? "level_up_confirmed" : "profession_chosen";
            if (probe is not null && probe.ReceiverToken != b.ReceiverToken)
            { this.done = true; return this.Finish(UiKeyVerdict.OutcomeApplied, probe, effect, "receiver_changed", elapsedMs); }
            if (elapsedMs >= SyncReceiverWaitMs)
            { this.done = true; return this.Finish(UiKeyVerdict.OutcomeApplied, probe, effect, "receiver_wait_elapsed", elapsedMs, settled: false); }
            return null;
        }
        // Vanilla TV closes the opening box before its owned callback opens the
        // forecast. Only that exact two-edge chain may verify a replacement.
        bool tvAdvance = t.Kind == UiKeyTable.Dialogue && t.Key == UiKeyTable.Advance
            && b.TvDialogueChain is not null && probe?.TvDialogueChain == b.TvDialogueChain
            && probe.MenuType == "DialogueBox" && probe.ReceiverToken is not null
            && probe.ReceiverToken != b.ReceiverToken && this.transitions == 2
            && this.firstTransitionFrom == b.ReceiverToken && this.firstTransitionTo is null
            && this.secondTransitionFrom is null && this.secondTransitionTo == probe.ReceiverToken;
        if (tvAdvance)
        { this.done = true; return this.Finish(UiKeyVerdict.OutcomeApplied, probe, "replaced", "owned_tv_advance", elapsedMs); }
        if (this.transitions > 1)
        { this.done = true; return this.Finish(UiKeyVerdict.OutcomeUncertain, probe, null, "multiple_transitions", elapsedMs); }
        if (probe is null)
        { this.done = true; return this.Finish(UiKeyVerdict.OutcomeUncertain, null, null, "identity_lost", elapsedMs); }
        bool question = t.Kind is UiKeyTable.Question or UiKeyTable.NpcQuestion;
        string? effect2 = probe.ReceiverToken is null ? "closed"
            : probe.ReceiverToken != b.ReceiverToken ? "replaced"
            : probe.KindFacts != b.KindFacts ? (question ? "answered" : "advanced")
            : null;
        if (effect2 is not null)
        {
            this.done = true;
            if (effect2 is "closed" or "replaced" && this.transitions != 1)
                return this.Finish(UiKeyVerdict.OutcomeUncertain, probe, null, "transition_unobserved", elapsedMs);
            return this.Finish(UiKeyVerdict.OutcomeApplied, probe, effect2, "proof", elapsedMs);
        }
        if (elapsedMs >= AsyncSettleMs)
        {
            this.done = true;
            return this.transitions == 0
                ? this.Finish(UiKeyVerdict.OutcomeNoEffect, probe, null, "deadline_no_proof", elapsedMs, settled: false)
                : this.Finish(UiKeyVerdict.OutcomeUncertain, probe, null, "deadline_after_transition", elapsedMs, settled: false);
        }
        return null;
    }

    UiKeyVerdict Finish(string outcome, UiKeyProbe? probe, string? effect, string reason, long elapsedMs, bool settled = true)
    {
        string transition = this.transitions == 0 ? "NoReceiverTransition"
            : (this.transitions == 1 || reason == "owned_tv_advance") && this.firstTransitionFrom == this.before?.ReceiverToken ? "ExpectedReceiverTransition"
            : "UnverifiedReceiverTransition";
        bool applied = outcome == UiKeyVerdict.OutcomeApplied;
        return new UiKeyVerdict(outcome, applied, effect, this.invoked ? 1 : 0, settled && outcome != UiKeyVerdict.OutcomeUncertain,
            transition, probe?.ReceiverToken, probe?.MenuType,
            applied && this.target?.Kind == UiKeyTable.LevelUpProfession ? this.target.ProfessionId : null, reason, elapsedMs);
    }
}

/// <summary>Busy composition shared by ModEntry admission and the hold-runtime harness.</summary>
public static class UiKeyRoute
{
    /// <summary>A ui_key is refused while any owned input (background hold/press, menu debt, other input) is outstanding.</summary>
    public static bool Busy(bool otherInputBusy, bool pressOverrideOutstanding, bool menuDebtActive, bool backgroundBusy) =>
        otherInputBusy || pressOverrideOutstanding || menuDebtActive || backgroundBusy;

    /// <summary>Admission gate term. Deliberately has no window-activity (IsActive) term.</summary>
    public static bool Ready(bool configEnabled, bool inboxEnabled, bool uiKeyInputEnabled, bool observerReady,
        bool isMainPlayer, bool isSplitScreen, bool worldReady, bool textSubscriber) =>
        configEnabled && inboxEnabled && uiKeyInputEnabled && observerReady
        && !isMainPlayer && !isSplitScreen && worldReady && !textSubscriber;
}
