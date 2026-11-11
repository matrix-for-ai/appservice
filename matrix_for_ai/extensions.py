import importlib
import logging
from dataclasses import dataclass, field
from typing import Callable

import aiohttp
from aiohttp import web

from .configuration import Configuration
from .ports.encryption import EncryptionRegistry
from .ports.matrix_transport import MatrixTransport
from .services.identity_registry import IdentityRegistry
from .services.incoming_event_router import EventObserver, IncomingEventRouter


logger = logging.getLogger(__name__)


@dataclass
class ExtensionContext:
    configuration: Configuration
    options: dict
    http_session: aiohttp.ClientSession
    transport: MatrixTransport
    identity_registry: IdentityRegistry
    encryption: EncryptionRegistry | None
    event_router: IncomingEventRouter
    route_registrars: list = field(default_factory=list)

    def add_routes(self, register: Callable[[web.UrlDispatcher], None]) -> None:
        self.route_registrars.append(register)

    def add_event_observer(self, observer: EventObserver) -> None:
        self.event_router.add_observer(observer)


def load_extensions(base_context: dict, extensions: dict) -> list[ExtensionContext]:
    loaded = []
    for module_name, options in extensions.items():
        module = importlib.import_module(module_name)
        context = ExtensionContext(options=options, **base_context)
        module.setup(context)
        logger.info("extension loaded: %s", module_name)
        loaded.append(context)
    return loaded
