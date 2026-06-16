"""Integration: taro bot-consumer round-trip over the fake Telegram client (S45.3).

Boots taro + bot-base + bot-telegram, seeds arcanas through taro's repository
(no raw SQL), then drives inbound ``/draw`` / ``/reading`` through the webhook.
Asserts the reading is delivered through bot-base and that **no** token debit
occurs for either an UNLINKED (anonymous) or a LINKED sender — taro-over-bot is
free. The Telegram transport is faked (no network); all data is created through
core services / the taro repository.
"""
import uuid

import pytest

from vbwd.models.enums import TokenTransactionType, UserRole

TARO_BOT_CONFIG = {
    "llm_connection_slug": "",
    "bot_enabled": True,
}

UNLINKED_SENDER = "770001"
LINKED_SENDER = "770002"
CHAT_ID = "880088"


def _register_user(app, email: str):
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    auth_service = app.container.auth_service()
    unique_email = email.replace("@", f"+{uuid.uuid4().hex[:8]}@")
    result = auth_service.register(email=unique_email, password="TaroBot123@")
    db.session.commit()
    user = UserRepository(db.session).find_by_id(result.user_id)
    return str(user.id), result.token


def _promote_to_admin(app, user_id: str) -> None:
    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    repository = UserRepository(db.session)
    user = repository.find_by_id(user_id)
    user.role = UserRole.ADMIN
    db.session.commit()


def _admin_headers(app):
    with app.app_context():
        user_id, token = _register_user(app, "tarobotadmin@example.com")
        _promote_to_admin(app, user_id)
    return {"Authorization": f"Bearer {token}"}


def _create_bot(app, client):
    headers = _admin_headers(app)
    body = {
        "name": f"tarobot-{uuid.uuid4().hex[:6]}",
        "username": "vbwd_taro_bot",
        "token": "222:TARO_BOT_TOKEN",
        "default": True,
        "webhook_secret": "taro-wh-secret",
        "enabled": True,
    }
    response = client.post(
        "/api/v1/plugins/bot-telegram/admin/bots", json=body, headers=headers
    )
    assert response.status_code == 201
    return body["name"], body["webhook_secret"]


def _enable_taro_bot(app, monkeypatch):
    """Turn on the taro bot seam on the live plugin + config store (test-scoped).

    Mutates the already-enabled plugin's config in place (``set_config``) so its
    ENABLED status is preserved — ``initialize`` would reset status to
    INITIALIZED and drop it from ``get_enabled_plugins``.
    """
    plugin = app.plugin_manager.get_plugin("taro")
    for key, value in TARO_BOT_CONFIG.items():
        plugin.set_config(key, value)

    original_get_config = app.config_store.get_config

    def _patched_get_config(plugin_name):
        if plugin_name == "taro":
            return dict(TARO_BOT_CONFIG)
        return original_get_config(plugin_name)

    monkeypatch.setattr(app.config_store, "get_config", _patched_get_config)


def _seed_arcanas(app):
    """Seed a deck through taro's repository (service/repo layer, no raw SQL)."""
    from vbwd.extensions import db
    from plugins.taro.src.repositories.arcana_repository import ArcanaRepository
    from plugins.taro.src.enums import ArcanaType

    repository = ArcanaRepository(db.session)
    if repository.get_all():
        return
    for index in range(5):
        repository.create(
            number=index,
            name=f"Arcana {index}",
            arcana_type=ArcanaType.MAJOR_ARCANA.value,
            upright_meaning=f"Upright meaning {index}",
            reversed_meaning=f"Reversed meaning {index}",
            image_url="https://example.com/card.svg",
        )
    db.session.commit()


def _grant_tokens(app, user_id, amount):
    from vbwd.extensions import db

    app.container.token_service().credit_tokens(
        user_id=uuid.UUID(user_id),
        amount=amount,
        transaction_type=TokenTransactionType.PURCHASE,
        description="integration seed",
    )
    db.session.commit()


def _issue_and_redeem_link(app, user_id, external_user_id):
    """Link a Telegram sender to a vbwd user via a one-time link token (D3)."""
    from vbwd.extensions import db
    from plugins.bot_base.bot_base.repositories.bot_link_repository import (
        BotLinkRepository,
    )
    from plugins.bot_base.bot_base.repositories.bot_link_token_repository import (
        BotLinkTokenRepository,
    )
    from plugins.bot_base.bot_base.services.link_service import LinkService

    link_service = LinkService(
        BotLinkRepository(db.session), BotLinkTokenRepository(db.session)
    )
    token = link_service.issue_token(uuid.UUID(user_id))
    link_service.redeem_token(
        token.token, provider_id="telegram", external_user_id=external_user_id
    )
    db.session.commit()


def _webhook_update(client, bot_name, secret, *, sender_ref, text):
    return client.post(
        f"/api/v1/plugins/bot-telegram/webhook/{bot_name}",
        json={
            "message": {
                "chat": {"id": int(CHAT_ID)},
                "from": {"id": int(sender_ref)},
                "text": text,
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )


@pytest.mark.integration
def test_draw_round_trip_anonymous_bills_nothing(
    app, client, _inject_fake_telegram_client, monkeypatch
):
    fake_telegram = _inject_fake_telegram_client

    _enable_taro_bot(app, monkeypatch)
    bot_name, secret = _create_bot(app, client)

    with app.app_context():
        _seed_arcanas(app)

    # Inbound /draw from an UNLINKED (anonymous) sender → free reading.
    response = _webhook_update(
        client, bot_name, secret, sender_ref=UNLINKED_SENDER, text="/draw"
    )
    assert response.status_code == 200

    delivered = fake_telegram.sent_messages[-1]["payload"]["text"]
    # The drawn card name + position appear in the rendered reading.
    assert "Arcana" in delivered
    assert "PRESENT" in delivered


@pytest.mark.integration
def test_reading_round_trip_linked_sender_bills_nothing(
    app, client, _inject_fake_telegram_client, monkeypatch
):
    fake_telegram = _inject_fake_telegram_client

    _enable_taro_bot(app, monkeypatch)
    bot_name, secret = _create_bot(app, client)

    with app.app_context():
        _seed_arcanas(app)
        user_id, _token = _register_user(app, "tarobotuser@example.com")
        _grant_tokens(app, user_id, 1000)
        _issue_and_redeem_link(app, user_id, LINKED_SENDER)
        starting_balance = app.container.token_service().get_balance(uuid.UUID(user_id))

    # A LINKED sender draws a full /reading — still free, zero debit.
    response = _webhook_update(
        client, bot_name, secret, sender_ref=LINKED_SENDER, text="/reading"
    )
    assert response.status_code == 200

    delivered = fake_telegram.sent_messages[-1]["payload"]["text"]
    assert "PAST" in delivered and "FUTURE" in delivered

    with app.app_context():
        final_balance = app.container.token_service().get_balance(uuid.UUID(user_id))

    # DoD: NO token debit for a taro bot command, even for a linked user.
    assert final_balance == starting_balance
