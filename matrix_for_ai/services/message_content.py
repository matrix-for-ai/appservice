from dataclasses import dataclass

from ..ports.encryption import EncryptedFile


POLL_START_KEYS = ("m.poll.start", "org.matrix.msc3381.poll.start")
POLL_RESPONSE_KEYS = ("m.poll.response", "org.matrix.msc3381.poll.response")
POLL_END_TYPES = ("m.poll.end", "org.matrix.msc3381.poll.end")
LOCATION_KEYS = ("m.location", "org.matrix.msc3488.location")
CALL_MEMBER_TYPE = "org.matrix.msc3401.call.member"
CALL_SIGNALLING_TYPES = (
    "m.call.notify", "org.matrix.msc4075.call.notify", "org.matrix.msc3401.call.notify",
    CALL_MEMBER_TYPE)
MEDIA_MSGTYPES = ("m.image", "m.audio", "m.video", "m.file")
DEFAULT_MIMETYPE = "application/octet-stream"
TEXT_KEYS = ("m.text", "org.matrix.msc1767.text", "body")


@dataclass(frozen=True)
class Text:
    body: str
    reply_to: str | None


@dataclass(frozen=True)
class Media:
    url: str
    encrypted_file: EncryptedFile | None
    mimetype: str
    filename: str
    details: dict
    reply_to: str | None

    @property
    def kind(self) -> str:
        for kind in ("image", "audio", "video"):
            if self.mimetype.startswith(f"{kind}/"):
                return kind
        return "file"


@dataclass(frozen=True)
class PollOption:
    id: str
    text: str


@dataclass(frozen=True)
class Poll:
    question: str
    options: tuple[PollOption, ...]
    reply_to: str | None


@dataclass(frozen=True)
class PollEnd:
    poll_event_id: str | None


@dataclass(frozen=True)
class Location:
    geo_uri: str
    coordinates: tuple[float, float] | None


@dataclass(frozen=True)
class Malformed:
    pass


@dataclass(frozen=True)
class NotAMessage:
    pass


Message = Text | Media | Poll | PollEnd | Location | Malformed | NotAMessage


def parse_message(event_type, content) -> Message:
    if not isinstance(event_type, str) or not isinstance(content, dict):
        return Malformed()
    if event_type.startswith("m.call.") or event_type in CALL_SIGNALLING_TYPES:
        return NotAMessage()
    if any(key in content for key in POLL_START_KEYS):
        return _poll(content)
    location = _location(content)
    if location is not None:
        return location
    if event_type in POLL_END_TYPES:
        relates = content.get("m.relates_to")
        referenced = relates.get("event_id") if isinstance(relates, dict) else None
        return PollEnd(referenced if isinstance(referenced, str) else None)
    if "file" in content or content.get("msgtype") in MEDIA_MSGTYPES:
        return _media(content)
    body = content.get("body", "")
    if not isinstance(body, str):
        return Malformed()
    if not body and _is_reference_fragment(content):
        return NotAMessage()
    return Text(body, reply_to(content))


def poll_answers(content) -> tuple[str, ...] | None:
    if not isinstance(content, dict):
        return None
    response = next(
        (content[key] for key in POLL_RESPONSE_KEYS if isinstance(content.get(key), dict)), None)
    if response is None:
        return None
    answers = response.get("answers")
    if not isinstance(answers, list):
        return ()
    return tuple(answer for answer in answers if isinstance(answer, str))


def reply_to(content: dict) -> str | None:
    relates = content.get("m.relates_to")
    in_reply_to = relates.get("m.in_reply_to") if isinstance(relates, dict) else None
    event_id = in_reply_to.get("event_id") if isinstance(in_reply_to, dict) else None
    return event_id if isinstance(event_id, str) and event_id else None


def quoted_text(message: Message) -> str | None:
    return message.body.strip() if isinstance(message, Text) else None


def _poll(content):
    poll = next((content[key] for key in POLL_START_KEYS if key in content), None)
    if not isinstance(poll, dict):
        return Malformed()
    question = _localized_text(poll.get("question"))
    answers = poll.get("answers")
    if question is None or not isinstance(answers, list):
        return Malformed()
    options = []
    for answer in answers:
        text = _localized_text(answer)
        option_id = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(option_id, str) or text is None:
            return Malformed()
        options.append(PollOption(option_id, text))
    return Poll(question, tuple(options), reply_to(content))


def _localized_text(node) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            for representation in value:
                if isinstance(representation, dict) \
                        and representation.get("mimetype", "text/plain") == "text/plain" \
                        and isinstance(representation.get("body"), str):
                    return representation["body"]
    return None


def _location(content):
    geo_uri = next(
        (content[key].get("uri") for key in LOCATION_KEYS if isinstance(content.get(key), dict)),
        content.get("geo_uri"))
    if not isinstance(geo_uri, str) or not geo_uri.startswith("geo:"):
        return None
    return Location(geo_uri, _coordinates(geo_uri))


def _coordinates(geo_uri):
    values = geo_uri[len("geo:"):].split(";", 1)[0].split(",")
    try:
        latitude, longitude = float(values[0]), float(values[1])
    except (IndexError, ValueError):
        return None
    return latitude, longitude


def _media(content):
    info = content.get("info") if isinstance(content.get("info"), dict) else {}
    mimetype = info.get("mimetype")
    mimetype = mimetype if isinstance(mimetype, str) and mimetype else DEFAULT_MIMETYPE
    if "file" in content:
        encrypted_file = _encrypted_file(content["file"])
        if encrypted_file is None:
            return Malformed()
        url = encrypted_file.url
    else:
        encrypted_file, url = None, content.get("url")
        if not isinstance(url, str):
            return Malformed()
    return Media(url, encrypted_file, mimetype, _filename(content), _details(info),
                 reply_to(content))


def _encrypted_file(node):
    if not isinstance(node, dict):
        return None
    key = node.get("key").get("k") if isinstance(node.get("key"), dict) else None
    sha256 = node.get("hashes").get("sha256") if isinstance(node.get("hashes"), dict) else None
    fields = (node.get("url"), key, sha256, node.get("iv"))
    if not all(isinstance(field, str) for field in fields):
        return None
    return EncryptedFile(*fields)


def _details(info):
    details = {}
    for source, target in (("size", "size"), ("w", "w"), ("h", "h"), ("duration", "duration_ms")):
        value = info.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            details[target] = value
    return details


def _filename(content):
    filename = content.get("filename") or content.get("body")
    return filename if isinstance(filename, str) and filename.strip() else "attachment"


def _is_reference_fragment(content):
    relates = content.get("m.relates_to")
    return isinstance(relates, dict) and relates.get("rel_type") == "m.reference"
