"""Oracle: taro tables must be `taro_`-prefixed (sprint S43.1)."""
from plugins.taro.src.models.arcana import Arcana


def test_arcana_table_is_plugin_prefixed():
    assert Arcana.__tablename__ == "taro_arcana"
    assert Arcana.__tablename__.startswith("taro_")
