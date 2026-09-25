from harness import OWNER, harness

from claw_telegram.config import Settings

PLAIN = 2002


async def say(h, text, uid=OWNER):
    h.fake.push_message(uid, text)
    await h.pump()
    return h.fake.texts(uid)[-1]


def test_daily_turns_from_env():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "DAILY_TURNS_USER": "50", "DAILY_TURNS_ADMIN": ""})
    assert s.role_daily_turns == {"owner": 0, "admin": 0, "user": 50}


async def test_quota_blocks_plain_users_after_their_daily_replies():
    async with harness(timezone="UTC", owner_ids=frozenset({OWNER}), allowed_user_ids=frozenset({PLAIN}),
                       role_daily_turns={"user": 2}) as h:
        assert (await say(h, "one", PLAIN)).startswith("echo: one")
        await say(h, "!fail", PLAIN)  # failed turns don't count
        assert (await say(h, "two", PLAIN)).startswith("echo: two")
        assert await say(h, "three", PLAIN) == ("You've used your 2 replies for today. "
                                                "The count resets at midnight (UTC).")
        assert (await say(h, "/help", PLAIN)).startswith("/start")  # commands still work
        for _ in range(3):
            await say(h, "owners are unlimited")
        assert (await say(h, "/usage", PLAIN)).splitlines() == [
            "Today: 2 of 2 replies (6 chars in, 54 out)",
            "Last 7 days: 2 replies (6 chars in, 54 out)",
            "Daily limit for your role (user): 2",
        ]
        assert await say(h, "/usage all", PLAIN) == "Only admins can see everyone's usage."
        everyone = (await say(h, "/usage all")).splitlines()
        assert everyone[1].startswith(f"{OWNER}: 3 replies") and everyone[2].startswith(f"{PLAIN}: 2 replies")
