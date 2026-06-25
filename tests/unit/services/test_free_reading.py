"""Unit tests for the anonymous, free bot reading path (S45.3).

The bot consumer needs a reading with **no** persisted session and **no** token
billing (tarot-over-bot is a free teaser). ``draw_free_reading`` reuses tarot's
existing interpretation logic (``arcana_repo.get_random`` + the per-card
interpretation already used by the web spread) without writing a
``TarotSession`` / ``TarotCardDraw`` row and without touching any token service.

These specs use ``MagicMock`` repos (no DB) following the chat unit-test idiom.
"""
from unittest.mock import MagicMock

import pytest

from plugins.tarot.src.enums import CardOrientation, CardPosition
from plugins.tarot.src.services.tarot_session_service import (
    FreeReadingCard,
    TarotSessionService,
)


def _arcana(name: str):
    arcana = MagicMock()
    arcana.name = name
    arcana.upright_meaning = f"{name} upright meaning"
    arcana.reversed_meaning = f"{name} reversed meaning"
    return arcana


@pytest.fixture
def service():
    arcana_repo = MagicMock()
    session_repo = MagicMock()
    card_draw_repo = MagicMock()
    # No LLM adapter / prompt service → interpretation falls back to base meaning.
    return TarotSessionService(
        arcana_repo=arcana_repo,
        session_repo=session_repo,
        card_draw_repo=card_draw_repo,
        llm_adapter=None,
        prompt_service=None,
    )


class TestDrawFreeReadingSingleCard:
    def test_single_card_returns_one_interpreted_card(self, service):
        service.arcana_repo.get_random.return_value = [_arcana("The Star")]

        cards = service.draw_free_reading(card_count=1)

        assert len(cards) == 1
        assert isinstance(cards[0], FreeReadingCard)
        assert cards[0].arcana_name == "The Star"
        assert cards[0].interpretation  # non-empty fallback meaning
        service.arcana_repo.get_random.assert_called_once_with(count=1)

    def test_single_card_persists_nothing(self, service):
        service.arcana_repo.get_random.return_value = [_arcana("The Sun")]

        service.draw_free_reading(card_count=1)

        # No session row, no card-draw row, no token deduction is ever written.
        service.session_repo.create.assert_not_called()
        service.card_draw_repo.create.assert_not_called()
        service.session_repo.update_tokens_consumed.assert_not_called()


class TestDrawFreeReadingFullSpread:
    def test_full_reading_returns_three_positioned_cards(self, service):
        service.arcana_repo.get_random.return_value = [
            _arcana("Past Card"),
            _arcana("Present Card"),
            _arcana("Future Card"),
        ]

        cards = service.draw_free_reading(card_count=3)

        assert len(cards) == 3
        positions = [card.position for card in cards]
        assert positions == [
            CardPosition.PAST.value,
            CardPosition.PRESENT.value,
            CardPosition.FUTURE.value,
        ]
        service.arcana_repo.get_random.assert_called_once_with(count=3)

    def test_full_reading_persists_nothing(self, service):
        service.arcana_repo.get_random.return_value = [
            _arcana("A"),
            _arcana("B"),
            _arcana("C"),
        ]

        service.draw_free_reading(card_count=3)

        service.session_repo.create.assert_not_called()
        service.card_draw_repo.create.assert_not_called()
        service.session_repo.update_tokens_consumed.assert_not_called()

    def test_orientation_is_a_valid_value(self, service):
        service.arcana_repo.get_random.return_value = [_arcana("Card")]

        card = service.draw_free_reading(card_count=1)[0]

        assert card.orientation in {
            CardOrientation.UPRIGHT.value,
            CardOrientation.REVERSED.value,
        }
