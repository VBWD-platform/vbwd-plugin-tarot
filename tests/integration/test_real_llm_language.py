"""Integration tests for real LLM communication with language parameters.

These tests use actual LLM API to validate language parameter flow end-to-end.
Requires valid API credentials in vbwd-backend/plugins/config.json.

To run:
    pytest plugins/taro/tests/integration/test_real_llm_language.py -v -s

Environment variables:
    SKIP_LLM_TESTS=1  - Skip real LLM tests (useful for CI without API keys)
"""
import pytest
import json
import os
from uuid import uuid4

from plugins.taro.src.services.taro_session_service import TaroSessionService
from plugins.taro.src.services.prompt_service import PromptService
from plugins.taro.src.repositories.arcana_repository import ArcanaRepository
from plugins.taro.src.repositories.taro_session_repository import TaroSessionRepository
from plugins.taro.src.repositories.taro_card_draw_repository import (
    TaroCardDrawRepository,
)
from plugins.taro.src.models.arcana import Arcana
from plugins.taro.src.enums import ArcanaType
from plugins.taro.src.services.taro_session_service import (
    CoreClientChatAdapter,
    LLMError,
)


def load_taro_config():
    """Load Taro configuration from the aggregate ``plugins/config.json``.

    Drives the real-LLM fixtures (``taro_config`` / ``llm_adapter``): when the
    aggregate has no credentials (the CI / local default) those fixtures skip,
    so this loader must read the aggregate — NOT the plugin's own config.json
    (which ships real credentials and templates). The two config-CONTENT tests
    instead use ``load_taro_plugin_config()`` below to assert the shipped
    template keys.
    """
    config_path = os.path.join(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ),
        "config.json",
    )

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        config = json.load(f)

    return config.get("taro", {})


def load_taro_plugin_config():
    """Load taro's OWN ``plugins/taro/config.json`` — the single source of truth
    that ships the prompt templates (``system_prompt``,
    ``situation_reading_template``, …).

    The aggregate ``plugins/config.json`` only holds persisted overrides, which
    a fresh install seeds from ``DEFAULT_CONFIG`` (base keys, no templates), so
    the config-content tests must assert against this file. It is a flat dict
    (no ``taro`` wrapper), unlike the aggregate.
    """
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "config.json",
    )

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        return json.load(f)


@pytest.fixture
def taro_config():
    """Fixture providing loaded Taro configuration"""
    try:
        return load_taro_config()
    except FileNotFoundError:
        pytest.skip("plugins/config.json not found — skipping config-dependent test")


@pytest.fixture
def llm_adapter(app, taro_config):
    """Real LLM adapter over the CORE client (S97.5).

    The model/endpoint/key now live in a central LLM connection; this fixture
    resolves the core client for taro's ``llm_connection_slug`` (else the active
    default) and skips when no active connection exists (the CI / local default).
    """
    if os.getenv("SKIP_LLM_TESTS") == "1":
        pytest.skip("SKIP_LLM_TESTS=1")

    slug = taro_config.get("llm_connection_slug") or None
    try:
        with app.app_context():
            llm_client = app.container.llm_client(slug=slug)
    except Exception:
        pytest.skip("No active LLM connection configured")

    return CoreClientChatAdapter(
        llm_client,
        system_prompt="You are an expert Tarot card reader providing mystical insights.",
    )


@pytest.fixture
def prompt_service(taro_config):
    """Fixture providing PromptService with config templates"""
    prompts_data = {
        "situation_reading": {
            "template": "You are an expert Tarot card reader.\n\nRESPOND IN {{language}} LANGUAGE.\n\nSituation: {{situation_text}}\n\nCards:\n{{cards_context}}\n\nProvide a brief reading:",  # noqa: E501
            "variables": ["language", "situation_text", "cards_context"],
        },
        "card_explanation": {
            "template": "You are an expert.\n\nRESPOND IN {{language}} LANGUAGE.\n\nCards: {{cards_context}}\n\nExplain the cards:",  # noqa: E501
            "variables": ["language", "cards_context"],
        },
        "follow_up_question": {
            "template": "You are oracle.\n\nRESPOND IN {{language}} LANGUAGE.\n\nQuestion: {{question}}\n\nAnswer:",
            "variables": ["language", "question"],
        },
    }

    return PromptService.from_dict(prompts_data)


@pytest.fixture
def db():
    """Hand back the shared SQLAlchemy object WITHOUT re-isolating.

    The integration conftest's autouse ``_isolate_test`` already wraps every test
    in a rolled-back transaction (swapping ``db.engine`` for a live connection).
    Pulling the function-scoped ``db`` fixture from the plugin-root conftest would
    open a SECOND ``rollback_isolation`` on top of that — capturing the already
    swapped Connection as the "real engine" and failing with
    ``'Connection' object has no attribute 'connect'``. These tests only need the
    db handle to build repositories; isolation is the autouse fixture's job.
    """
    from vbwd.extensions import db as _db

    return _db


