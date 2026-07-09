"""End-to-end tarot install seed: permission, access level, and tarif plan.

``populate_db()`` must, on a clean database, create the ``tarot.reading.view``
user permission, the ``tarot.user.reading`` access level holding it, and the
"Tarot AI-Reading Sessions" plan whose Features declare
``access_levels: tarot.user.reading``. The ``tarot.user.basic`` level is
DEFERRED — it must NOT be created here.

The seed is CREATE-ONLY / idempotent: re-running creates nothing new and never
rewrites an existing level's permission grants (operators may have edited them).
"""
from vbwd.extensions import db
from vbwd.models.role import Permission
from vbwd.models.user_access_level import AccessLevel

from plugins.subscription.subscription.models.tarif_plan import TarifPlan
from plugins.subscription.subscription.services.plan_feature_access_level_service import (  # noqa: E501
    PlanFeatureAccessLevelService,
)
from plugins.tarot.populate_db import populate_db

READING_PERMISSION = "tarot.reading.view"
DEFERRED_BASIC_LEVEL_SLUG = "tarot.user.basic"
READING_LEVEL_SLUG = "tarot.user.reading"
PLAN_SLUG = "tarot-ai-reading-sessions"


def _reading_level():
    return (
        db.session.query(AccessLevel)
        .filter(AccessLevel.slug == READING_LEVEL_SLUG)
        .first()
    )


def test_seed_creates_permission_level_and_plan():
    populate_db()

    permission = (
        db.session.query(Permission)
        .filter(Permission.name == READING_PERMISSION)
        .first()
    )
    assert permission is not None
    assert permission.resource == "tarot.reading"
    assert permission.action == "view"

    reading = _reading_level()
    assert reading is not None
    assert reading.is_system is True
    assert reading.linked_plan_slug is None
    assert [perm.name for perm in reading.permissions] == [READING_PERMISSION]

    plan = db.session.query(TarifPlan).filter(TarifPlan.slug == PLAN_SLUG).first()
    assert plan is not None
    assert plan.features.get("access_levels") == READING_LEVEL_SLUG


def test_seed_defers_basic_level():
    populate_db()

    assert (
        db.session.query(AccessLevel)
        .filter(AccessLevel.slug == DEFERRED_BASIC_LEVEL_SLUG)
        .first()
        is None
    ), "tarot.user.basic is deferred and must NOT be created by populate_db()"


def test_seed_when_permission_row_absent_uses_get_or_create():
    assert (
        db.session.query(Permission)
        .filter(Permission.name == READING_PERMISSION)
        .first()
        is None
    ), "precondition: the reading permission must not pre-exist on a clean DB"

    populate_db()

    assert (
        db.session.query(Permission)
        .filter(Permission.name == READING_PERMISSION)
        .first()
        is not None
    )


def test_seed_is_idempotent_and_create_only():
    populate_db()

    # An operator hand-edits the reading level: strip its permission grants.
    reading = _reading_level()
    reading.permissions = []
    db.session.flush()

    populate_db()  # second run must be a no-op, not a re-grant

    assert (
        list(_reading_level().permissions) == []
    ), "create-only: re-running must not rewrite a hand-edited level's grants"
    assert (
        db.session.query(AccessLevel)
        .filter(AccessLevel.slug == READING_LEVEL_SLUG)
        .count()
        == 1
    )
    assert db.session.query(TarifPlan).filter(TarifPlan.slug == PLAN_SLUG).count() == 1


def test_seeded_plan_features_parse_to_reading_level():
    populate_db()

    plan = db.session.query(TarifPlan).filter(TarifPlan.slug == PLAN_SLUG).first()
    slugs = PlanFeatureAccessLevelService.parse_access_level_slugs(plan.features)
    assert slugs == [READING_LEVEL_SLUG]
