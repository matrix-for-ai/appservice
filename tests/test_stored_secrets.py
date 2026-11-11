import json
import sqlite3

import pytest
import vodozemac
from nio.crypto.sessions import derive_pickle_key

from conftest import base_configuration
from matrix_for_ai.adapters.nio.stored_secrets import (
    NIO_DEFAULT_PICKLE_KEY, StoredSecrets, StoredSecretsError, rotate_encryption_key)
from matrix_for_ai.configuration import ConfigurationError, parse_configuration

KEY = "k" * 40
OTHER_KEY = "o" * 40


def test_with_a_key_the_files_are_sealed_and_open_only_with_it(tmp_path):
    path = str(tmp_path / "session.json")
    StoredSecrets(KEY).write_json(path, {"access_token": "secret-token"})
    assert "secret-token" not in (tmp_path / "session.json").read_text()
    assert StoredSecrets(KEY).read_json(path) == {"access_token": "secret-token"}
    with pytest.raises(StoredSecretsError, match="does not open"):
        StoredSecrets(OTHER_KEY).read_json(path)
    with pytest.raises(StoredSecretsError, match="is encrypted"):
        StoredSecrets("").read_json(path)


def test_without_a_key_the_files_stay_plain_and_a_key_asks_for_the_rotation(tmp_path):
    path = str(tmp_path / "session.json")
    StoredSecrets("").write_json(path, {"device_id": "D"})
    assert json.loads((tmp_path / "session.json").read_text()) == {"device_id": "D"}
    assert StoredSecrets("").pickle_key == NIO_DEFAULT_PICKLE_KEY
    with pytest.raises(StoredSecretsError, match="rotate-encryption-key"):
        StoredSecrets(KEY).read_json(path)
    assert StoredSecrets(KEY).read_json(str(tmp_path / "missing.json")) is None


def _store_with_pickles(path, pickle_key):
    alice, bob = vodozemac.Account(), vodozemac.Account()
    bob.generate_one_time_keys(1)
    session = alice.create_outbound_session(
        list(bob.one_time_keys.values())[0], bob.curve25519_key)
    inbound = vodozemac.InboundGroupSession(vodozemac.GroupSession().session_key)
    key = derive_pickle_key(pickle_key)
    database = sqlite3.connect(path)
    with database:
        database.execute("CREATE TABLE accounts (account BLOB)")
        database.execute("CREATE TABLE olmsessions (session BLOB)")
        database.execute("CREATE TABLE megolminboundsessions (session BLOB)")
        database.execute("INSERT INTO accounts VALUES (?)", (alice.pickle(key).encode(),))
        database.execute("INSERT INTO olmsessions VALUES (?)", (session.pickle(key).encode(),))
        database.execute(
            "INSERT INTO megolminboundsessions VALUES (?)", (inbound.pickle(key).encode(),))
    database.close()
    return alice.ed25519_key.to_base64()


def test_rotation_repickles_the_store_and_reseals_the_files(tmp_path):
    identity = tmp_path / "ai_nova"
    identity.mkdir()
    signing_key = _store_with_pickles(identity / "store.db", NIO_DEFAULT_PICKLE_KEY)
    StoredSecrets("").write_json(str(identity / "session.json"), {"access_token": "t"})
    StoredSecrets("").write_json(str(identity / "cross_signing.json"), {"master": "m"})

    assert rotate_encryption_key(str(tmp_path), "", KEY) == ["ai_nova"]

    database = sqlite3.connect(identity / "store.db")
    account, = database.execute("SELECT account FROM accounts").fetchone()
    session, = database.execute("SELECT session FROM olmsessions").fetchone()
    inbound, = database.execute("SELECT session FROM megolminboundsessions").fetchone()
    database.close()
    new_key = derive_pickle_key(KEY)
    assert vodozemac.Account.from_pickle(account.decode(), new_key).ed25519_key.to_base64() \
        == signing_key
    vodozemac.Session.from_pickle(session.decode(), new_key)
    vodozemac.InboundGroupSession.from_pickle(inbound.decode(), new_key)
    with pytest.raises(vodozemac.PickleException):
        vodozemac.Account.from_pickle(account.decode(), derive_pickle_key(NIO_DEFAULT_PICKLE_KEY))
    assert StoredSecrets(KEY).read_json(str(identity / "session.json")) == {"access_token": "t"}

    rotate_encryption_key(str(tmp_path), KEY, OTHER_KEY)
    assert StoredSecrets(OTHER_KEY).read_json(str(identity / "cross_signing.json")) == {
        "master": "m"}


def test_a_rotation_cut_halfway_finishes_when_run_again(tmp_path):
    crypto_dir = tmp_path / "crypto"
    for name in ("ai_ana", "ai_bea"):
        identity = crypto_dir / name
        identity.mkdir(parents=True)
        _store_with_pickles(identity / "store.db", NIO_DEFAULT_PICKLE_KEY)
        StoredSecrets("").write_json(str(identity / "session.json"), {"access_token": name})
        StoredSecrets("").write_json(str(identity / "cross_signing.json"), {"master": name})
    halfway = tmp_path / "halfway"
    halfway.mkdir()
    (crypto_dir / "ai_bea").rename(halfway / "ai_bea")
    rotate_encryption_key(str(crypto_dir), "", KEY)
    (halfway / "ai_bea").rename(crypto_dir / "ai_bea")

    assert rotate_encryption_key(str(crypto_dir), "", KEY) == ["ai_ana", "ai_bea"]
    for name in ("ai_ana", "ai_bea"):
        assert StoredSecrets(KEY).read_json(str(crypto_dir / name / "session.json")) == {
            "access_token": name}
        database = sqlite3.connect(crypto_dir / name / "store.db")
        account, = database.execute("SELECT account FROM accounts").fetchone()
        database.close()
        vodozemac.Account.from_pickle(account.decode(), derive_pickle_key(KEY))


def test_a_wrong_previous_key_stops_the_rotation(tmp_path):
    identity = tmp_path / "ai_nova"
    identity.mkdir()
    _store_with_pickles(identity / "store.db", KEY)
    with pytest.raises(StoredSecretsError, match="neither"):
        rotate_encryption_key(str(tmp_path), "w" * 40, OTHER_KEY)


def test_the_encryption_key_is_optional_but_validated_like_the_tokens(tmp_path):
    assert parse_configuration(base_configuration(tmp_path)).encryption_key == ""
    raw = base_configuration(tmp_path)
    raw["storage"]["encryption_key"] = KEY
    assert parse_configuration(raw).encryption_key == KEY
    raw["storage"]["encryption_key"] = "short"
    with pytest.raises(ConfigurationError, match="storage.encryption_key is shorter"):
        parse_configuration(raw)
    raw["storage"]["encryption_key"] = "CHANGE_ME" + "x" * 30
    with pytest.raises(ConfigurationError, match="placeholder"):
        parse_configuration(raw)
