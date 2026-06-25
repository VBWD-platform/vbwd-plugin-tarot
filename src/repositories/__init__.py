"""Tarot plugin repositories."""
from plugins.tarot.src.repositories.arcana_repository import ArcanaRepository
from plugins.tarot.src.repositories.tarot_session_repository import (
    TarotSessionRepository,
)
from plugins.tarot.src.repositories.tarot_card_draw_repository import (
    TarotCardDrawRepository,
)

__all__ = [
    "ArcanaRepository",
    "TarotSessionRepository",
    "TarotCardDrawRepository",
]
