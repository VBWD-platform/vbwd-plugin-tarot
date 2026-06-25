"""Repository for TarotSession data access."""
from typing import Optional, List
from datetime import datetime
from plugins.tarot.src.models.tarot_session import TarotSession
from plugins.tarot.src.enums import TarotSessionStatus


class TarotSessionRepository:
    """Repository for TarotSession model database operations."""

    def __init__(self, session):
        """Initialize repository with database session.

        Args:
            session: SQLAlchemy session for database operations
        """
        self.session = session

    def create(self, **kwargs) -> TarotSession:
        """Create new TarotSession."""
        session = TarotSession(**kwargs)
        self.session.add(session)
        self.session.commit()
        return session

    def get_by_id(self, session_id: str) -> Optional[TarotSession]:
        """Get TarotSession by ID."""
        return (
            self.session.query(TarotSession)
            .filter(TarotSession.id == session_id)
            .first()
        )

    def get_user_sessions(self, user_id: str) -> List[TarotSession]:
        """Get all sessions for a user, ordered by created_at descending."""
        return (
            self.session.query(TarotSession)
            .filter(TarotSession.user_id == user_id)
            .order_by(TarotSession.created_at.desc())
            .all()
        )

    def get_active_session(self, user_id: str) -> Optional[TarotSession]:
        """Get current ACTIVE session for user. Only one active session per user."""
        return (
            self.session.query(TarotSession)
            .filter(
                TarotSession.user_id == user_id,
                TarotSession.status == TarotSessionStatus.ACTIVE.value,
            )
            .first()
        )

    def get_sessions_by_status(self, status: TarotSessionStatus) -> List[TarotSession]:
        """Get all sessions with specific status."""
        return (
            self.session.query(TarotSession)
            .filter(TarotSession.status == status.value)
            .order_by(TarotSession.created_at.desc())
            .all()
        )

    def get_expired_sessions(
        self,
        before: datetime,
        status_only: Optional[TarotSessionStatus] = None,
    ) -> List[TarotSession]:
        """Get sessions expired before given datetime.

        Args:
            before: Only return sessions with expires_at < this datetime
            status_only: If provided, only return sessions with this status
        """
        query = self.session.query(TarotSession).filter(
            TarotSession.expires_at < before
        )

        if status_only:
            query = query.filter(TarotSession.status == status_only.value)

        return query.order_by(TarotSession.expires_at.desc()).all()

    def update_status(
        self,
        session_id: str,
        status: TarotSessionStatus,
        ended_at: Optional[datetime] = None,
    ) -> bool:
        """Update session status. Returns True if updated, False if not found."""
        session = self.get_by_id(session_id)
        if not session:
            return False

        session.status = status.value
        if ended_at:
            session.ended_at = ended_at

        self.session.commit()
        return True

    def count_user_sessions(self, user_id: str) -> int:
        """Count total sessions for user."""
        return (
            self.session.query(TarotSession)
            .filter(TarotSession.user_id == user_id)
            .count()
        )

    def count_active_sessions(self, user_id: str) -> int:
        """Count active sessions for user. Should typically be 0 or 1."""
        return (
            self.session.query(TarotSession)
            .filter(
                TarotSession.user_id == user_id,
                TarotSession.status == TarotSessionStatus.ACTIVE.value,
            )
            .count()
        )

    def delete(self, session_id: str) -> bool:
        """Delete TarotSession and cascade to TarotCardDraw. Returns True if deleted."""
        session = self.get_by_id(session_id)
        if session:
            self.session.delete(session)
            self.session.commit()
            return True
        return False

    def update_tokens_consumed(self, session_id: str, tokens: int) -> bool:
        """Add tokens to session consumption. Returns True if updated."""
        session = self.get_by_id(session_id)
        if not session:
            return False

        session.tokens_consumed += tokens
        self.session.commit()
        return True

    def increment_follow_up_count(self, session_id: str) -> bool:
        """Increment follow-up question count. Returns True if successful."""
        session = self.get_by_id(session_id)
        if not session:
            return False

        session.follow_up_count += 1
        self.session.commit()
        return True
