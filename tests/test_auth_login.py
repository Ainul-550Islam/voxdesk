"""Authentication: password handling, tokens, lockout, serialization safety."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from sqlalchemy import select

from app.auth import password as pw
from app.auth.jwt import TokenError, create_access_token, decode_access_token
from app.core.config import settings
from app.db.models import AuditAction, AuditLog, UserRole
from tests.conftest import TEST_PASSWORD, auth_headers, login, make_user

# ------------------------------------------------------------- password ---

def test_hash_is_not_the_plaintext():
    h = pw.hash_password(TEST_PASSWORD)
    assert TEST_PASSWORD not in h and h.startswith("$2b$")


def test_same_password_hashes_differently_each_time():
    assert pw.hash_password("Aa1!aaaaaaaa") != pw.hash_password("Aa1!aaaaaaaa")


def test_correct_password_verifies():
    assert pw.verify_password(TEST_PASSWORD, pw.hash_password(TEST_PASSWORD))


def test_incorrect_password_fails():
    assert not pw.verify_password("wrong-password-x1!", pw.hash_password(TEST_PASSWORD))


def test_verify_against_garbage_hash_returns_false_not_raise():
    assert pw.verify_password("x", "not-a-bcrypt-hash") is False
    assert pw.verify_password("x", None) is False


@pytest.mark.parametrize("bad,reason", [
    ("short1!A", "too short"),
    ("alllowercaseletters", "too few classes"),
    ("password", "common"),
])
def test_policy_rejects_weak_passwords(bad, reason):
    with pytest.raises(pw.PasswordPolicyError):
        pw.validate_policy(bad)


def test_policy_rejects_password_containing_email_local_part():
    with pytest.raises(pw.PasswordPolicyError):
        pw.validate_policy("jonathan-Aa1!xyz", email="jonathan@example.com")


def test_policy_accepts_a_strong_password():
    pw.validate_policy(TEST_PASSWORD, email="someone@example.com")


def test_over_72_bytes_is_rejected_not_silently_truncated():
    with pytest.raises(pw.PasswordPolicyError):
        pw.validate_policy("Aa1!" + "x" * 200)


def test_needs_rehash_detects_lower_cost(monkeypatch):
    """A hash cheaper than the current cost must be flagged for upgrade."""
    old = pw.bcrypt.hashpw(b"x", pw.bcrypt.gensalt(rounds=4)).decode()
    monkeypatch.setattr(pw, "BCRYPT_ROUNDS", 6)
    assert pw.needs_rehash(old) is True
    assert pw.needs_rehash(pw.bcrypt.hashpw(b"x", pw.bcrypt.gensalt(rounds=6)).decode()) is False


# ------------------------------------------------------------------ jwt ---

def test_access_token_round_trips():
    uid, tid = uuid.uuid4(), uuid.uuid4()
    token, expires_in = create_access_token(
        user_id=uid, tenant_id=tid, role="admin", token_version=3
    )
    claims = decode_access_token(token)
    assert claims.user_id == uid and claims.tenant_id == tid
    assert claims.role == "admin" and claims.token_version == 3
    assert 0 < expires_in <= settings.access_token_minutes * 60


def test_expired_token_is_rejected():
    token, _ = create_access_token(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="viewer",
        token_version=0, expires_minutes=-1,
    )
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_token_signed_with_another_key_is_rejected():
    token, _ = create_access_token(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="owner", token_version=0
    )
    forged = pyjwt.encode(
        pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                     audience=settings.jwt_audience, issuer=settings.jwt_issuer),
        "attacker-key-attacker-key-attacker", algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_unsigned_alg_none_token_is_rejected():
    """The classic JWT bypass: alg=none must never be accepted."""
    payload = {
        "sub": str(uuid.uuid4()), "tid": str(uuid.uuid4()), "role": "owner",
        "tv": 0, "typ": "access", "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    unsigned = pyjwt.encode(payload, key="", algorithm="none")
    with pytest.raises(TokenError):
        decode_access_token(unsigned)


def test_wrong_audience_is_rejected():
    payload = {
        "sub": str(uuid.uuid4()), "tid": str(uuid.uuid4()), "role": "owner",
        "tv": 0, "typ": "access", "iss": settings.jwt_issuer, "aud": "some-other-api",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    bad = pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(TokenError):
        decode_access_token(bad)


def test_garbage_string_is_rejected():
    with pytest.raises(TokenError):
        decode_access_token("not.a.jwt")


# ------------------------------------------------------ login endpoint ---

@pytest.mark.asyncio
async def test_valid_login_succeeds(client, owner_a):
    resp = await login(client, owner_a.email)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer" and body["access_token"]
    assert body["user"]["email"] == owner_a.email
    assert body["user"]["role"] == "owner"


@pytest.mark.asyncio
async def test_login_sets_httponly_refresh_cookie(client, owner_a):
    resp = await login(client, owner_a.email)
    cookie = resp.headers.get("set-cookie", "")
    assert "voxdesk_refresh=" in cookie and "HttpOnly" in cookie


@pytest.mark.asyncio
async def test_invalid_password_fails(client, owner_a):
    resp = await login(client, owner_a.email, "Definitely-Wrong-1!")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_inactive_user_cannot_login(client, db, tenant_a):
    user = await make_user(db, tenant_a, UserRole.ADMIN, active=False)
    assert (await login(client, user.email)).status_code == 401


@pytest.mark.asyncio
async def test_unknown_and_wrong_password_are_indistinguishable(client, owner_a):
    """No user enumeration: identical status and identical body."""
    unknown = await login(client, "nobody-here@example.com")
    wrong = await login(client, owner_a.email, "Definitely-Wrong-1!")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@pytest.mark.asyncio
async def test_login_response_never_contains_password_hash(client, owner_a):
    resp = await login(client, owner_a.email)
    assert "password" not in resp.text.lower()
    assert "hash" not in resp.text.lower()
    assert "$2b$" not in resp.text


@pytest.mark.asyncio
async def test_account_locks_after_repeated_failures(client, db, tenant_a, monkeypatch):
    monkeypatch.setattr(settings, "max_failed_logins", 3)
    user = await make_user(db, tenant_a, UserRole.VIEWER)
    for _ in range(3):
        await login(client, user.email, "Wrong-Password-1!")
    # Correct password now also fails, because the account is locked.
    assert (await login(client, user.email)).status_code == 401


# ------------------------------------------------------------ protected ---

@pytest.mark.asyncio
async def test_missing_token_is_rejected(client):
    assert (await client.get("/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_malformed_authorization_header_is_rejected(client):
    resp = await client.get("/auth/me", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_user_tenant_and_permissions(client, owner_a):
    headers = await auth_headers(client, owner_a)
    body = (await client.get("/auth/me", headers=headers)).json()
    assert body["user"]["id"] == str(owner_a.id)
    assert body["tenant"]["id"] == str(owner_a.tenant_id)
    assert "tenant:update" in body["permissions"]
    assert "password_hash" not in body["user"]


@pytest.mark.asyncio
async def test_deactivating_a_user_invalidates_their_live_token(client, db, tenant_a, owner_a):
    victim = await make_user(db, tenant_a, UserRole.MANAGER)
    headers = await auth_headers(client, victim)
    assert (await client.get("/auth/me", headers=headers)).status_code == 200

    owner_headers = await auth_headers(client, owner_a)
    resp = await client.patch(
        f"/api/team/users/{victim.id}/active",
        json={"is_active": False}, headers=owner_headers,
    )
    assert resp.status_code == 200
    # token_version was bumped, so the already-issued token stops working.
    assert (await client.get("/auth/me", headers=headers)).status_code == 401


# -------------------------------------------------------------- refresh ---

@pytest.mark.asyncio
async def test_refresh_rotates_and_returns_a_new_access_token(client, owner_a):
    first = await login(client, owner_a.email)
    refreshed = await client.post("/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != first.json()["access_token"]


@pytest.mark.asyncio
async def test_reusing_an_old_refresh_token_is_detected_and_kills_the_chain(client, owner_a):
    await login(client, owner_a.email)
    stolen = client.cookies.get("voxdesk_refresh")
    await client.post("/auth/refresh")          # rotates; `stolen` is now used

    replay = await client.post("/auth/refresh", json={"refresh_token": stolen})
    assert replay.status_code == 401
    # The whole chain is revoked, so the freshly rotated token is dead too.
    assert (await client.post("/auth/refresh")).status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_the_refresh_token(client, owner_a):
    headers = await auth_headers(client, owner_a)
    assert (await client.post("/auth/logout", headers=headers)).status_code == 204
    assert (await client.post("/auth/refresh")).status_code == 401


# ---------------------------------------------------------------- audit ---

@pytest.mark.asyncio
async def test_login_success_and_failure_are_audited(client, db, owner_a):
    await login(client, owner_a.email)
    await login(client, owner_a.email, "Wrong-Password-1!")

    rows = (await db.execute(select(AuditLog))).scalars().all()
    actions = {r.action for r in rows}
    assert AuditAction.LOGIN_SUCCESS in actions
    assert AuditAction.LOGIN_FAILURE in actions


@pytest.mark.asyncio
async def test_audit_log_never_stores_the_password(client, db, owner_a):
    await login(client, owner_a.email, TEST_PASSWORD)
    await login(client, owner_a.email, "Wrong-Password-1!")
    rows = (await db.execute(select(AuditLog))).scalars().all()
    blob = " ".join(f"{r.actor_email} {r.detail}" for r in rows)
    assert TEST_PASSWORD not in blob
    assert "Wrong-Password-1!" not in blob
