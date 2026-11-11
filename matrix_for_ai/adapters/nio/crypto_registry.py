import logging

from .crypto_engine import CryptoEngine
from .stored_secrets import StoredSecretsError


logger = logging.getLogger(__name__)


class CryptoEngineRegistry:
    def __init__(self, *, homeserver_url, server_name, as_token, store_root, stored_secrets):
        self._stored_secrets = stored_secrets
        self._homeserver_url = homeserver_url
        self._server_name = server_name
        self._as_token = as_token
        self._store_root = store_root
        self._engines_by_mxid = {}

    async def ensure(self, localpart):
        mxid = f"@{localpart}:{self._server_name}"
        if mxid in self._engines_by_mxid:
            return self._engines_by_mxid[mxid]
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
        self._engines_by_mxid[mxid] = engine
        logger.info("crypto engine ready for %s (device %s)", mxid, engine.device_id)
        return engine

    def get(self, mxid):
        return self._engines_by_mxid.get(mxid)

    async def close_all(self):
        for engine in self._engines_by_mxid.values():
            await engine.close()
