from dataclasses import dataclass
from typing import Protocol


TTS_RESERVED_OPTIONS = ("input", "response_format")
STT_RESERVED_OPTIONS = ("file", "response_format")


class SpeechUnavailable(Exception):
    def __init__(self, reason: str, *, provider_status: int | None = None) -> None:
        super().__init__(reason)
        self.provider_status = provider_status


@dataclass(frozen=True)
class VoiceMessage:
    opus_ogg_bytes: bytes
    duration_milliseconds: int
    waveform: list[int]


class Synthesizer(Protocol):
    async def synthesize(self, text: str, options: dict) -> VoiceMessage: ...

    def describe(self) -> dict: ...

    async def refresh_description(self) -> None: ...


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, mimetype: str, options: dict) -> str: ...

    def describe(self) -> dict: ...

    async def refresh_description(self) -> None: ...


@dataclass(frozen=True)
class SpeechServiceConfiguration:
    url: str
    api_key: str = ""
    info_url: str = ""
