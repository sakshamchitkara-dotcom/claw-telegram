from claw_telegram.store import Store


def test_history_is_per_chat_limited_and_starts_with_user():
    s = Store(":memory:")
    for i in range(5):
        s.add_message(1, "user", f"q{i}")
        s.add_message(1, "assistant", f"a{i}")
    s.add_message(2, "user", "other chat")
    h = s.history(1, 3)  # last 3 = a3, q4, a4 -> leading assistant dropped
    assert h == [{"role": "user", "content": "q4"}, {"role": "assistant", "content": "a4"}]
    assert s.count_messages(2) == 1


def test_reset_clears_history_and_rotates_session():
    s = Store(":memory:")
    s.add_message(1, "user", "hi")
    before = s.session_id(1)
    s.reset(1)
    assert s.history(1, 10) == []
    assert s.session_id(1) != before
    s.reset(1)
    assert s.session_id(1) == "telegram-1-2"


def test_backend_choice_survives_reset():
    s = Store(":memory:")
    s.set_backend(1, "hermes")
    s.reset(1)
    assert s.backend_for(1) == "hermes"


def test_tasks_resolve_once():
    s = Store(":memory:")
    tid = s.create_task(1, "echo", "ref", "rm -rf /tmp/x")
    assert s.resolve_task(tid, "approved")
    assert not s.resolve_task(tid, "denied")
    assert s.get_task(tid).status == "approved"
    assert [t.id for t in s.list_tasks(1)] == [tid]
