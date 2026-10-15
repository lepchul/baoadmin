import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import BaoAdmin
from api import TelegramError
from storage import Store
from formatting import capture, emoji_entities, render, validate, seconds, esc

CHAT = -1001234567890
ADMIN = 10
USER = 20
BOT = 99


def msg(text="", uid=ADMIN, reply=True, private=False):
    result = {
        "message_id": 100,
        "chat": {
            "id": uid if private else CHAT,
            "type": "private" if private else "supergroup",
            "title": "Test",
        },
        "from": {"id": uid, "first_name": "Tester"},
        "text": text,
    }
    if reply:
        result["reply_to_message"] = {
            "message_id": 9,
            "from": {"id": USER, "first_name": "Target"},
        }
    return result


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.roles = {ADMIN: "administrator", USER: "member", BOT: "administrator"}
        self.restrict = True
        self.fail_emoji = False
        self.fail_mute = False

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getChatMember":
            uid = params["user_id"]
            return {
                "status": self.roles.get(uid, "member"),
                "user": {"id": uid, "first_name": "Target"},
                "can_restrict_members": self.restrict,
                "can_delete_messages": True,
                "can_pin_messages": True,
            }
        if method == "getChat":
            return {
                "id": CHAT,
                "type": "supergroup",
                "title": "Test <group>",
                "permissions": {"can_send_messages": True, "can_send_photos": False},
            }
        if method == "getCustomEmojiStickers":
            return [{"custom_emoji_id": params["custom_emoji_ids"][0], "emoji": "🐾"}]
        if method == "getChatAdministrators":
            return [{"user": {"id": ADMIN, "first_name": "Admin"}}]
        if (
            method == "sendMessage"
            and self.fail_emoji
            and "<tg-emoji" in params["text"]
        ):
            raise TelegramError(
                {"error_code": 400, "description": "CUSTOM_EMOJI_INVALID"}
            )
        if method == "restrictChatMember" and self.fail_mute:
            raise TelegramError({"error_code": 400, "description": "RIGHT_FORBIDDEN"})
        return True

    def actions(self, name):
        return [p for n, p in self.calls if n == name]


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.db = Store(":memory:")
        self.app = BaoAdmin(self.api, self.db, {777})
        self.app.me = {"id": BOT, "username": "BaoAdminBot"}

    def tearDown(self):
        self.db.db.close()

    async def test_member_cannot_ban(self):
        await self.app.handle({"message": msg("/ban", uid=30)})
        self.assertFalse(self.api.actions("banChatMember"))
        self.assertIn("Нужны права", self.api.actions("sendMessage")[-1]["text"])

    async def test_admin_without_right_cannot_ban(self):
        self.api.restrict = False
        await self.app.handle({"message": msg("/ban")})
        self.assertFalse(self.api.actions("banChatMember"))

    async def test_admin_target_protected(self):
        self.api.roles[USER] = "administrator"
        await self.app.handle({"message": msg("/ban")})
        self.assertFalse(self.api.actions("banChatMember"))

    async def test_owner_no_group_override(self):
        await self.app.handle({"message": msg("/ban", uid=777)})
        self.assertFalse(self.api.actions("banChatMember"))

    async def test_mute_duration_and_reason(self):
        before = int(time.time())
        await self.app.message(msg("/mute 30m флуд"))
        p = self.api.actions("restrictChatMember")[0]
        self.assertEqual(p["user_id"], USER)
        self.assertTrue(before + 1800 <= p["until_date"] <= int(time.time()) + 1800)
        self.assertFalse(p["permissions"]["can_send_messages"])

    async def test_numeric_target(self):
        await self.app.message(msg("/mute 20 2h причина", reply=False))
        self.assertEqual(self.api.actions("restrictChatMember")[0]["user_id"], USER)

    async def test_invalid_duration_no_mutation(self):
        await self.app.handle({"message": msg("/mute 0m")})
        self.assertFalse(self.api.actions("restrictChatMember"))

    async def test_warn_limit_resets_after_mute(self):
        for _ in range(3):
            await self.app.message(msg("/warn флуд"))
        self.assertEqual(len(self.api.actions("restrictChatMember")), 1)
        self.assertEqual(self.db.warns(CHAT, USER), 0)
        self.assertEqual(self.db.warns(CHAT - 1, USER), 0)

    async def test_warn_retained_if_mute_fails(self):
        self.api.fail_mute = True
        for _ in range(3):
            await self.app.handle({"message": msg("/warn")})
        self.assertEqual(self.db.warns(CHAT, USER), 3)

    async def test_unban_does_not_kick_current_member(self):
        await self.app.message(msg("/unban"))
        self.assertTrue(self.api.actions("unbanChatMember")[0]["only_if_banned"])

    async def test_unmute_respects_default_permissions(self):
        await self.app.message(msg("/unmute"))
        self.assertFalse(
            self.api.actions("restrictChatMember")[0]["permissions"]["can_send_photos"]
        )

    async def test_stale_callback_denied(self):
        self.api.roles[ADMIN] = "member"
        await self.app.handle(
            {
                "callback_query": {
                    "id": "q",
                    "from": {"id": ADMIN},
                    "message": {"chat": {"id": ADMIN, "type": "private"}},
                    "data": f"toggle:{CHAT}:antilink",
                }
            }
        )
        self.assertFalse(self.db.settings(CHAT)["antilink"])

    async def test_pending_edit_rechecks_admin(self):
        self.app.pending[ADMIN] = (CHAT, "text", "rules", time.monotonic() + 900)
        self.api.roles[ADMIN] = "member"
        await self.app.handle({"message": msg("changed", private=True, reply=False)})
        self.assertNotEqual(self.db.template(CHAT, "rules"), "changed")

    async def test_flood_including_public_commands(self):
        self.db.set(CHAT, "antiflood", True)
        for _ in range(6):
            await self.app.message(msg("/id", uid=USER, reply=False))
        self.assertEqual(len(self.api.actions("restrictChatMember")), 1)
        self.assertEqual(len(self.api.actions("deleteMessage")), 1)

    async def test_admin_exempt_from_flood(self):
        self.db.set(CHAT, "antiflood", True)
        for _ in range(10):
            await self.app.message(msg("hello", reply=False))
        self.assertFalse(self.api.actions("restrictChatMember"))

    async def test_hidden_link_deleted(self):
        self.db.set(CHAT, "antilink", True)
        m = msg("click", uid=USER, reply=False)
        m["entities"] = [
            {
                "type": "text_link",
                "offset": 0,
                "length": 5,
                "url": "https://example.org",
            }
        ]
        await self.app.message(m)
        self.assertEqual(len(self.api.actions("deleteMessage")), 1)

    async def test_edited_link_deleted(self):
        self.db.set(CHAT, "antilink", True)
        await self.app.message(
            msg("https://example.org", uid=USER, reply=False), edited=True
        )
        self.assertEqual(len(self.api.actions("deleteMessage")), 1)

    async def test_disabled_and_maintenance_skip_automation(self):
        self.db.set(CHAT, "antilink", True)
        self.db.set(CHAT, "enabled", False)
        await self.app.message(msg("https://example.org", uid=USER, reply=False))
        self.db.set(CHAT, "enabled", True)
        self.db.meta("maintenance", "on")
        await self.app.message(msg("https://example.org", uid=USER, reply=False))
        self.assertFalse(self.api.actions("deleteMessage"))

    async def test_emoji_saved_and_fallback(self):
        await self.app.add_emoji(CHAT, "bao 1234", msg())
        self.db.text(CHAT, "warn", "{emoji:bao} {user} {reason}")
        self.api.fail_emoji = True
        await self.app.emit(
            CHAT, "warn", {"id": USER, "first_name": "<unsafe>"}, reason="<b>"
        )
        sent = self.api.actions("sendMessage")
        self.assertEqual(len(sent), 2)
        self.assertNotIn("<tg-emoji", sent[-1]["text"])
        self.assertIn("&lt;unsafe&gt;", sent[-1]["text"])

    async def test_bot_addressed_elsewhere_ignored(self):
        await self.app.message(msg("/ban@OtherBot"))
        self.assertFalse(self.api.actions("banChatMember"))

    async def test_anonymous_admin_ignored(self):
        m = msg("/ban")
        m["sender_chat"] = {"id": CHAT}
        await self.app.message(m)
        self.assertFalse(self.api.actions("banChatMember"))

    async def test_emoji_delete_used_prevented(self):
        self.db.emoji(CHAT, "bao", "123", "🐾")
        self.db.text(CHAT, "welcome", "{emoji:bao}")
        self.app.selected[ADMIN] = CHAT
        await self.app.handle(
            {"message": msg("/emoji del bao", private=True, reply=False)}
        )
        self.assertIn("bao", self.db.emojis(CHAT))

    async def test_report_cooldown(self):
        await self.app.message(msg("/report", uid=USER))
        await self.app.handle({"message": msg("/report", uid=USER)})
        self.assertEqual(len(self.api.actions("getChatAdministrators")), 1)

    async def test_text_command_preserves_custom_emoji(self):
        self.app.selected[ADMIN] = CHAT
        m = msg("/text welcome 🐾 {user}", private=True, reply=False)
        m["entities"] = [
            {"type": "custom_emoji", "offset": 14, "length": 2, "custom_emoji_id": "42"}
        ]
        await self.app.message(m)
        self.assertEqual(self.db.template(CHAT, "welcome"), "{ce:42:🐾} {user}")

    async def test_toggle_requires_moderation_right(self):
        self.api.restrict = False
        await self.app.handle(
            {
                "callback_query": {
                    "id": "q",
                    "from": {"id": ADMIN},
                    "message": {"chat": {"id": ADMIN, "type": "private"}},
                    "data": f"toggle:{CHAT}:antiflood",
                }
            }
        )
        self.assertFalse(self.db.settings(CHAT)["antiflood"])

    async def test_admin_menu_callback_sizes(self):
        await self.app.panel(ADMIN, CHAT)
        await self.app.texts(ADMIN, CHAT)
        for message in self.api.actions("sendMessage"):
            for row in message.get("reply_markup", {}).get("inline_keyboard", []):
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)


