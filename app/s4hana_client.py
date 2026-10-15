"""SAP S/4HANA On-Premise OData client backed by a BTP destination.

Required CF service bindings (VCAP_SERVICES):
* destination   -- destination-configuration REST API with embedded OAuth.
* connectivity  -- on-premise proxy host/port + OAuth for Proxy-Authorization.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

# ── Per-request BasicAuth override ───────────────────────────────────────────
# Set by the request dispatcher when a valid user token is decoded.
# Allows user's SAP credentials to be forwarded to every API call in that request.
_request_basic_auth: ContextVar[tuple | None] = ContextVar('_request_basic_auth', default=None)

def set_request_auth(username: str | None = None, password: str | None = None) -> None:
    """Store user SAP credentials for this request's context."""
    if username and password:
        _request_basic_auth.set((username, password))
    else:
        _request_basic_auth.set(None)

def clear_request_auth() -> None:
    _request_basic_auth.set(None)

logger = logging.getLogger(__name__)

DEFAULT_DESTINATION_NAME = os.environ.get("S4HANA_DESTINATION_NAME", "CIN_100")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("S4HANA_TIMEOUT_SECONDS", "60.0"))
TOKEN_TTL = float(os.environ.get("DESTINATION_TOKEN_TTL", "600"))
DEST_CACHE_TTL = float(os.environ.get("DESTINATION_CACHE_TTL", "300"))

RESPONSE_ENVELOPE = "odata_v2"
CSRF_REQUIRED = True


def _vcap() -> dict[str, Any]:
    raw = os.environ.get("VCAP_SERVICES")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("VCAP_SERVICES not valid JSON")
        return {}


def _first_binding(label: str) -> dict[str, Any] | None:
    bindings = _vcap().get(label) or []
    if not bindings:
        return None
    return (bindings[0] or {}).get("credentials") or {}


@dataclass
class Destination:
    url: str
    auth_type: str
    username: str | None = None
    password: str | None = None
    sap_client: str | None = None
    proxy_type: str = "Internet"
    additional: dict[str, str] = field(default_factory=dict)


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at


