"""TarotSessionService - business logic for Tarot sessions."""
from dataclasses import dataclass
from typing import Optional, Tuple, List
from datetime import timedelta
from vbwd.utils.datetime_utils import utcnow
from vbwd.llm.errors import LlmError
from uuid import uuid4
from random import randint
import os
import logging
import json
from plugins.tarot.src.models.tarot_session import TarotSession
from plugins.tarot.src.models.tarot_card_draw import TarotCardDraw
from plugins.tarot.src.repositories.arcana_repository import ArcanaRepository
from plugins.tarot.src.repositories.tarot_session_repository import (
    TarotSessionRepository,
)
from plugins.tarot.src.repositories.tarot_card_draw_repository import (
    TarotCardDrawRepository,
)
from plugins.tarot.src.enums import TarotSessionStatus, CardPosition, CardOrientation
from plugins.tarot.src.services.prompt_service import PromptService

logger = logging.getLogger(__name__)

# tarot's stable LLM-failure type. Since S97.5 the actual LLM call routes through
# the CORE client (``vbwd.llm``); tarot re-exposes its failure under this name so
# the service + routes keep one error type to catch (no cross-plugin import).
LLMError = LlmError


class CoreClientChatAdapter:
    """Thin ``.chat(messages=…)`` adapter over the CORE LLM client (S97.5).

    tarot's reading flow calls ``self.llm_adapter.chat(messages=…)`` with a
    per-plugin system prompt baked in at construction. The core client takes the
    system prompt per call, so this adapter binds it once and threads it through.
    A core :class:`LlmError` surfaces as tarot's :class:`LLMError` so the existing
    caller contract (and its ``except LLMError``) is preserved.
    """

    def __init__(self, llm_client, *, system_prompt: str) -> None:
        self._llm_client = llm_client
        self._system_prompt = system_prompt

    def chat(self, messages: list) -> str:
        # Defensive guard: an all-empty user prompt is a config misconfig (e.g. a
        # stale runtime config with no template). Fail fast with tarot's own
        # error instead of forwarding an empty message the upstream LLM rejects.
        if not any((message.get("content") or "").strip() for message in messages):
            raise LLMError("Refusing to call the LLM with an empty prompt")
        try:
            return self._llm_client.chat(messages, system_prompt=self._system_prompt)
        except LlmError as core_error:
            raise LLMError(str(core_error)) from core_error


# Positions for a full free reading, in spread order (PAST → PRESENT → FUTURE).
FULL_READING_POSITIONS = [
    CardPosition.PAST,
    CardPosition.PRESENT,
    CardPosition.FUTURE,
]
# A single-card pull stands alone in the PRESENT.
SINGLE_CARD_POSITION = CardPosition.PRESENT


@dataclass(frozen=True)
class FreeReadingCard:
    """An interpreted card from an anonymous, free (non-persisted) reading.

    Produced by :meth:`TarotSessionService.draw_free_reading` for the tarot bot
    consumer (S45.3). It carries no session/user/token state: a free reading is
    never written to the database and never bills tokens — tarot-over-bot is a
    free teaser, paid readings stay web-only.
    """

    arcana_name: str
    orientation: str
    position: str
    interpretation: str


