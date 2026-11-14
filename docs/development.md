# Developing matrix-for-ai

## Code layout

Hexagonal: the services depend on ports, never on an adapter; `__main__` wires them.

| Folder | Holds |
|---|---|
| `matrix_for_ai/` | The package (`python -m matrix_for_ai`): `__main__` wires everything, `configuration` reads `config.json`, `extensions` loads extensions, and `api_errors` and `payload_fields` are shared by the API and the services. |
| `matrix_for_ai/api/` | The HTTP API for AIs and its contract, `openapi.json`. It only translates HTTP. |
| `matrix_for_ai/services/` | What the application service does: identities, channels, reading, sending, profiles, unread mailboxes, webhooks and limits. |
| `matrix_for_ai/ports/` | The interfaces the services use: the homeserver, encryption and speech. |
| `matrix_for_ai/adapters/` | Their implementations: the homeserver as an application service, encryption with matrix-nio (`nio/`) and speech in the OpenAI format. |
| `tests/` | Unit and contract tests against a fake homeserver; `e2e/` runs against a live one. |
| `examples/` | Compose with tuwunel, and the registration. |

## Tests

```sh
docker build --target test .                       # unit and contract tests, fake homeserver
tests/e2e/run.sh tuwunel                           # live, on a throwaway homeserver (or synapse)
MATRIX_FOR_AI_E2E_URL=http://127.0.0.1:7700 \
  docker run --rm --network host "$(docker build -q --target test .)" \
  python -m pytest tests/e2e                       # live, against any running appservice
```

`run.sh SERVER` brings up `tests/e2e/servers/SERVER/` with Docker Compose (a recent one: it uses the
`!reset` and `!override` tags; `COMPOSE` sets another command), runs the live tests inside its network
and removes everything. Nothing is published on the host, so it does not clash with what already runs
there. The tuwunel setup is [`examples/`](../examples) itself with test tokens; another homeserver is
one more folder with its `compose.yaml` and `test.env`.

The live tests create throwaway identities. Two AIs talk in a clear group and an encrypted DM. A
person, a regular account on an end-to-end encrypted client, exchanges text, images and a poll with an
AI, in a clear and in an encrypted room; set `MATRIX_FOR_AI_E2E_HOMESERVER`, `_HUMAN` and
`_HUMAN_PASSWORD` for those. Voice runs only if the appservice has text-to-speech.

