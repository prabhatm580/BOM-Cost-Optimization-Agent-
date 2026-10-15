"""sitecustomize.py — runs automatically on every Python startup.

Stubs out optional AWS/Amazon packages that gen_ai_hub.proxy.langchain
tries to import at package level.  We don't need Bedrock/Amazon AI models
(we use GPT-4o via OpenAI-compatible API), so we short-circuit those
imports to prevent ModuleNotFoundError cascades during the gen_ai_hub init.
"""

import sys
import types


def _make_stub(name: str) -> types.ModuleType:
    """Create a minimal stub module."""
    m = types.ModuleType(name)
    m.__spec__ = None  # type: ignore[assignment]
    return m


# ---- aiobotocore stub ----
_aiobotocore = _make_stub("aiobotocore")
_aiobotocore_session = _make_stub("aiobotocore.session")

class _FakeAioBotocoreSession:
    pass

_aiobotocore_session.AioSession = _FakeAioBotocoreSession  # type: ignore[attr-defined]
sys.modules.setdefault("aiobotocore", _aiobotocore)
sys.modules.setdefault("aiobotocore.session", _aiobotocore_session)

# ---- langchain_aws stub (only needed to satisfy import at module level) ----
# The actual Bedrock classes are never called; we only need the names to exist.
try:
    import langchain_aws  # noqa: F401  — use real package if installed
except ImportError:
    _langchain_aws = _make_stub("langchain_aws")

    class _StubChatBedrock:
        pass

    class _StubChatBedrockConverse:
        pass

    _langchain_aws.ChatBedrock = _StubChatBedrock  # type: ignore[attr-defined]
    _langchain_aws.ChatBedrockConverse = _StubChatBedrockConverse  # type: ignore[attr-defined]
    sys.modules["langchain_aws"] = _langchain_aws
