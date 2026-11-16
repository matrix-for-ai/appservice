import asyncio
import logging

from .crypto_engine import CryptoEngine
from .stored_secrets import StoredSecrets, StoredSecretsError


logger = logging.getLogger(__name__)


class CryptoEngineRegistry:
    def __init__(self, *, homeserver_url: str, server_name: str, as_token: str, store_root: str,
                 stored_secrets: StoredSecrets) -> None:
        self._stored_secrets = stored_secrets
        self._homeserver_url = homeserver_url
        self._server_name = server_name
        self._as_token = as_token
        self._store_root = store_root
        self._engines_by_mxid: dict[str, CryptoEngine] = {}
        self._start_locks_by_mxid: dict[str, asyncio.Lock] = {}

    async def ensure(self, localpart: str) -> CryptoEngine | None:
        mxid = f"@{localpart}:{self._server_name}"
        async with self._start_locks_by_mxid.setdefault(mxid, asyncio.Lock()):
            if mxid not in self._engines_by_mxid:
                engine = await self._start(localpart, mxid)
                if engine is None:
                    return None
                self._engines_by_mxid[mxid] = engine
            return self._engines_by_mxid[mxid]

    async def _start(self, localpart: str, mxid: str) -> CryptoEngine | None:
        engine = CryptoEngine(
            homeserver_url=self._homeserver_url, server_name=self._server_name,
            as_token=self._as_token, localpart=localpart, store_root=self._store_root,
            stored_secrets=self._stored_secrets)
        try:
            await engine.start()
        except StoredSecretsError:
            raise
        except Exception as error:
            logger.warning("crypto engine failed for %s: %s", mxid, error)
            return None
        logger.info("crypto engine ready for %s (device %s)", mxid, engine.device_id)
        return engine

    def get(self, mxid: str) -> CryptoEngine | None:
        return self._engines_by_mxid.get(mxid)

    async def close_all(self) -> None:
        for engine in self._engines_by_mxid.values():
            await engine.close()
