"""Current schema head, derived from the migrations Octop actually ships.

Migration tests care about *shape*: which tables and columns a fresh database
ends up with. Pinning the head version as a literal makes every new migration
fail those tests for an unrelated reason, so read the head from disk instead.
"""

from __future__ import annotations

from octop.infra.db.migrate import _max_discovered_version

CURRENT_SCHEMA_VERSION = _max_discovered_version("sqlite")
