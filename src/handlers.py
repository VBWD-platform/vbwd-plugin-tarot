"""Event handlers for Tarot plugin - orchestrate business logic."""
import logging
from typing import Optional
from vbwd.extensions import db
from plugins.tarot.src.events import (
    TarotSessionCreatedEvent,
    TarotFollowUpRequestedEvent,
    TarotInterpretationGeneratedEvent,
    TarotFollowUpGeneratedEvent,
)
from plugins.tarot.src.models.arcana import Arcana
from plugins.tarot.src.services.tarot_session_service import TarotSessionService
from plugins.tarot.src.services.arcana_interpretation_service import (
    ArcanaInterpretationService,
)
from plugins.tarot.src.repositories.tarot_card_draw_repository import (
    TarotCardDrawRepository,
)

logger = logging.getLogger(__name__)


class TarotSessionCreatedHandler:
    """Handler for TarotSessionCreatedEvent - generates interpretations and deducts tokens."""

    def __init__(
        self,
        interpreter_service: ArcanaInterpretationService,
        token_service,  # Token service for deducting tokens
        card_draw_repo: TarotCardDrawRepository,
    ):
        """Initialize handler with dependencies."""
        self.interpreter_service = interpreter_service
        self.token_service = token_service
        self.card_draw_repo = card_draw_repo

    def handle(
        self, event: TarotSessionCreatedEvent
    ) -> Optional[TarotInterpretationGeneratedEvent]:
        """Handle TarotSessionCreatedEvent.

        1. Generate LLM interpretations for each card
        2. Update cards with interpretations
        3. Deduct tokens from user
        4. Emit TarotInterpretationGeneratedEvent

        Args:
            event: TarotSessionCreatedEvent

        Returns:
            TarotInterpretationGeneratedEvent if successful
        """
        try:
            assert event.session_id is not None
            total_tokens = event.initial_tokens_consumed

            # Get cards for this session
            cards = self.card_draw_repo.get_session_cards(event.session_id)

            # Generate interpretations for each card
            interpreted_card_ids = []
            for card in cards:
                # Get Arcana data
                arcana = (
                    db.session.query(Arcana).filter(Arcana.id == card.arcana_id).first()
                )

                if not arcana:
                    continue

                # Generate interpretation
                (
                    interpretation,
                    tokens,
                ) = self.interpreter_service.generate_interpretation(
                    arcana=arcana,
                    position=card.position,
                    orientation=card.orientation,
                )

                # Update card with interpretation
                self.card_draw_repo.update_interpretation(str(card.id), interpretation)

                total_tokens += tokens
                interpreted_card_ids.append(str(card.id))

            # Deduct tokens from user balance
            token_success = self.token_service.deduct_tokens(
                user_id=event.user_id,
                tokens=total_tokens,
                reason="tarot_session",
                reference_id=event.session_id,
            )

            if not token_success:
                # Log but don't fail - session already created
                logger.warning("Failed to deduct tokens for user %s", event.user_id)

            # Emit interpretation generated event
            return TarotInterpretationGeneratedEvent(
                card_ids=interpreted_card_ids,
                session_id=event.session_id,
                tokens_used=total_tokens,
                created_at=event.timestamp,
            )

        except Exception:
            logger.exception("Error handling session created event")
            return None


class TarotFollowUpHandler:
    """Handler for TarotFollowUpRequestedEvent - generates follow-up interpretation and deducts tokens."""

    def __init__(
        self,
        interpreter_service: ArcanaInterpretationService,
        session_service: TarotSessionService,
        token_service,  # Token service for deducting tokens
    ):
        """Initialize handler with dependencies."""
        self.interpreter_service = interpreter_service
        self.session_service = session_service
        self.token_service = token_service

    def handle(
        self, event: TarotFollowUpRequestedEvent
    ) -> Optional[TarotFollowUpGeneratedEvent]:
        """Handle TarotFollowUpRequestedEvent.

        1. Validate session exists and is active
        2. Check follow-up count not exceeded
        3. Generate follow-up interpretation via LLM
        4. Create new cards if needed (ADDITIONAL, NEW_SPREAD)
        5. Deduct tokens from user
        6. Increment follow-up count
        7. Emit TarotFollowUpGeneratedEvent

        Args:
            event: TarotFollowUpRequestedEvent

        Returns:
            TarotFollowUpGeneratedEvent if successful
        """
        try:
            assert event.session_id is not None
            assert event.follow_up_type is not None
            assert event.question is not None
            # Validate session exists
            session = self.session_service.get_session(event.session_id)
            if not session:
                logger.warning("Session %s not found", event.session_id)
                return None

            # Get original cards
            original_cards = self.session_service.get_session_spread(event.session_id)

            # Fetch Arcana objects
            original_arcanas = []
            for card in original_cards:
                arcana = (
                    db.session.query(Arcana).filter(Arcana.id == card.arcana_id).first()
                )
                if arcana:
                    original_arcanas.append(arcana)

            # Generate follow-up interpretation
            (
                interpretation,
                tokens,
            ) = self.interpreter_service.generate_follow_up_interpretation(
                original_cards=original_arcanas,
                follow_up_type=event.follow_up_type,
                question=event.question,
            )

            new_card_ids = None

            # Handle different follow-up types
            if event.follow_up_type == "ADDITIONAL":
                # Add one extra card
                extra_cards = self.session_service.arcana_repo.get_random(count=1)
                if extra_cards:
                    card = self.session_service.card_draw_repo.create(
                        session_id=event.session_id,
                        arcana_id=str(extra_cards[0].id),
                        position="ADDITIONAL",  # Custom position for extra card
                        orientation="UPRIGHT",
                        ai_interpretation="",
                    )
                    new_card_ids = [str(card.id)]

            elif event.follow_up_type == "NEW_SPREAD":
                # Generate completely new 3-card spread
                new_spread = self.session_service._generate_spread(session)
                new_card_ids = [str(c.id) for c in new_spread]

            # Deduct follow-up tokens
            follow_up_tokens = tokens + 5  # Base follow-up cost + LLM cost
            token_success = self.token_service.deduct_tokens(
                user_id=event.user_id,
                tokens=follow_up_tokens,
                reason="tarot_follow_up",
                reference_id=event.session_id,
            )

            if not token_success:
                logger.warning(
                    "Failed to deduct follow-up tokens for user %s", event.user_id
                )

            # Increment follow-up count
            updated_session = self.session_service.add_follow_up(event.session_id)
            if not updated_session:
                logger.warning(
                    "Failed to increment follow-up count for session %s",
                    event.session_id,
                )

            # Return follow-up generated event
            return TarotFollowUpGeneratedEvent(
                session_id=event.session_id,
                user_id=event.user_id,
                follow_up_count=updated_session.follow_up_count
                if updated_session
                else 1,
                tokens_consumed=follow_up_tokens,
                interpretation=interpretation,
                new_cards=new_card_ids,
                created_at=event.requested_at,
            )

        except Exception:
            logger.exception("Error handling follow-up event")
            return None
