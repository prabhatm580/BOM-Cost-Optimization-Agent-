"""Phase 2: Deploy using saved token from /tmp/cf_access_token.txt"""
import os, sys, json, time, zipfile, tempfile, urllib.request, urllib.error, urllib.parse, ssl

API      = "https://api.cf.us10.hana.ondemand.com"
APP      = "bom-cost-optimization-agent"
ORG      = "gdh-prj-vector-hackathon-apac-dev4-s2ydk575"
SPACE    = "Dev"
SRC      = os.path.dirname(os.path.abspath(__file__))
TOK_FILE = "/tmp/cf_access_token.txt"

ctx = ssl._create_unverified_context()
token = open(TOK_FILE).read().strip()
print(f"Token len={len(token)}")

def call(method, url, token=None, data=None, raw_body=None, extra=None):
    h = {"Accept": "application/json"}
    if token:  h["Authorization"] = f"Bearer {token}"
    if extra:  h.update(extra)
    body = None
    if data is not None:
        body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    elif raw_body is not None:
        body = raw_body
    rq = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(rq, context=ctx, timeout=120) as r:
            raw = r.read(); return (json.loads(raw) if raw else {}), r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:    return json.loads(raw), e.code
        except: return {"_raw": raw.decode()[:500]}, e.code

# Find App GUID
orgs,  _ = call("GET", f"{API}/v3/organizations?names={urllib.parse.quote(ORG)}", token)
org_guid  = (orgs.get("resources") or [{}])[0].get("guid", "")
spaces, _ = call("GET", f"{API}/v3/spaces?names={urllib.parse.quote(SPACE)}&organization_guids={org_guid}", token)
space_guid = (spaces.get("resources") or [{}])[0].get("guid", "")
apps,  _  = call("GET", f"{API}/v3/apps?names={APP}&space_guids={space_guid}", token)
app_guid   = (apps.get("resources") or [{}])[0].get("guid", "")
print(f"App GUID: {app_guid}")
if not app_guid: print("App not found"); sys.exit(1)

# Zip source
print("Zipping...")
with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
    zip_path = tf.name
SKIP = {"__pycache__", ".git", "migration-audit", "prebuilt_tests", "tests"}
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for root_dir in ["app"]:
        full_root = os.path.join(SRC, root_dir)
        for dirpath, dirs, files in os.walk(full_root):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for f in files:
                if f.endswith(".pyc"): continue
                full = os.path.join(dirpath, f)
                zf.write(full, os.path.relpath(full, SRC))
    for f in ["requirements.txt", "Procfile", "manifest.yml", "runtime.txt"]:
        full = os.path.join(SRC, f)
        if os.path.exists(full): zf.write(full, f)
print(f"Zip: {os.path.getsize(zip_path)/1024:.1f} KB")

# Upload package
pkg, _ = call("POST", f"{API}/v3/packages", token, data={
    "type": "bits", "relationships": {"app": {"data": {"guid": app_guid}}}})
pkg_guid = pkg.get("guid", "")
print(f"Package: {pkg_guid}")
if not pkg_guid: print("Failed:", pkg); sys.exit(1)

with open(zip_path, "rb") as f: zip_data = f.read()
boundary = "CFboundary9988776655"
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"bits\"; filename=\"app.zip\"\r\nContent-Type: application/zip\r\n\r\n"
        ).encode() + zip_data + f"\r\n--{boundary}--\r\n".encode()
_, upcode = call("POST", f"{API}/v3/packages/{pkg_guid}/upload", token,
    raw_body=body, extra={"Content-Type": f"multipart/form-data; boundary={boundary}"})
print(f"Upload HTTP: {upcode}")

for _ in range(30):
    time.sleep(3)
    p, _ = call("GET", f"{API}/v3/packages/{pkg_guid}", token)
    state = p.get("state",""); print(f"  pkg: {state}")
    if state == "READY": break
    if state in ("FAILED","EXPIRED"): print("Pkg failed"); sys.exit(1)

# Build
build, bcode = call("POST", f"{API}/v3/builds", token, data={"package": {"guid": pkg_guid}})
print(f"Build ({bcode}): {json.dumps(build)[:200]}")
build_guid = build.get("guid","")
if not build_guid: print("Build error:", build.get("errors", build)); sys.exit(1)

print(f"Build GUID: {build_guid}")
droplet_guid = ""
for i in range(60):
    time.sleep(5)
    b, _ = call("GET", f"{API}/v3/builds/{build_guid}", token)
    state = b.get("state",""); print(f"  [{i+1}] {state}")
    if state == "STAGED":
        droplet_guid = b.get("droplet",{}).get("guid",""); break
    if state == "FAILED": print("FAILED:", b.get("error","")); sys.exit(1)

print(f"Droplet: {droplet_guid}")
r1,c1 = call("PATCH", f"{API}/v3/apps/{app_guid}/relationships/current_droplet", token, data={"data":{"guid":droplet_guid}})
r2,c2 = call("POST",  f"{API}/v3/apps/{app_guid}/actions/restart", token)
print(f"Assign: {c1}  Restart: {c2}")
print("\n✅ BOMA v6.0.0 LIVE!")
print("🌐 https://bom-cost-optimization-agent.cfapps.us10.hana.ondemand.com/")
os.unlink(zip_path)
