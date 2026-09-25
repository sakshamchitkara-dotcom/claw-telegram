import time

from harness import OWNER, harness


async def say(h, text, uid=OWNER):
    h.fake.push_message(uid, text)
    await h.pump()
    return h.fake.texts(uid)[-1]


def make_due(h, sid, ago=0.0):
    h.bot.store.db.execute("UPDATE schedules SET next_run = ? WHERE id = ?", (time.time() - ago, sid))


async def test_remind_fires_once():
    async with harness(timezone="UTC") as h:
        assert (await say(h, "/remind 10m stretch your legs")).startswith("⏰ Reminder #1 set for ")
        await h.bot.run_due()
        assert h.fake.texts(OWNER)[-1].startswith("⏰ Reminder #1 set")  # not due yet
        make_due(h, 1)
        await h.bot.run_due()
        await h.bot.run_due()
        assert h.fake.texts(OWNER)[-1] == "⏰ Reminder: stretch your legs"
        assert h.fake.texts(OWNER).count("⏰ Reminder: stretch your legs") == 1
        assert h.bot.store.list_schedules(OWNER) == []


async def test_late_reminder_says_so():
    async with harness(timezone="UTC") as h:
        await say(h, "/remind 1h water")
        make_due(h, 1, ago=3600)
        await h.bot.run_due()
        assert h.fake.texts(OWNER)[-1] == "⏰ Reminder (late: the bot was offline): water"


async def test_every_runs_backend_prompt_and_reschedules():
    async with harness(timezone="UTC") as h:
        assert "Next:" in await say(h, "/every 0 9 * * * morning brief")
        make_due(h, 1)
        await h.bot.run_due()
        await h.bot.drain()
        header, reply = h.fake.texts(OWNER)[-2:]
        assert header == "🔁 #1: morning brief" and reply.startswith("echo: morning brief")
        sc = h.bot.store.get_schedule(1)
        assert sc.runs == 1 and sc.next_run > time.time()
        assert h.bot.store.count_messages(OWNER) == 2  # follow-ups can refer to it


async def test_schedule_management_and_validation():
    async with harness(timezone="UTC", allowed_user_ids=frozenset({OWNER, 7}), owner_ids=frozenset({OWNER}),
                       max_schedules_per_user=1) as h:
        assert (await say(h, "/every 61 * * * * x")).startswith("Bad schedule: '61' is outside 0-59")
        assert (await say(h, "/every 10s x")).startswith("Bad schedule: interval must be at least 60s")
        assert (await say(h, "/remind later x")).startswith("can't parse time 'later'")
        assert (await say(h, "/every @hourly")).startswith("Usage: /every")
        await say(h, "/every 2h drink water")
        await say(h, "/remind 5m tea")
        listing = await say(h, "/schedules")
        assert "🔁 #1 2h, next " in listing and "⏰ #2 at " in listing and ": tea" in listing
        assert await say(h, "/unschedule 99") == "No such schedule in this chat. See /schedules."
        assert await say(h, "/unschedule #1") == "Cancelled #1."
        # plain users have a quota
        await say(h, "/remind 5m a", uid=7)
        assert (await say(h, "/remind 5m b", uid=7)).startswith("You already have 1 schedules.")


async def test_schedule_of_removed_user_is_dropped():
    async with harness(timezone="UTC", owner_ids=frozenset({OWNER})) as h:
        h.bot.store.set_user(8, "user", OWNER)
        await say(h, "/remind 5m ping", uid=8)
        h.bot.store.remove_user(8)
        make_due(h, 1)
        await h.bot.run_due()
        assert h.bot.store.get_schedule(1) is None
        assert h.fake.texts(8)[-1].startswith("⏰ Reminder #1 set")