class _DestinationResolver:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._xsuaa_token: "_CachedToken | None" = None
        self._proxy_token: "_CachedToken | None" = None
        self._dest_cache: dict[str, tuple[Destination, float]] = {}

    async def _client_credentials_token(
        self, token_url: str, cid: str, csecret: str
    ) -> str:
        _auth_pair = (cid, csecret)
        async with httpx.AsyncClient(timeout=self._timeout) as _hc:
            r = await _hc.post(
                token_url.rstrip("/") + "/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=_auth_pair,
                headers={"Accept": "application/json"},
            )
        r.raise_for_status()
        return r.json()["access_token"]
    async def _xsuaa_access_token(self) -> str:
        if self._xsuaa_token and not self._xsuaa_token.expired():
            return self._xsuaa_token.value
        creds = _first_binding("destination") or {}
        if not creds:
            raise RuntimeError(
                "No 'destination' service binding found in VCAP_SERVICES. "
                "Bind proj-vector-destination-service to the app."
            )
        token = await self._client_credentials_token(
            creds["url"], creds["clientid"], creds["clientsecret"]
        )
        self._xsuaa_token = _CachedToken(token, time.monotonic() + TOKEN_TTL)
        return token

    async def _connectivity_proxy_token(self) -> str:
        if self._proxy_token and not self._proxy_token.expired():
            return self._proxy_token.value
        creds = _first_binding("connectivity") or {}
        if not creds:
            raise RuntimeError("OnPremise destination but no connectivity binding in VCAP_SERVICES.")
        token = await self._client_credentials_token(
            creds["token_service_url"], creds["clientid"], creds["clientsecret"]
        )
        self._proxy_token = _CachedToken(token, time.monotonic() + TOKEN_TTL)
        return token

    async def resolve(self, name: str) -> Destination:
        cached = self._dest_cache.get(name)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        creds = _first_binding("destination") or {}
        if not creds:
            raise RuntimeError("No destination service binding found in VCAP_SERVICES.")
        access_token = await self._xsuaa_access_token()
        uri = creds["uri"].rstrip("/")
        url = f"{uri}/destination-configuration/v1/destinations/{name}"
        async with httpx.AsyncClient(timeout=self._timeout) as _hc:
            r = await _hc.get(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Destination service {r.status_code} for {name!r}: {r.text}")
        body = r.json()
        cfg = body.get("destinationConfiguration") or {}
        d_url = (cfg.get("URL") or "").rstrip("/")
        if not d_url:
            raise RuntimeError(f"Destination {name!r} has no URL configured.")
        sap_client = cfg.get("sap-client") or cfg.get("SAP-Client") or cfg.get("sap_client")
        well_known = {
            "URL", "Name", "Type", "Authentication", "ProxyType",
            "User", "Password", "sap-client", "SAP-Client", "sap_client",
        }
        additional = {k: v for k, v in cfg.items() if k not in well_known}
        dest = Destination(
            url=d_url,
            auth_type=cfg.get("Authentication", "NoAuthentication"),
            username=cfg.get("User"),
            password=cfg.get('Password'),
            sap_client=sap_client,
            proxy_type=cfg.get('ProxyType', 'Internet'),
            additional=additional,
        )
        self._dest_cache[name] = (dest, time.monotonic() + DEST_CACHE_TTL)
        return dest

    async def proxy_settings(self) -> tuple[str, dict[str, str]]:
        creds = _first_binding('connectivity') or {}
        if not creds:
            raise RuntimeError('OnPremise destination but no connectivity binding.')
        host = creds['onpremise_proxy_host']
        port = creds['onpremise_proxy_http_port']
        proxy_url = f'http://' + str(host) + ':' + str(port)
        token = await self._connectivity_proxy_token()
        return proxy_url, {'Proxy-Authorization': f'Bearer ' + token}


class Client:
    """Async HTTP client for S/4HANA OData APIs via BTP Destination Service."""

    def __init__(self, destination=None, destination_name=DEFAULT_DESTINATION_NAME,
                 timeout=DEFAULT_TIMEOUT_SECONDS, resolver=None) -> None:
        self._destination = destination
        self._destination_name = destination_name
        self._timeout = timeout
        self._resolver = resolver or _DestinationResolver(timeout=timeout)

    async def destination(self) -> Destination:
        if self._destination is None:
            self._destination = await self._resolver.resolve(self._destination_name)
        return self._destination

    def _build_url(self, base_url: str, service_path: str) -> str:
        if not service_path.startswith("/"):
            service_path = "/" + service_path
        return f"{base_url}{service_path}"

    def _auth(self, dest: Destination):
        # Use per-request user credentials if available (forwarded from UI login)
        req_auth = _request_basic_auth.get()
        if req_auth:
            return req_auth
        if dest.auth_type == "BasicAuthentication" and dest.username and dest.password:
            return (dest.username, dest.password)
        return None

    def _base_headers(self, user_identity) -> dict:
        h = {"Accept": "application/json"}
        if user_identity:
            h["X-User-Identity"] = user_identity
        return h

    async def _client_kwargs(self, dest: Destination):
        kwargs = {"timeout": self._timeout}
        extra: dict = {}
        if dest.proxy_type.lower() == "onpremise":
            proxy_url, hdr = await self._resolver.proxy_settings()
            kwargs["proxy"] = proxy_url
            extra.update(hdr)
        return kwargs, extra

    def _inject_mandatory_params(self, dest: Destination, params: dict) -> dict:
        if dest.sap_client and "sap-client" not in params:
            params["sap-client"] = dest.sap_client
        return params

    def _unwrap(self, body) -> Any:
        if RESPONSE_ENVELOPE == "odata_v2" and isinstance(body, dict) and "d" in body:
            return body["d"]
        return body

    async def _fetch_csrf(self, hc, dest, service_root, extra_headers) -> str:
        params: dict = {}
        self._inject_mandatory_params(dest, params)
        headers = {"x-csrf-token": "fetch", "Accept": "application/json"}
        headers.update(extra_headers)
        r = await hc.get(self._build_url(dest.url, service_root), auth=self._auth(dest),
                         headers=headers, params=params)
        return r.headers.get("x-csrf-token") or r.headers.get("X-CSRF-Token") or ""

    async def get_service_entities(self, service_root: str) -> list[str]:
        """Call OData $metadata and return all EntitySet names found."""
        dest = await self.destination()
        client_kwargs, extra = await self._client_kwargs(dest)
        headers = {"Accept": "application/xml, text/xml"}
        headers.update(extra)
        params: dict = {}
        self._inject_mandatory_params(dest, params)
        url = self._build_url(dest.url, f"{service_root}/$metadata")
        async with httpx.AsyncClient(**client_kwargs) as hc:
            r = await hc.get(url, auth=self._auth(dest), headers=headers, params=params)
        if r.status_code >= 400:
            logger.warning("$metadata %s → HTTP %s", url, r.status_code)
            return []
        text = r.text
        import re
        return re.findall(r'EntitySet[^>]+Name="([^"]+)"', text)

    async def get_entity_set_properties(self, service_root: str, entity_set_name: str) -> dict:
        """
        Parse OData $metadata XML.  Given an EntitySet name, follows its EntityType
        reference to the actual EntityType definition and returns:
          {'keys': [...], 'all': [...], 'entity_type': '<type_name>'}
        """
        dest = await self.destination()
        client_kwargs, extra = await self._client_kwargs(dest)
        headers = {"Accept": "application/xml, text/xml"}
        headers.update(extra)
        params: dict = {}
        self._inject_mandatory_params(dest, params)
        url = self._build_url(dest.url, f"{service_root}/$metadata")
        async with httpx.AsyncClient(**client_kwargs) as hc:
            r = await hc.get(url, auth=self._auth(dest), headers=headers, params=params)
        if r.status_code >= 400:
            logger.warning("$metadata %s → HTTP %s", url, r.status_code)
            return {}
        import re
        text = r.text

        # Step 1: find EntitySet and its EntityType reference
        es_pattern = re.compile(
            r'<(?:\w+:)?EntitySet[^>]+Name="' + re.escape(entity_set_name) + r'"[^>]*/?>',
            re.DOTALL,
        )
        es_match = es_pattern.search(text)
        if not es_match:
            logger.warning("EntitySet '%s' not found in metadata", entity_set_name)
            return {}

        es_tag = es_match.group(0)
        et_ref_match = re.search(r'EntityType="([^"]+)"', es_tag)
        if not et_ref_match:
            logger.warning("EntitySet '%s' has no EntityType attribute", entity_set_name)
            return {}

        # EntityType ref is typically "Namespace.TypeName" — take only the local name
        et_full = et_ref_match.group(1)
        et_local = et_full.split(".")[-1]
        logger.info("EntitySet %s → EntityType ref: %s (local: %s)", entity_set_name, et_full, et_local)

        # Step 2: find the EntityType block by exact local name
        et_block_pattern = re.compile(
            r'<(?:\w+:)?EntityType[^>]+Name="' + re.escape(et_local) + r'"'
            r'.*?</(?:\w+:)?EntityType>',
            re.DOTALL,
        )
        et_match = et_block_pattern.search(text)
        if not et_match:
            logger.warning("EntityType '%s' block not found in metadata", et_local)
            return {}

        et_block = et_match.group(0)
        key_refs = re.findall(r'<(?:\w+:)?PropertyRef[^>]+Name="([^"]+)"', et_block)
        # Use \bProperty\b to avoid matching PropertyRef again
        all_props = re.findall(r'<(?:\w+:)?Property\s+[^>]*Name="([^"]+)"', et_block)
        logger.info("EntityType %s raw XML (first 600):\n%s", et_local, et_block[:600])
        return {"keys": key_refs, "all": all_props, "entity_type": et_local}

    # keep old name as alias for callers
    async def get_entity_properties(self, service_root: str, entity_type_name: str) -> dict:
        return await self.get_entity_set_properties(service_root, entity_type_name + "Set")

    async def get(self, service_path: str, params: dict | None = None,
                  user_identity: str | None = None) -> dict[str, Any]:
        dest = await self.destination()
        merged = dict(params or {})
        self._inject_mandatory_params(dest, merged)
        client_kwargs, extra = await self._client_kwargs(dest)
        headers = self._base_headers(user_identity)
        headers.update(extra)
        # Use a generous read timeout for large OData responses
        if "timeout" in client_kwargs:
            raw_t = client_kwargs["timeout"]
            if isinstance(raw_t, (int, float)):
                import httpx as _httpx_mod
                client_kwargs["timeout"] = _httpx_mod.Timeout(
                    connect=10.0, read=max(float(raw_t), 60.0), write=10.0, pool=5.0
                )
        async with httpx.AsyncClient(**client_kwargs) as hc:
            r = await hc.get(self._build_url(dest.url, service_path),
                             auth=self._auth(dest), headers=headers, params=merged)

        # Parse body — try multiple methods for robustness
        body = None
        try:
            body = r.json()
        except Exception:
            pass
        if body is None:
            try:
                import json as _json
                body = _json.loads(r.content.decode("utf-8", errors="replace"))
            except Exception:
                pass
        if body is None:
            try:
                import json as _json
                body = _json.loads(r.text)
            except Exception:
                pass

        logger.debug("[s4client] GET %s → HTTP %d body_type=%s body_keys=%s",
                     service_path, r.status_code,
                     type(body).__name__,
                     list(body.keys())[:5] if isinstance(body, dict) else "?")

        if r.status_code >= 400:
            # If body has OData data (d.results or results), treat as success
            if body and isinstance(body, dict):
                unwrapped = self._unwrap(body)
                if isinstance(unwrapped, dict) and ("results" in unwrapped or "__metadata" in unwrapped):
                    logger.info("SAP returned HTTP %d but body has OData data — treating as success: %s",
                                r.status_code, service_path)
                    return unwrapped
                # SAP error payload
                if "error" in body and isinstance(body.get("error"), dict):
                    err_msg = body["error"].get("message", {})
                    if isinstance(err_msg, dict):
                        err_msg = err_msg.get("value", r.text[:200])
                    return {"error": True, "status_code": r.status_code,
                            "message": err_msg, "url": self._build_url(dest.url, service_path)}
            return {"error": True, "status_code": r.status_code, "message": r.text[:500],
                    "url": self._build_url(dest.url, service_path)}

        if body is None:
            return {"error": True, "status_code": r.status_code, "message": r.text[:500]}
        return self._unwrap(body)

    async def post(self, service_path: str, body: dict, service_root: str,
                   user_identity: str | None = None) -> dict[str, Any]:
        dest = await self.destination()
        client_kwargs, extra = await self._client_kwargs(dest)
        async with httpx.AsyncClient(**client_kwargs) as hc:
            csrf = ""
            if CSRF_REQUIRED:
                csrf = await self._fetch_csrf(hc, dest, service_root, extra)
            headers = self._base_headers(user_identity)
            headers["Content-Type"] = "application/json"
            if CSRF_REQUIRED:
                headers["x-csrf-token"] = csrf
            headers.update(extra)
            params: dict = {}
            self._inject_mandatory_params(dest, params)
            r = await hc.post(self._build_url(dest.url, service_path), json=body,
                              auth=self._auth(dest), headers=headers, params=params)
        if r.status_code >= 400:
            return {"error": True, "status_code": r.status_code, "message": r.text}
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "raw": r.text}


_CLIENT: Client | None = None

def get_s4hana_client() -> Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = Client()
    return _CLIENT

def reset_client() -> None:
    global _CLIENT
    _CLIENT = None

__all__ = ["Client", "Destination", "DEFAULT_DESTINATION_NAME", "_DestinationResolver",
           "_first_binding", "get_s4hana_client", "reset_client"]
