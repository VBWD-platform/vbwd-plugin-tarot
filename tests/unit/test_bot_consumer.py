"""Unit tests for the tarot bot-consumer seam (S45.3).

``tarot`` is the *second* independent consumer on the S45 bridge (proving the
seam serves more than one plugin). It structurally implements
``BotCommandProvider`` (bot_namespace="tarot") so its commands light up over
every bot adapter unchanged. The bridge is **optional**: the bot methods lazily
import bot-base's neutral DTOs inside the method body, so ``tarot`` imports
cleanly even when bot-base is absent (no hard dependency).

Engineering requirements (BINDING): TDD-first · SOLID · DI · DRY (reuse tarot's
existing reading service — no new reading logic) · NO OVERENGINEERING · full
readable names · no core change · bridge optional · **no billing on any tarot
bot command**. Gate: ``bin/pre-commit-check.sh --plugin tarot --full`` green.

All tarot bot commands are FREE + ANONYMOUS — no link required, no token debit,
no identity mutation. tarot-over-bot is a free teaser; paid readings stay
web-only.
"""
import sys
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.bot_base.bot_base.ports import BotCommandProvider
from plugins.bot_base.bot_base.services.command_registry import CommandRegistry
from plugins.bot_base.bot_base.types import BotIdentity, BotInbound, BotReply, ChatRef
from plugins.bot_base.tests.unit.fakes import FakePluginManager
from plugins.tarot import TarotPlugin
from plugins.tarot.src.services.tarot_session_service import FreeReadingCard


def _make_inbound(
    *, command=None, text=None, identity=None, args=None, action_data=None
):
    chat_ref = ChatRef(provider_id="telegram", chat_id="4242")
    return BotInbound(
        provider_id="telegram",
        chat_ref=chat_ref,
        sender_ref="7",
        text=text,
        command=command,
        args=args or [],
        action_data=action_data,
        identity=identity,
    )


def _linked_identity():
    return BotIdentity(
        provider_id="telegram",
        external_user_id="7",
        vbwd_user_id=uuid4(),
    )


def _fake_card(name="The Star", position="PRESENT"):
    return FreeReadingCard(
        arcana_name=name,
        orientation="UPRIGHT",
        position=position,
        interpretation=f"{name}: a moment of hope.",
    )


@pytest.fixture
def enabled_tarot_plugin():
    """A TarotPlugin initialized with bot_enabled=True."""
    plugin = TarotPlugin()
    plugin.initialize({"bot_enabled": True})
    return plugin


# ── D1 inversion: collected only when bot_enabled=True ───────────────────────
class TestCommandRegistryCollection:
    def test_plugin_structurally_implements_provider_seam(self, enabled_tarot_plugin):
        assert isinstance(enabled_tarot_plugin, BotCommandProvider)

    def test_registry_collects_tarot_commands_when_bot_enabled(
        self, enabled_tarot_plugin
    ):
        registry = CommandRegistry(FakePluginManager([enabled_tarot_plugin]))

        assert enabled_tarot_plugin in registry.get_command_providers()
        index = registry.command_index()
        assert index["draw"] is enabled_tarot_plugin
        assert index["reading"] is enabled_tarot_plugin

    def test_registry_surfaces_no_tarot_command_when_bot_disabled(self):
        plugin = TarotPlugin()
        plugin.initialize({"bot_enabled": False})
        registry = CommandRegistry(FakePluginManager([plugin]))

        assert registry.command_index() == {}
        assert registry.collect_commands() == []
        assert plugin.get_bot_commands() == []

    def test_bot_disabled_is_the_default(self):
        plugin = TarotPlugin()
        plugin.initialize({})

        assert plugin.get_bot_commands() == []


# ── /draw + /reading expose tarot's own namespace ─────────────────────────────
class TestTarotCommandsExposed:
    def test_exposes_draw_and_reading_commands(self, enabled_tarot_plugin):
        commands = enabled_tarot_plugin.get_bot_commands()

        names = sorted(command.name for command in commands)
        assert names == ["draw", "reading"]
        assert {command.namespace for command in commands} == {"tarot"}


