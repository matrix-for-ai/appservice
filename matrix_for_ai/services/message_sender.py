import logging

from ..api_errors import ApiError
from ..payload_fields import (
    optional_positive_integer, optional_string, reject_unknown_fields, required_string)
from ..ports.encryption import EncryptionEngine
from ..ports.matrix_transport import MatrixTransport
from ..ports.speech import SpeechUnavailable, Synthesizer, VoiceMessage
from .channel_membership import ChannelMembership
from .identity_registry import ResolvedIdentity
from .image_types import decoded_image
from .profiles import ProfileService, validated_options
from .rate_limits import RateLimit


logger = logging.getLogger(__name__)

VOICE_MESSAGE_FILENAME = "Voice message.ogg"


class MessageSender:
    def __init__(
        self, *, transport: MatrixTransport, membership: ChannelMembership,
        profiles: ProfileService, encryption_for, speech_limit: RateLimit,
        synthesizer: Synthesizer | None = None,
    ) -> None:
        self._transport = transport
        self._membership = membership
        self._profiles = profiles
        self._encryption_for = encryption_for
        self._synthesizer = synthesizer
        self._speech_limit = speech_limit

    async def send(self, identity: ResolvedIdentity, room_id: str, payload: dict) -> dict:
        reject_unknown_fields(payload, ("type", "content"))
        message_type = required_string(payload, "type")
        parse = _PARSERS.get(message_type)
        if parse is None:
            raise ApiError(422, "invalid_type",
                           f"type must be one of {', '.join(_PARSERS)}, not {message_type!r}")
        content = payload.get("content")
        if content is None:
            raise ApiError(422, "content_required", "content is required")
        if not isinstance(content, dict):
            raise ApiError(422, "invalid_content", "content must be an object")
        reject_unknown_fields(content, _CONTENT_FIELDS[message_type], section="content")
        outgoing = parse(content)
        await self._membership.require(identity, room_id)
        if message_type == "audio" and self._synthesizer is None:
            raise ApiError(501, "tts_not_configured", "text-to-speech is not configured")
        route = _Route(self, identity.mxid, room_id)
        await route.prepare()
        return await getattr(self, f"_send_{message_type}")(route, outgoing)

    async def _send_text(self, route, outgoing: dict) -> dict:
        return route.result("text", await route.deliver(
            "m.room.message", {"msgtype": "m.text", "body": outgoing["text"]}))

    async def _send_audio(self, route, outgoing: dict) -> dict:
        voice = await self._synthesize(route.mxid, outgoing["transcript"], outgoing["tts"])
        return route.result("audio", await route.deliver(
            "m.room.message", await self._voice_content(route, voice, outgoing["transcript"])))

    async def _send_image(self, route, outgoing: dict) -> dict:
        reference = await route.upload(outgoing["image"], outgoing["mimetype"],
                                       outgoing["filename"])
        return route.result("image", await route.deliver(
            "m.room.message", {"msgtype": "m.image", "body": outgoing["filename"],
                               **reference, "info": outgoing["info"]}))

    async def _send_poll_response(self, route, outgoing: dict) -> dict:
        content = {
            "m.relates_to": {"rel_type": "m.reference", "event_id": outgoing["poll_event_id"]},
            "org.matrix.msc3381.poll.response": {"answers": outgoing["answers"]},
        }
        return route.result("poll_response", await route.deliver(
            "org.matrix.msc3381.poll.response", content))

    async def _synthesize(self, mxid, text, overrides) -> VoiceMessage:
        if self._synthesizer is None:
            raise ApiError(501, "tts_not_configured", "text-to-speech is not configured")
        self._speech_limit.take(mxid)
        options = self._profiles.speech_options(mxid, "tts", overrides)
        try:
            return await self._synthesizer.synthesize(text, options)
        except SpeechUnavailable as error:
            logger.warning("voice synthesis unavailable for %s: %s", mxid, error)
            if error.provider_status is None:
                raise ApiError(503, "tts_unavailable", "the speech provider is unreachable")
            raise ApiError(503, "tts_unavailable",
                           f"the speech provider answered {error.provider_status}",
                           upstream={"status": error.provider_status})

    async def _voice_content(self, route, voice: VoiceMessage, text: str) -> dict:
        reference = await route.upload(voice.opus_ogg_bytes, "audio/ogg", VOICE_MESSAGE_FILENAME)
        return {
            "msgtype": "m.audio", "body": VOICE_MESSAGE_FILENAME, **reference,
            "info": {"mimetype": "audio/ogg", "size": len(voice.opus_ogg_bytes),
                     "duration": voice.duration_milliseconds},
            "org.matrix.msc3245.voice": {},
            "org.matrix.msc1767.audio": {"duration": voice.duration_milliseconds,
                                         "waveform": voice.waveform},
            "org.matrix.msc1767.text": text,
        }


