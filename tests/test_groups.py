from harness import OWNER, STRANGER, harness

GROUP = -1001234


def group(**kw):
    return dict(allowed_group_ids=frozenset({GROUP})) | kw


async def test_group_answers_only_mentions_replies_and_commands():
    async with harness(**group()) as h:
        h.fake.push_message(OWNER, "just chatting", chat_id=GROUP)
        h.fake.push_message(OWNER, "hey @claw_test_bot2 not us", chat_id=GROUP)
        h.fake.push_message(OWNER, "/status@some_other_bot", chat_id=GROUP)
        await h.pump()
        assert h.fake.texts(GROUP) == []
        trigger = h.fake.push_message(OWNER, "@Claw_Test_Bot what's up?", chat_id=GROUP)["message"]
        await h.pump()
        (answer,) = h.fake.sent(GROUP)
        assert answer["text"].startswith("echo: what's up?")
        bot_msg = {"message_id": answer["message_id"], "from": {"id": 1, "is_bot": True}, "text": answer["text"]}
        h.fake.push_message(OWNER, "and more", chat_id=GROUP, reply_to_message=bot_msg)
        h.fake.push_message(OWNER, "/help@claw_test_bot", chat_id=GROUP)
        await h.pump()
        (followup,) = [m for m in h.fake.sent(GROUP) if m["text"].startswith("echo: and more")]
        (help_,) = [m for m in h.fake.sent(GROUP) if m["text"].startswith("/start")]
        assert "(history: 2 msgs)" in followup["text"]
        assert help_["reply_to"] > trigger["message_id"]  # command answers reply to the asker


async def test_group_not_in_allowlist_is_ignored_but_tells_members_its_id():
    async with harness() as h:
        h.fake.push_message(OWNER, "@claw_test_bot hi", chat_id=GROUP)
        h.fake.push_message(STRANGER, "/start", chat_id=GROUP)
        await h.pump()
        assert h.fake.texts(GROUP) == []
        h.fake.push_message(OWNER, "/start", chat_id=GROUP)
        await h.pump()
        assert h.fake.texts(GROUP) == [f"This group ({GROUP}) is not in ALLOWED_GROUP_IDS."]


async def test_strangers_in_allowed_group_are_ignored():
    async with harness(**group()) as h:
        h.fake.push_message(STRANGER, "@claw_test_bot hi", chat_id=GROUP)
        await h.pump()
        assert h.fake.texts(GROUP) == []


async def test_topic_creation_reply_is_not_a_reply_to_us():
    async with harness(**group()) as h:
        created = {"message_id": 5, "from": {"id": 1, "is_bot": True}, "forum_topic_created": {"name": "t"}}
        h.fake.push_message(OWNER, "chatter in topic", chat_id=GROUP, thread=5, reply_to_message=created)
        await h.pump()
        assert h.fake.texts(GROUP) == []


async def test_forum_topics_are_separate_conversations():
    async with harness(**group()) as h:
        for thread, text in [(7, "@claw_test_bot alpha"), (9, "@claw_test_bot beta"), (7, "@claw_test_bot again")]:
            h.fake.push_message(OWNER, text, chat_id=GROUP, thread=thread)
            await h.pump()
        h.fake.push_message(OWNER, "/status", chat_id=GROUP, thread=9)
        await h.pump()
        sent = h.fake.sent(GROUP)
        assert [m["message_thread_id"] for m in sent] == [7, 9, 7, 9]
        assert sent[0]["text"].startswith("echo: alpha")
        assert "(history: 0 msgs)" in sent[1]["text"]  # topic 9 did not see topic 7
        assert "(history: 2 msgs)" in sent[2]["text"]
        assert f"session: telegram-{GROUP}-t9-0" in sent[3]["text"] and "messages stored: 2" in sent[3]["text"]


async def test_approval_prompt_lands_in_the_topic():
    async with harness(**group()) as h:
        h.fake.push_message(OWNER, "@claw_test_bot !task ls", chat_id=GROUP, thread=3)
        await h.pump(drain=False)
        import asyncio
        for _ in range(100):
            if h.fake.with_keyboard(GROUP):
                break
            await asyncio.sleep(0.01)
        (prompt,) = h.fake.with_keyboard(GROUP)
        assert prompt["message_thread_id"] == 3
        h.fake.push_callback(OWNER, prompt, "ap:1:1")
        await h.pump()
