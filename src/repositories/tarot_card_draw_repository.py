"""Repository for TarotCardDraw data access."""
from typing import Optional, List
from plugins.tarot.src.models.tarot_card_draw import TarotCardDraw
from plugins.tarot.src.enums import CardPosition, CardOrientation


class TarotCardDrawRepository:
    """Repository for TarotCardDraw model database operations."""

    def __init__(self, session):
        """Initialize repository with database session.

        Args:
            session: SQLAlchemy session for database operations
        """
        self.session = session

    def create(self, **kwargs) -> TarotCardDraw:
        """Create new TarotCardDraw."""
        card = TarotCardDraw(**kwargs)
        self.session.add(card)
        self.session.commit()
        return card

    def get_by_id(self, card_id: str) -> Optional[TarotCardDraw]:
        """Get TarotCardDraw by ID."""
        return (
            self.session.query(TarotCardDraw)
            .filter(TarotCardDraw.id == card_id)
            .first()
        )

    def get_session_cards(self, session_id: str) -> List[TarotCardDraw]:
        """Get all cards in a session, ordered by position (PAST, PRESENT, FUTURE)."""
        position_order = {
            CardPosition.PAST.value: 1,
            CardPosition.PRESENT.value: 2,
            CardPosition.FUTURE.value: 3,
        }

        cards = (
            self.session.query(TarotCardDraw)
            .filter(TarotCardDraw.session_id == session_id)
            .all()
        )

        # Sort by position order
        return sorted(cards, key=lambda c: position_order.get(c.position, 99))

    def get_by_session_and_position(
        self,
        session_id: str,
        position: CardPosition,
    ) -> Optional[TarotCardDraw]:
        """Get specific card from session by position."""
        return (
            self.session.query(TarotCardDraw)
            .filter(
                TarotCardDraw.session_id == session_id,
                TarotCardDraw.position == position.value,
            )
            .first()
        )

    def get_by_arcana(self, arcana_id: str) -> List[TarotCardDraw]:
        """Get all card draws for specific Arcana."""
        return (
            self.session.query(TarotCardDraw)
            .filter(TarotCardDraw.arcana_id == arcana_id)
            .order_by(TarotCardDraw.created_at.desc())
            .all()
        )

    def get_by_orientation(self, orientation: CardOrientation) -> List[TarotCardDraw]:
        """Get all cards with specific orientation."""
        return (
            self.session.query(TarotCardDraw)
            .filter(TarotCardDraw.orientation == orientation.value)
            .order_by(TarotCardDraw.created_at.desc())
            .all()
        )

    def count_session_cards(self, session_id: str) -> int:
        """Count cards in session. Typically 3 (PAST, PRESENT, FUTURE)."""
        return (
            self.session.query(TarotCardDraw)
            .filter(TarotCardDraw.session_id == session_id)
            .count()
        )

    def update_interpretation(self, card_id: str, interpretation: str) -> bool:
        """Update card's AI interpretation. Returns True if updated."""
        card = self.get_by_id(card_id)
        if not card:
            return False

        card.ai_interpretation = interpretation
        self.session.commit()
        return True

    def delete(self, card_id: str) -> bool:
        """Delete TarotCardDraw. Returns True if deleted."""
        card = self.get_by_id(card_id)
        if card:
            self.session.delete(card)
            self.session.commit()
            return True
        return False

    def delete_session_cards(self, session_id: str) -> int:
        """Delete all cards in session. Returns count deleted."""
        count = (
            self.session.query(TarotCardDraw)
            .filter(TarotCardDraw.session_id == session_id)
            .delete()
        )
        self.session.commit()
        return count
