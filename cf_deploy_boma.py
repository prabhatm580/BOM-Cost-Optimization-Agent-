#!/usr/bin/env python3
"""
BOMA CF Deploy Script
---------------------
Deploys bom-cost-optimization-agent to Cloud Foundry as BOMA: AI Based BOM Assistant
at https://bom-cost-optimization-cap.cfapps.us10.hana.ondemand.com/

Usage:
    python3 cf_deploy_boma.py <your-cf-password>

or set env var:
    CF_PASSWORD=your-password python3 cf_deploy_boma.py
"""
import sys, os, json, zipfile, ssl, urllib.request, urllib.parse, base64, time, pathlib

CF_API      = "https://api.cf.us10.hana.ondemand.com"
CF_UAA      = "https://uaa.cf.us10.hana.ondemand.com"
CF_USER     = "prabhat.mishra01@sap.com"
CF_ORG      = "prabhat.mishra01@sap.com"          # adjust if different
CF_SPACE    = "dev"
APP_NAME    = "bom-cost-optimization-agent"
APP_DIR     = pathlib.Path(__file__).parent
MANIFEST    = APP_DIR / "manifest.yml"

ctx = ssl.create_default_context()

def http(method, url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            body = r.read()
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()
        try: return e.code, json.loads(body)
        except: return e.code, {"error": body.decode()[:500]}

def get_token(password):
    print("🔑 Getting CF token...")
    url = f"{CF_UAA}/oauth/token"
    payload = urllib.parse.urlencode({
        "grant_type": "password",
        "username": CF_USER,
        "password": password,
        "response_type": "token",
        "scope": "",
    }).encode()
    creds = base64.b64encode(b"cf:").decode()
    status, data = http("POST", url,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        data=payload)
    if status != 200:
        print(f"❌ Token failed: {status} - {data}")
        sys.exit(1)
    token = data["access_token"]
    print(f"✅ Token obtained")
    return token

def cf(method, path, token, body=None, content_type="application/json"):
    url = f"{CF_API}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        if isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
    return http(method, url, headers=headers, data=data)

def get_app_guid(token):
    status, data = cf("GET", f"/v3/apps?names={APP_NAME}", token)
    resources = data.get("resources", [])
    if resources:
        guid = resources[0]["guid"]
        state = resources[0]["state"]
        print(f"✅ Found app: {APP_NAME} | state: {state} | guid: {guid}")
        return guid
    return None

def get_space_guid(token):
    status, data = cf("GET", f"/v3/spaces?names={CF_SPACE}", token)
    resources = data.get("resources", [])
    if resources:
        guid = resources[0]["guid"]
        print(f"✅ Space: {CF_SPACE} | guid: {guid}")
        return guid
    print(f"❌ Space '{CF_SPACE}' not found. Available:")
    status2, data2 = cf("GET", "/v3/spaces", token)
    for s in data2.get("resources", []):
        print(f"   - {s['name']} | {s['guid']}")
    sys.exit(1)

def create_zip():
    zip_path = "/tmp/boma_deploy.zip"
    print(f"📦 Creating deployment zip...")
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.cache', 'venv', '.venv',
                 'agent_v2.zip', 'agent_v3.zip', 'agent_v5.zip', 'agent_v6.zip'}
    skip_ext  = {'.pyc', '.pyo', '.zip'}
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(str(APP_DIR)):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file in files:
                if any(file.endswith(e) for e in skip_ext):
                    continue
                full = os.path.join(root, file)
                rel  = os.path.relpath(full, str(APP_DIR))
                zf.write(full, rel)
    size = os.path.getsize(zip_path)
    print(f"✅ Zip created: {zip_path} ({size/1024/1024:.1f} MB)")
    return zip_path

