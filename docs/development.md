# Developing matrix-for-ai

## Code layout

Hexagonal: the services depend on ports, never on an adapter; `__main__` wires them. The
boundary is not strict: the services validate the request bodies and raise `ApiError`, which carries
the HTTP status and `code`, and the homeserver port mounts its push routes on an aiohttp application.

| Folder | Holds |
|---|---|
| `matrix_for_ai/` | The package (`python -m matrix_for_ai`): `__main__` wires everything, `configuration` reads `config.json`, `extensions` loads extensions, and `api_errors` and `payload_fields` are shared by the API and the services. |
| `matrix_for_ai/api/` | The HTTP API for AIs and its contract, `openapi.json`: routes, authentication, and results and errors as responses. |
| `matrix_for_ai/services/` | What the application service does: identities, channels, reading, sending, profiles, unread mailboxes, webhooks and limits. |
| `matrix_for_ai/ports/` | The interfaces the services use: the homeserver, encryption and speech. |
| `matrix_for_ai/adapters/` | Their implementations: the homeserver as an application service, encryption with matrix-nio (`nio/`) and speech in the OpenAI format. |
| `tests/` | Unit and contract tests against a fake homeserver; `e2e/` runs against a live one. |
| `examples/` | Compose with tuwunel, and the registration. |

## Tests

```sh
docker build --target test .                       # ruff, mypy, and unit and contract tests
tests/e2e/run.sh tuwunel                           # live, on a throwaway homeserver (or synapse)
docker run --rm --network host -e MATRIX_FOR_AI_E2E_URL=http://127.0.0.1:7700 \
  "$(docker build -q --target test .)" \
  python -m pytest -p no:cacheprovider tests/e2e    # live, against any running appservice
```

`run.sh SERVER` brings up `tests/e2e/servers/SERVER/` with its own Docker Compose, pinned by digest so
that every run uses the same one. It waits until every container is healthy, runs the live tests inside
its network, restarts the appservice to check that what arrives while it is down stays unread, and
removes everything. It needs Docker, or Podman with its socket on and `CONTAINER_ENGINE=podman`;
rootless Podman cannot run health checks in containers made through its Docker API, so there it only
waits for them to start. Nothing is published on the host, so it does not clash with what already runs
there; its network is `172.30.78.0/24`, so one run at a time. The tuwunel setup is
[`examples/`](../examples) itself with test tokens, and a `config.json` that also loads
`tests/e2e/encryption_extension.py`; another homeserver is one more folder with its
`compose.yaml` and `test.env`.

The live tests create throwaway identities. Two AIs talk in a clear group and an encrypted DM. A
person, a regular account on an end-to-end encrypted client, exchanges text, images and a poll with an
AI, in a clear and in an encrypted room; set `MATRIX_FOR_AI_E2E_HOMESERVER` (its URL), `_HUMAN` (the
account's user name) and `_HUMAN_PASSWORD` for those, with `-e` like the URL. Voice runs only if the
appservice has text-to-speech. The encryption API for extensions is tested with
`MATRIX_FOR_AI_E2E_EXTENSION=true`, against an appservice with E2EE that loads that extension.

Ruff and mypy are configured in `pyproject.toml`. The lockfiles come from pip-compile, run in the
`PYTHON_IMAGE` of the `Dockerfile`, with the command written at the top of each.


CI runs the unit tests natively on `linux/amd64` and `linux/arm64`, and the live ones on `linux/amd64`. A
release builds the image on each and publishes both under the same tags.
