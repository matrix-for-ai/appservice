import stat

from matrix_for_ai.services import identity_store
from matrix_for_ai.services.identity_store import (
    DATABASE_FILENAME, IdentityStore, Profile, UnreadEvent, Webhook)

ROOM = "!room:example.org"


def event(number: int) -> dict:
    return {"event_id": f"${number}", "sender": "@human:example.org", "origin_server_ts": number}


def test_unread_events_are_kept_in_order_and_survive_a_restart(tmp_path):
    mailbox = IdentityStore(tmp_path).mailbox
    for number in (1, 2, 2, 3):
        mailbox.note_unread(ROOM, event(number))
    reopened = IdentityStore(tmp_path).mailbox
    assert reopened.unread_event_ids(ROOM) == ["$1", "$2", "$3"]
    state = reopened.channel(ROOM)
    assert state.unread == 3 and state.last_ts == 3
    assert state.last_unread == UnreadEvent("$3", "@human:example.org", 3)


def test_a_flood_keeps_the_newest_events_and_the_exact_count(tmp_path, monkeypatch):
    monkeypatch.setattr(identity_store, "UNREAD_EVENTS_KEPT_PER_CHANNEL", 3)
    mailbox = IdentityStore(tmp_path).mailbox
    for number in range(1, 8):
        mailbox.note_unread(ROOM, event(number))
    assert mailbox.unread_event_ids(ROOM) == ["$5", "$6", "$7"]
    assert mailbox.unread_count(ROOM) == 7
    mailbox.ack_through(ROOM, "$6")
    assert mailbox.unread_event_ids(ROOM) == ["$7"]
    assert mailbox.unread_count(ROOM) == 1


def test_undecryptable_events_stay_unread_after_a_receipt(tmp_path):
    mailbox = IdentityStore(tmp_path).mailbox
    for number in (1, 2, 3):
        mailbox.note_unread(ROOM, event(number))
    mailbox.mark_undecryptable(ROOM, ["$1"])
    mailbox.ack_through(ROOM, "$2")
    assert mailbox.unread_event_ids(ROOM) == ["$1", "$3"]
    assert mailbox.undecryptable_event_ids(ROOM) == {"$1"}


def test_touch_only_moves_last_ts_forward_and_remove_forgets_the_channel(tmp_path):
    mailbox = IdentityStore(tmp_path).mailbox
    mailbox.touch(ROOM, 10)
    mailbox.touch(ROOM, 5)
    assert mailbox.channel(ROOM).last_ts == 10 and mailbox.channel(ROOM).unread == 0
    mailbox.note_unread(ROOM, event(4))
    mailbox.remove_channel(ROOM)
    assert mailbox.channel(ROOM) is None and mailbox.unread_event_ids(ROOM) == []
    assert mailbox.channels() == []


def test_token_webhook_and_profile_survive_a_restart(tmp_path):
    store = IdentityStore(tmp_path)
    store.set_token_sha256("a" * 64)
    store.set_webhook(Webhook("https://hooks.example.org/x", "secret"))
    store.set_profile(Profile(tts={"voice": "alloy"}, stt={}, transcribe_audio=False))
    reopened = IdentityStore(tmp_path)
    assert reopened.token_sha256() == "a" * 64
    assert reopened.webhook() == Webhook("https://hooks.example.org/x", "secret")
    assert reopened.profile() == Profile(tts={"voice": "alloy"}, transcribe_audio=False)


def test_deleting_reports_whether_there_was_something_to_delete(tmp_path):
    store = IdentityStore(tmp_path)
    assert store.profile() == Profile()
    assert not store.delete_token() and not store.delete_webhook()
    store.set_token_sha256("b" * 64)
    store.set_webhook(Webhook("https://hooks.example.org/x", "secret"))
    assert store.delete_token() and store.token_sha256() is None
    assert store.delete_webhook() and store.webhook() is None


def test_the_database_is_readable_only_by_its_owner(tmp_path):
    IdentityStore(tmp_path / "ai_someone").set_webhook(Webhook("https://h.example.org", "s"))
    assert stat.S_IMODE((tmp_path / "ai_someone").stat().st_mode) == 0o700
    for path in (tmp_path / "ai_someone").glob(f"{DATABASE_FILENAME}*"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
