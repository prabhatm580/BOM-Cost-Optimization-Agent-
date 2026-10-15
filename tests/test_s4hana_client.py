"""
Unit tests for s4hana_client.py — Step 5.3 of CF migration runbook.

All HTTP calls are stubbed via monkeypatch; no real network access.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Shared helpers / stubs
# ---------------------------------------------------------------------------

@dataclass
class FakeCall:
    method: str
    path: str
    params: dict
    user_identity: str | None
    body: dict | None = None
    service_root: str | None = None


@dataclass
class FakeClient:
    """Minimal stub for s4hana_client.Client used in tool tests."""
    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[FakeCall] = field(default_factory=list)

    def _resolve(self, call: FakeCall) -> dict:
        for needle, payload in self.responses.items():
            if needle in call.path:
                if callable(payload):
                    return payload(call)
                return dict(payload) if isinstance(payload, dict) else payload
        return {"results": []}

    async def get(self, service_path, params=None, user_identity=None):
        call = FakeCall("GET", service_path, dict(params or {}), user_identity)
        self.calls.append(call)
        return self._resolve(call)

    async def post(self, service_path, body, service_root, user_identity=None):
        call = FakeCall("POST", service_path, {}, user_identity, body, service_root)
        self.calls.append(call)
        return self._resolve(call)


def make_fake(responses: dict[str, Any] | None = None) -> FakeClient:
    return FakeClient(responses=dict(responses or {}))


# ---------------------------------------------------------------------------
# s4hana_client unit tests
# ---------------------------------------------------------------------------

class TestFirstBinding:
    """Test _first_binding VCAP parsing."""

    def test_returns_none_when_vcap_empty(self, add_agent_to_path, monkeypatch):
        monkeypatch.delenv("VCAP_SERVICES", raising=False)
        from s4hana_client import _first_binding
        assert _first_binding("destination") is None

    def test_returns_none_when_vcap_invalid_json(self, add_agent_to_path, monkeypatch):
        monkeypatch.setenv("VCAP_SERVICES", "not-json")
        from s4hana_client import _first_binding
        assert _first_binding("destination") is None

    def test_returns_none_when_label_missing(self, add_agent_to_path, monkeypatch):
        vcap = {"other-service": [{"credentials": {"url": "http://x"}}]}
        monkeypatch.setenv("VCAP_SERVICES", json.dumps(vcap))
        from s4hana_client import _first_binding
        assert _first_binding("destination") is None

    def test_returns_credentials_when_present(self, add_agent_to_path, monkeypatch):
        creds = {"uri": "http://dest", "clientid": "cid", "clientsecret": "sec", "url": "http://tok"}
        vcap = {"destination": [{"credentials": creds}]}
        monkeypatch.setenv("VCAP_SERVICES", json.dumps(vcap))
        from s4hana_client import _first_binding
        result = _first_binding("destination")
        assert result["uri"] == "http://dest"


class TestDestination:
    """Test Destination dataclass."""

    def test_defaults(self, add_agent_to_path):
        from s4hana_client import Destination
        d = Destination(url="http://s4", auth_type="NoAuthentication")
        assert d.proxy_type == "Internet"
        assert d.sap_client is None

    def test_auth_basic(self, add_agent_to_path):
        from s4hana_client import Destination
        d = Destination(url="http://s4", auth_type="BasicAuthentication",
                        username="user", password="pass")
        assert d.username == "user"


class TestClientUrlBuilding:
    """Test URL building logic."""

    @pytest.mark.asyncio
    async def test_url_with_leading_slash(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        c = Client(destination=dest)
        assert c._build_url("http://s4", "/sap/odata") == "http://s4/sap/odata"

    @pytest.mark.asyncio
    async def test_url_without_leading_slash(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        c = Client(destination=dest)
        assert c._build_url("http://s4", "sap/odata") == "http://s4/sap/odata"


class TestClientAuth:
    """Test _auth method."""

    def test_returns_tuple_for_basic_auth(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="BasicAuthentication",
                           username="user", password="pass")
        c = Client(destination=dest)
        result = c._auth(dest)
        assert result == ("user", "pass")

    def test_returns_none_for_no_auth(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        c = Client(destination=dest)
        assert c._auth(dest) is None

    def test_returns_none_when_username_missing(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="BasicAuthentication",
                           username=None, password="pass")
        c = Client(destination=dest)
        assert c._auth(dest) is None


class TestUserIdentityHeader:
    """Test X-User-Identity header injection."""

    def test_header_injected_when_identity_set(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        c = Client(destination=dest)
        headers = c._base_headers("ALICE")
        assert headers.get("X-User-Identity") == "ALICE"

    def test_header_absent_when_identity_none(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        c = Client(destination=dest)
        headers = c._base_headers(None)
        assert "X-User-Identity" not in headers


class TestMandatoryParams:
    """Test sap-client injection."""

    def test_injects_sap_client(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication", sap_client="100")
        c = Client(destination=dest)
        params = {}
        c._inject_mandatory_params(dest, params)
        assert params["sap-client"] == "100"

    def test_does_not_override_existing(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication", sap_client="100")
        c = Client(destination=dest)
        params = {"sap-client": "200"}
        c._inject_mandatory_params(dest, params)
        assert params["sap-client"] == "200"

    def test_skips_when_sap_client_none(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication", sap_client=None)
        c = Client(destination=dest)
        params = {}
        c._inject_mandatory_params(dest, params)
        assert "sap-client" not in params


class TestUnwrapResponse:
    """Test OData v2 envelope unwrapping."""

    def test_unwraps_odata_v2_d_results(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        c = Client(destination=dest)
        body = {"d": {"results": [{"key": "val"}]}}
        result = c._unwrap(body)
        assert result == {"results": [{"key": "val"}]}

    def test_passthrough_when_no_d_key(self, add_agent_to_path):
        from s4hana_client import Client, Destination
        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        c = Client(destination=dest)
        body = {"value": [{"key": "val"}]}
        result = c._unwrap(body)
        assert result == {"value": [{"key": "val"}]}


class TestClientGet:
    """Test get() with stubbed HTTP."""

    @pytest.mark.asyncio
    async def test_get_success_unwraps_odata(self, add_agent_to_path, monkeypatch):
        from s4hana_client import Client, Destination
        import s4hana_client as module

        dest = Destination(url="http://s4", auth_type="NoAuthentication", sap_client="100")

        class StubResponse:
            status_code = 200
            def json(self): return {"d": {"results": [{"Material": "M1"}]}}

        class StubAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw): return StubResponse()

        monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **kw: StubAsyncClient())
        c = Client(destination=dest)
        result = await c.get("/sap/odata/Entity")
        assert result.get("results") == [{"Material": "M1"}]

    @pytest.mark.asyncio
    async def test_get_returns_error_dict_on_4xx(self, add_agent_to_path, monkeypatch):
        from s4hana_client import Client, Destination
        import s4hana_client as module

        dest = Destination(url="http://s4", auth_type="NoAuthentication")

        class StubResponse:
            status_code = 404
            text = "Not Found"

        class StubAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw): return StubResponse()

        monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **kw: StubAsyncClient())
        c = Client(destination=dest)
        result = await c.get("/sap/odata/Missing")
        assert result["error"] is True
        assert result["status_code"] == 404

    @pytest.mark.asyncio
    async def test_get_handles_non_json_2xx(self, add_agent_to_path, monkeypatch):
        from s4hana_client import Client, Destination
        import s4hana_client as module

        dest = Destination(url="http://s4", auth_type="NoAuthentication")

        class StubResponse:
            status_code = 200
            text = "plain text"
            def json(self): raise ValueError("not json")

        class StubAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw): return StubResponse()

        monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **kw: StubAsyncClient())
        c = Client(destination=dest)
        result = await c.get("/sap/odata/Entity")
        assert result["error"] is True


class TestLazyDestinationResolution:
    """Test that destination is resolved lazily and only once."""

    @pytest.mark.asyncio
    async def test_resolver_called_once(self, add_agent_to_path):
        from s4hana_client import Client, Destination, _DestinationResolver

        dest = Destination(url="http://s4", auth_type="NoAuthentication")
        resolve_count = [0]

        class StubResolver(_DestinationResolver):
            async def resolve(self, name):
                resolve_count[0] += 1
                return dest

            async def proxy_settings(self):
                return "http://proxy:8080", {}

        import s4hana_client as module

        class StubResponse:
            status_code = 200
            def json(self): return {"d": {"results": []}}

        class StubAsyncClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw): return StubResponse()

        c = Client(resolver=StubResolver())

        with patch.object(module.httpx, "AsyncClient", lambda *a, **kw: StubAsyncClient()):
            await c.get("/path/one")
            await c.get("/path/two")

        assert resolve_count[0] == 1, "Resolver should be called only once"


class TestFakeClientInToolTests:
    """Tests using FakeClient to exercise tool logic without network."""

    @pytest.mark.asyncio
    async def test_fake_client_returns_configured_response(self, add_agent_to_path):
        fake = make_fake({"A_MaterialValuation": {"results": [{"Material": "M1", "StandardPrice": "100"}]}})
        result = await fake.get("/sap/odata/A_MaterialValuation", params={"$filter": "Material eq 'M1'"})
        assert result["results"][0]["Material"] == "M1"
        assert len(fake.calls) == 1
        assert fake.calls[0].method == "GET"
        assert "A_MaterialValuation" in fake.calls[0].path

    @pytest.mark.asyncio
    async def test_fake_client_returns_empty_for_unknown_path(self, add_agent_to_path):
        fake = make_fake({})
        result = await fake.get("/sap/odata/UnknownEntity")
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_fake_client_tracks_params(self, add_agent_to_path):
        fake = make_fake({"A_Product": {"results": []}})
        await fake.get("/sap/odata/A_Product", params={"$filter": "Product eq 'X'", "$top": 10})
        assert fake.calls[0].params["$filter"] == "Product eq 'X'"
        assert fake.calls[0].params["$top"] == 10
