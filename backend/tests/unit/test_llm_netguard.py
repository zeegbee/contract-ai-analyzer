"""SSRF-guard: схема, userinfo, loopback/private/link-local/metadata/IPv6, редиректы, DNS rebinding."""

from __future__ import annotations

import httpx
import pytest

from app.llm.errors import BlockedDestination, ProviderUnavailable
from app.llm.netguard import GuardedTransport, NetworkPolicy, classify, parse_base_url
from tests.llm_helpers import PUBLIC_IP, FakeResolver, Recorder

PUBLIC = NetworkPolicy()
PRIVATE_OK = NetworkPolicy(allow_private=True, allow_public=True, allow_http=True)
LOCAL_ONLY = NetworkPolicy(allow_private=True, allow_public=False, allow_http=True)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.org/", "scheme_not_allowed"),
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("gopher://example.org", "scheme_not_allowed"),
        ("https://user:pass@example.org/api", "userinfo_not_allowed"),
        ("https://user@example.org/api", "userinfo_not_allowed"),
        ("https://example.org/api?x=1", "invalid_url"),
        ("https://example.org/api#frag", "invalid_url"),
        ("https:///nohost", "invalid_url"),
        ("https://exa mple.org", "invalid_url"),
        ("https://example.org:99999", "invalid_url"),
        ("http://example.org", "https_required"),
        ("https://169.254.169.254/latest/meta-data", "metadata_blocked"),
        ("https://metadata.google.internal/computeMetadata/v1", "metadata_blocked"),
        ("https://[fd00:ec2::254]/", "metadata_blocked"),
        ("https://100.100.100.200/", "metadata_blocked"),
        ("https://127.0.0.1:11434", "loopback_blocked"),
        ("https://localhost:11434", "loopback_blocked"),
        ("https://[::1]:11434", "loopback_blocked"),
        ("https://[::ffff:127.0.0.1]/", "loopback_blocked"),
        ("https://10.0.0.5/", "private_address_blocked"),
        ("https://192.168.1.10/", "private_address_blocked"),
        ("https://172.16.0.1/", "private_address_blocked"),
        ("https://100.64.0.1/", "private_address_blocked"),
        ("https://[fd12:3456::1]/", "private_address_blocked"),
        ("https://169.254.10.10/", "address_blocked"),
        ("https://[fe80::1]/", "address_blocked"),
        ("https://0.0.0.0/", "address_blocked"),
        ("https://224.0.0.1/", "address_blocked"),
        ("https://255.255.255.255/", "address_blocked"),
        ("https://240.0.0.1/", "address_blocked"),
    ],
)
def test_static_url_rules_for_cloud(url: str, code: str) -> None:
    with pytest.raises(BlockedDestination) as exc:
        parse_base_url(url, PUBLIC)
    assert exc.value.code == code


def test_public_https_allowed() -> None:
    target = parse_base_url("https://openrouter.ai/api/v1/", PUBLIC)
    assert (target.scheme, target.host, target.port, target.path) == (
        "https",
        "openrouter.ai",
        443,
        "/api/v1",
    )


def test_private_network_flag_allows_local_ollama_but_never_metadata() -> None:
    assert parse_base_url("http://10.0.0.5:11434", PRIVATE_OK).literal_ip is not None
    assert parse_base_url("http://127.0.0.1:11434", PRIVATE_OK).port == 11434
    with pytest.raises(BlockedDestination) as exc:
        parse_base_url("http://169.254.169.254/", PRIVATE_OK)
    assert exc.value.code == "metadata_blocked"
    with pytest.raises(BlockedDestination):
        parse_base_url("http://[fe80::1]:11434", PRIVATE_OK)


def test_local_only_policy_rejects_public_addresses() -> None:
    with pytest.raises(BlockedDestination) as exc:
        parse_base_url(f"http://{PUBLIC_IP}:11434", LOCAL_ONLY)
    assert exc.value.code == "public_address_blocked"


