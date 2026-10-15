import json, http.client, ssl

# Token is read from file written by get_token.py
with open('/tmp/cf_token.txt') as f:
    lines = [l.strip() for l in f.read().strip().split('\n') if l.strip()]
token = lines[0] if lines else ''
print('Token prefix:', token[:25], '... len:', len(token))

ctx = ssl.create_default_context()

# Step 1: Get app GUID via CF API
print('\n--- CF API: Get app info ---')
conn = http.client.HTTPSConnection('[REDACTED]', context=ctx, timeout=30)
conn.request('GET', '/v3/apps?names=bom-cost-optimization-cap',
             headers={'Authorization': token, 'Accept': 'application/json'})
resp = conn.getresponse()
body = resp.read().decode()
print('Status:', resp.status)
data = json.loads(body)
apps = data.get('resources', [])
guid = ''
if apps:
    guid = apps[0]['guid']
    state = apps[0]['state']
    print('App GUID:', guid)
    print('App state:', state)

if guid:
    # Step 2: Get routes
    print('\n--- CF API: Get routes ---')
    conn2 = http.client.HTTPSConnection('[REDACTED]', context=ctx, timeout=30)
    conn2.request('GET', '/v3/apps/' + guid + '/routes',
                  headers={'Authorization': token, 'Accept': 'application/json'})
    resp2 = conn2.getresponse()
    rdata = json.loads(resp2.read().decode())
    for r in rdata.get('resources', []):
        print('Route:', r.get('url'))

# Step 3: Try direct connection to CAP app
print('\n--- Direct CAP connection ---')
cap_host = '[REDACTED]'
try:
    conn3 = http.client.HTTPSConnection(cap_host, context=ctx, timeout=30)
    conn3.request('GET', '/odata/v4/bom-optimizer/')
    resp3 = conn3.getresponse()
    print('Status:', resp3.status)
    print('Body:', resp3.read().decode()[:400])
except Exception as e:
    print('Error:', type(e).__name__, str(e)[:200])
