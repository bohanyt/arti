"""Quick test: Apakah YouTube innertube API bisa baca live chat?"""
import requests
import re
import json
import time
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

VIDEO_ID = "dQw4w9WgXcQ"  # placeholder — replace with YOUR_VIDEO_ID for live chat tests

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})
# Bypass consent page
session.cookies.set("CONSENT", "YES+cb.20240101-01-p0.en+FX+001", domain=".youtube.com")
session.cookies.set("SOCS", "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMTAxLjAxX3AwGgJlbiACGgYIgLCdsgY", domain=".youtube.com")

print(f"[1] Fetching live_chat page for video: {VIDEO_ID}")
try:
    resp = session.get(f"https://www.youtube.com/live_chat?v={VIDEO_ID}&is_popout=1", timeout=15)
    print(f"    Status: {resp.status_code}")
    print(f"    URL: {resp.url}")
    print(f"    Content length: {len(resp.text)}")
    
    # Check if consent page
    if "consent.youtube.com" in resp.url or "consent.google" in resp.url:
        print("    ❌ REDIRECTED TO CONSENT PAGE!")
    
    # Find ytInitialData
    match = re.search(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*(\{.*?\});', resp.text, re.DOTALL)
    if not match:
        # Try simpler pattern
        all_conts = re.findall(r'"continuation":"([^"]{20,})"', resp.text)
        print(f"    Continuation tokens found (regex): {len(all_conts)}")
        if all_conts:
            continuation = all_conts[0]
            print(f"    ✅ Using first token: {continuation[:50]}...")
        else:
            print("    ❌ No continuation tokens found!")
            # Show snippet of page
            print(f"    Page snippet (first 500 chars): {resp.text[:500]}")
            exit(1)
    else:
        data = json.loads(match.group(1))
        print(f"    ✅ ytInitialData parsed!")
        
        # Find continuation in parsed data
        def find_cont(obj, depth=0):
            if depth > 10: return None
            if not obj or not isinstance(obj, (dict, list)): return None
            if isinstance(obj, dict):
                if 'continuation' in obj and isinstance(obj['continuation'], str) and len(obj['continuation']) > 20:
                    return obj['continuation']
                for v in obj.values():
                    r = find_cont(v, depth+1)
                    if r: return r
            elif isinstance(obj, list):
                for item in obj:
                    r = find_cont(item, depth+1)
                    if r: return r
            return None
        
        continuation = find_cont(data)
        if continuation:
            print(f"    ✅ Continuation: {continuation[:50]}...")
        else:
            print("    ❌ No continuation in parsed data")
            exit(1)
        
        # Parse initial messages
        actions = []
        try:
            actions = data['contents']['liveChatRenderer']['actions']
        except:
            pass
        print(f"    Initial actions: {len(actions)}")
        for a in actions[:5]:
            item = a.get('addChatItemAction', {}).get('item', {})
            r = item.get('liveChatTextMessageRenderer', {})
            if r:
                author = r.get('authorName', {}).get('simpleText', '?')
                runs = r.get('message', {}).get('runs', [])
                msg = ''.join(run.get('text', '') for run in runs)
                print(f"    💬 {author}: {msg}")
    
    # Step 2: Poll innertube API
    print(f"\n[2] Polling innertube API...")
    api_url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240101.00.00"
            }
        },
        "continuation": continuation
    }
    
    resp2 = session.post(api_url, json=payload, timeout=15)
    print(f"    Status: {resp2.status_code}")
    
    data2 = resp2.json()
    
    # Check for errors
    if "error" in data2:
        print(f"    ❌ API Error: {data2['error']}")
    else:
        # Parse continuation
        conts = data2.get("continuationContents", {}).get("liveChatContinuation", {}).get("continuations", [])
        print(f"    Continuations: {len(conts)}")
        
        next_cont = None
        for c in conts:
            if "invalidationContinuationData" in c:
                next_cont = c["invalidationContinuationData"].get("continuation")
                timeout = c["invalidationContinuationData"].get("timeoutMs", "?")
                print(f"    ✅ Next continuation (invalidation, timeout={timeout}ms)")
            elif "timedContinuationData" in c:
                next_cont = c["timedContinuationData"].get("continuation")
                timeout = c["timedContinuationData"].get("timeoutMs", "?")
                print(f"    ✅ Next continuation (timed, timeout={timeout}ms)")
        
        actions = data2.get("continuationContents", {}).get("liveChatContinuation", {}).get("actions", [])
        print(f"    Actions (new messages): {len(actions)}")
        
        for a in actions[:10]:
            item = a.get('addChatItemAction', {}).get('item', {})
            r = item.get('liveChatTextMessageRenderer', {})
            if r:
                author = r.get('authorName', {}).get('simpleText', '?')
                runs = r.get('message', {}).get('runs', [])
                msg = ''.join(run.get('text', '') for run in runs)
                print(f"    💬 {author}: {msg}")
    
    print("\n✅ Test selesai!")
    
except Exception as e:
    print(f"    ❌ Error: {e}")
    import traceback
    traceback.print_exc()
