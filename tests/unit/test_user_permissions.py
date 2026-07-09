"""The tarot plugin declares the ``tarot.reading.view`` user permission (seam).

The core access-management API collects ``user_permissions`` from every enabled
plugin; tarot must contribute the reading permission so it appears in the
catalogue and can be granted through an access level.
"""
from plugins.tarot import TarotPlugin


def test_user_permissions_declares_reading_view():
    permissions = TarotPlugin().user_permissions

    keys = {entry["key"] for entry in permissions}
    assert "tarot.reading.view" in keys

    reading = next(
        entry for entry in permissions if entry["key"] == "tarot.reading.view"
    )
    assert reading["group"] == "Tarot"
    assert reading["label"]
