"""Tarot card reading plugin with LLM-powered interpretations."""
from typing import Optional, Dict, Any, List, TYPE_CHECKING
from vbwd.plugins.base import BasePlugin, PluginMetadata

if TYPE_CHECKING:
    from flask import Blueprint

    # Bot-base is an OPTIONAL bridge (S45.3 / D1 inversion). It is imported only
    # for type checking here; at runtime the bot-consumer methods lazily import
    # the neutral DTOs inside their bodies, so ``tarot`` imports cleanly even when
    # bot-base is absent (no hard dependency, no top-level ``bot_base`` import).
    from plugins.bot_base.bot_base.types import BotCommand, BotInbound, BotReply
    from plugins.tarot.src.services.tarot_session_service import (
        FreeReadingCard,
        TarotSessionService,
    )


DEFAULT_CONFIG = {
    # The model/endpoint/key now live in a CORE "LLM Connection" (S97). tarot
    # keeps only the optional slug of the connection to use; empty ⇒ the active
    # default connection.
    "llm_connection_slug": "",
    "session_duration_minutes": 30,
    "session_expiry_warning_minutes": 3,
    "base_session_tokens": 10,
    "follow_up_base_tokens": 5,
    "bot_enabled": False,
}

BOT_NAMESPACE = "tarot"
DRAW_COMMAND = "draw"
READING_COMMAND = "reading"
SINGLE_CARD_COUNT = 1
FULL_READING_CARD_COUNT = 3


class TarotPlugin(BasePlugin):
    """Tarot card reading with AI-powered interpretations.

    Class MUST be defined in __init__.py (not re-exported) due to
    discovery check obj.__module__ != full_module in manager.py.

    Also a bot-base **consumer** (S45.3): it structurally implements the
    ``BotCommandProvider`` seam (``bot_namespace`` + ``get_bot_commands`` +
    ``handle_action``) so its commands light up over every bot adapter
    (Telegram now, meinchat later) with no consumer change. The bridge is
    optional — see the lazy imports below. tarot is the *second* independent
    consumer on the seam, proving the bridge serves more than one plugin.

    All tarot bot commands are FREE + ANONYMOUS: no link required, no token
    billing, no identity mutation. tarot-over-bot is a free teaser; paid
    readings stay web-only via the ``create_session`` path.
    """

    #: The owning namespace bot-base routes commands / taps to (D1/D7).
    bot_namespace = BOT_NAMESPACE

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="tarot",
            version="1.0.0",
            author="VBWD Team",
            description="Tarot card reading with LLM-powered interpretations",
            dependencies=["subscription"],
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Initialize with defaults merged with provided config."""
        merged = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.tarot.src.routes import tarot_bp

        return tarot_bp

    def get_url_prefix(self) -> Optional[str]:
        return "/api/v1/tarot"

    @property
    def admin_permissions(self):
        return [
            {"key": "tarot.sessions.view", "label": "View sessions", "group": "Tarot"},
            {"key": "tarot.arcana.manage", "label": "Manage arcana", "group": "Tarot"},
            {"key": "tarot.configure", "label": "Tarot settings", "group": "Tarot"},
        ]

    def on_enable(self) -> None:
        pass

    def on_disable(self) -> None:
        pass

    # ── bot-base consumer seam (S45.3) ───────────────────────────────────────
    def get_bot_commands(self) -> List["BotCommand"]:
        """The commands ``tarot`` contributes to the bot menu.

        Returns ``[]`` when ``bot_enabled`` is false so bot-base's
        ``CommandRegistry`` never surfaces ``/draw`` / ``/reading`` — tarot web
        behavior stays entirely untouched. The neutral ``BotCommand`` DTO is
        imported lazily so this module loads even when bot-base is absent.
        """
        if not self.get_config("bot_enabled", False):
            return []

        from plugins.bot_base.bot_base.types import BotCommand

        return [
            BotCommand(
                name=DRAW_COMMAND,
                description="Pull a single tarot card (free)",
                namespace=BOT_NAMESPACE,
            ),
            BotCommand(
                name=READING_COMMAND,
                description="Get a full three-card tarot reading (free)",
                namespace=BOT_NAMESPACE,
            ),
        ]

    def handle_action(self, context: "BotInbound") -> "BotReply":
        """Handle a ``/draw`` / ``/reading`` command or a tapped choice (D7).

        Every tarot bot command is FREE + ANONYMOUS: a reading is produced for
        any sender (linked or not), with no token debit and no identity
        mutation. ``/reading`` is a full three-card spread; everything else
        (``/draw`` or a tapped card choice) is a single-card pull.
        """
        card_count = (
            FULL_READING_CARD_COUNT
            if context.command == READING_COMMAND
            else SINGLE_CARD_COUNT
        )
        reading_service = self._build_reading_service()
        cards = reading_service.draw_free_reading(card_count=card_count)
        return self._render_reading(cards)

    def _render_reading(self, cards: List["FreeReadingCard"]) -> "BotReply":
        """Render the drawn cards as a provider-neutral :class:`BotReply`."""
        from plugins.bot_base.bot_base.types import BotReply

        lines = [
            f"{card.position} — {card.arcana_name} ({card.orientation})\n"
            f"{card.interpretation}"
            for card in cards
        ]
        return BotReply(text="\n\n".join(lines))

    def _build_reading_service(self) -> "TarotSessionService":
        """Build the same reading service the web route uses (DRY).

        Resolves the service off the live ``db.session`` exactly as the web
        ``/api/v1/tarot`` routes do — so a bot reading reuses tarot's existing
        interpretation logic with zero new reading code. No token service is
        resolved here: the bot path never bills.
        """
        from vbwd.extensions import db
        from plugins.tarot.src.repositories.arcana_repository import ArcanaRepository
        from plugins.tarot.src.repositories.tarot_session_repository import (
            TarotSessionRepository,
        )
        from plugins.tarot.src.repositories.tarot_card_draw_repository import (
            TarotCardDrawRepository,
        )
        from plugins.tarot.src.services.tarot_session_service import TarotSessionService

        return TarotSessionService(
            arcana_repo=ArcanaRepository(db.session),
            session_repo=TarotSessionRepository(db.session),
            card_draw_repo=TarotCardDrawRepository(db.session),
        )
