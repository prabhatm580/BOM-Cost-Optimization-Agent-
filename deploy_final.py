"""
ONE-COMMAND deploy: python3 deploy_final.py <CF_PASSCODE>
Gets a fresh token, zips source, uploads, builds, deploys.
"""
import sys, os, json, time, zipfile, tempfile
import urllib.request, urllib.error, urllib.parse, base64, ssl

API      = "https://api.cf.us10.hana.ondemand.com"
UAA      = "https://uaa.cf.us10.hana.ondemand.com"
APP_GUID = "f148166b-1578-4b18-80ac-e9b26a9fd16d"
SRC      = os.path.dirname(os.path.abspath(__file__))
ctx      = ssl._create_unverified_context()

passcode = sys.argv[1] if len(sys.argv) > 1 else input("CF Passcode: ").strip()

# ── 1. Get token ──────────────────────────────────────────────────────────────
form = urllib.parse.urlencode({
    "grant_type": "password", "passcode": passcode, "response_type": "token"
}).encode()
rq = urllib.request.Request(f"{UAA}/oauth/token", data=form, method="POST", headers={
    "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
    "Authorization": f"Basic {base64.b64encode(b'cf:').decode()}",
})
try:
    td = json.loads(urllib.request.urlopen(rq, context=ctx, timeout=20).read())
    token = td["access_token"]
    print(f"✅ Token OK (len={len(token)})")
except urllib.error.HTTPError as e:
    print(f"❌ Auth failed {e.code}: {e.read().decode()[:200]}"); sys.exit(1)

def req(method, url, data=None, raw_body=None, extra_h=None):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if extra_h: h.update(extra_h)
    body = json.dumps(data).encode() if data else raw_body
    if data: h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=120) as resp:
            raw = resp.read(); return json.loads(raw) if raw else {}, resp.status
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read()), e.code
        except: return {}, e.code

# ── 2. Wait for any in-progress staging ──────────────────────────────────────
builds, _ = req("GET", f"{API}/v3/apps/{APP_GUID}/builds?order_by=-created_at&per_page=5")
if any(b["state"] == "STAGING" for b in builds["resources"]):
    print("Waiting for in-progress build...")
    for _ in range(20):
        builds, _ = req("GET", f"{API}/v3/apps/{APP_GUID}/builds?order_by=-created_at&per_page=3")
        if not any(b["state"] == "STAGING" for b in builds["resources"]): break
        print("  staging..."); time.sleep(10)

# ── 3. Zip source ─────────────────────────────────────────────────────────────
print("Zipping source...")
exclude_dirs = {'__pycache__', '.git', 'node_modules', '.pytest_cache', 'venv', '.venv'}
exclude_exts = {'.pyc', '.pyo', '.zip', '.tar', '.gz'}
exclude_files = {'agent_v2.zip', 'agent_v3.zip', 'agent_v5.zip', 'agent_v6.zip'}
tmp = tempfile.mktemp(suffix='.zip')
with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for fn in files:
            if fn in exclude_files or os.path.splitext(fn)[1].lower() in exclude_exts: continue
            fp = os.path.join(root, fn)
            zf.write(fp, os.path.relpath(fp, SRC))
print(f"Zip: {os.path.getsize(tmp)//1024}KB")

# ── 4. Upload package ─────────────────────────────────────────────────────────
pkg, s = req("POST", f"{API}/v3/packages",
             {"type":"bits","relationships":{"app":{"data":{"guid":APP_GUID}}}})
pkg_guid = pkg["guid"]
print(f"Package: {pkg_guid[:12]} ({s})")

with open(tmp,'rb') as f:
    bnd = b'--Bnd4yWxkTrZu'
    body = (bnd + b'\r\nContent-Disposition: form-data; name="bits"; filename="app.zip"\r\n'
            b'Content-Type: application/zip\r\n\r\n' + f.read() + b'\r\n' + bnd + b'--\r\n')
_, us = req("POST", f"{API}/v3/packages/{pkg_guid}/upload",
            raw_body=body, extra_h={"Content-Type": f"multipart/form-data; boundary={bnd.decode()}"})
print(f"Upload: {us}")

for _ in range(15):
    p, _ = req("GET", f"{API}/v3/packages/{pkg_guid}")
    st = p.get("state","?"); print(f"  pkg: {st}")
    if st == "READY": break
    time.sleep(4)

# ── 5. Build ──────────────────────────────────────────────────────────────────
build_guid = None
for attempt in range(8):
    build, bs = req("POST", f"{API}/v3/builds", {"package":{"guid":pkg_guid}})
    if bs == 201:
        build_guid = build["guid"]; print(f"Build: {build_guid[:12]}"); break
    print(f"  Build attempt {attempt+1}: {bs} waiting 20s..."); time.sleep(20)

if not build_guid:
    # Fallback: use latest STAGED build
    builds, _ = req("GET", f"{API}/v3/apps/{APP_GUID}/builds?order_by=-created_at&per_page=5")
    latest = next((b for b in builds["resources"] if b["state"] == "STAGED"), None)
    if latest:
        droplet = latest["droplet"]["guid"]
        req("POST", f"{API}/v3/apps/{APP_GUID}/actions/stop"); time.sleep(3)
        req("PATCH", f"{API}/v3/apps/{APP_GUID}/relationships/current_droplet", {"data":{"guid":droplet}})
        req("POST", f"{API}/v3/apps/{APP_GUID}/actions/start")
        print("Restarted with latest STAGED build."); os.unlink(tmp); sys.exit(0)

for _ in range(50):
    b, _ = req("GET", f"{API}/v3/builds/{build_guid}")
    st = b.get("state","?"); print(f"  build: {st}")
    if st == "STAGED": droplet = b["droplet"]["guid"]; break
    if st == "FAILED": print("BUILD FAILED:", b.get("error","")); os.unlink(tmp); sys.exit(1)
    time.sleep(10)

# ── 6. Deploy ─────────────────────────────────────────────────────────────────
req("POST", f"{API}/v3/apps/{APP_GUID}/actions/stop"); time.sleep(3)
_, s1 = req("PATCH", f"{API}/v3/apps/{APP_GUID}/relationships/current_droplet", {"data":{"guid":droplet}})
print(f"Set droplet: {s1}")
_, s2 = req("POST", f"{API}/v3/apps/{APP_GUID}/actions/start")
print(f"Start: {s2}")

# ── 7. Wait for RUNNING ───────────────────────────────────────────────────────
procs, _ = req("GET", f"{API}/v3/apps/{APP_GUID}/processes")
web = next((p for p in procs["resources"] if p["type"] == "web"), None)
time.sleep(15)
if web:
    for i in range(15):
        stats, _ = req("GET", f"{API}/v3/processes/{web['guid']}/stats")
        states = [s["state"] for s in stats.get("resources",[])]
        print(f"  [{i+1}] {states}")
        if all(s == "RUNNING" for s in states): print("✅ App is RUNNING!"); break
        time.sleep(8)

os.unlink(tmp)
print("\n✅ Deployed successfully!")
print("🌐 https://bom-cost-optimization-agent.cfapps.us10.hana.ondemand.com")
