"""Which schedule items are copies of another satellite's.

The controller's schedule sharing (controller/em_schedule_share.py) stores a
shared item on each receiving satellite under an id of this shape. The Echo
keeps no origin field, so the id is the marker, and this pattern must stay in
step with the controller's (a controller test checks both).

Copies still ring and still answer "stop" on the satellite holding them; they
are left out of the entities and of speech caching because the original
already has both, and a second to-do row or sensor for the same alert is a
duplicate.
"""
import re

_SHARED = re.compile(r"^sh-[0-9a-f]{8}-[0-9a-f]{24}\Z")


def is_shared(item):
    return bool(_SHARED.match(str(item.get("id", ""))))
