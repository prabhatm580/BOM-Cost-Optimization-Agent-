# BOM Cost Optimization Agent

An AI-powered agent that performs cross-plant BOM cost optimization analysis, classifies components as Make/Buy/Strategic Buy, and triggers review workflows for ABC Automotive.

## Overview

Uses A2A Protocol, LangGraph, LiteLLM, and SAP Cloud SDK.

## Structure

- `app/main.py` — A2A server entry point
- `app/agent_executor.py` — Request handling
- `app/agent.py` — Agent logic with LangGraph
- `app/tools.py` — 7 BOM analysis tools (Make/Buy classification, cost variance)
- `app/mcp_tools.py` — MCP tool loader (Agent Gateway)
- `app/load_skill_resources.py` — Skill resource loader
