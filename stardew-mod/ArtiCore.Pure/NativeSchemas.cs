using System.Text.Json.Nodes;
using Json.Schema;

namespace ArtiCore.Pure;

public static class NativeSchemas
{
    static readonly Dictionary<string, JsonSchema> schemas = new();
    static readonly EvaluationOptions options = new();
    static NativeSchemas()
    {
        foreach (string name in new[] { "common", "halt", "stop", "rearm", "verb", "verb_phases", "session", "occurrence_ring", "observation" }) {
            using var stream = typeof(NativeSchemas).Assembly.GetManifestResourceStream(name + ".schema.json")!;
            var schema = JsonSchema.FromText(new StreamReader(stream).ReadToEnd());
            schemas.Add(name, schema);
            options.SchemaRegistry.Register(schema);
        }
        schemas.Add("night", JsonSchema.FromText("{\"$ref\":\"arti:contracts/observation.schema.json#/$defs/Night\"}"));
        schemas.Add("occurrence", JsonSchema.FromText("{\"$ref\":\"arti:contracts/occurrence_ring.schema.json#/$defs/Record\"}"));
        options.SchemaRegistry.Fetch = uri => schemas[uri.OriginalString.Split('#')[0].Split('/').Last().Split(':').Last().Split('.')[0]];
    }
    public static bool Valid(string name, JsonNode value)
    {
        lock (schemas) return schemas[name].Evaluate(value, options).IsValid;
    }
}
