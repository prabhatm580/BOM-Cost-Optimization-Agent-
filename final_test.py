#!/usr/bin/env python3
import urllib.request, json, time

# Route from CF
ROUTE = "[REDACTED]
AGENT = "https://" + ROUTE + "/"

def ask(mid, q):
    p = json.dumps({
        "jsonrpc": "2.0", "id": mid, "method": "message/send",
        "params": {"message": {"messageId": mid, "role": "user",
                               "parts": [{"kind": "text", "text": q}]}}
    }).encode()
    req = urllib.request.Request(AGENT, data=p,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        b = json.loads(r.read())
        arts = b.get("result", {}).get("artifacts", [])
        return arts[0].get("parts", [{}])[0].get("text", "") if arts else str(b)[:200]

tests = [
    ("t1", "Get procurement type for F-10A at plant 1010. Raw JSON."),
    ("t2", "Get purchase info records for F-10A. Raw JSON."),
    ("t3", "Get PO history for F-10A at plant 1010. Raw JSON."),
    ("t4", "Get material valuation for F-10A at plant 1010. Raw JSON."),
    ("t5", "Compare BOM for F-10A between plants 1010 and 1710. Raw JSON."),
]
for tid, q in tests:
    print(f"=== {tid} ===")
    print(ask(f"{tid}_final", q)[:350])
    print()
    time.sleep(2)
