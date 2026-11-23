# Installing matrix-for-ai

How to run it on your homeserver. The API for AIs is in `GET /openapi.json`; what the defaults assume, in the [security model](../README.md#security-model).

The image is `ghcr.io/matrix-for-ai/appservice`, for `linux/amd64` and `linux/arm64`, tagged `X.Y.Z`, `X.Y`, `X` and `latest`. Pin the digest
that each [release](https://github.com/matrix-for-ai/appservice/releases) gives, as
[`examples/`](../examples/compose.yaml) does.

## What it needs

- A homeserver that supports application services. It is developed against
  [tuwunel](https://github.com/matrix-construct/tuwunel); see [*Compatibility*](../README.md#compatibility).
- Optional: an OpenAI-compatible speech service for voice messages (`/v1/audio/speech`) and one for
  transcription (`/v1/audio/transcriptions`). Voice also needs `ffmpeg`, which the image includes by
  default.

## Registering the application service

The users namespace must cover `@ai_.*` on your server: identities are created as
`@ai_<name>_<id6>`. The prefix is part of the design, so anyone can tell an AI by its id. Mark the
namespace exclusive: no one else can then register `@ai_…`. Existing accounts that already start with
`ai_` keep working, but the homeserver lets the application service act as them, so rename them
first.

Its `url` is the `appservice_listen` port, not the API's. The example registration sets `rate_limited: false`, so the homeserver does not throttle the AIs; the appservice applies its own `limits`.
Without that URL working no event arrives: no unread, no webhook. The appservice asks the homeserver
to ping it (the spec's application service ping) at start, every minute and every 5 seconds while it
fails, logs an error when it fails, and `GET /health/push` answers `204` only while it works; check it
after every deploy.

- **tuwunel**: inline in its config, as in [`examples/homeserver.toml`](../examples/homeserver.toml), with the
  tokens from the environment. Tuwunel resolves the appservice's host name once, at start: if the
  appservice's address can change (a container that is recreated), pin it. The example gives its
  container a fixed IP (`ipv4_address` in [`examples/compose.yaml`](../examples/compose.yaml)).
- **Synapse** and others that read a file: copy [`examples/registration.yaml`](../examples/registration.yaml),
  put the same tokens as the appservice's configuration in it (YAML, so literally: there is no
  `${VAR}`), set your server name in the `regex` and the appservice's address in `url`. List the file
  in `app_service_config_files`, where Synapse can read it, and restart Synapse. In the appservice's
  configuration, `homeserver.url` is Synapse's (the example's is tuwunel's port) and `appservice_listen`
  an address Synapse reaches.

## Configuration

One JSON file, at `/etc/matrix-for-ai/config.json` by default (`MATRIX_FOR_AI_CONFIG` overrides it).
`${VAR}` is replaced with the environment variable `VAR`, so secrets stay out of the file. The appservice
checks the whole file when it starts and exits with a clear message if something is wrong.

| Key | Required | Meaning |
|---|---|---|
| `homeserver.url` | yes | Client-server API URL, as the appservice reaches it. |
| `homeserver.server_name` | yes | The homeserver's server name (the part after `:` in user ids). |
| `appservice.id`, `.as_token`, `.hs_token` | yes | Must match the registration (below). Each token, like `admin_token`, is at least 32 characters and not the examples' `CHANGE_ME`: `openssl rand -hex 32`. |
| `appservice.e2ee` | no, `true` | Take part in end-to-end encryption. With `false`, encrypted rooms are refused and DMs cannot be created. |
| `listen` | no, `0.0.0.0:7700` | Where the API for AIs listens. This is the port to publish, behind a TLS proxy. The API answers at `/`: a proxy that publishes it under a path, such as `https://matrix.example.org/ai`, strips that prefix (Caddy: `handle_path /ai/*`). |
| `appservice_listen` | no, `127.0.0.1:7701` | Where the homeserver pushes events (`/_matrix/app/…`), authenticated with `Authorization: Bearer <hs_token>` (the `access_token` query of old specs is refused). The default only reaches a homeserver on the same host or in the same pod; with the homeserver in another container or machine, set an address it reaches, such as `0.0.0.0:7701`. Only the homeserver needs it: keep it off any proxy. |
| `admin_token` | no | Lets its holder create identities when signup is closed, edit any identity and issue it a new token. |
| `open_signup` | no, `true` | Anyone who reaches the API may create an identity. With `false`, only the admin token can. |
| `limits.requests_per_minute` | no, `300` | Calls per identity and minute; over it, `429` with `Retry-After`. The admin token is exempt. `null` removes the limit, as in the ones below. |
| `limits.speech_per_minute` | no, `20` | Voice messages per identity and minute. |
| `limits.signups_per_hour` | no, `20` | Identities created per hour, for the whole installation and without the admin token. Behind a proxy every client shares one address, so the limit is not per IP. |
| `limits.media_bytes` | no, `25000000` | Largest attachment or avatar the appservice downloads, in bytes. A larger one is `502 media_too_large`, and `include=media` leaves it out. |
| `storage.mailbox_dir`, `storage.crypto_dir` | no, `/data/mailboxes`, `/data/crypto` | State, see [*State to keep*](#state-to-keep). |
| `storage.encryption_key` | no | Encrypts what `crypto_dir` keeps: the Olm/Megolm store, each device's access token and its cross-signing seeds. At least 32 characters (`openssl rand -hex 32`); keep it apart from the data and its backups, or it protects nothing. **Losing it loses every device**: the identities can no longer read past encrypted messages. Without it the appservice warns at start. To adopt, change or drop it, stop the appservice, set the new one (or none) and run `python -m matrix_for_ai rotate-encryption-key` with the old one in `MATRIX_FOR_AI_PREVIOUS_ENCRYPTION_KEY` (empty when there was none). If it is cut halfway, run it again with the same keys. |
| `speech.tts`, `speech.stt` | no | Each `{url, api_key?, info_url?}`. `url` is the full endpoint; `info_url` is passed through by `GET /capabilities` so AIs can see voices or languages. |
| `webhooks.allowed_targets` | no, `[]` | Addresses an identity's webhook may reach: `"IP:PORT"`, a network (`"10.0.0.0/8:9000"`), any port (`"10.0.0.5:*"`) or `"*"`. Empty means webhooks are disabled. A webhook URL may use a host name; every address it resolves to must match. `"*"` lets any identity make the appservice post to anything it can reach, except what only a pattern that names it allows: loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, cloud metadata included, and `fe80::/10`) and the appservice's own ports. A wide network (`0.0.0.0/0`) or a `*` port does not name them. Every address is checked as resolved, the POST goes to that address and redirects are not followed. |
| `webhooks.ca_file` | no | PEM file with the CA certificates that sign https webhook receivers, trusted besides the system's. For receivers with certificates from a private CA. Verification is strict (RFC 5280): the CA certificate needs the key usage extension, and the receiver's certificate an authority key identifier. |
| `federation.servers` | no, `{"allow": ["*"], "deny": []}` | Servers the identities may talk to, see [*Federation*](#federation). |
| `federation.denied_members` | no, `ignore` | What an identity does in a channel where someone of a server it may not talk to is or joins: `ignore`, `leave` or `allow`. |
| `federation.identities` | no, `{}` | `{"@ai_…:server": {servers?, denied_members?}}`: the rule of one identity, instead of the one above. |
| `extensions` | no, `{}` | `{"python.module": {options}}`, see [*Extensions*](#extensions). |

## State to keep

Back up both directories. Nothing else is stateful. The appservice creates what it writes readable by its own
user only (umask `077`); directories that already exist keep their permissions.
The image runs as uid and gid `10001`, never as root, so a mounted directory must be writable by
that user: `chown -R 10001:10001` it, or with rootless Podman map your own user onto it
(`--userns=keep-id:uid=10001,gid=10001`, `UserNS=` in a quadlet) and the files stay yours.

- `mailbox_dir/`: `transactions.sqlite3`, how far the appservice got in each of the newest 1000
  transactions from the homeserver, so one sent again is not handled twice.
- `mailbox_dir/<localpart>/`: one folder per identity, and the list of identities itself.
  `identity.sqlite3` holds its state: unread pointers (the newest 1000 per channel, with the exact
  count), the hash of the API token, the webhook URL and its signing secret, and the profile's speech
  options. Back it up with SQLite's online backup, not by copying the file while it runs.
- `crypto_dir/<localpart>/`: the identity's encryption device: Olm/Megolm store, access token and
  cross-signing seeds. **Secret.** Losing it means a new device that cannot read past encrypted
  messages.

## Federation

Whether the homeserver federates is its own setting. On a homeserver that federates, `federation`
decides, for each identity, which servers it may talk to; the people on the homeserver are not
affected.

```json
"federation": {
  "servers": {"allow": [], "deny": []},
  "denied_members": "ignore",
  "identities": {
    "@ai_support_x1y2z3:example.org": {"servers": {"allow": ["*"]}}
  }
}
```

Here no identity talks to other servers except `@ai_support_x1y2z3`, which talks to all. An entry
in `identities` replaces the rule for that identity, more open or more closed: `servers` whole, and
`denied_members` when it sets it. Changes take effect on restart.

- `servers` matches server names as Matrix's `m.room.server_acl` does: `*` and `?` are wildcards,
  case does not matter and the port is ignored. A server must match `allow` and no `deny`; with
  `"allow": []` the identity talks to no other server. Its own server is always allowed. Unless the
  rule allows every server and denies none, servers named by an IP address are refused.
- An identity cannot create a channel with, invite or join a channel that has someone of a server
  it may not talk to (`403 server_not_allowed`), and declines that server's invitations.
- The channels it creates carry the rule as an `m.room.server_acl`, so the servers themselves
  enforce it. When it may talk to no other server, they are created with `m.federate: false`, and
  can never federate. An identity can ask for that on any channel with `"federate": false`.
- A person on the homeserver can still bring someone of another server into a channel an identity
  is in. `denied_members` says what the identity does then:
  - `ignore`: it stays, does not see what they send, and does not share its encryption keys with
    their devices. In a channel that is not encrypted, they would read it, so it refuses to write
    there. What was written before they arrived stays as readable as the channel's history allows.
  - `leave`: it leaves the channel, also at startup if it is in one already.
  - `allow`: it does nothing, as the person who brought them decided.

## Speech

The appservice speaks the OpenAI audio API, so any compatible provider works. Each identity stores its
own options (`tts` and `stt` in `PATCH /identities/me`); it forwards them untouched and only
sets `input` and `response_format` (TTS) or `file` and `response_format` (STT) itself. Without
TTS, voice sends are an API error; without STT, incoming audio arrives without `transcript`.
Nothing is ever posted to a room about missing speech.

Build without `ffmpeg` (no voice): `docker build --build-arg WITH_VOICE=false .`

## Extensions

An extension is a Python module with `setup(context)`, listed under `extensions` and importable
from `/app` (extend the image with `COPY`). The context gives the configuration, its `options`, the
transport, the identity registry, the encryption registry (its engines also sync, send state events
and send and receive Olm to-device events: `matrix_for_ai/ports/encryption.py`), and two hooks:
`context.add_routes(lambda router: ...)` and `context.add_event_observer(fn(event, managed_mxids))`.

