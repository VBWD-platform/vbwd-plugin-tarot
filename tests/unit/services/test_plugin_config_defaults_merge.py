"""RED — tarot resolves effective config as descriptor defaults merged UNDER saved.

Bug: POST /session/<id>/card-explanation 500s because the rendered prompt is the
EMPTY STRING. TarotSessionService read ONLY the operator-saved runtime config
(``plugins/config.json`` -> ``tarot``), which is stale and lacks every template
key (system_prompt, card_explanation_template, ...) and ``llm_connection_slug``.
Each template therefore resolved to ``""`` -> empty prompt -> Anthropic 400.

The fix: effective config = descriptor defaults (each key's ``["default"]`` in
``plugins/tarot/config.json``) merged UNDER the operator-saved values. A present
saved value overrides its default; a missing key falls back to the default.

Engineering requirements (binding, restated): TDD-first (this RED set precedes
the fix); DevOps-first (no DB / no network — pure config resolution); SOLID
(single responsibility per loader; open/closed — no core touched); DI; DRY (one
config-read home consumed by both the LLM adapter and the PromptService);
Liskov; clean code; no overengineering. Quality guard:
``bin/pre-commit-check.sh --plugin tarot --full``.
"""

from unittest.mock import MagicMock

import pytest

from plugins.tarot.src.services import tarot_session_service as session_module
from plugins.tarot.src.services.tarot_session_service import TarotSessionService

# The template keys that the stale runtime config lacks entirely.
TEMPLATE_KEYS = (
    "system_prompt",
    "card_interpretation_template",
    "situation_reading_template",
    "card_explanation_template",
    "follow_up_question_template",
    "initial_greeting",
)


class TestEffectiveConfigFallsBackToDescriptorDefaults:
    """_read_plugin_config() must expose descriptor defaults for missing keys."""

    def test_template_keys_resolve_to_nonempty_descriptor_defaults(self):
        """Every template key resolves to a non-empty default even when the
        stale saved runtime config omits it."""
        effective_config = TarotSessionService._read_plugin_config()

        for template_key in TEMPLATE_KEYS:
            assert template_key in effective_config, (
                f"{template_key} missing from effective config — descriptor "
                f"default not merged in"
            )
            assert effective_config[
                template_key
            ], f"{template_key} resolved empty — this is the empty-prompt bug"

    def test_card_explanation_template_matches_descriptor_default(self):
        """The resolved card_explanation_template equals the descriptor default."""
        descriptor_defaults = TarotSessionService._descriptor_defaults()
        effective_config = TarotSessionService._read_plugin_config()

        assert (
            effective_config["card_explanation_template"]
            == descriptor_defaults["card_explanation_template"]
        )
        assert "{{cards_context}}" in effective_config["card_explanation_template"]

    def test_llm_connection_slug_present_from_descriptor(self):
        """The llm_connection_slug key (absent in stale saved config) resolves."""
        effective_config = TarotSessionService._read_plugin_config()
        assert "llm_connection_slug" in effective_config


class TestPromptServiceUsesDescriptorDefaults:
    """The PromptService built from effective config renders non-empty prompts."""

    def test_card_explanation_renders_nonempty(self):
        """render('card_explanation', ...) yields a non-empty prompt with the
        cards context interpolated — the exact path the 500 route exercises."""
        prompt_service = TarotSessionService._initialize_prompt_service()
        assert prompt_service is not None

        cards_context = (
            "PAST: The Fool (Upright)\n  Meaning: New beginnings\n\n"
            "PRESENT: The Magician (Upright)\n  Meaning: Manifestation\n\n"
            "FUTURE: The World (Upright)\n  Meaning: Completion"
        )
        rendered = prompt_service.render(
            "card_explanation",
            {"cards_context": cards_context, "language": "English"},
        )

        assert rendered.strip(), "card_explanation prompt rendered empty"
        assert "The Fool" in rendered
        assert "English" in rendered

    def test_all_templates_render_nonempty(self):
        """No template resolves to an empty prompt after the defaults merge."""
        prompt_service = TarotSessionService._initialize_prompt_service()
        assert prompt_service is not None

        render_contexts = {
            "system_prompt": {},
            "card_interpretation": {
                "card_name": "The Fool",
                "orientation": "Upright",
                "position": "PRESENT",
                "base_meaning": "New beginnings",
                "position_context": "current situation",
            },
            "situation_reading": {
                "situation_text": "A career choice",
                "cards_context": "The Fool",
                "language": "English",
            },
            "card_explanation": {"cards_context": "The Fool", "language": "English"},
            "follow_up_question": {
                "cards_context": "The Fool",
                "question": "What next?",
                "language": "English",
            },
            "initial_greeting": {},
        }
        for slug, context in render_contexts.items():
            rendered = prompt_service.render(slug, context)
            assert rendered.strip(), f"prompt '{slug}' rendered empty"


