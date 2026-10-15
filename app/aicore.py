"""Resolve SAP AI Core credentials from a BTP destination and create an LLM.

Two strategies:
  1. gen_ai_hub init_llm (needs botocore — added to requirements.txt)
  2. langchain_openai.ChatOpenAI -> AI Core deployment endpoint (direct fallback)

Entry points: init_llm_from_destination(), build_aicore_llm()
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

AICORE_DESTINATION_ENV = "AICORE_DESTINATION_NAME"
DEFAULT_AICORE_DESTINATION = "aicore"

# Env var names used to cache resolved credentials
_ENV_BASE_URL = "AICORE_BASE_URL"
_ENV_AUTH_URL = "AICORE_AUTH_URL"
_ENV_CID      = "AICORE_CLIENT_ID"
_ENV_CSEC     = "AICORE_CLIENT_SECRET"
_ENV_RG       = "AICORE_RESOURCE_GROUP"


# ---------------------------------------------------------------------------
# Strategy 1 — gen_ai_hub (botocore required by package __init__)
# ---------------------------------------------------------------------------

def _get_gen_ai_hub():
    """Return a gen_ai_hub-backed LLM factory or None if unavailable.

    gen_ai_hub.proxy.langchain.__init__ imports Amazon/Bedrock packages
    (aiobotocore, langchain_aws) that may not be installed.  We bypass
    this by loading the submodules we need directly via importlib.util,
    never triggering the package-level __init__.

    Confirmed working approach (from CF task diagnostics):
      importlib.util.find_spec("gen_ai_hub.proxy.langchain.openai") succeeds
      and module_from_spec + exec_module loads it successfully without going
      through __init__.
    """
    try:
        import importlib.util as _ilu
        import sys as _sys
        import types as _types

        # 1. GenAIHubProxyClient — no Amazon dependency
        from gen_ai_hub.proxy.gen_ai_hub_proxy import GenAIHubProxyClient  # type: ignore

        # 2. Pre-stub the gen_ai_hub.proxy.langchain package so that
        #    relative imports inside openai.py (.base, .init_models) work.
        _pkg = "gen_ai_hub.proxy.langchain"
        if _pkg not in _sys.modules:
            # Create a minimal stub package; its __path__ makes Python
            # resolve sub-module specs correctly without executing __init__
            _pkg_parent_path = None
            try:
                import gen_ai_hub.proxy  # type: ignore
                _pkg_parent_path = list(gen_ai_hub.proxy.__path__)
            except Exception:
                pass
            _stub_pkg = _types.ModuleType(_pkg)
            _stub_pkg.__package__ = _pkg
            if _pkg_parent_path:
                import os as _os
                _stub_pkg.__path__ = [_os.path.join(p, "langchain") for p in _pkg_parent_path]  # type: ignore[assignment]
            else:
                _stub_pkg.__path__ = []  # type: ignore[assignment]
            _sys.modules[_pkg] = _stub_pkg

        # 3. Load each needed submodule via spec (bypasses __init__)
        def _load_submodule(full_name: str):
            if full_name in _sys.modules:
                return _sys.modules[full_name]
            spec = _ilu.find_spec(full_name)
            if spec is None:
                raise ImportError(f"Cannot find spec for {full_name}")
            mod = _ilu.module_from_spec(spec)
            mod.__package__ = full_name.rsplit(".", 1)[0]
            _sys.modules[full_name] = mod  # register BEFORE exec to handle circular refs
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod

        _load_submodule("gen_ai_hub.proxy.langchain.base")
        _load_submodule("gen_ai_hub.proxy.langchain.init_models")
        _openai_mod = _load_submodule("gen_ai_hub.proxy.langchain.openai")
        _GHChatOpenAI = _openai_mod.ChatOpenAI  # type: ignore[attr-defined]

        # 4. Set proxy version
        from gen_ai_hub.proxy import set_proxy_version  # type: ignore
        set_proxy_version("gen-ai-hub")

        def _init_llm(model_name: str, **kwargs):
            proxy = GenAIHubProxyClient()
            # gen_ai_hub ChatOpenAI uses 'proxy_model_name' (not 'model_name')
            # to look up the deployment via proxy_client
            return _GHChatOpenAI(proxy_client=proxy, proxy_model_name=model_name, **kwargs)

        logger.debug("_get_gen_ai_hub: using GenAIHubProxyClient + openai submodule")
        return _init_llm

    except (ImportError, ModuleNotFoundError):
        # Amazon/other deps missing — fall through to Strategy 2
        return None
    except Exception as exc:
        logger.debug("_get_gen_ai_hub unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Strategy 2 — direct ChatOpenAI -> AI Core deployment endpoint
# ---------------------------------------------------------------------------

async def _aicore_bearer_token(token_url: str, cid: str, pw: str) -> str:
    """Client-credentials OAuth token from AI Core XSUAA."""
    async with httpx.AsyncClient(timeout=20.0) as hc:
        r = await hc.post(
            token_url.rstrip("/") + "/oauth/token",
            data={"grant_type": "client_credentials", "response_type": "token"},
            auth=(cid, pw),
            headers={"Accept": "application/json"},
        )
    r.raise_for_status()
    return r.json()["access_token"]


def _find_deployment_url_via_proxy(model_name: str) -> str | None:
    """
    Use GenAIHubProxyClient._get_matched_deployments() to find a deployment URL.
    This uses the gen_ai_hub SDK's own internal lookup (handles tenant routing).
    Returns the deployment base URL or None.
    """
    try:
        from gen_ai_hub.proxy.gen_ai_hub_proxy import GenAIHubProxyClient  # type: ignore
        proxy = GenAIHubProxyClient()
        deployments = proxy._get_matched_deployments(model_name=model_name)  # type: ignore[attr-defined]
        if deployments:
            dep_url = str(deployments[0].url).rstrip("/")
            logger.info("Found deployment via proxy: %s for model %s", dep_url[-20:], model_name)
            return dep_url
    except Exception as exc:
        logger.debug("_find_deployment_url_via_proxy failed: %s", exc)
    return None


async def _find_deployment_id(base_url: str, token: str, model_name: str, rg: str) -> str | None:
    """Find a RUNNING AI Core deployment ID for model_name."""
    list_url = base_url.rstrip("/") + "/v2/inference/deployments?status=RUNNING"
    hdrs = {
        "Authorization": f"Bearer {token}",
        "AI-Resource-Group": rg,
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as hc:
            r = await hc.get(list_url, headers=hdrs)
        if r.status_code >= 400:
            logger.warning("deployments list HTTP %s: %s", r.status_code, r.text[:200])
            return None
        needle = model_name.lower().replace("-", "").replace("_", "")
        resources = r.json().get("resources") or []
        # Pass 1: match by configurationName containing model name
        for dep in resources:
            cfg = (dep.get("configurationName") or dep.get("modelName") or "").lower().replace("-", "").replace("_", "")
            if needle in cfg or "gpt4" in cfg or "gpt-4" in cfg.replace("_","-"):
                logger.info("Matched deployment %s for model %s", dep["id"], model_name)
                return dep["id"]
        # Pass 2: first running deployment
        if resources:
            dep_id = resources[0]["id"]
            logger.warning("No name-match; using first running deployment %s", dep_id)
            return dep_id
    except Exception as exc:
        logger.warning("_find_deployment_id failed: %s", exc)
    return None


async def _find_deployment_url(base_url: str, token: str, model_name: str, rg: str) -> str | None:
    dep_id = await _find_deployment_id(base_url, token, model_name, rg)
    if dep_id:
        return base_url.rstrip("/") + f"/v2/inference/deployments/{dep_id}"
    return None


async def _build_direct_llm(
    base_url: str,
    token_url: str,
    cid: str,
    pw: str,
    rg: str,
    model_name: str,
    temperature: float,
    max_tokens: int | None,
) -> Any | None:
    """Build a ChatOpenAI instance pointing at AI Core (strategy 2)."""
    try:
        from langchain_openai import ChatOpenAI  # type: ignore
    except ImportError:
        logger.warning("langchain_openai not available for direct AI Core strategy")
        return None
    try:
        token = await _aicore_bearer_token(token_url, cid, pw)
    except Exception as exc:
        logger.warning("Bearer token acquisition failed: %s", exc)
        return None
    dep_url = await _find_deployment_url(base_url, token, model_name, rg)
    if dep_url:
        # OpenAI-compatible endpoint: base_url must end with /
        openai_base = dep_url.rstrip("/") + "/"
    else:
        openai_base = base_url.rstrip("/") + "/v2/inference/"
        logger.warning("No deployment found; using fallback base %s", openai_base)
    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": token,
        "base_url": openai_base,
        "temperature": temperature,
        "default_headers": {
            "AI-Resource-Group": rg,
            "api-version": "2024-02-01",
        },
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    try:
        llm = ChatOpenAI(**kwargs)
        logger.info("Direct ChatOpenAI for AI Core OK (base=%s model=%s)", openai_base, model_name)
        return llm
    except Exception as exc:
        logger.warning("ChatOpenAI construction failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Destination helpers
# ---------------------------------------------------------------------------

from s4hana_client import _first_binding, _DestinationResolver  # noqa: E402


async def _fetch_aicore_destination_raw(name: str) -> dict[str, Any]:
    """GET the raw destination object from the BTP Destination Service."""
    creds = _first_binding("destination") or {}
    if not creds:
        raise RuntimeError("No 'destination' service binding found in VCAP_SERVICES.")
    resolver = _DestinationResolver()
    token = await resolver._xsuaa_access_token()  # noqa: SLF001
    uri = creds["uri"].rstrip("/")
    url = f"{uri}/destination-configuration/v1/destinations/{name}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if r.status_code >= 400:
        raise RuntimeError(
            f"Destination service returned {r.status_code} for '{name}': {r.text}"
        )
    return r.json()


def _populate_aicore_env(cfg: dict[str, str]) -> tuple[str, str, str, str, str]:
    """Extract AI Core fields from a destination config and push them to env vars."""
    base_url = (cfg.get("URL") or "").rstrip("/")
    cid      = cfg.get("clientId") or ""
    pw       = cfg.get("clientSecret") or ""
    tok_url  = cfg.get("tokenServiceURL") or ""
    rg       = (
        cfg.get("URL.headers.AI-Resource-Group")
        or cfg.get("AI_RESOURCE_GROUP")
        or "default"
    )
    if not (base_url and cid and pw and tok_url):
        missing = [k for k, v in {"URL": base_url, "clientId": cid,
                                   "clientSecret": pw, "tokenServiceURL": tok_url}.items() if not v]
        raise RuntimeError(
            f"Destination is missing required fields: {missing}. Got keys: {sorted(cfg.keys())}"
        )
    os.environ[_ENV_BASE_URL] = base_url
    os.environ[_ENV_AUTH_URL] = tok_url
    os.environ[_ENV_CID]      = cid
    os.environ[_ENV_CSEC]     = pw
    os.environ[_ENV_RG]       = rg
    logger.info("AI Core destination resolved (base=%s, rg=%s)", base_url, rg)
    return base_url, cid, pw, tok_url, rg


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def init_llm_from_destination(
    model_name: str,
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    destination_name: str | None = None,
) -> Any:
    """
    Resolve AI Core credentials from a BTP destination and return a LangChain LLM.

    Strategy 1: gen_ai_hub init_llm (botocore required).
    Strategy 2: langchain_openai.ChatOpenAI -> AI Core deployment endpoint.
    Returns None if both fail.
    """
    name = destination_name or os.environ.get(AICORE_DESTINATION_ENV, DEFAULT_AICORE_DESTINATION)

    # Fetch and cache credentials if not already done
    base_url = os.environ.get(_ENV_BASE_URL, "")
    cid      = os.environ.get(_ENV_CID, "")
    pw       = os.environ.get(_ENV_CSEC, "")
    tok_url  = os.environ.get(_ENV_AUTH_URL, "")
    rg       = os.environ.get(_ENV_RG, "default")

    if not (base_url and cid and pw and tok_url):
        payload = await _fetch_aicore_destination_raw(name)
        cfg = payload.get("destinationConfiguration") or {}
        base_url, cid, pw, tok_url, rg = _populate_aicore_env(cfg)

    # ---- Strategy 1: gen_ai_hub (ENABLED - works on this AI Core tenant) ----
    # gen_ai_hub uses GenAIHubProxyClient to auto-discover running deployments.
    # Any server-side ARIA prompt is overridden by pre_model_hook in agent.py
    # which replaces SystemMessage before every LLM call.
    init_llm_fn = _get_gen_ai_hub()
    if init_llm_fn is not None:
        kw: dict = {"temperature": temperature}
        if max_tokens is not None:
            kw["max_tokens"] = max_tokens
        try:
            llm = init_llm_fn(model_name, **kw)
            logger.info("LLM initialized via gen_ai_hub (model=%s)", model_name)
            return llm
        except Exception as exc:
            logger.warning("gen_ai_hub init_llm failed: %s - trying direct", exc)

    # ---- Strategy 2: direct ChatOpenAI -> AI Core deployment ----
    logger.info("Trying direct ChatOpenAI for AI Core (model=%s)", model_name)
    llm = await _build_direct_llm(base_url, tok_url, cid, pw, rg, model_name, temperature, max_tokens)
    if llm is not None:
        logger.info("LLM initialized via direct ChatOpenAI (model=%s)", model_name)
        return llm

    logger.error("Both LLM strategies failed - check AI Core destination config")
    return None


def build_aicore_llm(model: str | None = None, temperature: float = 0.0) -> Any | None:
    """Backward-compatible synchronous helper. Uses direct ChatOpenAI → AI Core (no gen_ai_hub proxy)."""
    import asyncio
    m = model or os.environ.get("AICORE_MODEL", "gpt-4o")
    base_url = os.environ.get(_ENV_BASE_URL, "")
    cid      = os.environ.get(_ENV_CID, "")
    pw       = os.environ.get(_ENV_CSEC, "")
    tok_url  = os.environ.get(_ENV_AUTH_URL, "")
    rg       = os.environ.get(_ENV_RG, "default")
    if not (base_url and cid and pw and tok_url):
        logger.warning("build_aicore_llm: missing env vars, cannot build direct LLM")
        return None
    try:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(_build_direct_llm(base_url, tok_url, cid, pw, rg, m, temperature, None))
    except Exception as exc:
        logger.warning("build_aicore_llm direct failed: %s", exc)
        return None


__all__ = ["init_llm_from_destination", "build_aicore_llm"]
