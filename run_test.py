import urllib.request, json, time

AGENT = "[REDACTED]

def ask(mid, q):
    p = json.dumps({"jsonrpc":"2.0","id":mid,"method":"message/send","params":{"message":{"messageId":mid,"role":"user","parts":[{"kind":"text","text":q}]}}}).encode()
    req = urllib.request.Request(AGENT, data=p, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        b = json.loads(r.read())
        arts = b.get("result", {}).get("artifacts", [])
        return arts[0].get("parts", [{}])[0].get("text", "") if arts else str(b)[:200]

print("=== BOM CROSS-PLANT ===")
print(ask("bom_v3", "Compare BOM for F-10A between plants 1010 and 1710. Raw JSON only.")[:800])
time.sleep(2)
print()
print("=== INFO RECORDS ===")
print(ask("pir_v3", "Get info records for material F-10A. Raw JSON only.")[:400])
