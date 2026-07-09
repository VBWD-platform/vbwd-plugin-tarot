"""Tarot install seed — user permission, access levels, and the reading plan.

Run at install time (after ``flask seed-rbac``, which runs BEFORE tarot is
enabled — so tarot's ``user_permissions`` are NOT in the catalogue yet and the
``tarot.reading.view`` row may be absent). This seed therefore GET-OR-CREATEs
that permission, then creates one access level and a recurring tarif plan:

* ``tarot.user.reading`` — holds ``tarot.reading.view`` (unlocks the fe-user
  burger-menu entry and the readings page).
* "Tarot AI-Reading Sessions" plan — its Features declare
  ``access_levels: tarot.user.reading`` so the subscription plugin's
  ``AccessLevelHandler`` grants the level on ``subscription.activated`` (the
  supported grant path — NOT ``linked_plan_slug``).

CREATE-ONLY / idempotent: every step skips when the row already exists and NEVER
rewrites an existing access level's permission grants — operators may have edited
them. Safe to re-run; a second run changes nothing.
"""
import logging
from decimal import Decimal
from uuid import uuid4

logger = logging.getLogger(__name__)

READING_PERMISSION = "tarot.reading.view"

READING_LEVEL = {
    "slug": "tarot.user.reading",
    "name": "Tarot Reading",
    "description": "Grants access to AI-powered tarot readings.",
    "permissions": [READING_PERMISSION],
}

READING_PLAN_SLUG = "tarot-ai-reading-sessions"
READING_PLAN_NAME = "Tarot AI-Reading Sessions"
READING_PLAN_PRICE = 9.0


def populate_db():
    """Seed tarot's user permission, access levels, and reading plan (idempotent).

    Must run inside an existing Flask app context (the install shell and the
    ``__main__`` block below both provide one). Commits once at the end — this is
    a script, not an event callback.
    """
    from vbwd.extensions import db

    session = db.session

    permission = _get_or_create_permission(session, READING_PERMISSION)
    _get_or_create_access_level(session, READING_LEVEL, permission_rows=[permission])
    _get_or_create_reading_plan(session)

    session.commit()


def _get_or_create_permission(session, name):
    """Return the permission row by name, creating it the way core does."""
    from vbwd.models.role import Permission

    existing = session.query(Permission).filter(Permission.name == name).first()
    if existing is not None:
        return existing

    resource, _, action = name.rpartition(".")
    permission = Permission(
        id=uuid4(),
        name=name,
        resource=resource or name,
        action=action or name,
        description=name,
    )
    session.add(permission)
    session.flush()
    return permission


def _get_or_create_access_level(session, level_spec, permission_rows):
    """Create an access level once (CREATE-ONLY — never rewrites existing grants)."""
    from vbwd.models.user_access_level import AccessLevel

    existing = (
        session.query(AccessLevel)
        .filter(AccessLevel.slug == level_spec["slug"])
        .first()
    )
    if existing is not None:
        # Never touch an operator-edited level's permission grants.
        return existing

    level = AccessLevel(
        name=level_spec["name"],
        slug=level_spec["slug"],
        description=level_spec["description"],
        is_system=True,
        linked_plan_slug=None,
        permissions=list(permission_rows),
    )
    session.add(level)
    session.flush()
    return level


def _get_or_create_reading_plan(session):
    """Create the recurring reading plan once; its Features grant the level."""
    from vbwd.models.currency import Currency
    from vbwd.models.enums import BillingPeriod

    from plugins.subscription.subscription.models.tarif_plan import TarifPlan

    existing = (
        session.query(TarifPlan).filter(TarifPlan.slug == READING_PLAN_SLUG).first()
    )
    if existing is not None:
        return existing

    if session.query(Currency).filter_by(code="EUR").first() is None:
        session.add(
            Currency(
                code="EUR",
                name="Euro",
                symbol="€",
                exchange_rate=Decimal("1.0"),
            )
        )
        session.flush()

    plan = TarifPlan(
        name=READING_PLAN_NAME,
        slug=READING_PLAN_SLUG,
        description="Recurring access to AI-powered tarot readings.",
        price=READING_PLAN_PRICE,
        billing_period=BillingPeriod.MONTHLY,
        trial_days=0,
        features={"access_levels": READING_LEVEL["slug"]},
        is_active=True,
        sort_order=0,
    )
    session.add(plan)
    session.flush()
    return plan


if __name__ == "__main__":
    from vbwd.app import create_app

    flask_app = create_app()
    with flask_app.app_context():
        populate_db()
