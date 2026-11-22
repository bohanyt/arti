namespace ArtiStardewBridge.Protocol;

/// <summary>
/// PM-1 fix8 (runbooks/CONTROLLER_CONTRACT_v1.md): the receiver-state terms of the ui_press / ui_nav arm. ExecuteUiPad
/// arms with <see cref="Early"/> + its request binding + <see cref="Late"/>; the published affordances are the dry run
/// of exactly these terms over every candidate control, so native is the single affordance source and a refusal of a
/// published control is a DEFECT (never a planner fault).
/// </summary>
public readonly record struct UiArmGate(bool BackendReady, bool Busy, bool Stopping, bool Latched, bool EconomyReady,
    string? Debt = null)
{
    public string? Early() => this.Debt ?? (!this.BackendReady ? "UiPadNotReady" : this.Busy ? "UiPadBusy"
        : this.Stopping ? "UiPadNotReady" : null);

    public string? Late(UiPadFacts f, UiPadPlan? plan, string? navDir) => this.Latched ? "UiPadNotReady"
        : navDir is not null ? UiPadContextTable.NavRefusal(f, navDir)
        : plan is { IsEconomy: true } && !this.EconomyReady ? "UiPadNotReady"
        : plan is null ? "UiPadUnsupported" : plan.Refusal;

    public string? Refusal(UiPadFacts f, UiPadPlan? plan, string? navDir) => this.Early() ?? this.Late(f, plan, navDir);
}

public static class UiAffordances
{
    static readonly string[] Buttons = { UiPadTable.BtnA, UiPadTable.BtnB, UiPadTable.BtnX, UiPadTable.BtnY, UiPadTable.BtnLB,
        UiPadTable.BtnRB, UiPadTable.BtnLT, UiPadTable.BtnRT, UiPadTable.BtnStart, UiPadTable.BtnSelect };

    /// <summary>Contract role of an admitted plan, from native's own step/does vocabulary.</summary>
    static string? Role(UiPadPlan plan)
    {
        string? op = plan.Steps.Count == 1 ? plan.Steps[0].G3Action : null;
        string does = plan.Does.ToLowerInvariant();
        return op is "close" or "key_cancel" || does.Contains("close") ? "exit"
            : op == "key_confirm" || does.Contains("advance") || does.StartsWith("ok") ? "advance" : null;
    }

    /// <summary>The observed OK of a ready advance-only page, from native safety (ui_safety.advance_id); -1 if none.</summary>
    static int AdvanceId(UiPadFacts f)
    {
        if (f.GenericValues?.GetValueOrDefault("ui_safety") is not IReadOnlyDictionary<string, object> s
            || s.GetValueOrDefault("state") as string is not ("ADVANCE_ONLY" or "NIGHT_SUMMARY")) return -1;
        return s.GetValueOrDefault("advance_id") is int id ? id : -1;
    }

