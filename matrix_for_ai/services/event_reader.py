import base64
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass

from ..ports.encryption import EncryptedFile, EncryptionEngine
from ..ports.matrix_transport import HomeserverError, MatrixTransport, MediaTooLarge, MediaUri
from ..ports.speech import SpeechUnavailable, Transcriber
from .identity_registry import IdentityRegistry
from .message_content import (
    POLL_END_TYPES, POLL_START_KEYS, Location, Malformed, Media, Message, NotAMessage, Poll,
    PollEnd, Text, parse_message, poll_answers, quoted_text)


logger = logging.getLogger(__name__)

BEACON_TYPES = ("m.beacon", "org.matrix.msc3672.beacon")
MESSAGE_EVENT_TYPES = (
    ("m.room.encrypted", "m.room.message") + POLL_START_KEYS + POLL_END_TYPES + BEACON_TYPES)
TIMELINE_EVENT_TYPES = MESSAGE_EVENT_TYPES + ("m.room.member",)
RENDERED_MEMBERSHIPS = ("join", "leave", "ban")
MEDIA_TYPES = ("image", "audio", "video", "file")

SKIP_EVENT = object()
TRANSCRIPT_CACHE_SIZE = 256


@dataclass(frozen=True)
class MediaFile:
    data: bytes
    mimetype: str
    filename: str


def is_rendered_membership(membership) -> bool:
    return membership in RENDERED_MEMBERSHIPS


class KeysReceivedOnce:
    def __init__(self, engine: EncryptionEngine) -> None:
        self._engine = engine
        self._keys_received = False

    async def decrypt_event(self, raw_event: dict, room_id: str) -> dict | None:
        decrypted = await self._engine.decrypt_event(raw_event, room_id)
        if decrypted is None and not self._keys_received:
            self._keys_received = True
            await self._engine.receive_keys()
            decrypted = await self._engine.decrypt_event(raw_event, room_id)
        return decrypted

    def decrypt_attachment(self, ciphertext: bytes, encrypted_file: EncryptedFile) -> bytes | None:
        return self._engine.decrypt_attachment(ciphertext, encrypted_file)