@pytest.mark.skipif(
    os.getenv("SKIP_LLM_TESTS") == "1",
    reason="Skipping real LLM tests (set SKIP_LLM_TESTS=1 to skip)",
)
class TestRealLLMLanguageCommunication:
    """Integration tests with real LLM API communication"""

    def _create_sample_arcanas(self, db):
        """Helper to create sample Arcana cards"""
        arcanas = []
        for i in range(3):
            arcana = Arcana(
                number=i,
                name=f"The {['Magician', 'High Priestess', 'Empress'][i]}",
                arcana_type=ArcanaType.MAJOR_ARCANA.value,
                upright_meaning="Upright meaning",
                reversed_meaning="Reversed meaning",
                image_url="https://example.com/card.jpg",
            )
            arcanas.append(arcana)
        db.session.add_all(arcanas)
        db.session.commit()
        return arcanas

    def test_real_llm_situation_reading_with_russian_language(
        self, db, llm_adapter, prompt_service
    ):
        """Test real LLM communication with Russian language instruction"""
        # Setup: Real repositories
        arcana_repo = ArcanaRepository(db.session)
        session_repo = TaroSessionRepository(db.session)
        card_draw_repo = TaroCardDrawRepository(db.session)

        self._create_sample_arcanas(db)

        # Setup: Service with real LLM adapter
        service = TaroSessionService(
            arcana_repo=arcana_repo,
            session_repo=session_repo,
            card_draw_repo=card_draw_repo,
            llm_adapter=llm_adapter,
            prompt_service=prompt_service,
        )

        # Create session
        user_id = str(uuid4())
        session = service.create_session(user_id=user_id)

        # Call service with Russian language
        try:
            result = service.generate_situation_reading(
                session_id=str(session.id),
                situation_text="I am facing a major life decision",
                language="ru",
            )

            # Validate: Got a response
            assert result is not None
            assert len(result) > 0

            # Validate: Response is substantial (not just echo)
            assert len(result) > 10

            # Log the response for manual inspection
            print(f"\n✅ Russian LLM Response:\n{result}\n")

        except LLMError as e:
            pytest.fail(f"LLM API call failed: {e}")

    def test_real_llm_with_german_language(self, db, llm_adapter, prompt_service):
        """Test real LLM communication with German language instruction"""
        arcana_repo = ArcanaRepository(db.session)
        session_repo = TaroSessionRepository(db.session)
        card_draw_repo = TaroCardDrawRepository(db.session)

        self._create_sample_arcanas(db)

        service = TaroSessionService(
            arcana_repo=arcana_repo,
            session_repo=session_repo,
            card_draw_repo=card_draw_repo,
            llm_adapter=llm_adapter,
            prompt_service=prompt_service,
        )

        user_id = str(uuid4())
        session = service.create_session(user_id=user_id)

        try:
            result = service.generate_situation_reading(
                session_id=str(session.id),
                situation_text="Ich habe eine wichtige Entscheidung zu treffen",
                language="de",
            )

            assert result is not None
            assert len(result) > 10

            print(f"\n✅ German LLM Response:\n{result}\n")

        except LLMError as e:
            pytest.fail(f"LLM API call failed: {e}")

    def test_real_llm_with_french_language(self, db, llm_adapter, prompt_service):
        """Test real LLM communication with French language instruction"""
        arcana_repo = ArcanaRepository(db.session)
        session_repo = TaroSessionRepository(db.session)
        card_draw_repo = TaroCardDrawRepository(db.session)

        self._create_sample_arcanas(db)

        service = TaroSessionService(
            arcana_repo=arcana_repo,
            session_repo=session_repo,
            card_draw_repo=card_draw_repo,
            llm_adapter=llm_adapter,
            prompt_service=prompt_service,
        )

        user_id = str(uuid4())
        session = service.create_session(user_id=user_id)

        try:
            result = service.generate_situation_reading(
                session_id=str(session.id),
                situation_text="Je dois prendre une décision importante",
                language="fr",
            )

            assert result is not None
            assert len(result) > 10

            print(f"\n✅ French LLM Response:\n{result}\n")

        except LLMError as e:
            pytest.fail(f"LLM API call failed: {e}")

    def test_real_llm_follow_up_question_with_language(
        self, db, llm_adapter, prompt_service
    ):
        """Test real LLM communication for follow-up questions with language"""
        arcana_repo = ArcanaRepository(db.session)
        session_repo = TaroSessionRepository(db.session)
        card_draw_repo = TaroCardDrawRepository(db.session)

        self._create_sample_arcanas(db)

        service = TaroSessionService(
            arcana_repo=arcana_repo,
            session_repo=session_repo,
            card_draw_repo=card_draw_repo,
            llm_adapter=llm_adapter,
            prompt_service=prompt_service,
        )

        user_id = str(uuid4())
        session = service.create_session(user_id=user_id)

        try:
            # Test with Spanish language
            result = service.answer_oracle_question(
                session_id=str(session.id),
                question="¿Cuál es el siguiente paso?",
                language="es",
            )

            assert result is not None
            assert len(result) > 10

            print(f"\n✅ Spanish Follow-up Response:\n{result}\n")

        except LLMError as e:
            pytest.fail(f"LLM API call failed: {e}")

    def test_real_llm_language_instruction_in_response(
        self, db, llm_adapter, prompt_service
    ):
        """Test that LLM actually receives and respects language instruction"""
        arcana_repo = ArcanaRepository(db.session)
        session_repo = TaroSessionRepository(db.session)
        card_draw_repo = TaroCardDrawRepository(db.session)

        self._create_sample_arcanas(db)

        service = TaroSessionService(
            arcana_repo=arcana_repo,
            session_repo=session_repo,
            card_draw_repo=card_draw_repo,
            llm_adapter=llm_adapter,
            prompt_service=prompt_service,
        )

        user_id = str(uuid4())
        session = service.create_session(user_id=user_id)

        try:
            # Get Russian response
            russian_result = service.generate_situation_reading(
                session_id=str(session.id),
                situation_text="A career question",
                language="ru",
            )

            # Get English response (for comparison)
            english_result = service.generate_situation_reading(
                session_id=str(session.id),
                situation_text="A career question",
                language="en",
            )

            # Validate both got responses
            assert russian_result is not None
            assert english_result is not None
            assert len(russian_result) > 10
            assert len(english_result) > 10

            print(f"\n✅ Russian Response:\n{russian_result}\n")
            print(f"\n✅ English Response:\n{english_result}\n")

            # Note: We can't easily programmatically validate language detection
            # for non-English responses, but manual inspection will confirm

        except LLMError as e:
            pytest.fail(f"LLM API call failed: {e}")

    def test_real_llm_error_handling(self, db, prompt_service):
        """A core LLM failure surfaces as taro's ``LLMError`` to the caller."""
        from unittest.mock import MagicMock

        from vbwd.llm.errors import LlmError

        # A core client whose call fails — wrapped by taro's chat adapter, which
        # must re-raise the failure as taro's own LLMError.
        failing_client = MagicMock()
        failing_client.chat.side_effect = LlmError("invalid credentials")
        bad_llm = CoreClientChatAdapter(failing_client, system_prompt="x")

        arcana_repo = ArcanaRepository(db.session)
        session_repo = TaroSessionRepository(db.session)
        card_draw_repo = TaroCardDrawRepository(db.session)

        self._create_sample_arcanas(db)

        service = TaroSessionService(
            arcana_repo=arcana_repo,
            session_repo=session_repo,
            card_draw_repo=card_draw_repo,
            llm_adapter=bad_llm,
            prompt_service=prompt_service,
        )

        user_id = str(uuid4())
        session = service.create_session(user_id=user_id)

        # Should raise LLMError with invalid credentials
        with pytest.raises(LLMError):
            service.generate_situation_reading(
                session_id=str(session.id), situation_text="Test", language="ru"
            )


