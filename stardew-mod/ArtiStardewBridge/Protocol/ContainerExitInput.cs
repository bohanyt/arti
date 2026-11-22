using System.Text.Json;
using System.Security.Cryptography;
using System.Text;

namespace ArtiStardewBridge.Protocol;

public sealed record ContainerExitTarget(string ReceiverToken,string ContainerToken,string SourceFactId,
    string ReadbackToken,string TargetToken)
{
    public const string Primitive="container_exit";
    public bool IsValid => new[]{ReceiverToken,ContainerToken,ReadbackToken,TargetToken}
        .All(s=>!string.IsNullOrWhiteSpace(s)&&s.Length<=128)
        && !string.IsNullOrWhiteSpace(SourceFactId)&&SourceFactId.Length<=256;
    public Dictionary<string,object> Args()=>new(){["receiver_token"]=ReceiverToken,["container_token"]=ContainerToken,
        ["source_fact_id"]=SourceFactId,["readback_token"]=ReadbackToken,["target_token"]=TargetToken};
    public static bool TryParse(JsonElement args,out ContainerExitTarget? target)
    {
        target=null;
        string[] keys={"receiver_token","container_token","source_fact_id","readback_token","target_token"};
        if(args.ValueKind!=JsonValueKind.Object || args.EnumerateObject().Count()!=keys.Length
            || args.EnumerateObject().Select(p=>p.Name).Distinct().Count()!=keys.Length
            || args.EnumerateObject().Any(p=>!keys.Contains(p.Name)||p.Value.ValueKind!=JsonValueKind.String))return false;
        var values=keys.Select(k=>args.GetProperty(k).GetString()!).ToArray();
        target=new(values[0],values[1],values[2],values[3],values[4]);return target.IsValid;
    }
    public static ContainerExitTarget? Read(JsonElement container)
    {
        if(container.ValueKind!=JsonValueKind.Object || !container.TryGetProperty("available",out var available)
            || available.ValueKind!=JsonValueKind.True || !container.TryGetProperty("close_target",out var close)
            || close.ValueKind!=JsonValueKind.Object || close.GetProperty("kind").GetString()!="ordinary_chest_close")return null;
        var target=new ContainerExitTarget(container.GetProperty("receiver_token").GetString()!,
            container.GetProperty("container_token").GetString()!,container.GetProperty("source_fact_id").GetString()!,
            container.GetProperty("readback_token").GetString()!,close.GetProperty("target_token").GetString()!);
        return target.IsValid?target:null;
    }
}

// Per-request immutable readbacks, through the existing owner/inbox. Not another store.
public sealed record ContainerExitReadback(string? ReceiverToken,long Generation,string ActorContents,
    string ContainerContents,int ActiveSlot,bool CursorClear,bool WorldFree)
{
    public static string? Contents(IEnumerable<PresentedItem?> items,int maximum)
    {
        var bounded=items.Take(maximum+1).ToArray();
        if(bounded.Length>maximum || bounded.Any(i=>i is {IsValid:false}))return null;
        var content=bounded.Where(i=>i is not null).Select(i=>i!).GroupBy(i=>(i.QualifiedItemId,i.Quality))
            .OrderBy(g=>g.Key.QualifiedItemId,StringComparer.Ordinal).ThenBy(g=>g.Key.Quality)
            .Select(g=>new object[]{g.Key.QualifiedItemId,g.Key.Quality,g.Sum(i=>(long)i.Stack)}).ToArray();
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(content)))).ToLowerInvariant();
    }
}
public sealed record ContainerExitVerdict(string Outcome,bool Exited,bool CleanupConfirmed,int ClickCount,
    bool ExpectedTransition,long? ResultContextGeneration);

public sealed class ContainerExitInput
{
    bool consumed,duringClick,fault,unexpected;
    int transitions;
    string? receiver;
    public void ObserveReceiverTransition(string? from,string? to,bool originalIdentity,bool succeeded)
    {
        transitions++;
        if(!duringClick || transitions!=1 || from!=receiver || to is not null || !originalIdentity || !succeeded)
            unexpected=true;
    }
    public ContainerExitVerdict Execute(ContainerExitTarget wanted,ContainerExitTarget? current,
        ContainerExitReadback before,bool eligible,Action click,Action release,Func<ContainerExitReadback?> read)
    {
        if(consumed)return new("ExitRejected",false,true,0,false,null);
        consumed=true;
        if(!eligible || !wanted.IsValid || wanted!=current || before.ReceiverToken!=wanted.ReceiverToken
            || !before.CursorClear || before.ActiveSlot<0 || before.ActorContents.Length!=64
            || before.ContainerContents.Length!=64)return new("ExitRejected",false,true,0,false,null);
        receiver=wanted.ReceiverToken;
        bool clean=false;ContainerExitReadback? first=null,last=null;
        try {
            duringClick=true;
            try{click();}finally{duringClick=false;}
            first=read();
        }catch{fault=true;}
        finally{try{release();clean=true;}catch{fault=true;}}
        try{last=read();}catch{fault=true;}
        bool transition=!unexpected && transitions==1 && first is not null && first==last
            && first.ReceiverToken is null && first.Generation==before.Generation+1 && first.WorldFree;
        bool exited=transition&&!fault&&clean&&first!.CursorClear&&first.ActiveSlot==before.ActiveSlot
            && first.ActorContents==before.ActorContents&&first.ContainerContents==before.ContainerContents;
        return new(exited?"MenuExited":unexpected||transitions!=1?"ExitTransitionUnexpected":"ExitUncertain",
            exited,clean,1,transition,last?.Generation);
    }
}
