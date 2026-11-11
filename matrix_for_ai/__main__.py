import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

from .adapters.matrix_appservice_transport import MatrixAppserviceTransport
from .adapters.openai_speech import OpenAiSpeechSynthesizer, OpenAiTranscriber
from .api.access_log import AccessLoggerWithoutQuery
from .api.http_api import MAX_REQUEST_BYTES, HttpApi, build_application
from .configuration import Configuration, ConfigurationError, load_configuration
from .extensions import load_extensions
from .ports.matrix_transport import HomeserverError
from .services.channel_membership import ChannelMembership
from .services.channels import ChannelService
from .services.event_reader import EventReader
from .services.identity_registry import IdentityRegistry
from .services.incoming_event_router import IncomingEventRouter
from .services.message_sender import MessageSender
from .services.profiles import ProfileService
from .services.push_reachability import PushReachability
from .services.rate_limits import per_hour, per_minute
from .services.webhook_notifier import WebhookNotifier


logger = logging.getLogger("matrix_for_ai")

PRIVATE_FILES_UMASK = 0o077
CONFIG_PATH = Path(os.environ.get("MATRIX_FOR_AI_CONFIG", "/etc/matrix-for-ai/config.json"))

SPEECH_DESCRIPTION_REFRESH_SECONDS = 300
PUSH_CHECK_SECONDS = 60
PREVIOUS_ENCRYPTION_KEY_VARIABLE = "MATRIX_FOR_AI_PREVIOUS_ENCRYPTION_KEY"


def build_encryption(configuration: Configuration):
    if not configuration.e2ee:
        return None
    from .adapters.nio.crypto_registry import CryptoEngineRegistry
    from .adapters.nio.stored_secrets import StoredSecrets
    if not configuration.encryption_key:
        logger.warning("storage.encryption_key is not set: the encryption devices' keys, tokens "
                       "and cross-signing seeds are stored unencrypted in %s",
                       configuration.crypto_dir)
    return CryptoEngineRegistry(
        homeserver_url=configuration.homeserver_url,
        server_name=configuration.server_name,
        as_token=configuration.as_token,
        store_root=configuration.crypto_dir,
        stored_secrets=StoredSecrets(configuration.encryption_key),
    )


def compose(
    configuration: Configuration, http_session, transport, encryption, speech=None,
):
    identity_registry = IdentityRegistry(
        mailbox_base_dir=Path(configuration.mailbox_dir),
        server_name=configuration.server_name,
    )
    identity_registry.load()

    async def encryption_for(mxid: str):
        if encryption is None:
            return None
        engine = encryption.get(mxid)
        if engine is not None:
            return engine
        localpart = identity_registry.localpart_of(mxid)
        return await encryption.ensure(localpart) if localpart else None

    if speech is None:
        speech = {
            "tts": OpenAiSpeechSynthesizer(http_session, configuration.tts)
            if configuration.tts else None,
            "stt": OpenAiTranscriber(http_session, configuration.stt)
            if configuration.stt else None,
        }
    synthesizer, transcriber = speech.get("tts"), speech.get("stt")
    webhook_notifier = WebhookNotifier(
        http_session=http_session, identity_registry=identity_registry,
        allowed_targets=configuration.webhook_allowed_targets,
        ca_file=configuration.webhook_ca_file,
        own_ports=(configuration.listen_port, configuration.appservice_listen_port))
    event_router = IncomingEventRouter(
        identity_registry=identity_registry, transport=transport,
        webhook_notifier=webhook_notifier)
    transport.set_incoming_event_handler(event_router.route)
    limits = configuration.limits
    profiles = ProfileService(
        transport=transport, identity_registry=identity_registry,
        max_media_bytes=limits.media_bytes)
    membership = ChannelMembership(transport=transport)
    event_reader = EventReader(
        transport=transport, identity_registry=identity_registry, transcriber=transcriber,
        max_media_bytes=limits.media_bytes)
    push_reachability = PushReachability(
        transport=transport, appservice_id=configuration.appservice_id,
        on_recovered=event_router.accept_pending_invitations)
    http_api = HttpApi(
        push_reachability=push_reachability,
        transport=transport,
        identity_registry=identity_registry,
        channels=ChannelService(
            transport=transport, membership=membership, event_reader=event_reader,
            event_router=event_router,
            encryption_for=encryption_for, e2ee_enabled=encryption is not None),
        sender=MessageSender(
            transport=transport, membership=membership, profiles=profiles,
            encryption_for=encryption_for, synthesizer=synthesizer,
            speech_limit=per_minute(limits.speech_per_minute, "voice messages")),
        profiles=profiles,
        webhook_notifier=webhook_notifier,
        speech_services={"tts": synthesizer, "stt": transcriber},
        encryption_for=encryption_for,
        e2ee_enabled=encryption is not None,
        request_limit=per_minute(limits.requests_per_minute, "requests"),
        signup_limit=per_hour(limits.signups_per_hour, "signups"),
        admin_token=configuration.admin_token,
        open_signup=configuration.open_signup,
    )
    application = build_application()
    http_api.register_routes(application)
    application.cleanup_ctx.append(_checked_every(push_reachability.check, PUSH_CHECK_SECONDS))
    application.cleanup_ctx.append(
        _speech_descriptions_kept_fresh([synthesizer, transcriber]))
    appservice_application = web.Application(client_max_size=MAX_REQUEST_BYTES)
    transport.register_push_routes(appservice_application)
    extension_contexts = load_extensions({
        "configuration": configuration, "http_session": http_session,
        "transport": transport, "identity_registry": identity_registry,
        "encryption": encryption, "event_router": event_router,
    }, configuration.extensions)
    for context in extension_contexts:
        for register in context.route_registrars:
            register(application.router)
    return application, appservice_application, identity_registry, event_router


