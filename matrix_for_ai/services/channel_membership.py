from ..api_errors import ApiError
from ..ports.matrix_transport import MatrixTransport
from .identity_registry import ResolvedIdentity


PRESENT = ("join", "invite")


class ChannelMembership:
    def __init__(self, *, transport: MatrixTransport) -> None:
        self._transport = transport

    async def require(self, identity: ResolvedIdentity, room_id: str) -> None:
        if identity.mailbox.channel(room_id) is not None:
            return
        membership = await self._transport.membership_of(
            room_id=room_id, user_id=identity.mxid, as_identity=identity.mxid)
        if membership not in PRESENT:
            raise ApiError(404, "not_member", f"{identity.mxid} is not in {room_id}")
