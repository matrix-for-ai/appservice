import asyncio

from aiohttp import web


def setup(context):
    routes = _EncryptionRoutes(context.encryption)
    context.add_routes(routes.register)


class _EncryptionRoutes:
    def __init__(self, encryption):
        self._encryption = encryption
        self._captured_by_mxid = {}
        self._stop = asyncio.Event()

    def register(self, router):
        router.add_get("/e2e/{mxid}/device", self._device)
        router.add_post("/e2e/{mxid}/capture", self._capture)
        router.add_get("/e2e/{mxid}/captured", self._captured)
        router.add_post("/e2e/{mxid}/to_device", self._send_to_device)
        router.add_put("/e2e/{mxid}/state", self._put_state)

    async def _engine(self, request):
        mxid = request.match_info["mxid"]
        engine = await self._encryption.ensure(mxid[1:].split(":", 1)[0])
        if engine is None:
            raise web.HTTPServiceUnavailable(text=f"no encryption engine for {mxid}")
        return engine

    async def _device(self, request):
        engine = await self._engine(request)
        return web.json_response({"device_id": engine.device_id})

    async def _capture(self, request):
        engine = await self._engine(request)
        captured = self._captured_by_mxid.setdefault(engine.mxid, [])
        engine.install_olm_event_capture(
            request.query["type"],
            lambda sender, _sender_key, payload: captured.append(
                {"sender": sender, "payload": payload}))
        await engine.sync_once(timeout=0)
        asyncio.get_running_loop().create_task(engine.sync_loop(self._stop, timeout=1000))
        return web.Response(status=204)

    async def _captured(self, request):
        engine = await self._engine(request)
        return web.json_response(self._captured_by_mxid.get(engine.mxid, []))

    async def _send_to_device(self, request):
        engine = await self._engine(request)
        body = await request.json()
        await engine.send_olm_to_device(
            body["user_id"], body["device_id"], body["type"], body["content"])
        return web.Response(status=204)

    async def _put_state(self, request):
        engine = await self._engine(request)
        body = await request.json()
        response = await engine.put_state(
            body["room"], body["type"], body["content"], body["state_key"])
        return web.json_response({"event_id": response.event_id})
