import json
import os
import re
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .ports.speech import SpeechServiceConfiguration
from .services.webhook_notifier import parsed_target


_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{(\w+)\}")
_LISTEN_PATTERN = re.compile(r"(?P<host>[^:]*):(?P<port>\d+)")
_SERVER_NAME_PATTERN = re.compile(r"[A-Za-z0-9.\-]+(:\d+)?")
_PLACEHOLDER_MARK = "CHANGE_ME"
_MINIMUM_SECRET_LENGTH = 32
_HOW_TO_GENERATE = "generate one with: openssl rand -hex 32"


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Configuration:
    homeserver_url: str
    server_name: str
    appservice_id: str
    as_token: str
    hs_token: str
    e2ee: bool
    listen_host: str
    listen_port: int
    appservice_listen_host: str
    appservice_listen_port: int
    admin_token: str
    mailbox_dir: str
    crypto_dir: str
    encryption_key: str
    webhook_allowed_targets: tuple
    webhook_ca_file: str
    tts: SpeechServiceConfiguration | None
    stt: SpeechServiceConfiguration | None
    limits: "Limits"
    extensions: dict = field(default_factory=dict)
    open_signup: bool = True


@dataclass(frozen=True)
class Limits:
    requests_per_minute: int | None = 300
    speech_per_minute: int | None = 20
    signups_per_hour: int | None = 20
    media_bytes: int | None = 25_000_000


def load_configuration(config_path: Path) -> Configuration:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(f"cannot read {config_path}: {error.strerror}")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"{config_path} is not valid json: {error}")
    return parse_configuration(_resolve_environment_placeholders(raw))


def parse_configuration(raw) -> Configuration:
    reader = _Reader(raw, "")
    homeserver = reader.section("homeserver")
    appservice = reader.section("appservice")
    storage = reader.section("storage", required=False)
    speech = reader.section("speech", required=False)
    webhooks = reader.section("webhooks", required=False)
    limits = reader.section("limits", required=False)
    listen_host, listen_port = reader.address("listen", default="0.0.0.0:7700")
    appservice_host, appservice_port = reader.address("appservice_listen", default="127.0.0.1:7701")
    if appservice_port == listen_port:
        raise ConfigurationError("appservice_listen must use a port other than listen's")
    server_name = homeserver.string("server_name")
    if not _SERVER_NAME_PATTERN.fullmatch(server_name):
        raise ConfigurationError("homeserver.server_name must be a host name like example.org")
    extensions = reader.value("extensions", default={})
    if not isinstance(extensions, dict) or not all(
            isinstance(options, dict) for options in extensions.values()):
        raise ConfigurationError("extensions must map module names to option objects")
    configuration = Configuration(
        homeserver_url=homeserver.url("url").rstrip("/"),
        server_name=server_name,
        appservice_id=appservice.string("id"),
        as_token=appservice.secret("as_token"),
        hs_token=appservice.secret("hs_token"),
        e2ee=appservice.boolean("e2ee", default=True),
        listen_host=listen_host,
        listen_port=listen_port,
        appservice_listen_host=appservice_host,
        appservice_listen_port=appservice_port,
        admin_token=reader.secret("admin_token", required=False),
        mailbox_dir=storage.string("mailbox_dir", default="/data/mailboxes"),
        crypto_dir=storage.string("crypto_dir", default="/data/crypto"),
        encryption_key=storage.secret("encryption_key", required=False),
        webhook_allowed_targets=webhooks.targets("allowed_targets"),
        webhook_ca_file=webhooks.certificate_file("ca_file"),
        tts=speech.speech_service("tts"),
        stt=speech.speech_service("stt"),
        extensions=extensions,
        limits=Limits(
            requests_per_minute=limits.limit("requests_per_minute", default=Limits.requests_per_minute),
            speech_per_minute=limits.limit("speech_per_minute", default=Limits.speech_per_minute),
            signups_per_hour=limits.limit("signups_per_hour", default=Limits.signups_per_hour),
            media_bytes=limits.limit("media_bytes", default=Limits.media_bytes),
        ),
        open_signup=reader.boolean("open_signup", default=True),
    )
    reader.reject_unknown()
    return configuration


