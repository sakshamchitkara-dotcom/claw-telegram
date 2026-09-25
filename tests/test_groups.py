from harness import OWNER, harness

GROUP = -1001234


async def test_forum_topics_are_separate_conversations():
    async with harness() as h:
        for thread, text in [(7, "@claw_test_bot alpha"), (9, "@claw_test_bot beta"), (7, "@claw_test_bot again")]:
            h.fake.push_message(OWNER, text, chat_id=GROUP, thread=thread)
            await h.pump()
        h.fake.push_message(OWNER, "/status", chat_id=GROUP, thread=9)
        await h.pump()
        sent = h.fake.sent(GROUP)
        assert [m["message_thread_id"] for m in sent] == [7, 9, 7, 9]
        assert "(history: 0 msgs)" in sent[1]["text"]  # topic 9 did not see topic 7
        assert "(history: 2 msgs)" in sent[2]["text"]
        assert f"session: telegram-{GROUP}-t9-0" in sent[3]["text"] and "messages stored: 2" in sent[3]["text"]
