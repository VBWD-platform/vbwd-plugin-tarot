"""Enumeration types for Tarot plugin."""
import enum


class ArcanaType(enum.Enum):
    """Tarot card arcana type."""

    MAJOR_ARCANA = "MAJOR_ARCANA"
    CUPS = "CUPS"
    WANDS = "WANDS"
    SWORDS = "SWORDS"
    PENTACLES = "PENTACLES"


class CardOrientation(enum.Enum):
    """Card orientation in spread."""

    UPRIGHT = "UPRIGHT"
    REVERSED = "REVERSED"


class CardPosition(enum.Enum):
    """Card position in 3-card spread."""

    PAST = "PAST"
    PRESENT = "PRESENT"
    FUTURE = "FUTURE"


class TarotSessionStatus(enum.Enum):
    """Tarot session status."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
