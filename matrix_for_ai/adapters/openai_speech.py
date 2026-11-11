import array
import asyncio
import json
import logging
import shutil

import aiohttp

from ..ports.speech import SpeechServiceConfiguration, SpeechUnavailable, VoiceMessage


logger = logging.getLogger(__name__)

_WAVEFORM_BUCKETS = 30
_WAVEFORM_SAMPLE_RATE = 8000
_FILE_EXTENSION_BY_MIMETYPE = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "m4a", "audio/webm": "webm",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac",
}


class _OpenAiCompatibleService:
    def __init__(
        self, http_session: aiohttp.ClientSession, configuration: SpeechServiceConfiguration
    ) -> None:
        self._http_session = http_session
        self._configuration = configuration
        self._description: dict = {"available": True, "info": None}

    def _headers(self) -> dict:
        if self._configuration.api_key:
            return {"Authorization": f"Bearer {self._configuration.api_key}"}
        return {}

    def describe(self) -> dict:
        return self._description

    async def refresh_description(self) -> None:
        if not self._configuration.info_url:
            return
        try:
            async with self._http_session.get(
                self._configuration.info_url, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    self._keep_last_good_description(f"provider answered {response.status}")
                    return
                info = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            self._keep_last_good_description(error.__class__.__name__)
            return
        self._description = {"available": True, "info": info}

    def _keep_last_good_description(self, info_error: str) -> None:
        logger.warning("speech info_url failed: %s", info_error)
        if self._description["info"] is None:
            self._description = {"available": True, "info": None, "info_error": info_error}


class OpenAiSpeechSynthesizer(_OpenAiCompatibleService):
    async def synthesize(self, text: str, options: dict) -> VoiceMessage:
        request_body = {**options, "input": text, "response_format": "wav"}
        try:
            async with self._http_session.post(
                self._configuration.url, json=request_body, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=90),
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    raise SpeechUnavailable(f"provider answered {response.status}: {detail}",
                                            provider_status=response.status)
                audio = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise SpeechUnavailable(f"provider unreachable: {error.__class__.__name__}")
        return await encode_voice_message(audio)


class OpenAiTranscriber(_OpenAiCompatibleService):
    async def transcribe(self, audio: bytes, mimetype: str, options: dict) -> str:
        form = aiohttp.FormData()
        extension = _FILE_EXTENSION_BY_MIMETYPE.get(mimetype.split(";")[0].strip(), "ogg")
        form.add_field("file", audio, filename=f"audio.{extension}", content_type=mimetype)
        for name, value in options.items():
            form.add_field(name, value if isinstance(value, str) else json.dumps(value))
        form.add_field("response_format", "json")
        try:
            async with self._http_session.post(
                self._configuration.url, data=form, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    raise SpeechUnavailable(f"provider answered {response.status}: {detail}",
                                            provider_status=response.status)
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise SpeechUnavailable(f"provider unreachable: {error.__class__.__name__}")
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise SpeechUnavailable("provider answered without text")
        return body["text"].strip()


def audio_tools_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def encode_voice_message(audio: bytes) -> VoiceMessage:
    if not audio_tools_available():
        raise SpeechUnavailable("ffmpeg is not installed")
    opus_ogg_bytes = await _run_ffmpeg(
        audio, ["-ar", "48000", "-ac", "1", "-c:a", "libopus", "-b:a", "24k",
                "-map_metadata", "-1", "-f", "ogg"])
    pcm = await _run_ffmpeg(
        audio, ["-ar", str(_WAVEFORM_SAMPLE_RATE), "-ac", "1", "-f", "s16le"])
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    return VoiceMessage(
        opus_ogg_bytes=opus_ogg_bytes,
        duration_milliseconds=len(samples) * 1000 // _WAVEFORM_SAMPLE_RATE,
        waveform=waveform_of(samples),
    )


def waveform_of(samples, bucket_count: int = _WAVEFORM_BUCKETS) -> list[int]:
    if not samples:
        return [0] * bucket_count
    peaks = []
    for bucket in range(bucket_count):
        chunk = samples[
            bucket * len(samples) // bucket_count:(bucket + 1) * len(samples) // bucket_count]
        peaks.append(max(max(chunk), -min(chunk)) if chunk else 0)
    loudest = max(peaks)
    if loudest == 0:
        return [0] * bucket_count
    return [peak * 1024 // loudest for peak in peaks]


async def _run_ffmpeg(audio: bytes, output_arguments: list[str]) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        *output_arguments, "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        output, errors = await asyncio.wait_for(process.communicate(audio), timeout=60)
    except asyncio.TimeoutError:
        process.kill()
        raise SpeechUnavailable("audio conversion timed out")
    if process.returncode != 0 or not output:
        logger.warning("ffmpeg failed: %s", errors.decode(errors="replace")[:200])
        raise SpeechUnavailable("audio conversion failed")
    return output