def test_classify_unwraps_mapped_and_nat64() -> None:
    import ipaddress

    assert classify(ipaddress.ip_address("::ffff:10.0.0.1")) == "private"
    assert classify(ipaddress.ip_address("64:ff9b::a9fe:a9fe")) == "metadata"  # 169.254.169.254
    assert classify(ipaddress.ip_address("2002:7f00:1::")) == "loopback"  # 6to4 от 127.0.0.1
    assert classify(ipaddress.ip_address(PUBLIC_IP)) == "public"


async def _get(transport: GuardedTransport, url: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        return await client.get(url)


async def test_connects_to_pinned_ip_with_original_host_and_sni() -> None:
    recorder = Recorder(httpx.Response(200, json={}))
    transport = GuardedTransport(PUBLIC, inner=httpx.MockTransport(recorder), resolver=FakeResolver())
    await _get(transport, "https://openrouter.ai/api/v1/models")
    [request] = recorder.requests
    assert request.url.host == PUBLIC_IP
    assert request.headers["host"] == "openrouter.ai"
    assert request.extensions["sni_hostname"] == "openrouter.ai"


async def test_hostname_resolving_to_private_address_is_blocked_before_connect() -> None:
    recorder = Recorder(httpx.Response(200))
    resolver = FakeResolver({"evil.example": ["10.1.2.3"]})
    transport = GuardedTransport(PUBLIC, inner=httpx.MockTransport(recorder), resolver=resolver)
    with pytest.raises(BlockedDestination) as exc:
        await _get(transport, "https://evil.example/")
    assert exc.value.code == "private_address_blocked"
    assert recorder.requests == []


async def test_any_bad_address_in_dns_answer_blocks() -> None:
    resolver = FakeResolver({"mixed.example": [PUBLIC_IP, "169.254.169.254"]})
    transport = GuardedTransport(
        PUBLIC, inner=httpx.MockTransport(Recorder(httpx.Response(200))), resolver=resolver
    )
    with pytest.raises(BlockedDestination) as exc:
        await _get(transport, "https://mixed.example/")
    assert exc.value.code == "metadata_blocked"


async def test_dns_rebinding_checked_on_every_request() -> None:
    recorder = Recorder(httpx.Response(200))
    resolver = FakeResolver({"rebind.example": [[PUBLIC_IP], ["127.0.0.1"]]})
    transport = GuardedTransport(PUBLIC, inner=httpx.MockTransport(recorder), resolver=resolver)
    assert (await _get(transport, "https://rebind.example/")).status_code == 200
    with pytest.raises(BlockedDestination) as exc:
        await _get(transport, "https://rebind.example/")
    assert exc.value.code == "loopback_blocked"
    assert len(recorder.requests) == 1
    assert resolver.calls == ["rebind.example", "rebind.example"]


async def test_numeric_host_forms_are_resolved_and_checked() -> None:
    # «2130706433» = 127.0.0.1: не IP-литерал для ipaddress, но DNS/libc превращает его в loopback.
    resolver = FakeResolver({"2130706433": ["127.0.0.1"]})
    transport = GuardedTransport(
        PUBLIC, inner=httpx.MockTransport(Recorder(httpx.Response(200))), resolver=resolver
    )
    with pytest.raises(BlockedDestination):
        await _get(transport, "https://2130706433/")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_are_errors(status: int) -> None:
    recorder = Recorder(httpx.Response(status, headers={"Location": "http://169.254.169.254/"}))
    transport = GuardedTransport(PUBLIC, inner=httpx.MockTransport(recorder), resolver=FakeResolver())
    with pytest.raises(BlockedDestination) as exc:
        await _get(transport, "https://openrouter.ai/api/v1/models")
    assert exc.value.code == "unexpected_redirect"
    assert len(recorder.requests) == 1


async def test_empty_dns_answer_is_unavailable() -> None:
    transport = GuardedTransport(
        PUBLIC,
        inner=httpx.MockTransport(Recorder(httpx.Response(200))),
        resolver=FakeResolver({"x.example": []}),
    )
    with pytest.raises(ProviderUnavailable):
        await _get(transport, "https://x.example/")
