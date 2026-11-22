using System.Text;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Revision monotonic untuk UI yang TERLIHAT. Sequence transport boleh naik
/// tanpa perubahan UI; revision ini hanya naik saat fingerprint berubah.
/// </summary>
public sealed class UiRevisionTracker
{
    private string? previous;
    private bool initialized;

    public long Current { get; private set; }

    public long Observe(MenuInfo? menu)
    {
        string fingerprint = Fingerprint(menu);
        if (!this.initialized || !string.Equals(this.previous, fingerprint, StringComparison.Ordinal))
        {
            this.initialized = true;
            this.previous = fingerprint;
            this.Current++;
        }
        return this.Current;
    }

    public void Reset()
    {
        this.previous = null;
        this.initialized = false;
        this.Current = 0;
    }

    private static string Fingerprint(MenuInfo? menu)
    {
        if (menu is null)
            return "<no-menu>";

        StringBuilder text = new();
        Add(text, menu.MenuType);
        Add(text, menu.PageType);
        Add(text, menu.SnapId);
        Add(text, menu.FocusKey);
        Add(text, menu.VisibleText);
        Add(text, menu.Speaker);
        Add(text, menu.CloseId);
        foreach (UiComponentInfo component in menu.Components)
        {
            Add(text, component.Key);
            Add(text, component.NativeId);
            Add(text, component.Source);
            Add(text, component.Role);
            Add(text, component.Name);
            Add(text, component.Label);
            Add(text, component.Bounds?.X);
            Add(text, component.Bounds?.Y);
            Add(text, component.Bounds?.W);
            Add(text, component.Bounds?.H);
            Add(text, component.Neighbors.Up);
            Add(text, component.Neighbors.Right);
            Add(text, component.Neighbors.Down);
            Add(text, component.Neighbors.Left);
            Add(text, component.Text);
            Add(text, component.Count);
            Add(text, component.Selected ? 1 : 0);
        }
        return text.ToString();
    }

    private static void Add(StringBuilder target, string? value)
    {
        target.Append(value?.Length ?? -1).Append(':').Append(value).Append('|');
    }

    private static void Add(StringBuilder target, int? value)
    {
        target.Append(value?.ToString() ?? "null").Append('|');
    }
}
