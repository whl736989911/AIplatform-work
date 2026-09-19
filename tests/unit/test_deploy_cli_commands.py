"""Deploy scripts must invoke octop commands that exist.

The worker tier is the case that motivated this: the entrypoint execs
``octop workbuddy-worker`` while the CLI only ever exposed ``octop workbuddy
worker``, so the production worker container would have died on start with
"Error: No such command" -- a failure no unit test could see, because the script
is only ever read by the container. Reading the scripts here and resolving each
``octop ...`` invocation against the real CLI keeps the deployment and the
command surface from drifting apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from octop.cli.main import cli

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((REPO / "deploy" / "scripts").glob("*.sh"))

# An invocation only counts at a command position -- after a separator, a newline
# or ``exec`` -- so prose inside a message ("octop migration run failed") is not
# mistaken for a command.
_INVOCATION = re.compile(
    r"(?:^|[\n;&|(]|\bexec[ \t]+)[ \t]*octop((?:[ \t]+[A-Za-z0-9][A-Za-z0-9_-]*)*)",
    re.MULTILINE,
)


def _invocations(text: str) -> list[list[str]]:
    """Every ``octop <words>`` command a script runs, without its flags."""
    found: list[list[str]] = []
    for match in _INVOCATION.finditer(text):
        words: list[str] = []
        for word in match.group(1).split():
            if word.startswith("-"):
                break
            words.append(word)
        if words:
            found.append(words)
    return found


def _resolves(words: list[str]) -> bool:
    """Whether the CLI really has this command (and, for a group, this word)."""
    root = click.Context(cli)
    command = cli.get_command(root, words[0])
    if command is None:
        return False
    for word in words[1:]:
        if not isinstance(command, click.Group):
            # The rest are the command's own arguments.
            return True
        nested = command.get_command(click.Context(command), word)
        if nested is None:
            return False
        command = nested
    return True


def test_deploy_scripts_exist() -> None:
    """A guard over nothing guards nothing."""
    assert SCRIPTS, "no deploy scripts were found"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_every_octop_invocation_resolves(script: Path) -> None:
    """Each command the script runs is a command the CLI has."""
    unresolved = [
        " ".join(words)
        for words in _invocations(script.read_text(encoding="utf-8"))
        if not _resolves(words)
    ]
    assert not unresolved, f"{script.name} runs commands the CLI does not have: {unresolved}"


def test_the_worker_entrypoint_names_the_real_worker_command() -> None:
    """The exact regression: the entrypoint must call the command that exists."""
    entrypoint = (REPO / "deploy" / "scripts" / "app-entrypoint.sh").read_text(encoding="utf-8")
    assert "octop workbuddy worker" in entrypoint
    assert "octop workbuddy-worker" not in entrypoint
