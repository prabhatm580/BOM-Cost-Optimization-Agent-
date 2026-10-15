#!/usr/bin/env python3
import urllib.request, json, sys

APP_ROUTE = "[REDACTED]"
url = f"https://{APP_ROUTE}/"

payload = json.dumps({
    "jsonrpc": "2.0",
    "id": "smoke-1",
    "method": "message/send",
    "params": {
        "message": {
            "role": "user",
            "parts": [{"type": "text", "text": "ping"}]
        }
    }
}).encode()

req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode()
        print("STATUS:", resp.status)
        data = json.loads(body)
        print(json.dumps(data, indent=2))
        # Check for state_modifier error
        result_str = json.dumps(data)
        if "state_modifier" in result_str:
            print("\n❌ FAIL: 'state_modifier' error still present")
            sys.exit(1)
        elif "error" in result_str.lower():
            print("\n⚠️  Got an error (but not state_modifier) — check above")
        else:
            print("\n✅ PASS: No state_modifier error")
except urllib.error.HTTPError as e:
    print("HTTP ERROR:", e.code, e.reason)
    print(e.read().decode())
except Exception as e:
    print("ERROR:", type(e).__name__, e)
    sys.exit(1)
