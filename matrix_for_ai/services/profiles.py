import dataclasses
import json

from ..api_errors import ApiError
from ..payload_fields import reject_unknown_fields
from ..ports.matrix_transport import MatrixTransport, MediaTooLarge, MediaUri
from ..ports.speech import STT_RESERVED_OPTIONS, TTS_RESERVED_OPTIONS
from .identity_registry import IdentityRegistry
from .identity_store import Profile
from .image_types import SUPPORTED, image_mimetype


MAX_OPTIONS_BYTES = 4096
MAX_OPTIONS_KEYS = 64
SPEECH_SECTIONS = {"tts": TTS_RESERVED_OPTIONS, "stt": STT_RESERVED_OPTIONS}
READ_ONLY_FIELDS = ("mxid", "localpart")


def validated_options(section: str, value) -> dict:
    if not isinstance(value, dict):
        raise ApiError(422, f"invalid_{section}", f"{section} must be an object")
    reserved = [key for key in SPEECH_SECTIONS[section] if key in value]
    if reserved:
        raise ApiError(
            422, "reserved_option", f"{section} must not set {', '.join(reserved)}: the appservice sets them")
    if len(value) > MAX_OPTIONS_KEYS or len(json.dumps(value)) > MAX_OPTIONS_BYTES:
        raise ApiError(
            422, "options_too_large",
            f"{section} exceeds {MAX_OPTIONS_KEYS} keys or {MAX_OPTIONS_BYTES} bytes")
    return value


def merge_patch(target, patch):
    if not isinstance(patch, dict):
        return patch
    merged = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = merge_patch(merged.get(key), value)
    return merged


class ProfileService:
    def __init__(
        self, *, transport: MatrixTransport, identity_registry: IdentityRegistry,
        max_media_bytes: int | None = None,
    ) -> None:
        self._transport = transport
        self._max_media_bytes = max_media_bytes
        self._identity_registry = identity_registry

    async def describe(self, mxid: str) -> dict:
        matrix = await self._transport.fetch_profile(identity=mxid)
        profile = self._identity_registry.store_for(mxid).profile()
        return {
            "mxid": mxid,
            "localpart": self._identity_registry.localpart_of(mxid),
            "matrix": {"display_name": matrix.get("displayname"),
                       "avatar_url": matrix.get("avatar_url")},
            "tts": profile.tts,
            "stt": profile.stt,
            "transcribe_audio": profile.transcribe_audio,
        }

    def speech_options(self, mxid: str, section: str, overrides=None) -> dict:
        options = getattr(self._identity_registry.store_for(mxid).profile(), section)
        if overrides is not None:
            options = merge_patch(options, validated_options(section, overrides))
        return options

    async def update(self, mxid: str, patch: dict) -> None:
        reject_unknown_fields(
            patch, (*READ_ONLY_FIELDS, "matrix", *SPEECH_SECTIONS, "transcribe_audio"))
        read_only = [field for field in READ_ONLY_FIELDS if field in patch]
        if read_only:
            raise ApiError(422, "read_only_field", f"{', '.join(read_only)} cannot be changed")
        display_name = _display_name_patch(patch)
        store = self._identity_registry.store_for(mxid)
        current = store.profile()
        profile = _patched_profile(current, patch)
        if display_name is not _UNCHANGED:
            await self._transport.set_display_name(identity=mxid, display_name=display_name)
        if profile != current:
            store.set_profile(profile)

    async def avatar(self, mxid: str):
        avatar_url = await self._avatar_url(mxid)
        media_uri = MediaUri.parse(avatar_url)
        try:
            data = None if media_uri is None else await self._transport.download_media(
                media_uri=media_uri, as_identity=mxid, max_bytes=self._max_media_bytes)
        except MediaTooLarge:
            raise ApiError(502, "media_too_large", "the avatar is over this server's limit")
        if data is None:
            raise ApiError(502, "homeserver_error", "the homeserver did not return the avatar")
        return data, image_mimetype(data) or "application/octet-stream"

    async def set_avatar(self, mxid: str, data: bytes, declared_mimetype: str) -> tuple[str, bool]:
        detected = image_mimetype(data)
        if detected is None:
            raise ApiError(422, "invalid_avatar", f"the body is not a {', '.join(SUPPORTED)} image")
        if detected != declared_mimetype:
            raise ApiError(422, "mimetype_mismatch",
                           f"Content-Type says {declared_mimetype}, but the bytes are {detected}")
        replaced = await self._current_avatar_url(mxid) is not None
        content_uri = await self._transport.upload_media(
            data=data, mime_type=detected, filename="avatar")
        await self._transport.set_avatar_url(identity=mxid, avatar_url=content_uri)
        return content_uri, replaced

    async def remove_avatar(self, mxid: str) -> None:
        await self._avatar_url(mxid)
        await self._transport.set_avatar_url(identity=mxid, avatar_url=None)

    async def _avatar_url(self, mxid: str) -> str:
        avatar_url = await self._current_avatar_url(mxid)
        if avatar_url is None:
            raise ApiError(404, "avatar_not_found", f"{mxid} has no avatar")
        return avatar_url

    async def _current_avatar_url(self, mxid: str) -> str | None:
        matrix = await self._transport.fetch_profile(identity=mxid)
        return matrix.get("avatar_url") or None


_UNCHANGED = object()


def _patched_profile(current: Profile, patch: dict) -> Profile:
    changes = {}
    for section in SPEECH_SECTIONS:
        if section not in patch:
            continue
        if patch[section] is None:
            changes[section] = {}
            continue
        validated_options(section, patch[section])
        changes[section] = validated_options(
            section, merge_patch(getattr(current, section), patch[section]))
    if "transcribe_audio" in patch:
        transcribe_audio = patch["transcribe_audio"]
        if transcribe_audio is not None and not isinstance(transcribe_audio, bool):
            raise ApiError(422, "invalid_transcribe_audio",
                           "transcribe_audio must be a boolean, or null for the default (true)")
        changes["transcribe_audio"] = (
            Profile().transcribe_audio if transcribe_audio is None else transcribe_audio)
    return dataclasses.replace(current, **changes)


def _display_name_patch(patch: dict):
    if "matrix" not in patch:
        return _UNCHANGED
    matrix = patch["matrix"]
    if not isinstance(matrix, dict):
        raise ApiError(422, "invalid_matrix",
                       "matrix must be an object; set its fields to null to remove them")
    reject_unknown_fields(matrix, ("display_name", "avatar_url"), section="matrix")
    if "avatar_url" in matrix:
        raise ApiError(422, "read_only_field",
                       "matrix.avatar_url cannot be patched: PUT or DELETE /identities/{id}/avatar")
    if "display_name" not in matrix:
        return _UNCHANGED
    display_name = matrix["display_name"]
    if display_name is None:
        return None
    if not isinstance(display_name, str) or not display_name.strip():
        raise ApiError(422, "invalid_display_name",
                       "matrix.display_name must be a non-empty string, or null to remove it")
    return display_name.strip()
