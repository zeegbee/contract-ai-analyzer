"""Защита настраиваемых адресов провайдеров от SSRF (THREAT_MODEL §4.5).

- Только `http`/`https`, без userinfo, query и fragment; `http` — только при разрешённой частной сети.
- Имя разрешается в DNS при каждом запросе; проверяются **все** адреса; подключение идёт к
  проверенному IP (pinning), а `Host`/SNI и проверка сертификата — по исходному имени. Так подмена
  ответа DNS между проверкой и подключением (DNS rebinding) не приводит к обращению на другой адрес.
- Metadata-адреса облаков, link-local, multicast, unspecified и зарезервированные диапазоны
  запрещены всегда; loopback, частные сети и CGNAT — только с `allow_private_network`.
- Для «локального» провайдера (`allow_public=False`) запрещены публичные адреса: текст договора
  не уйдёт в интернет, даже если DNS-имя локальной Ollama начнёт указывать наружу.
- Любой ответ 3xx — ошибка (редиректы не выполняются).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.llm.errors import BlockedDestination, ProviderUnavailable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]

DNS_TIMEOUT_SECONDS = 5.0

# Адреса и имена metadata-сервисов облаков — запрещены при любых настройках.
METADATA_IPS: frozenset[IPAddress] = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "169.254.169.254",  # AWS, GCP, Azure, OpenStack, DigitalOcean …
        "169.254.170.2",  # AWS ECS task metadata
        "169.254.169.123",  # AWS time sync
        "169.254.169.253",  # AWS DNS
        "fd00:ec2::254",  # AWS IMDS IPv6
        "fd00:ec2::23",
        "100.100.100.200",  # Alibaba Cloud
        "168.63.129.16",  # Azure WireServer
        "192.0.0.192",  # Oracle Cloud
    )
)
METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

LOOPBACK_HINT = (
    "Адрес localhost/127.0.0.1 внутри контейнера указывает на сам контейнер backend, а не на хост. "
    "Для Ollama в Docker Compose используйте http://ollama:11434, для Ollama на хосте — "
    "host.docker.internal или IP хоста в частной сети."
)


@dataclass(frozen=True)
class NetworkPolicy:
    # loopback, частные сети (RFC 1918, ULA) и CGNAT — для доверенной локальной Ollama.
    allow_private: bool = False
    # False — «только локальная сеть»: публичные адреса запрещены.
    allow_public: bool = True
    # http без TLS — только для частной сети.
    allow_http: bool = False


@dataclass(frozen=True)
class ParsedTarget:
    scheme: str
    host: str
    port: int
    path: str
    literal_ip: IPAddress | None


def _blocked(code: str, message: str) -> BlockedDestination:
    return BlockedDestination(message, code=code)


def parse_base_url(url: str, policy: NetworkPolicy) -> ParsedTarget:
    """Синтаксическая проверка адреса (без DNS). Вызывается при сохранении и перед каждым запросом."""
    if not url or len(url) > 500 or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise _blocked("invalid_url", "Некорректный адрес")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _blocked("invalid_url", "Некорректный адрес") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _blocked("scheme_not_allowed", "Разрешены только адреса http:// и https://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _blocked("userinfo_not_allowed", "Адрес не должен содержать имя пользователя или пароль")
    if parts.query or parts.fragment:
        raise _blocked("invalid_url", "Адрес не должен содержать параметры запроса или фрагмент")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise _blocked("invalid_url", "В адресе не указан хост")
    if scheme == "http" and not policy.allow_http:
        raise _blocked(
            "https_required", "Требуется https://; http допустим только для локальной Ollama в частной сети"
        )
    if host in METADATA_HOSTNAMES:
        raise _blocked("metadata_blocked", "Адреса metadata-сервисов запрещены")
    literal: IPAddress | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        check_ip(literal, policy)
    elif host == "localhost" or host.endswith(".localhost"):
        check_ip(ipaddress.ip_address("127.0.0.1"), policy)
    return ParsedTarget(
        scheme=scheme,
        host=host,
        port=port or (443 if scheme == "https" else 80),
        path=parts.path.rstrip("/"),
        literal_ip=literal,
    )


def _unwrap(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip.sixtofour is not None:
            return ip.sixtofour
    return ip


def classify(ip: IPAddress) -> str:
    """metadata | forbidden | loopback | private | public."""
    ip = _unwrap(ip)
    if ip in METADATA_IPS:
        return "metadata"
    if ip.is_loopback:  # до is_reserved: ::1 входит в зарезервированный ::/8
        return "loopback"
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip.is_reserved:
        return "forbidden"
    if isinstance(ip, ipaddress.IPv4Address) and ip == ipaddress.IPv4Address("255.255.255.255"):
        return "forbidden"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return "private"
    if ip.is_private:
        return "private"
    if ip.is_global:
        return "public"
    return "forbidden"


def check_ip(ip: IPAddress, policy: NetworkPolicy) -> None:
    kind = classify(ip)
    if kind == "metadata":
        raise _blocked("metadata_blocked", "Адреса metadata-сервисов запрещены")
    if kind == "forbidden":
        raise _blocked(
            "address_blocked", "Адрес из запрещённого диапазона (link-local, multicast, служебный)"
        )
    if kind == "loopback" and not policy.allow_private:
        raise _blocked(
            "loopback_blocked", "Адрес loopback запрещён без разрешения частной сети. " + LOOPBACK_HINT
        )
    if kind == "private" and not policy.allow_private:
        raise _blocked(
            "private_address_blocked",
            "Адрес в частной сети запрещён; Superadmin может разрешить частную сеть для локальной Ollama",
        )
    if kind == "public" and not policy.allow_public:
        raise _blocked(
            "public_address_blocked",
            "Провайдер помечен как локальный, но адрес находится в публичной сети",
        )


def in_container() -> bool:
    return Path("/.dockerenv").exists()


def url_warnings(url: str) -> list[str]:
    """Предупреждения для администратора (не ошибки)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return []
    warnings = []
    if (host == "localhost" or host.startswith("127.") or host == "::1") and in_container():
        warnings.append("loopback_in_container")
    return warnings


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(DNS_TIMEOUT_SECONDS):
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, TimeoutError):
        raise ProviderUnavailable("Не удалось разрешить имя хоста провайдера", code="dns_failed") from None
    result: list[IPAddress] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if ip not in result:
            result.append(ip)
    return result


class GuardedTransport(httpx.AsyncBaseTransport):
    """Транспорт с проверкой адреса и подключением к проверенному IP на каждом запросе."""

    def __init__(
        self,
        policy: NetworkPolicy,
        inner: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.policy = policy
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.resolver = resolver or system_resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        target = parse_base_url(str(url.copy_with(query=None, fragment=None)), self.policy)
        if target.literal_ip is not None:
            addresses = [target.literal_ip]
        else:
            addresses = await self.resolver(target.host, target.port)
            if not addresses:
                raise ProviderUnavailable("Не удалось разрешить имя хоста провайдера", code="dns_failed")
            # Все адреса должны быть разрешены: иначе rebinding-атака выбрала бы «плохой».
            for ip in addresses:
                check_ip(ip, self.policy)
            pinned = addresses[0]
            # Host уже выставлен httpx по исходному имени; SNI и проверка сертификата — по имени.
            request.url = url.copy_with(host=str(pinned))
            if target.scheme == "https":
                request.extensions = {**request.extensions, "sni_hostname": target.host}
        response = await self.inner.handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise _blocked("unexpected_redirect", "Провайдер ответил перенаправлением; переходы запрещены")
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()
