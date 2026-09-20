"""Refresh tokens: a quiet session renews, and a replayed one kills its family.

An access token is short-lived on purpose, and an active client already gets it
extended (``maybe_sliding_renew_token``).  What a refresh token adds is the two
things a stateless token cannot do: let a client that went quiet for longer than
the access TTL come back without signing in again, and let a sign-out actually
end something.  These tests pin both, plus the property that makes rotation worth
having — a spent token is a replay, and a replay costs the whole family, not just
itself.
"""

from __future__ import annotations

import pytest

from tests.support.auth import TEST_PASSWORD, bearer, bootstrap_admin


@pytest.fixture
async def client(app_client):
    yield app_client


async def _sign_in(client, home) -> dict:
    await bootstrap_admin(client, home)
    r = await client.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_sign_in_hands_out_a_refresh_token(client) -> None:
    c, _srv, home = client
    session = await _sign_in(c, home)
    assert session["access_token"] and session["expires_in"] > 0
    assert session["refresh_token"], session
    # The refresh window outlives the access token — that is the whole point.
    assert session["refresh_expires_in"] > session["expires_in"], session
    assert session["user"]["username"] == "admin"


async def test_a_renewal_rotates_the_token_and_a_replay_kills_the_family(client) -> None:
    c, _srv, home = client
    session = await _sign_in(c, home)

    renewed = await c.post("/api/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert renewed.status_code == 200, renewed.text
    fresh = renewed.json()
    assert fresh["refresh_token"] != session["refresh_token"], fresh
    # The new access token is a working credential.
    me = await c.get("/api/auth/me", headers=bearer(fresh["access_token"]))
    assert me.status_code == 200, me.text

    # Presenting the spent token again can only be a replay: it is refused…
    replay = await c.post("/api/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert replay.status_code == 401, replay.text
    # …and it costs the whole family, so the token issued a moment ago is dead too.
    after = await c.post("/api/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert after.status_code == 401, after.text


async def test_an_unknown_refresh_token_is_refused(client) -> None:
    c, _srv, home = client
    await _sign_in(c, home)
    r = await c.post("/api/auth/refresh", json={"refresh_token": "x" * 40})
    assert r.status_code == 401, r.text


async def test_sign_out_ends_the_session(client) -> None:
    c, _srv, home = client
    session = await _sign_in(c, home)
    out = await c.post(
        "/api/auth/logout",
        json={"refresh_token": session["refresh_token"]},
        headers=bearer(session["access_token"]),
    )
    assert out.status_code == 204, out.text
    # The session is over: nothing can grow another access token out of it.
    r = await c.post("/api/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert r.status_code == 401, r.text


async def test_a_refresh_token_from_another_session_does_not_leak(client) -> None:
    """Two sign-ins are two families: revoking one must not touch the other."""
    c, _srv, home = client
    first = await _sign_in(c, home)
    second = await c.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
    assert second.status_code == 200, second.text
    other = second.json()
    assert other["refresh_token"] != first["refresh_token"]

    spent = await c.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert spent.status_code == 200, spent.text
    replay = await c.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401, replay.text

    # The other sign-in is untouched by the first one's replay.
    alive = await c.post("/api/auth/refresh", json={"refresh_token": other["refresh_token"]})
    assert alive.status_code == 200, alive.text