class TestSavedValuesOverrideDescriptorDefaults:
    """A present saved value wins; a missing key falls back to the default."""

    def test_saved_value_overrides_default(self, monkeypatch):
        monkeypatch.setattr(
            TarotSessionService,
            "_descriptor_defaults",
            staticmethod(
                lambda: {
                    "system_prompt": "DEFAULT system prompt",
                    "card_explanation_template": "DEFAULT explanation {{cards_context}}",
                    "llm_connection_slug": "",
                }
            ),
        )
        monkeypatch.setattr(
            TarotSessionService,
            "_saved_runtime_config",
            staticmethod(
                lambda: {
                    "system_prompt": "OPERATOR system prompt",
                    "llm_connection_slug": "my-connection",
                }
            ),
        )

        effective_config = TarotSessionService._read_plugin_config()

        # Present saved values override the descriptor defaults.
        assert effective_config["system_prompt"] == "OPERATOR system prompt"
        assert effective_config["llm_connection_slug"] == "my-connection"
        # Missing saved key falls back to the descriptor default.
        assert (
            effective_config["card_explanation_template"]
            == "DEFAULT explanation {{cards_context}}"
        )


class TestChatAdapterEmptyPromptGuard:
    """The chat adapter refuses to forward an all-empty prompt to the LLM."""

    def test_empty_prompt_raises_llm_error_before_calling_client(self):
        core_client = MagicMock()
        adapter = session_module.CoreClientChatAdapter(
            core_client, system_prompt="You are a reader."
        )

        with pytest.raises(session_module.LLMError):
            adapter.chat(messages=[{"role": "user", "content": "   "}])

        core_client.chat.assert_not_called()

    def test_nonempty_prompt_still_reaches_client(self):
        core_client = MagicMock()
        core_client.chat.return_value = "The cards reveal insight."
        adapter = session_module.CoreClientChatAdapter(
            core_client, system_prompt="You are a reader."
        )

        reply = adapter.chat(messages=[{"role": "user", "content": "Explain my cards"}])

        assert reply == "The cards reveal insight."
        core_client.chat.assert_called_once()


class TestDescriptorDefaultExtraction:
    """_extract_descriptor_defaults tolerates the {type,default,description} shape."""

    def test_extracts_default_from_descriptor_shape(self):
        descriptor_data = {
            "system_prompt": {
                "type": "string",
                "default": "You are a reader.",
                "description": "the system prompt",
            },
            "base_session_tokens": {
                "type": "number",
                "default": 10,
                "description": "cost",
            },
        }
        defaults = TarotSessionService._extract_descriptor_defaults(descriptor_data)
        assert defaults == {
            "system_prompt": "You are a reader.",
            "base_session_tokens": 10,
        }

    def test_passes_through_non_descriptor_shaped_values(self):
        """A value not in {type,default,description} shape passes through."""
        descriptor_data = {"already_flat": "plain-value", "another": 5}
        defaults = TarotSessionService._extract_descriptor_defaults(descriptor_data)
        assert defaults == {"already_flat": "plain-value", "another": 5}