class EventReader:
    def __init__(
        self, *, transport: MatrixTransport, identity_registry: IdentityRegistry,
        transcriber: Transcriber | None = None, max_media_bytes: int | None = None,
    ) -> None:
        self._transport = transport
        self._max_media_bytes = max_media_bytes
        self._identity_registry = identity_registry
        self._transcriber = transcriber
        self._transcripts: OrderedDict[tuple, str] = OrderedDict()

    async def build_record(self, room_id, event_id, mxid, engine: EncryptionEngine | None):
        raw = await self._transport.fetch_event(
            room_id=room_id, event_id=event_id, as_identity=mxid)
        if not raw:
            return None
        return await self.record_of(raw, room_id, mxid, engine)

    async def record_of(self, raw: dict, room_id, mxid, engine: EncryptionEngine | None):
        base = {
            "event_id": raw.get("event_id"), "room": room_id,
            "sender": raw.get("sender"), "ts": raw.get("origin_server_ts"),
        }
        raw_type = raw.get("type")
        if raw_type == "m.room.member":
            membership = self._membership_content(raw, mxid)
            if membership is None:
                return SKIP_EVENT
            return {**base, "type": "membership", "content": membership}
        if _is_redacted(raw):
            return SKIP_EVENT
        if raw_type == "m.room.encrypted":
            decrypted = await engine.decrypt_event(raw, room_id) if engine is not None else None
            if decrypted is None:
                return {**base, "type": "undecryptable", "content": {}}
            message = parse_message(decrypted.get("type"), decrypted.get("content"))
        else:
            message = parse_message(raw_type, raw.get("content"))
        return await self._record_of_message(base, message, mxid, engine)

    async def enrich(self, record: dict, mxid: str, engine: EncryptionEngine | None,
                     include: frozenset) -> dict:
        if record["type"] in MEDIA_TYPES and "media" in include:
            media = await self.media_bytes(mxid, record["room"], record["event_id"], engine,
                                           optional=True)
            if media is not None:
                record["content"]["data_base64"] = base64.b64encode(media.data).decode()
        elif record["type"] == "poll" and "poll" in include:
            results = await self.poll_results(mxid, record["room"], record["event_id"], engine)
            if results is not None:
                record["content"]["results"] = results
        return record

    async def media_bytes(self, mxid, room_id, event_id, engine: EncryptionEngine | None, *,
                          optional: bool = False):
        message = await self._message_of(mxid, room_id, event_id, engine)
        if not isinstance(message, Media):
            return None
        data = await (self._optional_download if optional else self._download)(
            message, mxid, engine)
        return MediaFile(data, message.mimetype, message.filename) if data is not None else None

    async def poll_results(self, mxid, room_id, poll_event_id, engine: EncryptionEngine | None):
        poll = await self._message_of(mxid, room_id, poll_event_id, engine)
        if not isinstance(poll, Poll):
            return None
        relations = await self._transport.fetch_event_relations(
            room_id=room_id, event_id=poll_event_id, as_identity=mxid)
        latest_vote_by_voter: dict = {}
        closed = False
        for relation in relations:
            inner_type, content = await self._type_and_content(relation, room_id, engine)
            if inner_type in POLL_END_TYPES:
                closed = True
                continue
            answers = poll_answers(content)
            voter = relation.get("sender")
            if answers is None or voter is None:
                continue
            timestamp = relation.get("origin_server_ts", 0)
            if voter not in latest_vote_by_voter or timestamp >= latest_vote_by_voter[voter][0]:
                latest_vote_by_voter[voter] = (timestamp, answers)
        counts = {option.id: 0 for option in poll.options}
        votes_by_voter: dict = {}
        for voter, (_timestamp, answers) in latest_vote_by_voter.items():
            chosen = [answer for answer in answers if answer in counts]
            votes_by_voter[voter] = chosen
            for choice in chosen:
                counts[choice] += 1
        return {"counts": counts, "votes": votes_by_voter,
                "total_voters": len(latest_vote_by_voter), "closed": closed}

    async def _record_of_message(self, base, message: Message, mxid, engine):
        match message:
            case NotAMessage():
                return SKIP_EVENT
            case Malformed():
                return {**base, "type": "malformed", "content": {}}
            case PollEnd(poll_event_id=poll_event_id):
                return {**base, "type": "poll_end",
                        "content": {"poll_event_id": poll_event_id} if poll_event_id else {}}
            case Location(geo_uri=geo_uri, coordinates=coordinates):
                content = {"geo_uri": geo_uri}
                if coordinates is not None:
                    content["lat"], content["lon"] = coordinates
                return {**base, "type": "location", "content": content}
            case Poll():
                record = {**base, "type": "poll", "content": {
                    "question": message.question,
                    "options": [{"id": option.id, "text": option.text}
                                for option in message.options]}}
            case Media():
                record = await self._media_record(base, message, mxid, engine)
            case Text():
                record = {**base, "type": "text", "content": {"text": message.body}}
        if message.reply_to is not None:
            record["reply_to"] = await self._reply_quote(
                base["room"], message.reply_to, mxid, engine)
        return record

    async def _media_record(self, base, media: Media, mxid, engine):
        details = {"mimetype": media.mimetype, "filename": media.filename, **media.details}
        record = {**base, "type": media.kind, "content": details}
        if media.kind == "audio" \
                and self._identity_registry.store_for(mxid).profile().transcribe_audio:
            transcript = await self._transcribe(media, mxid, engine)
            if transcript is not None:
                details["transcript"] = transcript
        return record

    async def _transcribe(self, media: Media, mxid, engine):
        if self._transcriber is None:
            return None
        options = self._identity_registry.store_for(mxid).profile().stt
        key = (media.url, json.dumps(options, sort_keys=True))
        if key in self._transcripts:
            self._transcripts.move_to_end(key)
            return self._transcripts[key]
        audio = await self._optional_download(media, mxid, engine)
        if audio is None:
            return None
        try:
            text = await self._transcriber.transcribe(audio, media.mimetype, options)
        except SpeechUnavailable as error:
            logger.warning("transcription unavailable: %s", error)
            return None
        self._transcripts[key] = text
        if len(self._transcripts) > TRANSCRIPT_CACHE_SIZE:
            self._transcripts.popitem(last=False)
        return text

    async def _optional_download(self, media: Media, mxid, engine):
        try:
            return await self._download(media, mxid, engine)
        except MediaTooLarge:
            return None
        except HomeserverError as error:
            logger.warning("attachment %s left out: %s", media.url, error)
            return None

    async def _download(self, media: Media, mxid, engine):
        media_uri = MediaUri.parse(media.url)
        if media_uri is None:
            return None
        ciphertext = await self._transport.download_media(
            media_uri=media_uri, as_identity=mxid, max_bytes=self._max_media_bytes)
        if ciphertext is None or media.encrypted_file is None:
            return ciphertext
        if engine is None:
            return None
        return engine.decrypt_attachment(ciphertext, media.encrypted_file)

    async def _message_of(self, mxid, room_id, event_id, engine) -> Message | None:
        raw = await self._transport.fetch_event(
            room_id=room_id, event_id=event_id, as_identity=mxid)
        if not raw:
            return None
        inner_type, content = await self._type_and_content(raw, room_id, engine)
        return None if inner_type is None else parse_message(inner_type, content)

    async def _type_and_content(self, event, room_id, engine):
        if event.get("type") != "m.room.encrypted":
            return event.get("type"), event.get("content")
        decrypted = await engine.decrypt_event(event, room_id) if engine is not None else None
        if decrypted is None:
            return None, None
        return decrypted.get("type"), decrypted.get("content")

    def _membership_content(self, event, reader_mxid):
        subject_mxid = event.get("state_key") or ""
        content = event.get("content") or {}
        membership = content.get("membership")
        invites_the_reader = membership == "invite" and subject_mxid == reader_mxid
        others_come_or_go = not self._identity_registry.is_managed(subject_mxid) \
            and is_rendered_membership(membership)
        if not invites_the_reader and not others_come_or_go:
            return None
        rendered = {"user": subject_mxid, "membership": membership}
        if isinstance(content.get("displayname"), str):
            rendered["display_name"] = content["displayname"]
        return rendered

    async def _reply_quote(self, room_id, quoted_id, mxid, engine):
        quote = {"event_id": quoted_id}
        raw = await self._transport.fetch_event(
            room_id=room_id, event_id=quoted_id, as_identity=mxid)
        if raw:
            quote["sender"] = raw.get("sender")
            inner_type, content = await self._type_and_content(raw, room_id, engine)
            text = quoted_text(parse_message(inner_type, content))
            if text is not None:
                quote["text"] = text
        return quote


def _is_redacted(raw: dict) -> bool:
    return "redacted_because" in (raw.get("unsigned") or {}) or not raw.get("content")
