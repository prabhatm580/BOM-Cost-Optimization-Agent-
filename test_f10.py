import urllib.request, json, time, sys

# Read agent URL from run_test.py
with open("/home/user/project/assets/bom-cost-optimization-agent/run_test.py") as f:
    for line in f:
        if line.startswith("AGENT"):
            AGENT = line.split('"')[1]
            break

print(f"Agent: {AGENT[:60]}...")

def ask(mid, q, timeout=120):
    p = json.dumps({
        "jsonrpc": "2.0",
        "id": mid,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": mid,
                "role": "user",
                "parts": [{"kind": "text", "text": q}]
            }
        }
    }).encode()
    req = urllib.request.Request(
        AGENT, data=p,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = json.loads(r.read())
        arts = b.get("result", {}).get("artifacts", [])
        if arts:
            return arts[0].get("parts", [{}])[0].get("text", "")
        return str(b)[:400]

MATERIAL = "F-10"

print("\n" + "="*60)
print(f"BOM COST OPTIMIZATION ANALYSIS")
print(f"Material: {MATERIAL} | Plants: 1010 & 1710")
print("="*60)

# Test 1: Procurement Type
print("\n[1/5] PROCUREMENT TYPE")
print("-"*40)
r1 = ask("t1_f10", f"What is the procurement type for material {MATERIAL} at plant 1010? Return raw JSON only.")
print(r1[:600])
time.sleep(3)

# Test 2: Purchase Info Records
print("\n[2/5] PURCHASE INFO RECORDS")
print("-"*40)
r2 = ask("t2_f10", f"Get purchase info records for material {MATERIAL}. Return raw JSON only.")
print(r2[:600])
time.sleep(3)

# Test 3: PO History
print("\n[3/5] PO HISTORY")
print("-"*40)
r3 = ask("t3_f10", f"Get purchase order history for material {MATERIAL} at plant 1010. Return raw JSON only.")
print(r3[:600])
time.sleep(3)

# Test 4: Material Valuation
print("\n[4/5] MATERIAL VALUATION")
print("-"*40)
r4 = ask("t4_f10", f"Get material valuation data for {MATERIAL} at plant 1010. Return raw JSON only.")
print(r4[:600])
time.sleep(3)

# Test 5: BOM Cross-Plant
print("\n[5/5] BOM CROSS-PLANT COMPARISON")
print("-"*40)
r5 = ask("t5_f10", f"Compare BOM for material {MATERIAL} between plants 1010 and 1710. Return raw JSON only.")
print(r5[:800])

print("\n" + "="*60)
print("ALL 5 TOOLS TESTED WITH MATERIAL F-10")
print("="*60)
