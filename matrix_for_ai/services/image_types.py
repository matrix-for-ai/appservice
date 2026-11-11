import base64
import binascii

from ..api_errors import ApiError

SUPPORTED = ("image/png", "image/jpeg", "image/gif", "image/webp")


def image_mimetype(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decoded_image(encoded: str, declared_mimetype: str | None, *, field: str,
                  mimetype_field: str) -> tuple[bytes, str]:
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, f"invalid_{field}", f"{field} is not valid base64")
    detected = image_mimetype(data)
    if detected is None:
        raise ApiError(422, f"invalid_{field}",
                       f"{field} is not a {', '.join(SUPPORTED)} image")
    if declared_mimetype is not None and declared_mimetype != detected:
        raise ApiError(422, f"{mimetype_field}_mismatch",
                       f"{mimetype_field} says {declared_mimetype}, but the bytes are {detected}")
    return data, detected
