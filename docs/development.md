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
MATRIX_FOR_AI_E2E_URL=http://127.0.0.1:7700 \
  docker run --rm --network host "$(docker build -q --target test .)" \
  python -m pytest tests/e2e                       # live, against any running appservice
```

The live tests create two throwaway identities and talk in a clear group and an encrypted DM.