def upload_and_deploy(token, app_guid, zip_path):
    print(f"⬆️  Creating build package...")
    # Create package
    status, pkg = cf("POST", f"/v3/packages", token, {
        "type": "bits",
        "relationships": {"app": {"data": {"guid": app_guid}}}
    })
    if status not in (200, 201):
        print(f"❌ Package create failed: {status} - {pkg}")
        sys.exit(1)
    pkg_guid = pkg["guid"]
    print(f"✅ Package: {pkg_guid}")

    # Upload zip
    print(f"⬆️  Uploading zip ({os.path.getsize(zip_path)/1024/1024:.1f} MB)...")
    boundary = "---CFBOUNDARY"
    with open(zip_path, "rb") as f:
        zip_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="bits"; filename="app.zip"\r\n'
        f"Content-Type: application/zip\r\n\r\n"
    ).encode() + zip_data + f"\r\n--{boundary}--\r\n".encode()

    url = f"{CF_API}/v3/packages/{pkg_guid}/upload"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as r:
            print(f"✅ Upload: HTTP {r.status}")
    except urllib.error.HTTPError as e:
        print(f"⚠️  Upload HTTP {e.code}: {e.read().decode()[:200]}")

    # Wait for package ready
    print("⏳ Waiting for package processing...")
    for _ in range(30):
        time.sleep(3)
        s, d = cf("GET", f"/v3/packages/{pkg_guid}", token)
        state = d.get("state", "")
        print(f"   Package state: {state}")
        if state == "READY":
            break
        if state == "FAILED":
            print(f"❌ Package failed: {d}")
            sys.exit(1)

    # Create build
    print("🔨 Creating build (staging)...")
    status, build = cf("POST", "/v3/builds", token, {
        "package": {"guid": pkg_guid}
    })
    if status not in (200, 201):
        print(f"❌ Build failed: {status} - {build}")
        sys.exit(1)
    build_guid = build["guid"]
    print(f"✅ Build: {build_guid}")

    # Wait for build staged
    print("⏳ Staging app (this takes ~2-3 min)...")
    for i in range(60):
        time.sleep(5)
        s, d = cf("GET", f"/v3/builds/{build_guid}", token)
        state = d.get("state", "")
        print(f"   [{i*5}s] Build state: {state}")
        if state == "STAGED":
            droplet_guid = d["droplet"]["guid"]
            print(f"✅ Staged! Droplet: {droplet_guid}")
            break
        if state == "FAILED":
            print(f"❌ Staging failed: {d.get('error')}")
            sys.exit(1)
    else:
        print("❌ Staging timeout")
        sys.exit(1)

    # Set current droplet
    print("🎯 Setting current droplet...")
    cf("PATCH", f"/v3/apps/{app_guid}/relationships/current_droplet", token,
       {"data": {"guid": droplet_guid}})

    # Start app
    print("🚀 Starting app...")
    cf("POST", f"/v3/apps/{app_guid}/actions/start", token, {})
    print("⏳ Waiting for app to start...")
    for i in range(40):
        time.sleep(5)
        s, d = cf("GET", f"/v3/processes?app_guids={app_guid}&types=web", token)
        processes = d.get("resources", [])
        if processes:
            p = processes[0]
            inst = p.get("instances", 0)
            print(f"   [{i*5}s] instances={inst}")
            if inst >= 1:
                time.sleep(5)
                print(f"\n🎉 BOMA deployed successfully!")
                print(f"🌐 URL: https://bom-cost-optimization-cap.cfapps.us10.hana.ondemand.com/")
                print(f"🤖 Agent: BOMA — AI Based BOM Assistant")
                break

def main():
    password = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CF_PASSWORD", "")
    if not password:
        print("Usage: python3 cf_deploy_boma.py <your-cf-password>")
        print("   or: CF_PASSWORD=xxx python3 cf_deploy_boma.py")
        sys.exit(1)

    print("=" * 60)
    print("🤖 BOMA — AI Based BOM Assistant — CF Deploy")
    print("=" * 60)

    token      = get_token(password)
    space_guid = get_space_guid(token)
    app_guid   = get_app_guid(token)

    if not app_guid:
        print(f"Creating new app '{APP_NAME}'...")
        status, data = cf("POST", "/v3/apps", token, {
            "name": APP_NAME,
            "relationships": {"space": {"data": {"guid": space_guid}}},
            "lifecycle": {"type": "buildpack",
                          "data": {"buildpacks": ["python_buildpack"]}},
            "environment_variables": {
                "JOULE_RUNTIME": "0",
                "S4HANA_DESTINATION_NAME": "CIN_100",
                "AICORE_DESTINATION_NAME": "aicore",
                "AICORE_MODEL": "gpt-4o",
                "LOG_LEVEL": "INFO",
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": "/home/vcap/app/app",
            }
        })
        if status not in (200, 201):
            print(f"❌ App create failed: {status} - {data}")
            sys.exit(1)
        app_guid = data["guid"]
        print(f"✅ App created: {app_guid}")

    zip_path = create_zip()
    upload_and_deploy(token, app_guid, zip_path)

if __name__ == "__main__":
    main()
