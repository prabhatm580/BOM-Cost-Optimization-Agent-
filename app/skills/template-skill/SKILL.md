---
name: bom-cost-optimization
description: Instructions for cross-plant BOM cost analysis and Make/Buy/Strategic Buy classification for ABC Automotive
---

# BOM Cost Optimization Skill

## Classification Rules

### Make
- procurementType == 'E' (in-house production), OR
- costVariancePercent < 5%

### Strategic Buy
- costVariancePercent >= 20% AND (hasMultipleSuppliers == False OR strategicImportance == True)

### Buy
- costVariancePercent >= 5% AND NOT Strategic Buy

## Confidence Levels
- High: variance > 15%
- Medium: 5% ≤ variance ≤ 15%
- Low: variance < 5%

## Milestone Logging
Log every classification:
- `[M1.achieved: Make/Buy classified for {material}, variance={pct}%]`
- `[M1.missed: classification failed for {material}, reason={reason}]`

Log every workflow trigger:
- `[M2.achieved: workflow triggered for {analysis_id}]`
- `[M2.missed: workflow trigger failed for {analysis_id}, reason={reason}]`

## Cost Variance Formula
- costVarianceAbsoluteINR = abs(costPlant1 - costPlant2)
- costVariancePercent = round(abs(costPlant1 - costPlant2) / max(costPlant1, costPlant2) * 100, 2)
- totalImpactINR = costVarianceAbsoluteINR × annualVolume