    /// <summary>
    /// The affordances block: every (verb, args) the arm admits now. An open menu always names its exit / advance /
    /// answer / decline, or "none" with the reason. At most one pending decision (the top receiver's question).
    /// <paramref name="extra"/> = container choices (exit / transfer) admitted by their own arm predicates.
    /// </summary>
    public static Dictionary<string, object> Publish(UiPadFacts f, UiArmGate gate, IEnumerable<Dictionary<string, object>>? extra = null)
    {
        string context = UiPadContextTable.ContextOf(f);
        var list = new List<Dictionary<string, object>>();
        string? first = null, exitRefused = null;
        UiPadPlan Plan(string button, IReadOnlyDictionary<string, string>? reserved, out string? why)
        {
            var plan = UiPadContextTable.AdmitPress(new UiPressTarget(f.ReceiverToken ?? "", f.MenuType, context, "", button,
                f.SnappedId ?? -1, Reserved: reserved), f);
            why = gate.Refusal(f, plan, null);
            first ??= why;
            return plan;
        }
        foreach (string button in Buttons)
        {
            var plan = Plan(button, null, out string? why);
            if (why is not null) { if (button == UiPadTable.BtnB) exitRefused = why; continue; }
            var entry = new Dictionary<string, object> { ["verb"] = plan.Steps.Count == 1 && plan.Steps[0].G3Action == "scroll"
                ? "scroll" : "choose", ["control"] = button };
            if (Role(plan) is { } role) entry["role"] = role;
            list.Add(entry);
        }
        // Component choices (choice=id): grouped by the exact admitted variant set, so the wire stays bounded.
        var groups = new Dictionary<string, List<int>>(StringComparer.Ordinal);
        var typed = new List<int>();
        var options = f.UiKey is { Kind: UiKeyTable.Question, Options: { Count: > 0 } o } ? o : null;
        int advanceId = AdvanceId(f);
        foreach (var c in f.GenericMenu ? f.Comps.Where(c => c.Visible) : Enumerable.Empty<UiPadComponent>())
        {
            var variants = new List<string>();
            foreach (string button in new[] { UiPadTable.BtnA, UiPadTable.BtnX })
                foreach (bool shift in new[] { false, true })
                {
                    var reserved = new Dictionary<string, string>(StringComparer.Ordinal) { ["choice"] = c.Id.ToString() };
                    if (shift) reserved["shift"] = "true";
                    Plan(button, reserved, out string? why);
                    if (why is null) variants.Add((shift ? "shift" : "") + button);
                }
            if (f.CursorSettle?.Cursor is not null)
            {   // settle the cursor item into this observed destination (GenericUiPolicy settle payload)
                Plan(UiPadTable.BtnA, new Dictionary<string, string>(StringComparer.Ordinal) { ["choice"] = c.Id.ToString(), ["settle"] = "true" },
                    out string? settleWhy);
                if (settleWhy is null) variants.Add("settle");
            }
            if (c.Kind == "text_field")
            {
                Plan(UiPadTable.BtnY, new Dictionary<string, string>(StringComparer.Ordinal) { ["choice"] = c.Id.ToString(), ["text"] = "a" },
                    out string? typeWhy);
                if (typeWhy is null) typed.Add(c.Id);
            }
            if (variants.Count == 0) continue;
            if (f.MenuType == "ReadyCheckDialog" && c.Kind == "close" && variants.Contains(UiPadTable.BtnA))
            {
                list.Add(new() { ["verb"] = "choose", ["control"] = c.Id, ["button"] = "A", ["role"] = "exit" });
                continue;
            }
            if (c.Id == advanceId && variants.Contains(UiPadTable.BtnA))
            {   // native safety's observed advance (single OK / night summary OK): the page's own way on
                list.Add(new() { ["verb"] = "choose", ["control"] = c.Id, ["button"] = UiPadTable.BtnA, ["role"] = "advance" });
                continue;
            }
            if (options is not null && c.Id >= 0 && c.Id < options.Count && variants.Contains(UiPadTable.BtnA))
            {   // a question response is answered, nothing else (the ui_press wire schema admits only A here);
                // the game's own answer key decides answer vs decline
                list.Add(new() { ["verb"] = "choose", ["control"] = c.Id, ["button"] = UiPadTable.BtnA,
                    ["role"] = options[c.Id].ResponseKey == "No" ? "decline" : "answer", ["response_key"] = options[c.Id].ResponseKey });
                continue;
            }
            string key = string.Join(" ", variants);
            (groups.TryGetValue(key, out var ids) ? ids : groups[key] = new()).Add(c.Id);
        }
        foreach (var (key, ids) in groups)
            list.Add(new() { ["verb"] = "choose", ["controls"] = ids.ToArray(), ["buttons"] = key.Split(' ') });
        if (typed.Count > 0)
            list.Add(new() { ["verb"] = "type", ["controls"] = typed.ToArray() });
        // Pinned question (TV / gift / services, non-generic): one choice per admissible response index.
        if (!f.GenericMenu && options is not null && context == UiPadTable.CtxQuestion)
            for (int i = 0; i < options.Count; i++)
            {
                Plan(UiPadTable.BtnA, new Dictionary<string, string>(StringComparer.Ordinal) { ["choice"] = i.ToString() }, out string? why);
                if (why is null)
                    list.Add(new() { ["verb"] = "choose", ["control"] = i, ["button"] = UiPadTable.BtnA,
                        ["role"] = options[i].ResponseKey == "No" ? "decline" : "answer", ["response_key"] = options[i].ResponseKey });
            }
        if (!f.GenericMenu)
        {
            var dirs = UiPadTable.Directions.Where(d => gate.Refusal(f, null, d) is null).ToArray();
            if (dirs.Length > 0) list.Add(new() { ["verb"] = "choose", ["control"] = "stick", ["dirs"] = dirs });
        }
        if (f.ShopCatalog is { } catalog)
            foreach (var row in catalog.Rows.Where(r => r.Control is not null))
                foreach (int quantity in new[] { 1, 5, 25 })
                {
                    Plan("A", new Dictionary<string, string> { ["choice"] = row.Control!.Value.ToString(), ["qty"] = quantity.ToString() }, out string? why);
                    if (why is null) list.Add(new() { ["verb"] = "choose", ["control"] = row.Control!.Value,
                        ["button"] = "A", ["quantity"] = quantity, ["role"] = "buy" });
                }
        if (extra is not null) list.AddRange(extra);
        var block = new Dictionary<string, object> { ["v"] = 1, ["list"] = list.ToArray() };
        int exit = list.FindIndex(e => e.GetValueOrDefault("role") is "exit" or "advance" or "decline" or "answer");
        if (exit < 0)
            block["none"] = exitRefused ?? first ?? "no_admissible_control";
        var choices = Enumerable.Range(0, list.Count).Where(i => list[i].GetValueOrDefault("role") is "answer" or "decline").ToArray();
        string? kind = f.UiKey?.Kind is UiKeyTable.Question or UiKeyTable.NpcQuestion or UiKeyTable.Dialogue or UiKeyTable.LevelUp
            or UiKeyTable.LevelUpProfession ? f.UiKey.Kind : context == UiPadTable.CtxReadyCheck ? context : choices.Length > 0 ? "question" : null;
        if (kind is not null)
        {   // the controller re-asks on its own bounded budget; "decline" is the game's own decline / close / advance
            var pending = new Dictionary<string, object> { ["kind"] = kind, ["receiver_token"] = f.ReceiverToken ?? "",
                ["choices"] = choices };
            int decline = list.FindIndex(e => e.GetValueOrDefault("role") is "decline");
            if (decline < 0) decline = list.FindIndex(e => e.GetValueOrDefault("role") is "exit" or "advance");
            if (decline >= 0) pending["decline"] = decline;
            block["pending"] = pending;
        }
        return block;
    }
}
