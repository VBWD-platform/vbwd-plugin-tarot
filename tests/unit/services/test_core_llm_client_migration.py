"""S97.5 RED — taro routes its reading through the CORE LLM client.

The key decoupling: taro must STOP importing ``plugins.chat.src.llm_adapter``
(a cross-plugin LLM import) and resolve the central core client instead.

Engineering requirements (binding, restated): TDD-first (RED before the
migration); DevOps-first (no DB/network — the core client is faked); SOLID
(Dependency inversion — taro depends on the core ``llm_client`` port, not on
another plugin's adapter); DI (resolved from the container); DRY (one LLM client
home, in core); Liskov; clean code; no overengineering. Quality guard:
``bin/pre-commit-check.sh --plugin taro --full``.
"""

from pathlib import Path
from unittest.mock import MagicMock

from plugins.taro.src.services import taro_session_service as session_module

TARO_SRC_DIR = Path(session_module.__file__).resolve().parents[1]


def test_taro_source_imports_no_plugins_chat_llm_module():
    """No ``from plugins.chat`` (or any ``plugins.*`` LLM) import in taro src."""
    offenders = []
    for python_file in TARO_SRC_DIR.rglob("*.py"):
        text = python_file.read_text(encoding="utf-8")
        if "plugins.chat" in text:
            offenders.append(python_file.name)
    assert (
        offenders == []
    ), f"taro still imports a chat LLM module (cross-plugin coupling): {offenders}"


def test_taro_source_imports_no_legacy_llm_api_keys():
    """No ``llm_api_endpoint`` / ``llm_api_key`` reader remains in taro src."""
    offenders = []
    for python_file in TARO_SRC_DIR.rglob("*.py"):
        text = python_file.read_text(encoding="utf-8")
        for legacy_key in ("llm_api_endpoint", "llm_api_key"):
            if legacy_key in text:
                offenders.append(f"{python_file.name}: {legacy_key}")
    assert offenders == [], f"legacy LLM config readers remain in taro: {offenders}"


def test_core_client_adapter_chats_through_core_client():
    """taro's core-client chat adapter delegates to the core client's ``.chat``."""
    core_client = MagicMock()
    core_client.chat.return_value = "The cards reveal transformation."

    adapter = session_module.CoreClientChatAdapter(
        core_client, system_prompt="You are a Tarot reader."
    )
    reply = adapter.chat(messages=[{"role": "user", "content": "Draw a card"}])

    assert reply == "The cards reveal transformation."
    _args, call_kwargs = core_client.chat.call_args
    assert call_kwargs["system_prompt"] == "You are a Tarot reader."


def test_core_client_adapter_wraps_core_error_as_taro_llm_error():
    """A core LLM failure surfaces as taro's own ``LLMError`` (caller contract)."""
    from vbwd.llm.errors import LlmError

    core_client = MagicMock()
    core_client.chat.side_effect = LlmError("upstream failed")

    adapter = session_module.CoreClientChatAdapter(core_client, system_prompt="x")

    import pytest

    with pytest.raises(session_module.LLMError):
        adapter.chat(messages=[{"role": "user", "content": "Draw"}])