class TarotSessionService:
    """Service for managing Tarot sessions with business logic."""

    # Token consumption costs
    SESSION_BASE_TOKENS = 10  # Fixed cost per session creation
    SITUATION_READING_TOKENS = 15  # Cost for LLM situation reading
    FOLLOW_UP_TOKENS = 5  # Cost per follow-up question
    CARD_EXPLANATION_TOKENS = 10  # Cost for card explanation

    def __init__(
        self,
        arcana_repo: ArcanaRepository,
        session_repo: TarotSessionRepository,
        card_draw_repo: TarotCardDrawRepository,
        llm_adapter: Optional["CoreClientChatAdapter"] = None,
        prompt_service: Optional[PromptService] = None,
    ):
        """Initialize service with repositories, LLM adapter, and prompt service."""
        self.arcana_repo = arcana_repo
        self.session_repo = session_repo
        self.card_draw_repo = card_draw_repo

        # Initialize LLM adapter if not provided
        if llm_adapter is None:
            llm_adapter = self._initialize_llm_adapter()
        self.llm_adapter = llm_adapter

        # Initialize PromptService if not provided
        if prompt_service is None:
            prompt_service = self._initialize_prompt_service()
        self.prompt_service = prompt_service

    @staticmethod
    def _initialize_llm_adapter() -> Optional["CoreClientChatAdapter"]:
        """Resolve the chat adapter from the CORE LLM connection (S97.5).

        tarot keeps only the optional ``llm_connection_slug`` (empty ⇒ the active
        default connection); the model/endpoint/key live in the central LLM
        connection. When no active connection can be resolved (or no app context
        is available) the adapter stays ``None`` and the reading flow degrades to
        the fallback meanings — exactly as before.
        """
        try:
            from flask import current_app

            tarot_config = TarotSessionService._read_plugin_config()
            slug = tarot_config.get("llm_connection_slug") or None
            system_prompt = tarot_config.get(
                "system_prompt",
                "You are an expert Tarot card reader providing mystical insights.",
            )

            llm_client = current_app.container.llm_client(  # type: ignore[attr-defined]
                slug=slug
            )
            return CoreClientChatAdapter(llm_client, system_prompt=system_prompt)
        except Exception as initialization_error:
            logger.warning(
                "No active LLM connection for tarot (%s). "
                "Card interpretations will use fallback meanings.",
                initialization_error,
            )
            return None

    @staticmethod
    def _extract_descriptor_defaults(descriptor_data: dict) -> dict:
        """Flatten a descriptor-shaped config into ``{key: default_value}``.

        The plugin's own ``config.json`` describes each setting as a
        ``{"type", "default", "description"}`` dict; extract its ``default``.
        A value that is not in that shape is passed through unchanged so the
        loader is tolerant of hand-flattened or partial descriptors.
        """
        defaults: dict = {}
        for setting_key, setting_value in descriptor_data.items():
            if isinstance(setting_value, dict) and "default" in setting_value:
                defaults[setting_key] = setting_value["default"]
            else:
                defaults[setting_key] = setting_value
        return defaults

    @staticmethod
    def _descriptor_defaults() -> dict:
        """Descriptor defaults from the plugin's own ``config.json`` (best effort).

        Path: ``plugins/tarot/src/services`` -> up 3 -> ``plugins/tarot``.
        Missing/unreadable descriptor falls back to ``{}`` and never raises.
        """
        tarot_plugin_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        descriptor_path = os.path.join(tarot_plugin_dir, "config.json")
        if not os.path.exists(descriptor_path):
            return {}
        with open(descriptor_path, "r") as descriptor_file:
            descriptor_data = json.load(descriptor_file)
        return TarotSessionService._extract_descriptor_defaults(descriptor_data)

    @staticmethod
    def _saved_runtime_config() -> dict:
        """Operator-saved tarot values from the runtime store (best effort).

        Path: ``plugins/tarot/src/services`` -> up 4 -> ``plugins`` ->
        ``config.json`` -> ``tarot``. Missing/unreadable store falls back to
        ``{}`` and never raises.
        """
        plugins_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        config_path = os.path.join(plugins_dir, "config.json")
        if not os.path.exists(config_path):
            return {}
        with open(config_path, "r") as config_file:
            config_data = json.load(config_file)
        return config_data.get("tarot", {})

    @staticmethod
    def _read_plugin_config() -> dict:
        """Effective tarot config: descriptor defaults merged UNDER saved values.

        Descriptor defaults form the base; operator-saved values override any
        key they define. A key absent from the (possibly stale) saved config
        falls back to its descriptor default — so template keys and
        ``llm_connection_slug`` never resolve to an empty string just because
        the runtime store predates the current schema.
        """
        return {
            **TarotSessionService._descriptor_defaults(),
            **TarotSessionService._saved_runtime_config(),
        }

    @staticmethod
    def _initialize_prompt_service() -> Optional[PromptService]:
        """Initialize PromptService from plugin configuration.

        Reconstructs prompts from flat config structure:
        - system_prompt
        - card_interpretation_template
        - situation_reading_template
        - card_explanation_template
        - follow_up_question_template
        - initial_greeting

        Returns:
            PromptService instance or None if configuration not found
        """
        try:
            # Effective config = descriptor defaults merged UNDER operator-saved
            # values (single source of truth, shared with the LLM adapter).
            tarot_config = TarotSessionService._read_plugin_config()

            # Reconstruct prompts from flat config structure
            # Note: Temperature and max_tokens use global LLM settings (llm_temperature, llm_max_tokens)
            prompts_data = {
                "system_prompt": {
                    "template": tarot_config.get("system_prompt", ""),
                    "variables": [],
                },
                "card_interpretation": {
                    "template": tarot_config.get("card_interpretation_template", ""),
                    "variables": [
                        "card_name",
                        "orientation",
                        "position",
                        "base_meaning",
                        "position_context",
                    ],
                },
                "situation_reading": {
                    "template": tarot_config.get("situation_reading_template", ""),
                    "variables": ["situation_text", "cards_context"],
                },
                "card_explanation": {
                    "template": tarot_config.get("card_explanation_template", ""),
                    "variables": ["cards_context"],
                },
                "follow_up_question": {
                    "template": tarot_config.get("follow_up_question_template", ""),
                    "variables": ["cards_context", "question"],
                },
                "initial_greeting": {
                    "template": tarot_config.get("initial_greeting", ""),
                    "variables": [],
                },
            }

            if not prompts_data:
                logger.warning("No prompts configured in plugin config")
                return None

            return PromptService.from_dict(prompts_data)
        except Exception as e:
            logger.error(f"Failed to initialize PromptService: {e}")
            return None

    def create_session(
        self,
        user_id: str,
        daily_limit: int = 3,
        max_follow_ups: int = 3,
        session_tokens: int = SESSION_BASE_TOKENS,
    ) -> Optional[TarotSession]:
        """Create new Tarot session with 3-card spread.

        Args:
            user_id: User creating session
            daily_limit: Max sessions allowed per day for this user
            max_follow_ups: Max follow-up questions allowed
            session_tokens: Tokens to consume for this session

        Returns:
            TarotSession if created, None if daily limit exceeded
        """
        # Check daily limit
        allowed, _ = self.check_daily_limit(user_id, daily_limit)
        if not allowed:
            return None

        # Create session
        now = utcnow()
        session = self.session_repo.create(
            user_id=user_id,
            status=TarotSessionStatus.ACTIVE.value,
            started_at=now,
            expires_at=now + timedelta(minutes=30),  # 30-minute session
            spread_id=f"spread-{uuid4()}",
            tokens_consumed=session_tokens,
            follow_up_count=0,
            max_follow_ups=max_follow_ups,
        )

        # Generate 3-card spread
        self._generate_spread(session)

        return session

    def _generate_spread(self, session: TarotSession) -> List[TarotCardDraw]:
        """Generate 3-card spread (PAST, PRESENT, FUTURE) for session.

        Args:
            session: TarotSession to generate spread for

        Returns:
            List of 3 TarotCardDraw cards with interpretations
        """
        # Get 3 random Arcanas
        arcanas = self.arcana_repo.get_random(count=3)
        positions = [CardPosition.PAST, CardPosition.PRESENT, CardPosition.FUTURE]

        cards = []
        for arcana, position in zip(arcanas, positions):
            # Randomize orientation: 70% upright, 30% reversed
            is_upright = randint(1, 100) <= 70
            orientation = (
                CardOrientation.UPRIGHT if is_upright else CardOrientation.REVERSED
            )

            # Generate interpretation for this card
            interpretation = self._generate_card_interpretation(
                arcana=arcana,
                position=position,
                orientation=CardOrientation.UPRIGHT
                if is_upright
                else CardOrientation.REVERSED,
            )

            card = self.card_draw_repo.create(
                session_id=str(session.id),
                arcana_id=str(arcana.id),
                position=position.value,
                orientation=orientation.value,
                ai_interpretation=interpretation,
            )
            cards.append(card)

        return cards

    def _generate_card_interpretation(
        self,
        arcana,
        position: CardPosition,
        orientation: CardOrientation,
    ) -> str:
        """Generate LLM interpretation for a single card using PromptService.

        Args:
            arcana: The Arcana model
            position: Position in spread (PAST, PRESENT, FUTURE)
            orientation: Card orientation (UPRIGHT, REVERSED)

        Returns:
            Interpretation string (LLM-generated or base meaning)
        """
        # Try LLM interpretation first
        if self.llm_adapter and self.prompt_service:
            try:
                meaning = (
                    arcana.upright_meaning
                    if orientation == CardOrientation.UPRIGHT
                    else arcana.reversed_meaning
                )

                position_context = {
                    CardPosition.PAST: "This card represents influences from the past",
                    CardPosition.PRESENT: "This card represents the current situation",
                    CardPosition.FUTURE: "This card represents what may come ahead",
                }.get(position, "")

                # Render prompt from PromptService
                prompt = self.prompt_service.render(
                    "card_interpretation",
                    {
                        "card_name": arcana.name,
                        "orientation": orientation.value,
                        "position": position.value,
                        "base_meaning": meaning,
                        "position_context": position_context,
                    },
                )

                response = self.llm_adapter.chat(
                    messages=[{"role": "user", "content": prompt}]
                )

                if response:
                    logger.info(f"Generated interpretation for {arcana.name}")
                    return response.strip()

            except LLMError as e:
                logger.warning(
                    f"LLM error generating interpretation: {e}. Using base meaning."
                )
            except Exception as e:
                logger.error(
                    f"Unexpected error generating interpretation: {e}. Using base meaning."
                )

        # Fallback: use base meaning
        meaning = (
            arcana.upright_meaning
            if orientation == CardOrientation.UPRIGHT
            else arcana.reversed_meaning
        )
        return f"{arcana.name}: {meaning}"

    def draw_free_reading(self, card_count: int = 1) -> List["FreeReadingCard"]:
        """Produce an anonymous, free reading with no persistence and no billing.

        Reuses tarot's existing reading primitives — random Arcana selection
        (:meth:`ArcanaRepository.get_random`), the 70/30 upright/reversed roll,
        and the per-card interpretation (:meth:`_generate_card_interpretation`,
        the same logic the web spread uses) — but writes **no** ``TarotSession``
        or ``TarotCardDraw`` row and debits **no** tokens. This backs the tarot
        bot consumer (S45.3): a free teaser available to any sender, linked or
        not. Paid readings stay on the web ``create_session`` path.

        Args:
            card_count: 1 for a single-card pull (``/draw``), 3 for a full
                reading (``/reading``).

        Returns:
            A list of :class:`FreeReadingCard`, one per drawn Arcana.
        """
        arcanas = self.arcana_repo.get_random(count=card_count)
        if len(arcanas) == 1:
            positions = [SINGLE_CARD_POSITION]
        else:
            positions = FULL_READING_POSITIONS[: len(arcanas)]

        cards: List[FreeReadingCard] = []
        for arcana, position in zip(arcanas, positions):
            is_upright = randint(1, 100) <= 70
            orientation = (
                CardOrientation.UPRIGHT if is_upright else CardOrientation.REVERSED
            )
            interpretation = self._generate_card_interpretation(
                arcana=arcana,
                position=position,
                orientation=orientation,
            )
            cards.append(
                FreeReadingCard(
                    arcana_name=arcana.name,
                    orientation=orientation.value,
                    position=position.value,
                    interpretation=interpretation,
                )
            )

        return cards

    def get_session(self, session_id: str) -> Optional[TarotSession]:
        """Get session by ID."""
        return self.session_repo.get_by_id(session_id)

    def get_user_active_session(self, user_id: str) -> Optional[TarotSession]:
        """Get user's current active session."""
        return self.session_repo.get_active_session(user_id)

    def get_session_spread(self, session_id: str) -> List[TarotCardDraw]:
        """Get 3-card spread for session."""
        return self.card_draw_repo.get_session_cards(session_id)

    def get_user_session_history(
        self, user_id: str, limit: int = 10
    ) -> List[TarotSession]:
        """Get user's session history (for revisiting past readings)."""
        sessions = self.session_repo.get_user_sessions(user_id)
        return sessions[:limit]

    def count_today_sessions(self, user_id: str) -> int:
        """Count ACTIVE sessions created today for user.

        Only ACTIVE sessions count towards the daily limit.
        CLOSED and EXPIRED sessions do not consume the user's quota.
        """
        sessions = self.session_repo.get_user_sessions(user_id)

        today = utcnow().date()
        today_count = sum(
            1
            for s in sessions
            if s.started_at.date() == today
            and s.status == TarotSessionStatus.ACTIVE.value
        )

        return today_count

    def check_daily_limit(
        self,
        user_id: str,
        daily_limit: int,
    ) -> Tuple[bool, int]:
        """Check if user can create session (daily limit).

        Args:
            user_id: User to check
            daily_limit: Max sessions per day for this user

        Returns:
            Tuple of (allowed: bool, remaining: int)
        """
        today_count = self.count_today_sessions(user_id)
        remaining = max(0, daily_limit - today_count)

        return (remaining > 0, remaining)

    def is_session_expired(self, session: TarotSession) -> bool:
        """Check if session has expired."""
        if session.status != TarotSessionStatus.ACTIVE.value:
            return False

        return utcnow() > session.expires_at

    def has_expiry_warning(self, session: TarotSession) -> bool:
        """Check if session should show 3-minute expiry warning."""
        if session.status != TarotSessionStatus.ACTIVE.value:
            return False

        now = utcnow()
        time_until_expiry = (session.expires_at - now).total_seconds()

        # Warning when 3 minutes or less remain
        return 0 < time_until_expiry <= 180

    def add_follow_up(self, session_id: str) -> Optional[TarotSession]:
        """Add follow-up question to session.

        Args:
            session_id: Session to add follow-up to

        Returns:
            Updated session if successful, None if limit exceeded or session not found
        """
        session = self.get_session(session_id)
        if not session:
            return None

        # Check if at max follow-ups
        if session.follow_up_count >= session.max_follow_ups:
            return None

        # Check if session expired
        if self.is_session_expired(session):
            return None

        # Increment follow-up count
        self.session_repo.increment_follow_up_count(session_id)

        return self.get_session(session_id)

    def deduct_tokens(self, session_id: str, tokens: int) -> bool:
        """Deduct tokens from session for LLM operations.

        Args:
            session_id: Session to deduct tokens from
            tokens: Number of tokens to deduct

        Returns:
            True if successful, False if session not found
        """
        try:
            result = self.session_repo.update_tokens_consumed(session_id, tokens)
            if result:
                logger.info(f"Deducted {tokens} tokens from session {session_id}")
            return result
        except Exception as e:
            logger.error(f"Failed to deduct tokens: {e}")
            return False

    def close_session(self, session_id: str) -> bool:
        """Close an active session.

        Args:
            session_id: Session to close

        Returns:
            True if closed, False if not found
        """
        return self.session_repo.update_status(
            session_id,
            TarotSessionStatus.CLOSED,
            ended_at=utcnow(),
        )

    def cleanup_expired_sessions(self) -> int:
        """Mark expired active sessions as EXPIRED.

        Returns:
            Count of sessions updated
        """
        now = utcnow()
        expired_sessions = self.session_repo.get_expired_sessions(before=now)

        count = 0
        for session in expired_sessions:
            if session.status == TarotSessionStatus.ACTIVE.value:
                self.session_repo.update_status(
                    str(session.id), TarotSessionStatus.EXPIRED
                )
                count += 1

        return count

    def add_tokens_consumed(self, session_id: str, tokens: int) -> bool:
        """Add tokens to session consumption (for LLM response cost).

        Args:
            session_id: Session consuming tokens
            tokens: Tokens to add

        Returns:
            True if updated, False if not found
        """
        return self.session_repo.update_tokens_consumed(session_id, tokens)

    def get_user_sessions(
        self, user_id: str, limit: int = 10, offset: int = 0
    ) -> List[TarotSession]:
        """Get user's sessions with pagination support.

        Args:
            user_id: User ID
            limit: Number of sessions to return
            offset: Number of sessions to skip

        Returns:
            List of sessions
        """
        sessions = self.session_repo.get_user_sessions(user_id)
        return sessions[offset : offset + limit]

    def count_user_sessions(self, user_id: str) -> int:
        """Count total sessions for user.

        Args:
            user_id: User ID

        Returns:
            Total count of sessions
        """
        return self.session_repo.count_user_sessions(user_id)

    def get_active_session(self, user_id: str) -> Optional[TarotSession]:
        """Get user's active session.

        Args:
            user_id: User ID

        Returns:
            Active session or None
        """
        return self.get_user_active_session(user_id)

    def reset_today_sessions(self, user_id: str) -> int:
        """Close all active sessions created today for user (admin utility).

        Used by admins to reset a user's daily session counter.
        Closes all ACTIVE sessions created today.

        Args:
            user_id: User ID to reset sessions for

        Returns:
            Count of sessions that were closed
        """
        sessions = self.session_repo.get_user_sessions(user_id)
        today = utcnow().date()

        closed_count = 0
        for session in sessions:
            # Only close ACTIVE sessions created today
            if (
                session.status == TarotSessionStatus.ACTIVE.value
                and session.started_at.date() == today
            ):
                self.session_repo.update_status(
                    str(session.id),
                    TarotSessionStatus.CLOSED,
                    ended_at=utcnow(),
                )
                closed_count += 1

        return closed_count

    def generate_situation_reading(
        self,
        session_id: str,
        situation_text: str,
        language: str = "en",
    ) -> str:
        """Generate LLM-powered contextual reading based on situation and cards.

        Args:
            session_id: Session ID with the 3-card spread
            situation_text: User-provided situation description (max 100 words)
            language: Language code (en, de, es, fr, ja, ru, th, zh). LLM will respond in this language.

        Returns:
            Contextual interpretation from LLM

        Raises:
            ValueError: If situation_text is invalid or session not found
            LLMError: If LLM is unavailable or request fails
        """
        # Validate situation_text
        if not situation_text or not situation_text.strip():
            raise ValueError("Situation text is required and cannot be empty")

        # Count words
        word_count = len(situation_text.split())
        if word_count > 100:
            raise ValueError(f"Situation text must be ≤ 100 words (got {word_count})")

        # Get session and its cards
        session = self.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        cards = self.get_session_spread(session_id)
        if len(cards) != 3:
            raise ValueError(f"Session must have exactly 3 cards, found {len(cards)}")

        # Build context string from cards
        cards_context = self._build_cards_context(cards)

        # LLM is required for situation reading
        if not self.llm_adapter or not self.prompt_service:
            raise LLMError(
                "LLM adapter or PromptService not initialized. Cannot generate situation reading."
            )

        try:
            # Convert language code to full language name
            language_name = self._get_language_name(language)

            # Render prompt from PromptService with language instruction
            prompt = self.prompt_service.render(
                "situation_reading",
                {
                    "situation_text": situation_text,
                    "cards_context": cards_context,
                    "language": language_name,
                },
            )

            response = self.llm_adapter.chat(
                messages=[{"role": "user", "content": prompt}]
            )

            if response:
                logger.info(f"Generated situation reading for session {session_id}")
                # Deduct tokens for LLM operation
                self.deduct_tokens(session_id, self.SITUATION_READING_TOKENS)
                return response.strip()
            else:
                raise LLMError("LLM returned empty response")

        except LLMError as e:
            logger.error(f"LLM error generating situation reading: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error generating situation reading: {e}")
            raise LLMError(f"Failed to generate situation reading: {str(e)}")

    def _build_cards_context(self, cards: List[TarotCardDraw]) -> str:
        """Build a context string describing the cards in the spread.

        Args:
            cards: List of 3 TarotCardDraw cards

        Returns:
            Formatted string describing the cards
        """
        position_names = {
            CardPosition.PAST.value: "PAST",
            CardPosition.PRESENT.value: "PRESENT",
            CardPosition.FUTURE.value: "FUTURE",
        }

        context_parts = []
        for card in sorted(
            cards,
            key=lambda c: list(position_names.values()).index(
                position_names.get(c.position, "")
            ),
        ):
            pos_name = position_names.get(card.position, "Unknown")
            orientation_name = (
                "Upright"
                if card.orientation == CardOrientation.UPRIGHT.value
                else "Reversed"
            )
            context_parts.append(
                f"{pos_name}: {card.arcana.name} ({orientation_name})\n"
                f"  Meaning: {card.ai_interpretation}"
            )

        return "\n\n".join(context_parts)

    @staticmethod
    def _get_language_name(language_code: str) -> str:
        """Convert language code to full language name for LLM prompts.

        Args:
            language_code: Language code (en, de, es, etc.)

        Returns:
            Full language name, defaults to English if code not found
        """
        language_names = {
            "en": "English",
            "de": "Deutsch (German)",
            "es": "Español (Spanish)",
            "fr": "Français (French)",
            "ja": "日本語 (Japanese)",
            "ru": "Русский (Russian)",
            "th": "ไทย (Thai)",
            "zh": "中文 (Chinese)",
        }
        return language_names.get(language_code.lower(), "English")

    def answer_oracle_question(
        self,
        session_id: str,
        question: str,
        language: str = "en",
    ) -> str:
        """Answer a follow-up question about the reading in the chat.

        Args:
            session_id: Session ID
            question: User's follow-up question
            language: Language code (en, de, es, fr, ja, ru, th, zh). LLM will respond in this language.

        Returns:
            Oracle's answer to the question

        Raises:
            ValueError: If question is invalid or session not found
            LLMError: If LLM is unavailable or request fails
        """
        # Validate input
        if not question or not question.strip():
            raise ValueError("Question is required")

        # Get session and cards
        session = self.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        cards = self.get_session_spread(session_id)
        if len(cards) != 3:
            raise ValueError(f"Session must have exactly 3 cards, found {len(cards)}")

        cards_context = self._build_cards_context(cards)

        # LLM is required for answering questions
        if not self.llm_adapter or not self.prompt_service:
            raise LLMError(
                "LLM adapter or PromptService not initialized. Cannot answer question."
            )

        try:
            # Convert language code to full language name
            language_name = self._get_language_name(language)

            # Render prompt from PromptService with language instruction
            prompt = self.prompt_service.render(
                "follow_up_question",
                {
                    "cards_context": cards_context,
                    "question": question,
                    "language": language_name,
                },
            )

            response = self.llm_adapter.chat(
                messages=[{"role": "user", "content": prompt}]
            )

            if response:
                logger.info(f"Answered oracle question for session {session_id}")
                # Deduct tokens for LLM operation
                self.deduct_tokens(session_id, self.FOLLOW_UP_TOKENS)
                return response.strip()
            else:
                raise LLMError("LLM returned empty response")

        except LLMError as e:
            logger.error(f"LLM error answering question: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error answering question: {e}")
            raise LLMError(f"Failed to answer question: {str(e)}")
