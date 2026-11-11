import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..api_errors import ApiError
from ..ports.matrix_transport import HomeserverError, MatrixTransport
from .identity_store import IdentityStore, Mailbox


logger = logging.getLogger(__name__)

_LOCALPART_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
NAME_PATTERN = re.compile(r"[a-z0-9_]+")
_MAX_MINT_ATTEMPTS = 8


@dataclass(frozen=True)
class ResolvedIdentity:
    mxid: str
    mailbox: Mailbox


@dataclass(frozen=True)
class IdentityRegistration:
    mxid: str
    localpart: str
    display_name: str
    api_token: str


class IdentityRegistry:
    def __init__(self, *, mailbox_base_dir: Path, server_name: str) -> None:
        self._mailbox_base_dir = mailbox_base_dir
        self._stores: dict[str, IdentityStore] = {}
        self._server_name = server_name
        self._managed_localparts: set[str] = set()
        self._token_to_localpart: dict[str, str] = {}

    def load(self) -> None:
        try:
            self._managed_localparts = {
                entry.name for entry in self._mailbox_base_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith((".", "_"))
            }
        except OSError:
            self._managed_localparts = set()
        logger.info("identities discovered: %d", len(self._managed_localparts))
        self._token_to_localpart = {}
        for localpart in self._managed_localparts:
            token_hash = self._store(localpart).token_sha256()
            if token_hash is not None:
                self._token_to_localpart[token_hash] = localpart

    def is_managed(self, mxid: str) -> bool:
        localpart = self._localpart_of(mxid)
        return localpart is not None and localpart in self._managed_localparts

    def managed_mxids(self) -> list[str]:
        return [self._mxid_of(localpart) for localpart in self._managed_localparts]

    def managed_localparts(self) -> list[str]:
        return sorted(self._managed_localparts)

    def mailbox_for(self, mxid: str) -> Mailbox | None:
        store = self.store_for(mxid)
        return None if store is None else store.mailbox

    def store_for(self, mxid: str) -> IdentityStore | None:
        localpart = self._localpart_of(mxid)
        if localpart is None or localpart not in self._managed_localparts:
            return None
        return self._store(localpart)

    def _store(self, localpart: str) -> IdentityStore:
        if localpart not in self._stores:
            self._stores[localpart] = IdentityStore(self._mailbox_base_dir / localpart)
        return self._stores[localpart]

    def resolve(self, identity: str) -> ResolvedIdentity | None:
        localpart = self._localpart_of(identity) if identity.startswith("@") else identity
        if localpart is None or localpart not in self._managed_localparts:
            return None
        return ResolvedIdentity(
            self._mxid_of(localpart), self._store(localpart).mailbox)

    def issue_token(self, localpart: str) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)
        self._store(localpart).set_token_sha256(token_hash)
        self._token_to_localpart = {
            known: owner for known, owner in self._token_to_localpart.items() if owner != localpart}
        self._token_to_localpart[token_hash] = localpart
        return token

    def revoke_token(self, localpart: str) -> bool:
        self._token_to_localpart = {
            known: owner for known, owner in self._token_to_localpart.items() if owner != localpart}
        return self._store(localpart).delete_token()

    def identity_for_token(self, token: str) -> ResolvedIdentity | None:
        localpart = self._token_to_localpart.get(self._hash_token(token))
        if localpart is None or localpart not in self._managed_localparts:
            return None
        return ResolvedIdentity(
            self._mxid_of(localpart), self._store(localpart).mailbox)

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def register_identity(
        self, *, transport: MatrixTransport, name: str, display_name: str | None,
    ) -> IdentityRegistration:
        localpart = await self._register_minted_localpart(transport, name)
        mxid = self._mxid_of(localpart)
        self._store(localpart)
        self._managed_localparts.add(localpart)
        display_name = display_name or name.replace("_", " ").title()
        try:
            await transport.set_display_name(identity=mxid, display_name=display_name)
        except HomeserverError as error:
            logger.warning("%s registered without its display name: %s", mxid, error)
        return IdentityRegistration(mxid=mxid, localpart=localpart, display_name=display_name,
                                    api_token=self.issue_token(localpart))

    async def _register_minted_localpart(self, transport: MatrixTransport, name: str) -> str:
        for _ in range(_MAX_MINT_ATTEMPTS):
            candidate_localpart = f"ai_{name}_{self._mint_localpart_id()}"
            registration = await transport.register_application_service_user(
                localpart=candidate_localpart)
            if registration.outcome == "created":
                return candidate_localpart
        raise ApiError(503, "no_free_id", "no free id was found for this name; retry")

    def localpart_of(self, mxid: str) -> str | None:
        return self._localpart_of(mxid)

    def _localpart_of(self, mxid: str) -> str | None:
        if not mxid.startswith("@") or ":" not in mxid:
            return None
        localpart, _, server = mxid[1:].partition(":")
        if server != self._server_name:
            return None
        return localpart

    def _mxid_of(self, localpart: str) -> str:
        return f"@{localpart}:{self._server_name}"

    @staticmethod
    def _mint_localpart_id(length: int = 6) -> str:
        return "".join(secrets.choice(_LOCALPART_ID_ALPHABET) for _ in range(length))
