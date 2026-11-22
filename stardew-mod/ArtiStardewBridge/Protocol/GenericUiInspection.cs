using System.Collections;
using System.Collections.Concurrent;
using System.Reflection;

namespace ArtiStardewBridge.Protocol;

// Reflection is structural: fields only; no getter, hover, callback, snap or writes.
public static class GenericUiInspection
{
    // A component's visible flag alone is insufficient: native menus can gate
    // drawing/clicking with show<Field>(). Only the adapter may trust a native
    // visibility predicate. Never call arbitrary mod callbacks or menu.readyToClose.
    public static bool NativeVisible(object owner, string field, bool visible, Func<MethodInfo, bool> trusted)
    {
        if (!visible || string.IsNullOrEmpty(field)) return visible;
        string name = "show" + char.ToUpperInvariant(field[0]) + field[1..];
        var predicate = owner.GetType().GetMethod(name, BindingFlags.Public | BindingFlags.Instance,
            null, Type.EmptyTypes, null);
        if (predicate?.ReturnType != typeof(bool) || !trusted(predicate)) return visible;
        try { return predicate.Invoke(owner, null) is true; }
        catch { return false; } // unreadable native gate is not an offered press
    }

    static readonly ConcurrentDictionary<Type, FieldInfo[]> Cache = new();
    public static IReadOnlyList<FieldInfo> Fields(Type type) => Cache.GetOrAdd(type, t =>
    {
        var fields = new List<FieldInfo>();
        for (Type? current = t; current is not null && current != typeof(object); current = current.BaseType)
            fields.AddRange(current.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
                | BindingFlags.DeclaredOnly).Where(f => !f.IsStatic).OrderBy(f => f.Name, StringComparer.Ordinal));
        return fields.ToArray();
    });

    public static bool Walk(IEnumerable<(string Path, object Value)> roots, Func<object, bool> expand,
        Action<object, string> inspect, int maxDepth = 6, int maxNodes = 8192, int maxCollection = 512)
    {
        var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);
        bool complete = true;
        int nodes = 0;
        void Read(object? value, string path, int depth)
        {
            if (value is null) return;
            if (++nodes > maxNodes) { complete = false; return; }
            if (value is string || value.GetType().IsValueType) { inspect(value, path); return; }
            if (!seen.Add(value)) return;
            inspect(value, path);
            if (value is IDictionary dictionary)
            {
                if (depth >= maxDepth) { complete = false; return; }
                int i = 0;
                foreach (DictionaryEntry entry in dictionary)
                {
                    if (i >= maxCollection) { complete = false; break; }
                    Read(entry.Key, path + ".key[" + i + "]", depth + 1);
                    Read(entry.Value, path + ".value[" + i++ + "]", depth + 1);
                }
                return;
            }
            if (value is IEnumerable list)
            {
                if (depth >= maxDepth) { complete = false; return; }
                int i = 0;
                foreach (object? item in list)
                {
                    if (i >= maxCollection) { complete = false; break; }
                    Read(item, path + "[" + i++ + "]", depth + 1);
                }
                return;
            }
            if (!expand(value)) return;
            if (depth >= maxDepth) { complete = false; return; }
            foreach (FieldInfo field in Fields(value.GetType())) Read(field.GetValue(value), path + "." + field.Name, depth + 1);
        }
        foreach (var root in roots) Read(root.Value, root.Path, 0);
        return complete;
    }
}
