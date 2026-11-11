import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from ..api_errors import ApiError
from ..payload_fields import reject_unknown_fields, required_string
from .identity_registry import IdentityRegistry
from .identity_store import Webhook


logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Signature-256"
_NETWORKS_ONLY_IF_NAMED = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10"))


class WebhookNotifier:
    def __init__(
        self, *, http_session: aiohttp.ClientSession, identity_registry: IdentityRegistry,
        allowed_targets, ca_file: str = "", coalesce_seconds: float = 2.0,
        timeout_seconds: float = 3.0, own_ports: tuple[int, ...] = (),
    ) -> None:
        self._own_ports = frozenset(own_ports)
        self._http_session = http_session
        self._identity_registry = identity_registry
        self._allowed_targets = tuple(parsed_target(target) for target in allowed_targets)
        self._tls_context = tls_context(ca_file)
        self._coalesce_seconds = coalesce_seconds
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._pending: dict[tuple[str, str], str] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self._allowed_targets)

    async def register(self, mxid: str, payload: dict) -> tuple[str, bool]:
        reject_unknown_fields(payload, ("url",))
        url = required_string(payload, "url")
        await self._allowed_addresses(url)
        replaced = self.describe(mxid) is not None
        secret = secrets.token_urlsafe(32)
        self._identity_registry.store_for(mxid).set_webhook(Webhook(url, secret))
        return secret, replaced

    async def _allowed_addresses(self, url: str) -> tuple["WebhookTarget", list]:
        target = _webhook_target(url)
        if not self.enabled:
            raise ApiError(501, "webhooks_disabled", "this installation allows no webhook targets")
        addresses = [target.address] if target.address else await _resolved(target)
        if not all(self._allows(address, target.port) for address in addresses):
            raise ApiError(422, "url_not_allowed",
                           "url target is not in the installation's webhook allowlist")
        return target, addresses

    def _allows(self, address, port: int) -> bool:
        address = getattr(address, "ipv4_mapped", None) or address
        if address.is_unspecified or address.is_multicast:
            return False
        named_network = next(
            (network for network in _NETWORKS_ONLY_IF_NAMED if address in network), None)
        return any(
            target.allows(address, port)
            and (named_network is None
                 or (target.network is not None and target.network.subnet_of(named_network)))
            and (port not in self._own_ports or target.port == port)
            for target in self._allowed_targets)

    def unregister(self, mxid: str) -> bool:
        return self._identity_registry.store_for(mxid).delete_webhook()

    def describe(self, mxid: str):
        webhook = self._webhook(mxid)
        return None if webhook is None else {"url": webhook.url}

    def notify(self, mxid: str, room_id: str, sender: str) -> None:
        if self._webhook(mxid) is None:
            return
        key = (mxid, room_id)
        already_pending = key in self._pending
        self._pending[key] = sender
        if already_pending:
            return
        task = asyncio.create_task(self._deliver_after_window(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver_after_window(self, key: tuple[str, str]) -> None:
        await asyncio.sleep(self._coalesce_seconds)
        sender = self._pending.pop(key, "")
        mxid, room_id = key
        store = self._identity_registry.store_for(mxid)
        webhook = None if store is None else store.webhook()
        if webhook is None:
            return
        try:
            target, addresses = await self._allowed_addresses(webhook.url)
        except ApiError as error:
            logger.warning("webhook for %s skipped: %s", mxid, error.detail)
            return
        unread = store.mailbox.unread_count(room_id)
        if not unread:
            return
        body = json.dumps({
            "identity": mxid, "room": room_id, "sender": sender, "unread": unread,
            "ts": int(time.time() * 1000), "delivery_id": uuid.uuid4().hex,
        }).encode("utf-8")
        signature = hmac.new(webhook.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        await self._post_to_first_reachable(mxid, webhook.url, target, addresses, body, signature)

    async def _post_to_first_reachable(self, mxid, url, target, addresses, body, signature) -> None:
        headers = {"Host": target.netloc, "Content-Type": "application/json",
                   _SIGNATURE_HEADER: f"sha256={signature}"}
        tls = {}
        if target.scheme == "https":
            tls = {"ssl": self._tls_context,
                   "server_hostname": None if target.address else target.host}
        for address in addresses:
            try:
                async with self._http_session.post(
                        URL(url).with_host(str(address)), data=body, headers=headers,
                        timeout=self._timeout, allow_redirects=False, **tls) as response:
                    if response.status >= 300:
                        logger.info("webhook for %s answered %d", mxid, response.status)
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                logger.info("webhook for %s unreachable at %s: %s",
                            mxid, address, error.__class__.__name__)

    def _webhook(self, mxid: str) -> Webhook | None:
        store = self._identity_registry.store_for(mxid)
        return None if store is None else store.webhook()


@dataclass(frozen=True)
class TargetPattern:
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None
    port: int | None

    def allows(self, address, port: int) -> bool:
        return ((self.network is None or address in self.network)
                and (self.port is None or port == self.port))


def parsed_target(target: str) -> TargetPattern:
    if target == "*":
        return TargetPattern(None, None)
    host, separator, port = target.rpartition(":")
    if not separator:
        raise ValueError(target)
    host = host.removeprefix("[").removesuffix("]")
    network = None if host == "*" else ipaddress.ip_network(host)
    if port == "*":
        return TargetPattern(network, None)
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(target)
    return TargetPattern(network, int(port))


def tls_context(ca_file: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    return context


@dataclass(frozen=True)
class WebhookTarget:
    scheme: str
    host: str
    port: int
    netloc: str
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def _webhook_target(url: str) -> WebhookTarget:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ApiError(422, "invalid_url", "url must be http or https")
    if parts.username or parts.password:
        raise ApiError(422, "invalid_url", "url must not carry credentials")
    if not parts.hostname:
        raise ApiError(422, "invalid_url", "url has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise ApiError(422, "invalid_url", "url port is not valid")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    return WebhookTarget(parts.scheme, parts.hostname, port, parts.netloc, address)


async def _resolved(target: WebhookTarget) -> list:
    try:
        addresses = await resolve_addresses(target.host, target.port)
    except OSError:
        raise ApiError(422, "invalid_url", f"url host {target.host} does not resolve")
    if not addresses:
        raise ApiError(422, "invalid_url", f"url host {target.host} does not resolve")
    return addresses


async def resolve_addresses(host: str, port: int) -> list:
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(ipaddress.ip_address(record[4][0]) for record in records))
