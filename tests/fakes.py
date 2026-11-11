import asyncio
import base64
import itertools
import json

from aiohttp import web

from matrix_for_ai.ports.matrix_transport import (
    ApplicationServiceUserRegistration, EventContext, HomeserverError, MediaTooLarge, PushCheck,
    TimelineChunk)
from matrix_for_ai.ports.speech import SpeechUnavailable, VoiceMessage

SERVER = "example.org"
AS_TOKEN = "as-token-0123456789abcdef0123456789"
HS_TOKEN = "hs-token-0123456789abcdef0123456789"
PUSH_CHECK = PushCheck(supported=True, reachable=True, status=200)
ADMIN_TOKEN = "admin-token-0123456789abcdef01234567"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PNG_BASE64 = base64.b64encode(PNG).decode()


class FakeHomeserver:
    def __init__(self) -> None:
        self.users: set[str] = set()
        self.rooms: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.media: dict[str, bytes] = {}
        self.media_downloads: list[tuple[str, str]] = []
        self.read_markers: dict[tuple, str] = {}
        self.profiles: dict[str, dict] = {}
        self._ids = itertools.count(1)

    def new_id(self, sigil: str) -> str:
        return f"{sigil}{next(self._ids)}:{SERVER}"

    def create_room(self, creator: str, invitees, *, encrypted: bool, name=None,
                    public=False) -> str:
        room_id = self.new_id("!")
        self.rooms[room_id] = {
            "encrypted": encrypted, "members": {creator}, "invited": set(invitees), "name": name,
            "public": public}
        return room_id

    def add_event(self, room_id: str, sender: str, event_type: str, content: dict,
                  state_key=None) -> dict:
        event = {
            "event_id": self.new_id("$"), "room_id": room_id, "sender": sender,
            "type": event_type, "content": content, "origin_server_ts": next(self._ids),
        }
        if state_key is not None:
            event["state_key"] = state_key
        self.events[event["event_id"]] = event
        return event