class _Reader:
    def __init__(self, raw, path: str) -> None:
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{path or 'configuration'} must be an object")
        self._raw = raw
        self._path = path
        self._known: set[str] = set()
        self._children: list[_Reader] = []

    def _name(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def value(self, key: str, *, default=None, required: bool = False):
        self._known.add(key)
        if key not in self._raw or self._raw[key] is None:
            if required:
                raise ConfigurationError(f"{self._name(key)} is required")
            return default
        return self._raw[key]

    def section(self, key: str, *, required: bool = True) -> "_Reader":
        child = _Reader(self.value(key, default={}, required=required), self._name(key))
        self._children.append(child)
        return child

    def string(self, key: str, *, default: str | None = None) -> str:
        value = self.value(key, default=default, required=default is None)
        if not isinstance(value, str) or (default is None and not value.strip()):
            raise ConfigurationError(f"{self._name(key)} must be a non-empty string")
        return value

    def secret(self, key: str, *, required: bool = True) -> str:
        value = self.string(key, default=None if required else "")
        if not value and not required:
            return value
        if _PLACEHOLDER_MARK in value:
            raise ConfigurationError(
                f"{self._name(key)} is still the example's placeholder; {_HOW_TO_GENERATE}")
        if len(value) < _MINIMUM_SECRET_LENGTH:
            raise ConfigurationError(
                f"{self._name(key)} is shorter than {_MINIMUM_SECRET_LENGTH} characters; "
                f"{_HOW_TO_GENERATE}")
        return value

    def address(self, key: str, *, default: str) -> tuple[str, int]:
        match = _LISTEN_PATTERN.fullmatch(self.string(key, default=default))
        if not match:
            raise ConfigurationError(f"{self._name(key)} must look like HOST:PORT, e.g. {default}")
        return match["host"] or "0.0.0.0", int(match["port"])

    def limit(self, key: str, *, default: int) -> int | None:
        self._known.add(key)
        if key not in self._raw:
            return default
        value = self._raw[key]
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigurationError(f"{self._name(key)} must be a positive integer, or null for no limit")
        return value

    def url(self, key: str, *, default: str | None = None) -> str:
        value = self.string(key, default=default)
        if value and urlsplit(value).scheme not in ("http", "https"):
            raise ConfigurationError(f"{self._name(key)} must be an http(s) URL")
        return value

    def certificate_file(self, key: str) -> str:
        path = self.string(key, default="")
        if path:
            try:
                ssl.create_default_context().load_verify_locations(cafile=path)
            except (OSError, ssl.SSLError) as error:
                raise ConfigurationError(f"{self._name(key)} is not a readable PEM certificate: {error}")
        return path

    def boolean(self, key: str, *, default: bool) -> bool:
        value = self.value(key, default=default)
        if not isinstance(value, bool):
            raise ConfigurationError(f"{self._name(key)} must be true or false")
        return value

    def targets(self, key: str) -> tuple:
        value = self.value(key, default=[])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigurationError(f"{self._name(key)} must be a list of strings")
        for item in value:
            try:
                parsed_target(item)
            except ValueError:
                raise ConfigurationError(
                    f"{self._name(key)}: {item!r} is not IP:PORT, CIDR:PORT, a * port or *")
        return tuple(value)

    def speech_service(self, key: str) -> SpeechServiceConfiguration | None:
        if self.value(key) is None:
            return None
        service = self.section(key)
        url = service.url("url", default="")
        configuration = SpeechServiceConfiguration(
            url=url,
            api_key=service.string("api_key", default=""),
            info_url=service.url("info_url", default=""),
        )
        return configuration if url else None

    def reject_unknown(self) -> None:
        unknown = sorted(set(self._raw) - self._known)
        if unknown:
            raise ConfigurationError(
                f"unknown keys in {self._path or 'configuration'}: {', '.join(unknown)}")
        for child in self._children:
            child.reject_unknown()


def _resolve_environment_placeholders(value):
    if isinstance(value, str):
        return _ENV_PLACEHOLDER_PATTERN.sub(_substitute_environment_variable, value)
    if isinstance(value, dict):
        return {key: _resolve_environment_placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment_placeholders(item) for item in value]
    return value


def _substitute_environment_variable(match: re.Match) -> str:
    variable_name = match.group(1)
    try:
        return os.environ[variable_name]
    except KeyError:
        raise ConfigurationError(
            f"configuration references environment variable {variable_name!r} but it is not set")