# ── /draw (anonymous, free) → reading via existing service ───────────────────
class TestDrawCommandAnonymousFree:
    def test_draw_returns_reading_reply_for_unlinked_sender(
        self, enabled_tarot_plugin, monkeypatch
    ):
        fake_service = MagicMock()
        fake_service.draw_free_reading.return_value = [_fake_card("The Sun")]
        monkeypatch.setattr(
            enabled_tarot_plugin, "_build_reading_service", lambda: fake_service
        )

        # identity=None → unlinked / anonymous sender, NO link required.
        reply = enabled_tarot_plugin.handle_action(
            _make_inbound(command="draw", identity=None)
        )

        assert isinstance(reply, BotReply)
        assert "The Sun" in reply.text
        fake_service.draw_free_reading.assert_called_once_with(card_count=1)

    def test_reading_returns_full_spread_reply(self, enabled_tarot_plugin, monkeypatch):
        fake_service = MagicMock()
        fake_service.draw_free_reading.return_value = [
            _fake_card("Past", "PAST"),
            _fake_card("Present", "PRESENT"),
            _fake_card("Future", "FUTURE"),
        ]
        monkeypatch.setattr(
            enabled_tarot_plugin, "_build_reading_service", lambda: fake_service
        )

        reply = enabled_tarot_plugin.handle_action(
            _make_inbound(command="reading", identity=None)
        )

        assert "Past" in reply.text and "Future" in reply.text
        fake_service.draw_free_reading.assert_called_once_with(card_count=3)


# ── DoD: ZERO token debit on ANY tarot bot command (linked OR unlinked) ───────
class TestNoTokenBillingEver:
    @pytest.mark.parametrize("command", ["draw", "reading"])
    @pytest.mark.parametrize("identity", [None, _linked_identity()])
    def test_bot_command_never_touches_a_token_service(
        self, enabled_tarot_plugin, monkeypatch, command, identity
    ):
        """The reading path must never resolve or call any token service.

        We hand the plugin a reading service whose ``draw_free_reading`` returns
        cards, and assert the plugin never asks the DI container for a token
        service. A linked identity must bill exactly as much as an unlinked one:
        nothing.
        """
        fake_service = MagicMock()
        fake_service.draw_free_reading.return_value = [_fake_card()]
        monkeypatch.setattr(
            enabled_tarot_plugin, "_build_reading_service", lambda: fake_service
        )

        reply = enabled_tarot_plugin.handle_action(
            _make_inbound(command=command, identity=identity)
        )

        assert isinstance(reply, BotReply)
        # The free reading path persists/charges nothing: only the read-only
        # ``draw_free_reading`` is ever invoked on the service.
        assert fake_service.draw_free_reading.called
        fake_service.create_session.assert_not_called()
        fake_service.deduct_tokens.assert_not_called()


# ── multi-step reading via BotReply.choices → handle_action (D7) ─────────────
class TestChoiceRoundTripD7:
    def test_reading_offers_choices_that_route_back_through_handle_action(
        self, enabled_tarot_plugin, monkeypatch
    ):
        fake_service = MagicMock()
        fake_service.draw_free_reading.return_value = [_fake_card("Picked", "PRESENT")]
        monkeypatch.setattr(
            enabled_tarot_plugin, "_build_reading_service", lambda: fake_service
        )

        # A tapped choice arrives as a namespaced action_data, still anonymous.
        tap = _make_inbound(action_data="tarot:draw:past", identity=None)
        reply = enabled_tarot_plugin.handle_action(tap)

        assert isinstance(reply, BotReply)
        assert "Picked" in reply.text
        # Still free: no billing primitives were touched.
        fake_service.deduct_tokens.assert_not_called()


# ── optional bridge: module imports with bot-base absent ─────────────────────
class TestBridgeOptional:
    def test_tarot_imports_without_bot_base_on_path(self):
        """The tarot package must import even when bot_base is unavailable.

        Simulate bot-base absent by blocking its import and re-importing the
        tarot package: a top-level ``import bot_base`` would raise here.
        """
        import importlib

        blocked_prefixes = ("plugins.bot_base", "plugins.tarot")
        saved = {
            name: module
            for name, module in sys.modules.items()
            if name.startswith(blocked_prefixes)
        }
        for name in list(saved):
            del sys.modules[name]

        class _BlockBotBase:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "plugins.bot_base" or fullname.startswith(
                    "plugins.bot_base."
                ):
                    raise ImportError("bot_base is absent in this scenario")
                return None

        finder = _BlockBotBase()
        sys.meta_path.insert(0, finder)
        try:
            tarot_module = importlib.import_module("plugins.tarot")
            plugin = tarot_module.TarotPlugin()
            assert plugin.metadata.name == "tarot"
            assert "bot-base" not in (plugin.metadata.dependencies or [])
        finally:
            sys.meta_path.remove(finder)
            for name in list(sys.modules):
                if name.startswith(blocked_prefixes):
                    del sys.modules[name]
            sys.modules.update(saved)

    def test_bot_base_not_a_hard_dependency(self, enabled_tarot_plugin):
        assert "bot-base" not in (enabled_tarot_plugin.metadata.dependencies or [])
        assert "bot_base" not in (enabled_tarot_plugin.metadata.dependencies or [])
