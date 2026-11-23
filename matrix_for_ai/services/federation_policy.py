import ipaddress
import re
from dataclasses import dataclass, field
from functools import lru_cache

DENIED_MEMBERS_MODES = ("ignore", "leave", "allow")


@dataclass(frozen=True)
class ServerRule:
    allow: tuple[str, ...] = ("*",)
    deny: tuple[str, ...] = ()

    @property
    def unrestricted(self) -> bool:
        return self.allow == ("*",) and not self.deny

    @property
    def closed(self) -> bool:
        return not self.allow or "*" in self.deny


@dataclass(frozen=True)
class FederationRule:
    servers: ServerRule = ServerRule()
    denied_members: str = "ignore"


@dataclass(frozen=True)
class FederationConfiguration:
    default: FederationRule = FederationRule()
    identities: dict[str, FederationRule] = field(default_factory=dict)


class FederationPolicy:
    def __init__(self, *, server_name: str, configuration: FederationConfiguration) -> None:
        self._server_name = host_of(server_name)
        self._configuration = configuration

    def rule_for(self, mxid: str) -> FederationRule:
        return self._configuration.identities.get(mxid, self._configuration.default)

    def unrestricted(self, mxid: str) -> bool:
        return self.rule_for(mxid).servers.unrestricted

    def is_local(self, user_id: str) -> bool:
        return host_of(user_id.partition(":")[2]) == self._server_name

    def allows_user(self, mxid: str, user_id: str) -> bool:
        return self.allows_server(mxid, user_id.partition(":")[2])

    def allows_server(self, mxid: str, server_name: str) -> bool:
        host = host_of(server_name)
        if host == self._server_name:
            return True
        servers = self.rule_for(mxid).servers
        if servers.unrestricted:
            return True
        if _is_ip_literal(host):
            return False
        return any(_matches(glob, host) for glob in servers.allow) \
            and not any(_matches(glob, host) for glob in servers.deny)

    def ignores(self, mxid: str, user_id: str) -> bool:
        return self.rule_for(mxid).denied_members != "allow" and not self.allows_user(mxid, user_id)

    def leaves_for(self, mxid: str, user_id: str) -> bool:
        return self.rule_for(mxid).denied_members == "leave" and not self.allows_user(mxid, user_id)

    def denied_among(self, mxid: str, user_ids) -> list[str]:
        return sorted(user for user in user_ids if not self.allows_user(mxid, user))

    def room_federation(self, mxid: str, *, federate: bool) -> tuple[bool, dict | None]:
        servers = self.rule_for(mxid).servers
        if not federate or servers.closed:
            return False, None
        if servers.unrestricted:
            return True, None
        return True, {"allow": [*servers.allow, self._server_name], "deny": list(servers.deny),
                      "allow_ip_literals": False}


def host_of(server_name: str) -> str:
    if server_name.startswith("["):
        return server_name[:server_name.find("]") + 1].lower()
    host, separator, port = server_name.rpartition(":")
    return (host if separator and port.isdigit() else server_name).lower()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _matches(glob: str, host: str) -> bool:
    return _glob_pattern(glob.lower()).fullmatch(host) is not None


@lru_cache(maxsize=256)
def _glob_pattern(glob: str) -> re.Pattern:
    return re.compile("".join(
        ".*" if character == "*" else "." if character == "?" else re.escape(character)
        for character in glob))
