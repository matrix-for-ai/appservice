import logging
from typing import Awaitable, Callable

from ..api_errors import ApiError
from ..ports.matrix_transport import MatrixTransport, PushCheck


logger = logging.getLogger(__name__)


class PushReachability:
    def __init__(self, *, transport: MatrixTransport, appservice_id: str,
                 on_recovered: Callable[[], Awaitable[None]]) -> None:
        self._transport = transport
        self._appservice_id = appservice_id
        self._on_recovered = on_recovered
        self._last: PushCheck | None = None

    async def check(self) -> None:
        outcome = await self._transport.ping_from_homeserver(appservice_id=self._appservice_id)
        if outcome != self._last:
            self._log(outcome)
        if outcome.reachable and self._last is not None and not self._last.reachable:
            await self._on_recovered()
        self._last = outcome

    def require_reachable(self) -> None:
        if self._last is None:
            raise ApiError(503, "push_unchecked", "the homeserver has not been asked yet")
        if not self._last.supported:
            raise ApiError(501, "push_check_unsupported",
                           "the homeserver does not implement the application service ping")
        if not self._last.reachable and not self._last.status:
            raise ApiError(503, "push_unreachable", "the homeserver did not answer the ping")
        if not self._last.reachable:
            raise ApiError(503, "push_unreachable",
                           "the homeserver cannot reach the appservice at its registered URL",
                           upstream={"status": self._last.status,
                                     **({"errcode": self._last.errcode}
                                        if self._last.errcode else {})})

    def _log(self, outcome: PushCheck) -> None:
        if not outcome.supported:
            logger.warning("the homeserver does not implement the application service ping: "
                           "whether it reaches appservice_listen is unchecked")
        elif not outcome.status:
            logger.error("the homeserver did not answer the application service ping")
        elif outcome.reachable:
            logger.info("the homeserver reaches the appservice at its registered URL")
        else:
            logger.error("the homeserver cannot reach the appservice at its registered URL "
                         "(%s %s): no events will arrive; check the registration's url and "
                         "appservice_listen", outcome.status, outcome.errcode or "")