def _parse_text(content: dict) -> dict:
    return {"text": required_string(content, "text")}


def _parse_audio(content: dict) -> dict:
    return {"transcript": required_string(content, "transcript"), "tts": _tts_overrides(content)}


def _parse_image(content: dict) -> dict:
    image, mimetype = decoded_image(
        required_string(content, "data_base64"), optional_string(content, "mimetype"),
        field="data_base64", mimetype_field="mimetype")
    info = {"mimetype": mimetype, "size": len(image)}
    for dimension in ("w", "h"):
        value = optional_positive_integer(content, dimension)
        if value is not None:
            info[dimension] = value
    return {"image": image, "mimetype": mimetype,
            "filename": optional_string(content, "filename") or f"image.{mimetype.split('/')[1]}",
            "info": info}


def _parse_poll_response(content: dict) -> dict:
    poll_event_id = content.get("poll_event_id")
    answers = content.get("answers")
    if not isinstance(poll_event_id, str) or not poll_event_id.startswith("$"):
        raise ApiError(422, "invalid_poll_event_id", "poll_event_id must be an event id ($…)")
    if (not isinstance(answers, list) or not answers
            or not all(isinstance(answer, str) and answer for answer in answers)):
        raise ApiError(422, "invalid_answers", "answers must be a non-empty list of option ids")
    return {"poll_event_id": poll_event_id, "answers": answers}


def _tts_overrides(content: dict) -> dict | None:
    overrides = content.get("tts")
    return None if overrides is None else validated_options("tts", overrides)


_PARSERS = {"text": _parse_text, "audio": _parse_audio, "image": _parse_image,
            "poll_response": _parse_poll_response}
_CONTENT_FIELDS = {"text": ("text",), "audio": ("transcript", "tts"),
                   "image": ("data_base64", "mimetype", "filename", "w", "h"),
                   "poll_response": ("poll_event_id", "answers")}


class _Route:
    def __init__(self, sender: MessageSender, mxid: str, room_id: str) -> None:
        self._sender = sender
        self.mxid = mxid
        self.room_id = room_id
        self.engine: EncryptionEngine | None = None
        self.encrypted = False

    async def prepare(self) -> None:
        self.encrypted = await self._sender._transport.is_room_encrypted(
            room_id=self.room_id, as_identity=self.mxid)
        if not self.encrypted:
            return
        self.engine = await self._sender._encryption_for(self.mxid)
        if self.engine is None:
            raise ApiError(
                501, "e2ee_unavailable",
                "the channel is encrypted but end-to-end encryption is unavailable")

    async def upload(self, data: bytes, mimetype: str, filename: str) -> dict:
        if self.engine is not None:
            encrypted_file = await self.engine.upload_encrypted(
                data=data, mime_type=mimetype, filename=filename)
            if encrypted_file is None:
                raise ApiError(502, "homeserver_error", "the media upload failed")
            return {"file": encrypted_file}
        content_uri = await self._sender._transport.upload_media(
            data=data, mime_type=mimetype, filename=filename)
        return {"url": content_uri}

    async def deliver(self, event_type: str, content: dict) -> str:
        if self.engine is None:
            return await self._sender._transport.send_event(
                room_id=self.room_id, event_type=event_type, content=content,
                as_identity=self.mxid)
        event_id = await self.engine.send_event(
            room_id=self.room_id, event_type=event_type, content=content)
        if not event_id:
            raise ApiError(502, "homeserver_error", "the homeserver did not accept the event")
        return event_id

    def result(self, message_type: str, event_id: str) -> dict:
        return {"event_id": event_id, "type": message_type, "encrypted": self.encrypted}

