# Migration summary

_For full detail, read `state.json`._

- Agent: `assets/bom-cost-optimization-agent`
- Last code run: 2026-07-08-065713
- Last deploy run: 2026-07-08-071300
- Runtime mode: dual-mode
- Systems: s4hana_onprem (CIN_100)
- AI Core destination: aicore
- LLM model: gpt-4o
- Deployment:
  - App: `bom-cost-optimization-agent` in `gdh-prj-vector-hackathon-apac-dev4-s2ydk575 / Dev`
  - Route: https://bom-cost-optimization-agent.cfapps.us10.hana.ondemand.com
  - Memory: 512M
  - Services bound: proj-vector-destination-service, proj-vector-connectivity-service
  - Smoke verdict: healthy
  - Last outcome: success
