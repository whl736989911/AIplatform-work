"""Login / logout / me / change-password."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server, sign_token
from octop.infra.auth.captcha import current_env, ensure_captcha, load_effective, public_config
from octop.infra.db.repos._base import now_ts
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.permissions import effective_permissions
from octop.infra.utils.locale import normalize_locale

router = APIRouter()


def _user_json(user: Any, *, locale: str | None = None) -> dict[str, Any]:
    loc = normalize_locale(locale)
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.value,
        "display_name": user.display_name,
        "locale": loc,
        "permissions": effective_permissions(user),
    }


# --------------------------------------------------------------------------- #
# session renewal (refresh tokens)
# --------------------------------------------------------------------------- #
#
# An access token is short-lived and stateless, and ``maybe_sliding_renew_token``
# already extends it for a client that keeps talking to us.  What that cannot
# cover is a client that goes quiet for longer than the access TTL: it has nothing
# left to present, so today it has to sign in again.  A refresh token closes that
# gap, and because it is stored — hashed — a sign-out can finally mean something.
#
# Only the hash is persisted and every use rotates it.  Presenting a token that was
# already rotated is the signature of a copy being replayed, so the whole family
# (one family == one sign-in) is revoked rather than quietly issuing another.

_REFRESH_TOKEN_BYTES = 32


def _refresh_token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_session(server: Any, user: Any, *, family_id: str | None = None) -> dict[str, Any]:
    """The token pair a sign-in (or a renewal) hands out.

    The plaintext refresh token leaves here and is never stored; only its hash is,
    together with the family it belongs to.  A renewal keeps the family, so a
    replayed ancestor can still revoke every token that grew out of the same
    sign-in.
    """
    secret = server.services.secret_repo.get("jwt")
    ttl = int(server.services.config.access_token_ttl_seconds)
    refresh_ttl = int(server.services.config.refresh_token_ttl_seconds)
    access = sign_token(
        secret, sub=user.id, uname=user.username, role=user.role.value, ttl_seconds=ttl
    )
    refresh = secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)
    server.services.refresh_token_repo.create(
        user_id=user.id,
        token_hash=_refresh_token_hash(refresh),
        family_id=family_id or secrets.token_hex(16),
        expires_at=now_ts() + refresh_ttl,
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ttl,
        "refresh_token": refresh,
        "refresh_expires_in": refresh_ttl,
    }


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class LogoutBody(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=512)


def me_payload(user: Any, server: Any) -> dict[str, Any]:
    """Profile JSON for ``/auth/me`` and OAuth bind/unbind responses."""
    payload = _user_json(user, locale=user.locale)
    row = server.user_manager.get_row(user.id)
    if row is None:
        payload["sso_linked"] = False
        payload["sso_kind"] = None
        payload["sso_identities"] = []
        payload["has_password"] = True
        return payload
    identities = [{"kind": item.kind} for item in server.user_manager.list_sso_identities(user.id)]
    payload["sso_identities"] = identities
    payload["sso_linked"] = bool(identities)
    payload["sso_kind"] = identities[0]["kind"] if identities else None
    payload["has_password"] = row.password_hash is not None
    return payload


class LoginBody(BaseModel):
    username: str
    password: str
    captcha_token: str | None = Field(default=None, max_length=4096)


class CaptchaPublicResponse(BaseModel):
    provider: str
    site_key: str | None = None


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


@router.get(
    "/captcha",
    summary="Public login captcha config",
    response_model=CaptchaPublicResponse,
    response_model_exclude_none=True,
)
async def get_captcha(server: Any = Depends(get_server)) -> CaptchaPublicResponse:
    """Return the active login captcha provider and public site key. No secret."""
    if server.user_manager.count() == 0:
        raise OctopError(ErrorCode.SETUP_REQUIRED, "initial admin not created")
    effective = load_effective(
        server.services.settings_repo,
        server.services.secret_repo,
        current_env(),
    )
    return CaptchaPublicResponse.model_validate(public_config(effective))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


@router.post("/login", summary="Sign in")
async def login(
    body: LoginBody, request: Request, server: Any = Depends(get_server)
) -> dict[str, Any]:
    """Exchange username (or email) and password for a JWT access token and user profile."""
    if server.user_manager.count() == 0:
        raise OctopError(ErrorCode.SETUP_REQUIRED, "initial admin not created")
    server.user_manager.raise_if_login_locked(body.username)
    effective = load_effective(
        server.services.settings_repo,
        server.services.secret_repo,
        current_env(),
    )
    await ensure_captcha(effective, body.captcha_token, _client_ip(request))
    user = await server.user_manager.authenticate(body.username, body.password)
    if user is None:
        raise OctopError(ErrorCode.AUTH_FAILED, "invalid credentials")
    session = issue_session(server, user)
    return {**session, "user": _user_json(user, locale=user.locale)}


@router.post("/refresh", summary="Exchange a refresh token for a new session")
async def refresh(body: RefreshBody, server: Any = Depends(get_server)) -> dict[str, Any]:
    """Rotate the refresh token and hand back a fresh pair.

    Exactly one use per token.  A token that was already rotated can only be a
    replay, so the family is revoked and the caller signs in again — that is the
    whole point of rotating: a stolen copy is worth one attempt, not a session.
    """
    repo = server.services.refresh_token_repo
    row = repo.get_by_hash(_refresh_token_hash(body.refresh_token))
    now = now_ts()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise OctopError(ErrorCode.AUTH_FAILED, "refresh token is not valid")
    if row.rotated_at is not None or not repo.rotate(row.id):
        # Either this token was already spent, or two requests raced for it: both
        # mean a copy is in play, so the family goes rather than a fresh session.
        repo.revoke_family(row.family_id)
        raise OctopError(ErrorCode.AUTH_FAILED, "refresh token was already used; sign in again")
    user = server.user_manager.get_by_id(row.user_id)
    if user is None:
        repo.revoke_family(row.family_id)
        raise OctopError(ErrorCode.USER_DISABLED, "user not active")
    session = issue_session(server, user, family_id=row.family_id)
    return {**session, "user": _user_json(user, locale=user.locale)}


@router.post("/logout", status_code=204, summary="Sign out")
async def logout(
    body: LogoutBody | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    """End the session: revoke its refresh family, then record the audit event.

    The access token itself stays stateless (it expires on its own within its TTL),
    but a caller that hands back its refresh token ends the *session*: no later
    renewal can grow another access token out of it.
    """
    if body is not None and body.refresh_token:
        row = server.services.refresh_token_repo.get_by_hash(
            _refresh_token_hash(body.refresh_token)
        )
        if row is not None:
            server.services.refresh_token_repo.revoke_family(row.family_id)
    server.services.audit_repo.write(actor=user.username, action="auth.logout")
    return Response(status_code=204)


@router.get("/me", summary="Current user profile")
async def me(
    user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, Any]:
    """Return the authenticated user's id, username, role, display name, and locale."""
    return me_payload(user, server)


@router.post("/change-password", status_code=204, summary="Change password")
async def change_password(
    body: ChangePasswordBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    """Verify the old password and set a new one for the current user."""
    await server.user_manager.change_password(user.username, body.old_password, body.new_password)
    return Response(status_code=204)


class UpdateMeBody(BaseModel):
    display_name: str | None = None
    locale: str | None = None


@router.patch("/me", summary="Update profile")
async def update_me(
    body: UpdateMeBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Update the current user's display name and/or locale.

    Use ``model_dump(exclude_unset=True)`` so an explicitly-provided ``null``
    (e.g. ``{"display_name": null}``) clears the field, while an omitted field
    leaves the current value untouched.
    """
    provided = body.model_dump(exclude_unset=True)
    if "display_name" in provided:
        await server.user_manager.set_display_name(user.username, body.display_name)
    if "locale" in provided:
        await server.user_manager.set_locale(user.username, body.locale)
    updated = server.user_manager.get(user.username)
    assert updated is not None
    return _user_json(updated, locale=updated.locale)