class TestLLMConfigurationLoading:
    """Tests for loading and validating LLM configuration"""

    def test_load_taro_config_from_file(self):
        """Taro's own config.json ships the LLM + prompt keys."""
        try:
            config = load_taro_plugin_config()
        except FileNotFoundError:
            pytest.skip("plugins/taro/config.json not found — taro not installed here")

        if not config:
            pytest.skip("plugins/taro/config.json is empty in this environment")

        # Validate required fields exist (the model/endpoint/key now live in the
        # central LLM connection; taro keeps only the connection slug).
        assert "llm_connection_slug" in config
        assert "system_prompt" in config

    def test_taro_config_has_language_templates(self):
        """Taro's own config.json ships the prompt templates."""
        try:
            config = load_taro_plugin_config()
        except FileNotFoundError:
            pytest.skip("plugins/taro/config.json not found — taro not installed here")

        if not config:
            pytest.skip("plugins/taro/config.json is empty in this environment")

        assert "situation_reading_template" in config
        assert "card_explanation_template" in config
        assert "follow_up_question_template" in config

    def test_create_chat_adapter_over_core_client(self):
        """taro's chat adapter wraps a core client and chats through it."""
        from unittest.mock import MagicMock

        core_client = MagicMock()
        core_client.chat.return_value = "A mystical reading."

        adapter = CoreClientChatAdapter(
            core_client, system_prompt="You are a Tarot reader."
        )

        reply = adapter.chat(messages=[{"role": "user", "content": "Draw a card"}])

        assert reply == "A mystical reading."
        _args, call_kwargs = core_client.chat.call_args
        assert call_kwargs["system_prompt"] == "You are a Tarot reader."

    def test_prompt_service_loads_from_config_templates(self, taro_config):
        """Test that PromptService can be created from config templates"""
        prompts_data = {
            "situation_reading": {
                "template": taro_config.get("situation_reading_template", ""),
                "variables": ["language", "situation_text", "cards_context"],
            }
        }

        # Should be able to create PromptService from config
        service = PromptService.from_dict(prompts_data)

        assert service is not None
        assert "situation_reading" in service.prompts
