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


def test_users_upsert_and_remove():
    s = Store(":memory:")
    assert s.user_role(5) is None
    s.set_user(5, "user", added_by=1)
    s.set_user(5, "admin", added_by=1)
    assert s.user_role(5) == "admin" and s.list_users() == [(5, "admin", 1)]
    assert s.remove_user(5) and not s.remove_user(5)


def test_audit_log_newest_first_and_per_chat():
    s = Store(":memory:")
    s.audit("approve", user_id=1, chat_id=10, task_id=3, detail="rm -rf x")
    s.audit("user.add", user_id=1, detail="5 as user")
    s.audit("deny", user_id=2, chat_id=11, task_id=4)
    assert [e.action for e in s.audit_log()] == ["deny", "user.add", "approve"]
    only = s.audit_log(chat_id=10)
    assert len(only) == 1 and only[0].task_id == 3 and only[0].detail == "rm -rf x"
