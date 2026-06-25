"""Oracle: tarot tables must be `tarot_`-prefixed (sprint S43.1)."""
from plugins.tarot.src.models.arcana import Arcana


def test_arcana_table_is_plugin_prefixed():
    assert Arcana.__tablename__ == "tarot_arcana"
    assert Arcana.__tablename__.startswith("tarot_")
