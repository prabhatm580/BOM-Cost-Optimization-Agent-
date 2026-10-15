"""Phase 1: Get CF token and save it. Run immediately after pasting passcode."""
import sys, json, urllib.request, urllib.parse, base64, ssl

UAA = "https://uaa.cf.us10.hana.ondemand.com"
passcode = sys.argv[1]
ctx = ssl._create_unverified_context()
creds64 = base64.b64encode(b"cf:").decode()
form = urllib.parse.urlencode({"grant_type":"password","passcode":passcode,"response_type":"token"}).encode()
rq = urllib.request.Request(f"{UAA}/oauth/token", data=form, headers={
    "Content-Type":"application/x-www-form-urlencoded",
    "Accept":"application/json",
    "Authorization":f"Basic {creds64}"}, method="POST")
with urllib.request.urlopen(rq, context=ctx, timeout=15) as r:
    td = json.loads(r.read())
token = td.get("access_token","")
if not token: print("FAILED:",td); sys.exit(1)
open("/tmp/cf_access_token.txt","w").write(token)
print(f"TOKEN_SAVED len={len(token)}")
