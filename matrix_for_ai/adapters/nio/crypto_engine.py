import asyncio
import base64
import io
import json
import logging
import os

import aiohttp
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from nio import AsyncClient, AsyncClientConfig, RoomPreset, ToDeviceMessage
from nio.crypto import decrypt_attachment
from nio.events import MegolmEvent
from nio.exceptions import EncryptionError


logger = logging.getLogger(__name__)


def _canonical_json(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _unpadded_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _preset(*, is_direct: bool, public: bool) -> RoomPreset:
    if public:
        return RoomPreset.public_chat
    return RoomPreset.trusted_private_chat if is_direct else RoomPreset.private_chat


class Ed25519SigningKey:
    SEED_BYTES = 32

    def __init__(self, seed: bytes) -> None:
        self._key = ECC.construct(curve="Ed25519", seed=seed)
        self.public_key = _unpadded_base64(self._key.public_key().export_key(format="raw"))

    def sign(self, message: str) -> str:
        return _unpadded_base64(eddsa.new(self._key, "rfc8032").sign(message.encode("utf-8")))


class CryptoEngine:
    def __init__(self, *, homeserver_url, server_name, as_token, localpart, store_root,
                 stored_secrets):
        self._stored_secrets = stored_secrets
        self._homeserver_url = homeserver_url
        self._server_name = server_name
        self._as_token = as_token
        self._localpart = localpart
        self.mxid = f"@{localpart}:{server_name}"
        self._store_dir = os.path.join(store_root, localpart)
        self._session_file = os.path.join(self._store_dir, "session.json")
        self._cross_signing_file = os.path.join(self._store_dir, "cross_signing.json")
        self._client = None
        self._sync_lock = asyncio.Lock()
        self._access_token = None
        self.device_id = None

    async def start(self):
        os.makedirs(self._store_dir, exist_ok=True)
        restored = self._load_persisted_session()
        access_token, device_id = restored if restored else await self._login_and_persist()
        self.device_id = device_id
        self._access_token = access_token
        config = AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True,
                                   pickle_key=self._stored_secrets.pickle_key)
        self._client = AsyncClient(
            self._homeserver_url, self.mxid, device_id=device_id,
            store_path=self._store_dir, config=config)
        self._client.restore_login(
            user_id=self.mxid, device_id=device_id, access_token=access_token)
        self._client.load_store()
        if self._client.should_upload_keys:
            await self._client.keys_upload()
        try:
            await self._bootstrap_cross_signing()
        except Exception as error:
            logger.warning("cross-signing bootstrap failed for %s: %s", self.mxid, error)

    async def receive_keys(self):
        await self._sync(timeout=0)

    async def decrypt_event(self, raw_event, room_id):
        decrypted = self._try_decrypt(raw_event, room_id)
        if decrypted is None:
            return None
        return getattr(decrypted, "source", None) or {}

    def decrypt_attachment(self, ciphertext, encrypted_file):
        try:
            return decrypt_attachment(
                ciphertext, encrypted_file.key, encrypted_file.sha256, encrypted_file.iv)
        except (EncryptionError, ValueError) as error:
            logger.warning("attachment %s does not decrypt for %s: %s",
                           encrypted_file.url, self.mxid, error)
            return None

    async def _ensure_room_loaded(self, room_id):
        if room_id in self._client.rooms:
            return
        await self._sync(timeout=8000)
        if room_id in self._client.rooms:
            return
        self._client.next_batch = None
        self._client.loaded_sync_token = None
        await self._sync(timeout=10000, full_state=True)

    async def _sync(self, **kwargs):
        async with self._sync_lock:
            return await self._client.sync(**kwargs)

    async def _prepare_send(self, room_id):
        if room_id in self._client.rooms:
            await self._sync(timeout=0)
        else:
            await self._ensure_room_loaded(room_id)
        client = self._client
        if client.should_query_keys:
            await client.keys_query()
        session = client.olm.outbound_group_sessions.get(room_id)
        if session is None:
            return
        devices = {
            (user_id, device.id)
            for user_id in client.rooms[room_id].users
            for device in client.device_store.active_user_devices(user_id)
            if not device.blacklisted}
        devices.discard((self.mxid, self.device_id))
        if not devices <= session.users_shared_with | session.users_ignored:
            del client.olm.outbound_group_sessions[room_id]

    async def upload_encrypted(self, *, data, mime_type, filename):
        upload, file_keys = await self._client.upload(
            lambda *_: io.BytesIO(data),
            content_type=mime_type, filename=filename,
            encrypt=True, filesize=len(data))
        content_uri = getattr(upload, "content_uri", None)
        if not content_uri or not file_keys:
            logger.warning("encrypted upload failed for %s: %s", self.mxid, upload)
            return None
        return {**file_keys, "url": content_uri}

    async def send_event(self, *, room_id, event_type, content):
        await self._prepare_send(room_id)
        response = await self._client.room_send(
            room_id, event_type, content, ignore_unverified_devices=True)
        event_id = getattr(response, "event_id", None)
        if event_id is None:
            logger.warning("encrypted send failed for %s in %s: %s", self.mxid, room_id, response)
        return event_id

    async def create_encrypted_room(self, *, invitees, name, topic, is_direct, public):
        create = await self._client.room_create(
            name=name, topic=topic, invite=invitees, is_direct=is_direct,
            preset=_preset(is_direct=is_direct, public=public),
            initial_state=[
                {"type": "m.room.encryption", "state_key": "",
                 "content": {"algorithm": "m.megolm.v1.aes-sha2"}},
                {"type": "m.room.history_visibility", "state_key": "",
                 "content": {"history_visibility": "invited"}},
            ])
        return getattr(create, "room_id", None)

    async def sync_once(self, timeout=8000):
        await self._sync(timeout=timeout)

    async def put_state(self, room_id, event_type, content, state_key=""):
        return await self._client.room_put_state(
            room_id, event_type, content, state_key=state_key)

    async def sync_loop(self, stop_event, timeout=5000):
        while not stop_event.is_set():
            try:
                await self._sync(timeout=timeout)
            except Exception as error:
                logger.warning("sync loop error for %s: %s", self.mxid, error)
                await asyncio.sleep(1)

    def install_olm_event_capture(self, event_type, handler):
        machine = self._client.olm
        handlers = getattr(machine, "_captured_olm_handlers", None)
        if handlers is None:
            handlers = machine._captured_olm_handlers = {}
            original = machine._handle_olm_event

            def capturing(sender, sender_key, payload):
                captured = handlers.get(payload.get("type")) if isinstance(payload, dict) else None
                if captured is not None:
                    try:
                        captured(sender, sender_key, payload)
                    except Exception as error:
                        logger.warning("olm event capture error: %s", error)
                return original(sender, sender_key, payload)

            machine._handle_olm_event = capturing
        handlers[event_type] = handler

    async def send_olm_to_device(self, user_id, device_id, event_type, content):
        olm = self._client.olm
        device = self._client.device_store[user_id].get(device_id)
        if device is None:
            olm.users_for_key_query.add(user_id)
            await self._client.keys_query()
            device = self._client.device_store[user_id].get(device_id)
            if device is None:
                raise RuntimeError(f"device {device_id} of {user_id} not found")
        session = olm.session_store.get(device.curve25519)
        if session is None:
            await self._client.keys_claim({user_id: [device_id]})
            session = olm.session_store.get(device.curve25519)
            if session is None:
                raise RuntimeError(f"no olm session for {device_id} (no one-time keys?)")
        olm_content = olm._olm_encrypt(session, device, event_type, content)
        message = ToDeviceMessage(
            type="m.room.encrypted", recipient=user_id,
            recipient_device=device_id, content=olm_content)
        return await self._client.to_device(message)

    async def close(self):
        if self._client is not None:
            await self._client.close()

    def _try_decrypt(self, raw_event, room_id):
        event = MegolmEvent.from_dict(self._with_legacy_sender_fields(raw_event, room_id))
        if not isinstance(event, MegolmEvent):
            return None
        try:
            return self._client.olm.decrypt_megolm_event(event, room_id)
        except EncryptionError:
            return None

    def _with_legacy_sender_fields(self, raw_event, room_id):
        content = raw_event.get("content", {})
        if content.get("sender_key") and content.get("device_id"):
            return raw_event
        sender_key = content.get("sender_key") or self._sender_key_of_session(
            room_id, content.get("session_id"))
        if sender_key is None:
            return raw_event
        return {**raw_event, "content": {
            **content, "sender_key": sender_key, "device_id": content.get("device_id", "")}}

    def _sender_key_of_session(self, room_id, session_id):
        for sender_key, sessions in self._client.olm.inbound_group_store[room_id].items():
            if session_id in sessions:
                return sender_key
        return None

    def _load_persisted_session(self):
        data = self._stored_secrets.read_json(self._session_file)
        return None if data is None else (data["access_token"], data["device_id"])

    async def _login_and_persist(self):
        access_token, device_id = await self._login_application_service()
        self._stored_secrets.write_json(
            self._session_file, {"access_token": access_token, "device_id": device_id})
        return access_token, device_id

    async def _login_application_service(self):
        headers = {"Authorization": f"Bearer {self._as_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._homeserver_url}/_matrix/client/v3/register", headers=headers,
                json={"type": "m.login.application_service", "username": self._localpart},
            ) as response:
                body = await response.json()
                if response.status != 200 and body.get("errcode") != "M_USER_IN_USE":
                    raise RuntimeError(f"register failed {response.status}: {body}")
            async with session.post(
                f"{self._homeserver_url}/_matrix/client/v3/login", headers=headers,
                json={"type": "m.login.application_service",
                      "identifier": {"type": "m.id.user", "user": self._localpart}},
            ) as response:
                body = await response.json()
                if response.status != 200:
                    raise RuntimeError(f"login failed {response.status}: {body}")
                return body["access_token"], body["device_id"]

    async def _bootstrap_cross_signing(self):
        signing_keys = self._load_or_create_cross_signing()
        async with aiohttp.ClientSession() as session:
            if not await self._cross_signing_present(session):
                await self._upload_cross_signing_keys(session, signing_keys)
            await self._self_sign_device(session, signing_keys["ssk"])

    def _load_or_create_cross_signing(self):
        names = ("master", "ssk", "usk")
        stored_seeds = self._stored_secrets.read_json(self._cross_signing_file)
        if stored_seeds is not None:
            return {name: Ed25519SigningKey(base64.b64decode(stored_seeds[name]))
                    for name in names}
        seeds = {name: os.urandom(Ed25519SigningKey.SEED_BYTES) for name in names}
        self._stored_secrets.write_json(
            self._cross_signing_file,
            {n: base64.b64encode(s).decode("ascii") for n, s in seeds.items()})
        return {name: Ed25519SigningKey(seeds[name]) for name in names}

    async def _cross_signing_present(self, session):
        async with session.post(
            f"{self._homeserver_url}/_matrix/client/v3/keys/query",
            headers={"Authorization": f"Bearer {self._access_token}"},
            json={"device_keys": {self.mxid: []}},
        ) as response:
            data = await response.json()
            return self.mxid in data.get("master_keys", {})

    async def _upload_cross_signing_keys(self, session, signing_keys):
        master = signing_keys["master"]
        master_pub = master.public_key
        master_key = {"user_id": self.mxid, "usage": ["master"],
                      "keys": {f"ed25519:{master_pub}": master_pub}}
        self_signing_key = self._sign_key_object({
            "user_id": self.mxid, "usage": ["self_signing"],
            "keys": {f"ed25519:{signing_keys['ssk'].public_key}": signing_keys["ssk"].public_key},
        }, master, master_pub)
        user_signing_key = self._sign_key_object({
            "user_id": self.mxid, "usage": ["user_signing"],
            "keys": {f"ed25519:{signing_keys['usk'].public_key}": signing_keys["usk"].public_key},
        }, master, master_pub)
        await session.post(
            f"{self._homeserver_url}/_matrix/client/v3/keys/device_signing/upload",
            headers={"Authorization": f"Bearer {self._access_token}"},
            json={"master_key": master_key, "self_signing_key": self_signing_key,
                  "user_signing_key": user_signing_key})
        logger.info("cross-signing keys uploaded for %s", self.mxid)

    def _sign_key_object(self, key_object, signer, signer_pub):
        signature = signer.sign(_canonical_json(
            {k: v for k, v in key_object.items() if k not in ("signatures", "unsigned")}))
        key_object["signatures"] = {self.mxid: {f"ed25519:{signer_pub}": signature}}
        return key_object

    async def _self_sign_device(self, session, ssk):
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with session.post(
            f"{self._homeserver_url}/_matrix/client/v3/keys/query",
            headers=headers, json={"device_keys": {self.mxid: [self.device_id]}},
        ) as response:
            data = await response.json()
        device_object = data.get("device_keys", {}).get(self.mxid, {}).get(self.device_id)
        if not device_object:
            return
        signing_id = f"ed25519:{ssk.public_key}"
        if signing_id in device_object.get("signatures", {}).get(self.mxid, {}):
            return
        signature = ssk.sign(_canonical_json(
            {k: v for k, v in device_object.items() if k not in ("signatures", "unsigned")}))
        device_object.setdefault("signatures", {}).setdefault(self.mxid, {})[signing_id] = signature
        await session.post(
            f"{self._homeserver_url}/_matrix/client/v3/keys/signatures/upload",
            headers=headers, json={self.mxid: {self.device_id: device_object}})
        logger.info("device %s self-signed for %s", self.device_id, self.mxid)
