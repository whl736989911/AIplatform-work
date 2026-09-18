"""WorkBuddy tenant export and deletion framework (production entry stays closed until a signed compliance policy exists).

Placeholder created by the wave-0 scaffold so the app factory can mount every
``/api/v1`` WorkBuddy router without depending on another group's branch. It
currently exposes no routes: the implementation lands on ``feat/workbuddy-compliance-lifecycle`` (Group 2)
together with migration ``022_workbuddy_lifecycle``, and replaces this file wholesale.

Nothing here pretends to work: an unimplemented module answers 404 for its paths
instead of returning fabricated data.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