class FormatTests(unittest.TestCase):
    def test_utf16_custom_emojis(self):
        m = {
            "text": "😀 x 🐾 end",
            "entities": [
                {
                    "type": "custom_emoji",
                    "offset": 5,
                    "length": 2,
                    "custom_emoji_id": "42",
                }
            ],
        }
        self.assertEqual(emoji_entities(m)[0][3], "🐾")
        self.assertEqual(capture(m), "😀 x {ce:42:🐾} end")

    def test_caption(self):
        m = {
            "caption": "🐾",
            "caption_entities": [
                {
                    "type": "custom_emoji",
                    "offset": 0,
                    "length": 2,
                    "custom_emoji_id": "42",
                }
            ],
        }
        self.assertEqual(capture(m), "{ce:42:🐾}")

    def test_expanded_template_limit(self):
        with self.assertRaises(ValueError):
            validate("{reason}" * 10, {})

    def test_unknown_template(self):
        with self.assertRaises(ValueError):
            validate("{emoji:missing}", {})
        with self.assertRaises(ValueError):
            validate("{token}", {})
        with self.assertRaises(ValueError):
            validate("x {", {})

    def test_escape_and_dash(self):
        self.assertEqual(
            render("<b>\u2014{reason}", {}, {"reason": esc("<x>")}),
            "&lt;b&gt;-&lt;x&gt;",
        )

    def test_duration_limits(self):
        self.assertEqual(seconds("1д"), 86400)
        for value in ("0m", "366d", "1s", "forever", "-1h"):
            with self.assertRaises(ValueError):
                seconds(value)

    def test_store_persistence_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "state.sqlite3")
            s = Store(path)
            s.set(1, "antiflood", True)
            s.warns(1, 5, 1)
            s.emoji(1, "bao", "123", "🐾")
            s.text(1, "rules", "test")
            s.db.close()
            s = Store(path)
            self.assertTrue(s.settings(1)["antiflood"])
            self.assertFalse(s.settings(2)["antiflood"])
            self.assertEqual(s.warns(1, 5), 1)
            self.assertEqual(s.emojis(2), {})
            self.assertEqual(s.template(1, "rules"), "test")
            s.db.close()


if __name__ == "__main__":
    unittest.main()
