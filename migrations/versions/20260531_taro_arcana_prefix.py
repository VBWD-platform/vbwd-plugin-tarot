"""S43.1 — prefix the bare `arcana` table with `taro_` → `taro_arcana`.

`arcana` is created by the monolith `vbwd_001`; this is taro's first own
migration (the `versions/` dir is already registered in alembic.ini). The
`taro_card_draw.arcana_id` FK auto-follows the table rename in Postgres; only its
model FK string changes (separately).

PRESERVES DATA: pure `ALTER TABLE … RENAME` (+ dependent constraint/index
renames) — no drop/recreate. Runs on PROD via `deploy.sh --migrate` in CI:
guarded + idempotent (safe on the monolith-built prod DB, a create_all dev DB,
and re-runs).
"""
import sqlalchemy as sa
from alembic import op

revision = "20260531_taro_arcana_prefix"
down_revision = "vbwd_001"
branch_labels = None
depends_on = None

_RENAMES = {"arcana": "taro_arcana"}


def _table_exists(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


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


def upgrade() -> None:
    conn = op.get_bind()
    for old, new in _RENAMES.items():
        if _table_exists(conn, old) and not _table_exists(conn, new):
            op.rename_table(old, new)
            _rename_dependents(conn, new, old, new)


def downgrade() -> None:
    conn = op.get_bind()
    for old, new in _RENAMES.items():
        if _table_exists(conn, new) and not _table_exists(conn, old):
            _rename_dependents(conn, new, new, old)
            op.rename_table(new, old)
