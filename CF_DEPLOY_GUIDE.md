# ☁️ Cloud Foundry Deployment Guide — BOMA Agent
## Deploy with SSO Passcode (One-Time Token)

---

## Step 1 — Install CF CLI (if not already done)

```bash
# macOS
brew install cloudfoundry/tap/cf-cli@8

# Windows (Chocolatey)
choco install cloudfoundry-cli

# Verify
cf --version
```

---

## Step 2 — Get Your SSO Passcode

Open this URL in your browser (replace with your actual CF API endpoint):

```
https://login.<your-cf-subdomain>.hana.ondemand.com/passcode
```

This gives you a **one-time passcode** that's valid for ~5 minutes.

---

## Step 3 — Login to CF with Passcode

```bash
# Login using SSO passcode (no username/password needed)
cf login -a https://api.cf.<your-cf-subdomain>.hana.ondemand.com --sso

# The CLI will prompt: "One Time Code (Get one at https://login...)"
# Paste your passcode from Step 2 → press Enter

# Select your Org and Space when prompted
```

---

## Step 4 — Navigate to Agent Folder

```bash
cd assets/bom-cost-optimization-agent
```

---

## Step 5 — Check Services Exist

```bash
# These must exist — create if missing
cf service proj-vector-destination-service
cf service proj-vector-connectivity-service

# If missing, create:
cf create-service destination   lite proj-vector-destination-service
cf create-service connectivity  lite proj-vector-connectivity-service
```

---

## Step 6 — Deploy the Agent

```bash
# Push without starting (to bind services cleanly)
cf push bom-cost-optimization-agent -f manifest.yml --no-start

# Bind services (only if not in manifest.yml)
cf bind-service bom-cost-optimization-agent proj-vector-destination-service
cf bind-service bom-cost-optimization-agent proj-vector-connectivity-service

# Start the app
cf start bom-cost-optimization-agent
```

---

## Step 7 — Verify Deployment

```bash
# Check app status
cf app bom-cost-optimization-agent

# View live logs
cf logs bom-cost-optimization-agent --recent

# Get the app URL
cf app bom-cost-optimization-agent | grep routes

# Test the agent card endpoint
curl https://<your-app-route>/.well-known/agent.json
```

---

## Step 8 — Update Agent Public URL (if needed)

```bash
APP_ROUTE=$(cf app bom-cost-optimization-agent | grep routes | awk '{print $2}')
cf set-env bom-cost-optimization-agent AGENT_PUBLIC_URL "https://${APP_ROUTE}/"
cf restart bom-cost-optimization-agent
```

---

## manifest.yml Summary

Key settings already configured:

| Setting | Value |
|---------|-------|
| App Name | `bom-cost-optimization-agent` |
| Memory | 512M |
| Instances | 1 |
| Buildpack | `python_buildpack` |
| Health Check | `/.well-known/agent.json` |
| `JOULE_RUNTIME` | `"0"` (Cloud Foundry mode) |
| `S4HANA_DESTINATION_NAME` | `CIN_100` |
| `AICORE_MODEL` | `gpt-4o` |

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `App failed to start` | Check `cf logs bom-cost-optimization-agent --recent` |
| `Service not found` | Run `cf create-service destination lite proj-vector-destination-service` |
| `401 Unauthorized` | Check destination `CIN_100` exists in BTP Cockpit |
| `Cannot connect to CIN_100` | Check BTP Connectivity service binding |
| `LLM not configured` | Check `AICORE_DESTINATION_NAME=aicore` destination in BTP |

---

## Quick One-Liner Deploy (after first login)

```bash
cd assets/bom-cost-optimization-agent && cf push bom-cost-optimization-agent -f manifest.yml
```

---

## 📝 Notes

- The **passcode expires in ~5 minutes** — run `cf login` immediately after getting it
- You do NOT need username/password when using `--sso`
- The deployment takes ~3–5 minutes for Python buildpack
- Agent will be healthy when `cf app bom-cost-optimization-agent` shows `running`