def _checked_every(check, seconds):
    async def check_forever():
        while True:
            try:
                await check()
            except Exception:
                logger.exception("periodic %s failed; retrying in %ss", check.__qualname__, seconds)
            await asyncio.sleep(seconds)

    async def context(_application):
        task = asyncio.create_task(check_forever())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return context


def _speech_descriptions_kept_fresh(services):
    described = [service for service in services if service is not None]

    async def refresh_speech_descriptions():
        await asyncio.gather(*(service.refresh_description() for service in described))

    return _checked_every(refresh_speech_descriptions, SPEECH_DESCRIPTION_REFRESH_SECONDS)


async def run(configuration: Configuration) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, stop.set)
    http_session = aiohttp.ClientSession()
    transport = MatrixAppserviceTransport(
        http_session=http_session,
        homeserver_url=configuration.homeserver_url,
        application_service_token=configuration.as_token,
        homeserver_token=configuration.hs_token,
    )
    encryption = build_encryption(configuration)
    application, appservice_application, identity_registry, event_router = compose(
        configuration, http_session, transport, encryption)

    runners = []
    for name, app, host, port in (
            ("HTTP API", application, configuration.listen_host, configuration.listen_port),
            ("appservice", appservice_application, configuration.appservice_listen_host,
             configuration.appservice_listen_port)):
        runner = web.AppRunner(app, access_log_class=AccessLoggerWithoutQuery)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
        logger.info("%s listening on %s:%d", name, host, port)
    try:
        await _catch_up_when_homeserver_ready(event_router, stop)
        if encryption is not None:
            for localpart in identity_registry.managed_localparts():
                if stop.is_set():
                    break
                await encryption.ensure(localpart)
        await stop.wait()
        logger.info("shutting down")
    finally:
        if encryption is not None:
            await encryption.close_all()
        for runner in runners:
            await runner.cleanup()
        await http_session.close()


async def _catch_up_when_homeserver_ready(
    event_router: IncomingEventRouter, stop: asyncio.Event,
) -> None:
    attempt = 0
    while not stop.is_set():
        try:
            await event_router.bootstrap_membership()
            await event_router.accept_pending_invitations()
            return
        except HomeserverError as error:
            attempt += 1
            delay_seconds = min(2 ** attempt, 30)
            logger.warning(
                "homeserver not ready to catch up (attempt %d): %s; retrying in %ds",
                attempt, error, delay_seconds)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay_seconds)
            except asyncio.TimeoutError:
                pass


def main() -> None:
    os.umask(PRIVATE_FILES_UMASK)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        configuration = load_configuration(CONFIG_PATH)
    except ConfigurationError as error:
        print(f"matrix-for-ai: invalid configuration: {error}", file=sys.stderr)
        sys.exit(2)
    if sys.argv[1:] == ["rotate-encryption-key"]:
        rotate_encryption_key_command(configuration)
        return
    if sys.argv[1:]:
        print("usage: python -m matrix_for_ai [rotate-encryption-key]", file=sys.stderr)
        sys.exit(2)
    asyncio.run(run(configuration))


def rotate_encryption_key_command(configuration: Configuration) -> None:
    from .adapters.nio.stored_secrets import StoredSecretsError, rotate_encryption_key
    previous_key = os.environ.get(PREVIOUS_ENCRYPTION_KEY_VARIABLE, "")
    try:
        rotated = rotate_encryption_key(
            configuration.crypto_dir, previous_key, configuration.encryption_key)
    except StoredSecretsError as error:
        print(f"matrix-for-ai: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"matrix-for-ai: {len(rotated)} identities now use "
          f"{'storage.encryption_key' if configuration.encryption_key else 'no key'}")


if __name__ == "__main__":
    main()
