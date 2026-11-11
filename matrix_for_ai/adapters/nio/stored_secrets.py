import base64
import json
import os
import sqlite3
from pathlib import Path

import vodozemac
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from nio.crypto.sessions import derive_pickle_key

NIO_DEFAULT_PICKLE_KEY = "DEFAULT_KEY"
SECRET_FILES = ("session.json", "cross_signing.json")
_ENVELOPE_ALGORITHM = "aes-256-gcm"
_PICKLED_COLUMNS = (
    ("accounts", "account", vodozemac.Account),
    ("olmsessions", "session", vodozemac.Session),
    ("megolminboundsessions", "session", vodozemac.InboundGroupSession),
)


class StoredSecretsError(Exception):
    pass


class StoredSecrets:
    def __init__(self, encryption_key: str) -> None:
        self._encryption_key = encryption_key

    @property
    def pickle_key(self) -> str:
        return self._encryption_key or NIO_DEFAULT_PICKLE_KEY

    def read_json(self, path: str) -> dict | None:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
        encrypted = stored.get("algorithm") == _ENVELOPE_ALGORITHM
        if encrypted and not self._encryption_key:
            raise StoredSecretsError(
                f"{path} is encrypted, and storage.encryption_key is not set")
        if not encrypted and self._encryption_key:
            raise StoredSecretsError(
                f"{path} is not encrypted with storage.encryption_key; "
                "adopt the key with: python -m matrix_for_ai rotate-encryption-key")
        return _opened(stored, self._encryption_key) if encrypted else stored

    def write_json(self, path: str, data: dict) -> None:
        payload = json.dumps(data)
        if self._encryption_key:
            payload = json.dumps(_sealed(payload.encode("utf-8"), self._encryption_key))
        temporary_path = f"{path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)


def rotate_encryption_key(crypto_dir: str, previous_key: str, new_key: str) -> list[str]:
    previous, new = StoredSecrets(previous_key), StoredSecrets(new_key)
    rotated = []
    for identity_dir in sorted(Path(crypto_dir).iterdir()):
        if not identity_dir.is_dir():
            continue
        for store_file in identity_dir.glob("*.db"):
            _repickle_store(store_file, previous.pickle_key, new.pickle_key)
        for name in SECRET_FILES:
            path = str(identity_dir / name)
            try:
                new.read_json(path)
                continue
            except StoredSecretsError:
                data = previous.read_json(path)
            new.write_json(path, data)
        rotated.append(identity_dir.name)
    return rotated


def _repickle_store(store_file: Path, previous_pickle_key: str, new_pickle_key: str) -> None:
    database = sqlite3.connect(store_file)
    try:
        with database:
            for table, column, kind in _PICKLED_COLUMNS:
                rows = database.execute(f"SELECT rowid, {column} FROM {table}").fetchall()
                for rowid, pickled in rows:
                    database.execute(
                        f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                        (_repickled(kind, pickled, previous_pickle_key, new_pickle_key), rowid))
    finally:
        database.close()


def _repickled(kind, pickled, previous_pickle_key: str, new_pickle_key: str) -> bytes:
    text = pickled.decode() if isinstance(pickled, bytes) else pickled
    new_key = derive_pickle_key(new_pickle_key)
    openers = (
        lambda: kind.from_pickle(text, new_key),
        lambda: kind.from_pickle(text, derive_pickle_key(previous_pickle_key)),
        lambda: kind.from_libolm_pickle(text, previous_pickle_key.encode()),
    )
    for open_pickle in openers:
        try:
            return open_pickle().pickle(new_key).encode()
        except (vodozemac.PickleException, vodozemac.LibolmPickleException):
            continue
    raise StoredSecretsError(
        "a stored encryption key opens with neither the previous nor the new encryption key")


def _file_key(encryption_key: str) -> bytes:
    return HKDF(encryption_key.encode("utf-8"), 32, b"", SHA256,
                context=b"matrix-for-ai stored secrets")


def _sealed(plaintext: bytes, encryption_key: str) -> dict:
    cipher = AES.new(_file_key(encryption_key), AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return {"algorithm": _ENVELOPE_ALGORITHM,
            "nonce": base64.b64encode(cipher.nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext + tag).decode("ascii")}


def _opened(stored: dict, encryption_key: str) -> dict:
    sealed = base64.b64decode(stored["ciphertext"])
    cipher = AES.new(_file_key(encryption_key), AES.MODE_GCM,
                     nonce=base64.b64decode(stored["nonce"]))
    try:
        plaintext = cipher.decrypt_and_verify(sealed[:-16], sealed[-16:])
    except ValueError:
        raise StoredSecretsError("storage.encryption_key does not open the stored secrets")
    return json.loads(plaintext)