class FakeTransport:
    def __init__(self, homeserver: FakeHomeserver) -> None:
        self.hs = homeserver
        self._handler = None
        self.timeline_requests: list[tuple] = []
        self.pings: list[str] = []
        self.push_check = PUSH_CHECK
        self.failing: dict[str, int] = {}

    def _answer_or_fail(self, call: str) -> None:
        if call in self.failing:
            raise HomeserverError(self.failing[call], {"errcode": "M_UNKNOWN"})

    def set_incoming_event_handler(self, handler) -> None:
        self._handler = handler

    async def ping_from_homeserver(self, *, appservice_id):
        if self.push_check is None:
            await asyncio.Event().wait()
        self.pings.append(appservice_id)
        return self.push_check

    def register_push_routes(self, application: web.Application) -> None:
        async def transaction(request):
            if request.headers.get("Authorization") != f"Bearer {HS_TOKEN}":
                return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
            for event in (await request.json()).get("events", []):
                await self._handler(event)
            return web.json_response({})
        application.router.add_put("/_matrix/app/v1/transactions/{txn}", transaction)

    async def send_event(self, *, room_id, event_type, content, as_identity):
        return self.hs.add_event(room_id, as_identity, event_type, content)["event_id"]

    async def create_room(self, *, as_identity, name, topic, invitees, is_direct, public):
        return self.hs.create_room(as_identity, invitees, encrypted=False, name=name, public=public)

    async def join_room(self, *, room_id, as_identity=None):
        room = self.hs.rooms.get(room_id)
        if room is None:
            return 404
        if not room["public"] and as_identity not in room["invited"]:
            return 403
        if as_identity not in room["members"]:
            self.hs.add_event(room_id, as_identity, "m.room.member", {"membership": "join"},
                              state_key=as_identity)
        room["invited"].discard(as_identity)
        room["members"].add(as_identity)
        return 200

    async def leave_and_forget_room(self, *, as_identity, room_id):
        room = self.hs.rooms.get(room_id)
        if room is None or as_identity not in room["members"] | room["invited"]:
            return 403
        room["members"].discard(as_identity)
        room["invited"].discard(as_identity)
        return 200

    async def set_display_name(self, *, identity, display_name):
        self._answer_or_fail("set_display_name")
        self._set_profile_field(identity, "displayname", display_name)

    async def set_avatar_url(self, *, identity, avatar_url):
        self._set_profile_field(identity, "avatar_url", avatar_url)

    def _set_profile_field(self, identity, field, value):
        profile = self.hs.profiles.setdefault(identity, {})
        if value is None:
            profile.pop(field, None)
        else:
            profile[field] = value

    async def fetch_profile(self, *, identity):
        return dict(self.hs.profiles.get(identity, {}))

    async def register_application_service_user(self, *, localpart):
        mxid = f"@{localpart}:{SERVER}"
        if mxid in self.hs.users:
            return ApplicationServiceUserRegistration(outcome="already_in_use")
        self.hs.users.add(mxid)
        return ApplicationServiceUserRegistration(outcome="created")

    async def fetch_event(self, *, room_id, event_id, as_identity=None):
        self._answer_or_fail("fetch_event")
        event = self.hs.events.get(event_id)
        return dict(event) if event and event["room_id"] == room_id else None

    async def fetch_event_relations(self, *, room_id, event_id, as_identity=None,
                                    rel_type="m.reference"):
        related = []
        for event in self.hs.events.values():
            relates = event["content"].get("m.relates_to") or {}
            if event["room_id"] == room_id and relates.get("event_id") == event_id:
                related.append(dict(event))
        return related

    async def fetch_event_context(self, *, room_id, event_id, as_identity):
        timeline = self._timeline(room_id)
        position = next((index for index, event in enumerate(timeline)
                         if event["event_id"] == event_id), None)
        if position is None:
            return None
        return EventContext(token_before=f"t{position}", token_after=f"t{position + 1}")

    async def fetch_timeline(self, *, room_id, as_identity, backwards, from_token, limit,
                             event_types):
        self.timeline_requests.append((room_id, backwards, from_token, limit))
        timeline = self._timeline(room_id)
        boundary = int(from_token[1:]) if from_token else (len(timeline) if backwards else 0)
        if backwards:
            start = max(boundary - limit, 0)
            events, next_boundary = timeline[start:boundary][::-1], start
            exhausted = start == 0
        else:
            end = boundary + limit
            events, next_boundary = timeline[boundary:end], min(end, len(timeline))
            exhausted = end >= len(timeline)
        return TimelineChunk(
            events=[dict(event) for event in events if event["type"] in event_types],
            next_token=None if exhausted else f"t{next_boundary}")

    def _timeline(self, room_id):
        return [event for event in self.hs.events.values() if event["room_id"] == room_id]

    async def download_media(self, *, media_uri, as_identity, max_bytes):
        self._answer_or_fail("download_media")
        self.hs.media_downloads.append((str(media_uri), as_identity))
        data = self.hs.media.get(str(media_uri))
        if data is not None and max_bytes is not None and len(data) > max_bytes:
            raise MediaTooLarge()
        return data

    async def upload_media(self, *, data, mime_type, filename):
        content_uri = f"mxc://{SERVER}/{next(self.hs._ids)}"
        self.hs.media[content_uri] = data
        return content_uri

    async def mark_room_read(self, *, as_identity, room_id, event_id):
        self.hs.read_markers[(as_identity, room_id)] = event_id

    async def list_joined_rooms(self, *, identity):
        self._answer_or_fail("list_joined_rooms")
        return [room_id for room_id, room in self.hs.rooms.items() if identity in room["members"]]

    async def list_invited_rooms(self, *, identity):
        self._answer_or_fail("list_invited_rooms")
        return [room_id for room_id, room in self.hs.rooms.items() if identity in room["invited"]]

    async def is_room_encrypted(self, *, room_id, as_identity):
        return self.hs.rooms[room_id]["encrypted"]

    async def membership_of(self, *, room_id, user_id, as_identity):
        room = self.hs.rooms.get(room_id)
        if room is None:
            return None
        if user_id in room["members"]:
            return "join"
        return "invite" if user_id in room["invited"] else None

    async def user_exists(self, *, user_id, as_identity):
        return True

    async def invite_user(self, *, room_id, user_id, as_identity):
        if as_identity not in self.hs.rooms[room_id]["members"]:
            return 403
        self.hs.rooms[room_id]["invited"].add(user_id)
        return 200

    async def kick_user(self, *, room_id, user_id, as_identity, reason=None):
        self.hs.rooms[room_id]["members"].discard(user_id)
        self.hs.rooms[room_id]["invited"].discard(user_id)
        return 200

    async def list_members(self, *, room_id, as_identity):
        return {member: {"display_name": None} for member in self.hs.rooms[room_id]["members"]}


