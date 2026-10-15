"""Вход, сессии, CSRF, rate limit, смена пароля — через HTTP API на реальном PostgreSQL."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.logging import configure_logging
from app.models.enums import UserRole
from app.security.sessions import utcnow
from tests.integration.conftest import DEFAULT_PASSWORD, ApiEnv, creds

pytestmark = pytest.mark.integration

NEW_PASSWORD = "Saffron-Glacier-Willow-37"


def pw(current: str, new: str) -> dict[str, str]:
    return {"current_password": current, "new_password": new}


async def test_login_sets_secure_cookies_and_me(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst, email="Analyst.One@Example.org")
    async with api.client() as c:
        r = await c.post("/api/auth/login", json=creds("  analyst.one@EXAMPLE.org ", DEFAULT_PASSWORD))
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "analyst"
        assert body["capabilities"]["manage_users"] is False
        assert body["capabilities"]["upload_documents"] is True
        assert "password" not in json.dumps(body).lower().replace("must_change_password", "")

        set_cookies = r.headers.get_list("set-cookie")
        session_cookie = next(h for h in set_cookies if h.startswith("cai_session="))
        csrf_cookie = next(h for h in set_cookies if h.startswith("cai_csrf="))
        assert "HttpOnly" in session_cookie
        assert "Path=/api" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        assert "Path=/" in csrf_cookie

        me = await c.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["id"] == str(user.id)

    # В БД — только HMAC токена, не сам токен.
    token = c.cookies["cai_session"]
    stored = await api.scalar("SELECT token_hash FROM sessions WHERE user_id = :u", u=user.id)
    assert stored and stored != token and len(stored) == 64
    assert await api.scalar("SELECT last_login_at IS NOT NULL FROM users WHERE id = :u", u=user.id)


async def test_unauthenticated_requests_rejected(api: ApiEnv) -> None:
    async with api.client() as c:
        r = await c.get("/api/auth/me")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "authentication_required"
        c.cookies.set("cai_session", "forged-token", path="/api")
        assert (await c.get("/api/auth/me")).status_code == 401
        assert (await c.get("/api/users")).status_code == 401


async def test_wrong_password_and_unknown_email_are_indistinguishable(api: ApiEnv) -> None:
    await api.create_user(UserRole.analyst, email="known@example.org")
    async with api.client() as c:
        wrong = await c.post("/api/auth/login", json=creds("known@example.org", "Wrong-Password-123"))
        unknown = await c.post("/api/auth/login", json=creds("nobody@example.org", "Wrong-Password-123"))
        malformed = await c.post("/api/auth/login", json=creds("not-an-email", "x"))
    for r in (wrong, unknown, malformed):
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_credentials"
        assert r.json()["error"]["message"] == "Неверный e-mail или пароль"
        assert "set-cookie" not in r.headers


async def test_login_failures_are_audited_without_password(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    async with api.client() as c:
        await c.post("/api/auth/login", json=creds(user.email, "Typed-Secret-Value-1"))
        await c.post("/api/auth/login", json=creds("Typed-Secret-In-Email", "x"))
        await api.login(user, client=c)
    rows = await api.rows("SELECT action, target_id, ip::text, details FROM audit_log ORDER BY id")
    actions = [r[0] for r in rows]
    assert actions == ["auth.login_failed", "auth.login_failed", "auth.login_success"]
    assert rows[0][1] == str(user.id)
    assert rows[0][3]["reason"] == "invalid_password"
    assert rows[1][1] is None and rows[1][3]["reason"] == "unknown_user"
    assert rows[0][2].startswith("203.0.113.10")
    dumped = json.dumps([list(map(str, r)) for r in rows])
    assert "Typed-Secret" not in dumped
    assert DEFAULT_PASSWORD not in dumped


async def test_rate_limit_per_email_including_unknown(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    limit = api.settings.login_max_failures_per_email
    async with api.client() as c:
        for email in (user.email, "ghost@example.org"):
            for _ in range(limit):
                r = await c.post("/api/auth/login", json=creds(email, "Wrong-Password-123"))
                assert r.status_code == 401
            r = await c.post("/api/auth/login", json=creds(email, DEFAULT_PASSWORD))
            assert r.status_code == 429
            assert r.json()["error"]["code"] == "login_rate_limited"
            assert 0 < int(r.headers["Retry-After"]) <= api.settings.login_window_minutes * 60
    locked_until = await api.scalar("SELECT locked_until FROM users WHERE id = :u", u=user.id)
    assert locked_until is not None and locked_until > utcnow()
    assert await api.scalar("SELECT failed_login_count FROM users WHERE id = :u", u=user.id) == limit


async def test_successful_login_resets_email_counter(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    limit = api.settings.login_max_failures_per_email
    async with api.client() as c:
        for _ in range(limit - 1):
            await c.post("/api/auth/login", json=creds(user.email, "Wrong-Password-123"))
        await api.login(user, client=c)
        for _ in range(limit - 1):
            r = await c.post("/api/auth/login", json=creds(user.email, "Wrong-Password-123"))
            assert r.status_code == 401
        await api.login(user, client=c)


async def test_rate_limit_per_ip(api: ApiEnv) -> None:
    api.app.state.login_limiter._max_ip = 3
    user = await api.create_user(UserRole.analyst)
    async with api.client() as c:
        for i in range(3):
            await c.post("/api/auth/login", json=creds(f"u{i}@example.org", "Wrong-Password-1"))
        r = await c.post("/api/auth/login", json=creds(user.email, DEFAULT_PASSWORD))
        assert r.status_code == 429


async def test_blocked_user_cannot_login(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst, is_active=False)
    async with api.client() as c:
        wrong = await c.post("/api/auth/login", json=creds(user.email, "Wrong-Password-123"))
        right = await c.post("/api/auth/login", json=creds(user.email, DEFAULT_PASSWORD))
    assert wrong.status_code == 401  # без верного пароля блокировка не раскрывается
    assert right.status_code == 403
    assert right.json()["error"]["code"] == "account_disabled"
    assert "set-cookie" not in right.headers


async def test_login_requires_allowed_origin(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    for origin in ("http://evil.example", None):
        async with api.client(origin=origin) as c:
            r = await c.post("/api/auth/login", json=creds(user.email, DEFAULT_PASSWORD))
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "csrf_failed"


async def test_csrf_required_for_mutations(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    c = await api.login(user)
    async with c:
        token = c.headers.pop("X-CSRF-Token")
        r = await c.post("/api/auth/sessions/revoke-others")
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
        r = await c.post("/api/auth/sessions/revoke-others", headers={"X-CSRF-Token": "wrong"})
        assert r.status_code == 403
        r = await c.post(
            "/api/auth/sessions/revoke-others",
            headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
        )
        assert r.status_code == 403
        r = await c.post("/api/auth/sessions/revoke-others", headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        # GET не требует CSRF-токена
        assert (await c.get("/api/auth/me")).status_code == 200


async def test_csrf_token_of_other_session_rejected(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    c1 = await api.login(user)
    c2 = await api.login(user)
    async with c1, c2:
        r = await c1.post(
            "/api/auth/sessions/revoke-others", headers={"X-CSRF-Token": c2.headers["X-CSRF-Token"]}
        )
        assert r.status_code == 403


async def test_logout_revokes_session(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    c = await api.login(user)
    async with c:
        token = c.cookies["cai_session"]
        assert (await c.post("/api/auth/logout")).status_code == 204
        c.cookies.set("cai_session", token, path="/api")
        assert (await c.get("/api/auth/me")).status_code == 401
    assert await api.scalar("SELECT revoked_reason FROM sessions WHERE user_id = :u", u=user.id) == "logout"


async def test_session_idle_and_absolute_expiry(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    c = await api.login(user)
    async with c:
        assert (await c.get("/api/auth/me")).status_code == 200
        await api.execute(
            "UPDATE sessions SET idle_expires_at = now() - interval '1 second' WHERE user_id = :u", u=user.id
        )
        assert (await c.get("/api/auth/me")).status_code == 401
    c = await api.login(user)
    async with c:
        await api.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 second' WHERE user_id = :u", u=user.id
        )
        assert (await c.get("/api/auth/me")).status_code == 401


async def test_idle_expiry_is_sliding(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    c = await api.login(user)
    async with c:
        await api.execute(
            "UPDATE sessions SET last_seen_at = now() - interval '10 minutes', "
            "idle_expires_at = now() + interval '1 minute' WHERE user_id = :u",
            u=user.id,
        )
        assert (await c.get("/api/auth/me")).status_code == 200
    idle = await api.scalar("SELECT idle_expires_at FROM sessions WHERE user_id = :u", u=user.id)
    assert idle > utcnow() + timedelta(minutes=api.settings.session_idle_minutes - 1)


async def test_must_change_password_restricts_access(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin, must_change_password=True)
    c = await api.login(admin)
    async with c:
        me = await c.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["must_change_password"] is True
        for path in ("/api/users", "/api/documents", "/api/admin/audit", "/api/auth/sessions"):
            r = await c.get(path)
            if path == "/api/auth/sessions":
                assert r.status_code == 200
                continue
            assert r.status_code == 403, path
            assert r.json()["error"]["code"] == "password_change_required"

        r = await c.post(
            "/api/auth/change-password",
            json=pw(DEFAULT_PASSWORD, NEW_PASSWORD),
        )
        assert r.status_code == 200
        assert r.json()["must_change_password"] is False
        c.headers["X-CSRF-Token"] = c.cookies["cai_csrf"]
        assert (await c.get("/api/users")).status_code == 200


async def test_change_password_rotates_and_revokes_sessions(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.analyst)
    other = await api.login(user)
    c = await api.login(user)
    async with c, other:
        old_token = c.cookies["cai_session"]
        r = await c.post(
            "/api/auth/change-password",
            json=pw(DEFAULT_PASSWORD, NEW_PASSWORD),
        )
        assert r.status_code == 200
        assert c.cookies["cai_session"] != old_token  # новая сессия (защита от фиксации)
        assert (await c.get("/api/auth/me")).status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 401
    async with api.client() as fresh:
        bad = await fresh.post("/api/auth/login", json=creds(user.email, DEFAULT_PASSWORD))
        assert bad.status_code == 401
        await api.login(user, NEW_PASSWORD, client=fresh)
    assert (
        await api.scalar(
            "SELECT count(*) FROM audit_log WHERE action = 'auth.password_changed' AND target_id = :u",
            u=str(user.id),
        )
        == 1
    )


@pytest.mark.parametrize(
    ("current", "new", "code"),
    [
        ("Wrong-Current-Pass-1", NEW_PASSWORD, "invalid_current_password"),
        (DEFAULT_PASSWORD, DEFAULT_PASSWORD, "password_policy"),
        (DEFAULT_PASSWORD, "Qz7!kp", "password_policy"),
        (DEFAULT_PASSWORD, "Password1234", "password_policy"),
    ],
)
async def test_change_password_validation(api: ApiEnv, current: str, new: str, code: str) -> None:
    user = await api.create_user(UserRole.analyst)
    c = await api.login(user)
    async with c:
        r = await c.post("/api/auth/change-password", json=pw(current, new))
        assert r.json()["error"]["code"] == code
        assert new not in r.text and current not in r.text


async def test_own_sessions_list_and_revoke(api: ApiEnv) -> None:
    user = await api.create_user(UserRole.viewer)
    stranger = await api.create_user(UserRole.viewer)
    c1 = await api.login(user)
    c2 = await api.login(user)
    cs = await api.login(stranger)
    async with c1, c2, cs:
        sessions = (await c1.get("/api/auth/sessions")).json()
        assert len(sessions) == 2
        assert sum(s["current"] for s in sessions) == 1
        other_id = next(s["id"] for s in sessions if not s["current"])

        # Чужую сессию отозвать нельзя — 404, как для несуществующей
        stranger_sessions = (await cs.get("/api/auth/sessions")).json()
        r = await c1.post(f"/api/auth/sessions/{stranger_sessions[0]['id']}/revoke")
        assert r.status_code == 404
        assert (await cs.get("/api/auth/me")).status_code == 200

        assert (await c1.post(f"/api/auth/sessions/{other_id}/revoke")).status_code == 204
        assert (await c2.get("/api/auth/me")).status_code == 401
        assert (await c1.get("/api/auth/me")).status_code == 200


async def test_password_rehashed_on_login_when_params_change(api: ApiEnv) -> None:
    from app.security.passwords import PasswordService

    user = await api.create_user(UserRole.analyst)
    old_hash = await api.scalar("SELECT password_hash FROM users WHERE id = :u", u=user.id)
    stronger = PasswordService.__new__(PasswordService)
    from app.security.passwords import HashParams, _hasher

    stronger._ph = _hasher(HashParams(time_cost=2, memory_cost=8 * 1024, parallelism=1))
    stronger._dummy_hash = stronger._ph.hash("x")
    api.app.state.auth_service.passwords = stronger
    c = await api.login(user)
    await c.aclose()
    new_hash = await api.scalar("SELECT password_hash FROM users WHERE id = :u", u=user.id)
    assert new_hash != old_hash and "t=2" in new_hash


async def test_passwords_and_tokens_not_logged(api: ApiEnv, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    user = await api.create_user(UserRole.analyst)
    async with api.client() as c:
        await c.post("/api/auth/login", json=creds(user.email, "Leaked-Wrong-Password-9"))
        await api.login(user, client=c)
        token = c.cookies["cai_session"]
        csrf = c.cookies["cai_csrf"]
        await c.post(
            "/api/auth/change-password",
            json=pw(DEFAULT_PASSWORD, NEW_PASSWORD),
        )
    out = capsys.readouterr().out
    assert "login_success" in out and "login_failed" in out
    for secret in ("Leaked-Wrong-Password-9", DEFAULT_PASSWORD, NEW_PASSWORD, token, csrf):
        assert secret not in out


async def test_csrf_rejection_is_logged_with_reason(api: ApiEnv, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    user = await api.create_user(UserRole.analyst)
    async with api.client(origin="http://127.0.0.1:8080") as c:
        r = await c.post("/api/auth/login", json=creds(user.email, DEFAULT_PASSWORD))
        assert r.status_code == 403
    c2 = await api.login(user)
    async with c2:
        await c2.post("/api/auth/sessions/revoke-others", headers={"X-CSRF-Token": "wrong"})
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if "csrf_rejected" in x]
    assert [(x["reason"], x["origin"]) for x in lines] == [
        ("origin_not_allowed", "http://127.0.0.1:8080"),
        ("token_mismatch", "http://localhost:8080"),
    ]
    assert all(DEFAULT_PASSWORD not in json.dumps(x) for x in lines)
