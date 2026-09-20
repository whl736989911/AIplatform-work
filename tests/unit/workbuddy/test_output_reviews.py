"""A correction replaces what a run produced, and never invents anything (A-09).

The review of an output is deliberately *not* part of the run: the execution has
already settled and its payloads stay the source of truth.  This file pins the one
rule that keeps a correction honest — it may only replace values the workflow
actually produced — because a reviewer who could add keys would be able to make a
later consumer read an invented result as real output.
"""

from __future__ import annotations

import pytest

from octop.infra.errors import OctopError
from octop.infra.workbuddy.runtime import validate_correction

PRODUCED = {"greeting": {"greeting": "hello world"}, "count": 1}


def test_a_correction_may_replace_the_values_the_run_produced() -> None:
    assert validate_correction(PRODUCED, {"count": 7}) == {"count": 7}
    # Replacing every value is allowed; the keys are still the run's own.
    assert validate_correction(PRODUCED, PRODUCED) == PRODUCED


def test_a_correction_refuses_a_key_the_run_never_produced() -> None:
    with pytest.raises(OctopError) as excinfo:
        validate_correction(PRODUCED, {"count": 7, "invented": "x"})
    assert "invented" in excinfo.value.message


def test_a_correction_needs_at_least_one_value() -> None:
    for empty in ({}, None):
        with pytest.raises(OctopError) as excinfo:
            validate_correction(PRODUCED, empty)
        assert "replaces" in excinfo.value.message


def test_a_correction_is_a_subset_not_a_replacement_of_the_whole_output() -> None:
    # Leaving a key out means "this one stays as produced", so the caller gets
    # back only what it actually changed.
    assert set(validate_correction(PRODUCED, {"count": 2})) == {"count"}