class FakeEngine:
    def __init__(self, homeserver: FakeHomeserver, mxid: str) -> None:
        self.hs = homeserver
        self.mxid = mxid
        self.device_id = "FAKEDEVICE"
        self.key_receptions = 0

    async def receive_keys(self):
        self.key_receptions += 1

    async def decrypt_event(self, raw_event, room_id):
        try:
            return json.loads(raw_event["content"]["ciphertext"])
        except ValueError:
            return None

    def decrypt_attachment(self, ciphertext, encrypted_file):
        return bytes(reversed(ciphertext))

    async def upload_encrypted(self, *, data, mime_type, filename):
        content_uri = f"mxc://{SERVER}/{next(self.hs._ids)}"
        self.hs.media[content_uri] = bytes(reversed(data))
        return {"url": content_uri, "key": {"k": "fake"}, "iv": "fake",
                "hashes": {"sha256": "fake"}, "v": "v2"}

    async def send_event(self, *, room_id, event_type, content):
        encrypted = {"ciphertext": json.dumps({"type": event_type, "content": content})}
        if "m.relates_to" in content:
            encrypted["m.relates_to"] = content["m.relates_to"]
        return self.hs.add_event(room_id, self.mxid, "m.room.encrypted", encrypted)["event_id"]

    async def create_encrypted_room(self, *, invitees, name, topic, is_direct, public):
        return self.hs.create_room(self.mxid, invitees, encrypted=True, name=name, public=public)


class FakeEncryptionRegistry:
    def __init__(self, homeserver: FakeHomeserver) -> None:
        self.hs = homeserver
        self.engines: dict[str, FakeEngine] = {}

    def get(self, mxid):
        return self.engines.get(mxid)

    async def ensure(self, localpart):
        mxid = f"@{localpart}:{SERVER}"
        return self.engines.setdefault(mxid, FakeEngine(self.hs, mxid))

    async def close_all(self):
        pass


class FakeSynthesizer:
    def __init__(self, *, fail: bool = False, provider_status: int | None = None) -> None:
        self.fail = fail
        self.provider_status = provider_status
        self.calls: list[tuple[str, dict]] = []

    async def synthesize(self, text, options):
        self.calls.append((text, options))
        if self.fail:
            raise SpeechUnavailable("provider answered: voice 'x' is not one of 'alba'",
                                    provider_status=self.provider_status)
        return VoiceMessage(opus_ogg_bytes=b"OggS-fake", duration_milliseconds=1200,
                            waveform=[0, 512, 1024])

    def describe(self):
        return {"available": True, "info": {"voices": ["fake"]}}

    async def refresh_description(self):
        pass


class FakeTranscriber:
    def __init__(self, *, text: str = "hello there", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[tuple[bytes, str, dict]] = []

    async def transcribe(self, audio, mimetype, options):
        self.calls.append((audio, mimetype, options))
        if self.fail:
            raise SpeechUnavailable("provider down")
        return self.text

    def describe(self):
        return {"available": True, "info": None}

    async def refresh_description(self):
        pass
