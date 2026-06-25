"""S104 — rename the `taro_*` tables to `tarot_*` and migrate the plan
`features` JSONB keys `daily_taro_limit`/`max_taro_follow_ups` to their
`*_tarot_*` spelling.

Follows the same contract as `20260531_taro_arcana_prefix`:
  * PRESERVES DATA — pure `ALTER TABLE … RENAME` (+ dependent constraint/index
    renames); no drop/recreate.
  * IDEMPOTENT + GUARDED — safe on a monolith-built prod DB, a `create_all` dev
    DB, and re-runs; only acts when the source object exists and the target
    does not.
  * STANDALONE — the plan-`features` data step touches `subscription_tarif_plan`
    (owned by the subscription plugin, on which tarot already depends). It is
    guarded by table/column existence, so it is a clean no-op when subscription
    is not installed.

The `tarot_card_draw.arcana_id` FK auto-follows the `taro_arcana` rename in
Postgres; only the model FK string changes (separately, in the rename script).
"""
import sqlalchemy as sa
from alembic import op

revision = "20260625_rename_taro_to_tarot"
down_revision = "20260531_taro_arcana_prefix"
branch_labels = None
depends_on = None

_TABLE_RENAMES = {
    "taro_session": "tarot_session",
    "taro_card_draw": "tarot_card_draw",
    "taro_arcana": "tarot_arcana",
}

_PLAN_TABLE = "subscription_tarif_plan"
_PLAN_COLUMN = "features"
_FEATURE_KEY_RENAMES = {
    "daily_taro_limit": "daily_tarot_limit",
    "max_taro_follow_ups": "max_tarot_follow_ups",
}


def _table_exists(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def _column_exists(conn, table: str, column: str) -> bool:
    insp = sa.inspect(conn)
    if not insp.has_table(table):
        return False
    return any(col["name"] == column for col in insp.get_columns(table))


def _rename_dependents(conn, table: str, frm: str, to: str) -> None:
    """Rename every constraint + plain index on `table` whose name contains
    `frm`, swapping the first occurrence for `to`. Constraints are renamed via
    ALTER TABLE (which also renames their backing index), so the index loop
    EXCLUDES constraint-backed indexes (by oid) to avoid a double rename."""
    constraints = (
        conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t)"
            ),
            {"t": table},
        )
        .scalars()
        .all()
    )
    for name in constraints:
        if frm in name:
            new = name.replace(frm, to, 1)
            op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{name}" TO "{new}"')
    plain_indexes = (
        conn.execute(
            sa.text(
                "SELECT i.relname FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "WHERE x.indrelid = to_regclass(:t) "
                "AND x.indexrelid NOT IN "
                "(SELECT conindid FROM pg_constraint WHERE conindid <> 0)"
            ),
            {"t": table},
        )
        .scalars()
        .all()
    )
    for name in plain_indexes:
        if frm in name:
            op.execute(f'ALTER INDEX "{name}" RENAME TO "{name.replace(frm, to, 1)}"')


def _rename_tables(conn, renames: dict) -> None:
    for old, new in renames.items():
        if _table_exists(conn, old) and not _table_exists(conn, new):
            op.rename_table(old, new)
            _rename_dependents(conn, new, old, new)


def _rename_feature_keys(conn, key_renames: dict) -> None:
    """Rename top-level keys inside the plan `features` document. Works whether
    the column is `json` or `jsonb`; only rows that actually carry the old key
    are touched (objects only — list/array/null `features` are skipped)."""
    if not _column_exists(conn, _PLAN_TABLE, _PLAN_COLUMN):
        return
    data_type = conn.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": _PLAN_TABLE, "c": _PLAN_COLUMN},
    ).scalar()
    cast = "jsonb" if data_type == "jsonb" else "json"
    for old_key, new_key in key_renames.items():
        op.execute(
            sa.text(
                f"UPDATE {_PLAN_TABLE} "
                f"SET {_PLAN_COLUMN} = ("
                f"  ({_PLAN_COLUMN}::jsonb - :old) "
                f"  || jsonb_build_object(:new, {_PLAN_COLUMN}::jsonb -> :old)"
                f")::{cast} "
                f"WHERE jsonb_exists({_PLAN_COLUMN}::jsonb, :old)"
            ).bindparams(old=old_key, new=new_key)
        )


def upgrade() -> None:
    conn = op.get_bind()
    _rename_tables(conn, _TABLE_RENAMES)
    _rename_feature_keys(conn, _FEATURE_KEY_RENAMES)


def downgrade() -> None:
    conn = op.get_bind()
    _rename_feature_keys(conn, {new: old for old, new in _FEATURE_KEY_RENAMES.items()})
    _rename_tables(conn, {new: old for old, new in _TABLE_RENAMES.items()})
