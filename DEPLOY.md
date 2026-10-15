# CF Deployment Guide — bom-cost-optimization-agent

The sandbox environment cannot reach the SAP BTP CF API endpoint (private network).  
Run these commands from your **local machine** or a BTP-connected CI/CD pipeline.

---

## Prerequisites

### 1. Install CF CLI v8
```bash
# macOS
brew install cloudfoundry/tap/cf-cli@8

# Linux
wget -q https://github.com/cloudfoundry/cli/releases/download/v8.9.0/cf8-cli_8.9.0_linux_x86-64.tgz
tar xzf cf8-cli_8.9.0_linux_x86-64.tgz
sudo mv cf8 /usr/local/bin/cf
```

### 2. Set CF API and log in
```bash
cf api [REDACTED]
cf login --sso
# Follow the SSO link, paste the one-time passcode
```

### 3. Create service instances (once per space)
```bash
cf create-service destination lite proj-vector-destination-service
cf create-service connectivity lite proj-vector-connectivity-service
```

---

## Deploy

```bash
cd assets/bom-cost-optimization-agent
./deploy-cf.sh
```

Or manually:
```bash
cf push bom-cost-optimization-agent -f manifest.yml
```

---

## Verify

```bash
# App status
cf app bom-cost-optimization-agent

# Logs
cf logs bom-cost-optimization-agent --recent

# Agent card health check
APP_URL=$(cf app bom-cost-optimization-agent | grep "routes:" | awk '{print $2}')
curl https://$APP_URL/.well-known/agent.json | python3 -m json.tool
```

---

## Environment Variables

Set in `manifest.yml` — no `.env` files needed:

| Variable | Value | Purpose |
|---|---|---|
| `JOULE_RUNTIME` | `0` | CF mode (not Joule) |
| `S4HANA_DESTINATION_NAME` | `S4HANA` | BTP destination for S/4HANA |
| `AICORE_DESTINATION_NAME` | `aicore` | BTP destination for AI Core |
| `AICORE_MODEL` | `gpt-4o` | LLM model |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Service Bindings

| Service | Type | Purpose |
|---|---|---|
| `proj-vector-destination-service` | destination | Resolves `S4HANA` and `aicore` destinations |
| `proj-vector-connectivity-service` | connectivity | OnPremise proxy for S/4HANA |

---

## Update Agent Public URL

After first deploy, update the agent card URL:
```bash
APP_ROUTE=$(cf app bom-cost-optimization-agent | grep "routes:" | awk '{print $2}')
cf set-env bom-cost-optimization-agent AGENT_PUBLIC_URL "https://$APP_ROUTE/"
cf restart bom-cost-optimization-agent
```

---

## Dual-Mode Reference

```
JOULE_RUNTIME=1  →  Joule Studio (litellm + MCP tools + sap_cloud_sdk)
JOULE_RUNTIME=0  →  CF (AI Core LLM + direct S/4HANA client, no MCP)
IBD_TESTING=1    →  Test mode (mock data, no network)
```
