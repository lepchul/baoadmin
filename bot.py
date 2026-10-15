"""Bao Admin. Telegram moderation with per-chat settings and text templates."""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
import logging
import os
import re
import time

import httpx
from dotenv import load_dotenv
from api import API, TelegramError
from formatting import (
    NAME,
    capture,
    clean,
    duration,
    emoji_entities,
    esc,
    mention,
    render,
    seconds,
    validate,
)
from storage import Store, TEMPLATES

log = logging.getLogger("bao_admin")
ADMIN = {"creator", "administrator"}
ALIASES = {
    "бан": "ban",
    "разбан": "unban",
    "мут": "mute",
    "размут": "unmute",
    "варн": "warn",
    "пред": "warn",
    "снятьпреды": "unwarn",
    "преды": "warns",
    "правила": "rules",
    "репорт": "report",
    "админ": "admin",
}
MOD = {
    "ban",
    "unban",
    "mute",
    "unmute",
    "warn",
    "unwarn",
    "warns",
    "del",
    "pin",
    "unpin",
}
LABELS = {
    "enabled": "Бот в группе",
    "welcome": "Приветствия",
    "antiflood": "Антифлуд",
    "antilink": "Запрет ссылок",
}
TEXT_LABELS = {
    "welcome": "Приветствие",
    "rules": "Правила",
    "warn": "Предупреждение",
    "mute": "Мут",
    "unmute": "Снятие мута",
    "ban": "Бан",
    "unban": "Разбан",
    "unwarn": "Сброс предупреждений",
}
HELP = """🐾 Bao Admin

Модерация ответом на сообщение или по числовому ID:
/warn причина
/mute 30m причина
/ban причина
/unmute, /unban, /unwarn, /warns
/del, /pin, /unpin ответом на сообщение

Поддерживаются: мут, размут, бан, разбан, варн, преды
Пример по ID: /mute 123456789 2h флуд

/rules - правила
/report причина - позвать админов ответом на сообщение
/id - ID пользователя и чата
/emojiid - ID премиум-эмодзи в сообщении или ответе
/admin - админ-меню в личке
/cancel - отменить ввод

В личке после выбора группы:
/text ключ текст - изменить шаблон
/emoji add имя ID - добавить эмодзи
/emoji del имя - удалить эмодзи
/emoji list - каталог
/limits 3 60 6 10 10 - лимит варнов, мут за варны (мин), сообщения, окно (сек), мут за флуд (мин)

Владелец бота: /maintenance on или off
"""


def keyboard(rows):
    return {
        "inline_keyboard": [
            [{"text": title, "callback_data": data} for title, data in row]
            for row in rows
        ]
    }


class BaoAdmin:
    def __init__(self, api, store, owners=()):
        self.api, self.db, self.owners = api, store, set(owners)
        self.me = {}
        self.selected = {}
        self.pending = {}
        self.flood = defaultdict(deque)
        self.report_times = {}
        self.last_prune = time.monotonic()

    async def say(self, chat, text, **kwargs):
        return await self.api.call(
            "sendMessage",
            chat_id=chat,
            text=clean(text),
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            **kwargs,
        )

    async def member(self, chat, uid):
        return await self.api.call("getChatMember", chat_id=chat, user_id=uid)

    async def require(self, chat, uid, right=None):
        member = await self.member(chat, uid)
        if member["status"] not in ADMIN:
            raise ValueError("Нужны права администратора этой группы")
        if right and member["status"] != "creator" and not member.get(right):
            raise ValueError("У тебя нет права " + right)
        return member

    async def bot_right(self, chat, right):
        member = await self.member(chat, self.me["id"])
        if member["status"] not in ADMIN or not member.get(right):
            raise ValueError("Выдай Bao Admin право " + right + " в настройках группы")

    async def emit(self, chat, key, user, actor=None, reason="", **extra):
        fields = {
            "user": mention(user),
            "chat": esc(
                (await self.api.call("getChat", chat_id=chat)).get("title", "чат")
            ),
            "admin": mention(actor) if actor else "Bao Admin",
            "reason": esc(reason[:300]),
            "duration": "",
            "count": "",
            "limit": "",
        }
        fields.update({k: esc(v) for k, v in extra.items()})
        body = self.db.template(chat, key)
        emojis = self.db.emojis(chat)
        rich = render(body, emojis, fields)
        try:
            return await self.say(chat, rich)
        except TelegramError as e:
            if e.code == 400 and "<tg-emoji " in rich:
                log.info("Custom emoji fallback in chat %s", chat)
                return await self.say(chat, render(body, emojis, fields, custom=False))
            raise

    async def groups(self, uid, page=0):
        accessible = []
        for row in self.db.chats():
            try:
                await self.require(row["id"], uid)
                accessible.append(row)
            except (ValueError, TelegramError):
                continue
        page = max(0, min(page, max(0, (len(accessible) - 1) // 8)))
        rows = [
            [(r["title"][:42], f"panel:{r['id']}")]
            for r in accessible[page * 8 : page * 8 + 8]
        ]
        nav = []
        if page:
            nav.append(("Назад", f"groups:{page-1}"))
        if (page + 1) * 8 < len(accessible):
            nav.append(("Далее", f"groups:{page+1}"))
        if nav:
            rows.append(nav)
        await self.say(
            uid,
            (
                "🐾 Bao Admin | группы\nВыбери группу для настройки."
                if accessible
                else "Групп пока нет. Добавь бота администратором в супергруппу и отправь там /admin."
            ),
            reply_markup=keyboard(rows),
        )

    async def panel(self, uid, chat):
        await self.require(chat, uid)
        self.selected[uid] = chat
        self.pending.pop(uid, None)
        cfg = self.db.settings(chat)
        info = await self.api.call("getChat", chat_id=chat)
        rows = [
            [(f"{'✅' if cfg[k] else '▫️'} {label}", f"toggle:{chat}:{k}")]
            for k, label in LABELS.items()
        ]
        rows += [
            [
                ("Тексты и эмодзи", f"texts:{chat}"),
                ("Каталог эмодзи", f"emojis:{chat}"),
            ],
            [("Лимиты", f"limits:{chat}"), ("Журнал", f"logs:{chat}")],
            [("Проверить права", f"rights:{chat}"), ("Группы", "groups:0")],
        ]
        mode = "\nТехработы: включены" if self.db.meta("maintenance") == "on" else ""
        await self.say(
            uid,
            f'🐾 <b>Bao Admin</b>\n{esc(info.get("title",chat))}\nID: <code>{chat}</code>{mode}',
            reply_markup=keyboard(rows),
        )

    async def texts(self, uid, chat):
        await self.require(chat, uid)
        rows = [
            [(label, f"edit:{chat}:{key}"), ("Сброс", f"resetask:{chat}:{key}")]
            for key, label in TEXT_LABELS.items()
        ]
        rows.append([("Назад", f"panel:{chat}")])
        await self.say(
            uid,
            "Выбери текст. Можно отправить премиум-эмодзи прямо в новом сообщении или использовать {emoji:имя}.",
            reply_markup=keyboard(rows),
        )

    async def callback(self, q):
        uid = q["from"]["id"]
        msg = q.get("message", {})
        if msg.get("chat", {}).get("type") != "private" or msg["chat"]["id"] != uid:
            await self.api.call(
                "answerCallbackQuery",
                callback_query_id=q["id"],
                text="Открой меню в личке",
                show_alert=True,
            )
            return
        await self.api.call("answerCallbackQuery", callback_query_id=q["id"])
        bits = q.get("data", "").split(":")
        action = bits[0]
        if action == "groups":
            return await self.groups(uid, int(bits[1]))
        if len(bits) < 2:
            return
        chat = int(bits[1])
        await self.require(chat, uid)
        self.selected[uid] = chat
        if action == "panel":
            await self.panel(uid, chat)
        elif action == "toggle" and bits[2] in LABELS:
            key = bits[2]
            if key == "antiflood":
                await self.require(chat, uid, "can_restrict_members")
            if key == "antilink":
                await self.require(chat, uid, "can_delete_messages")
            if key in ("antiflood", "antilink") and not self.db.settings(chat)[key]:
                await self.bot_right(chat, "can_delete_messages")
                if key == "antiflood":
                    await self.bot_right(chat, "can_restrict_members")
            self.db.set(chat, key, not self.db.settings(chat)[key])
            self.db.log(chat, uid, 0, "setting", key)
            await self.panel(uid, chat)
        elif action == "texts":
            await self.texts(uid, chat)
        elif action == "edit" and bits[2] in TEMPLATES:
            key = bits[2]
            self.pending[uid] = (chat, "text", key, time.monotonic() + 900)
            await self.say(
                uid,
                f"Текст: {TEXT_LABELS[key]}\n\n<pre>{esc(self.db.template(chat,key))}</pre>\nПришли новый текст. Доступны {{user}}, {{chat}}, {{admin}}, {{reason}}, {{count}}, {{limit}}, {{duration}}, {{emoji:имя}}. HTML не нужен.\n/cancel для отмены",
            )
        elif action == "resetask" and bits[2] in TEMPLATES:
            await self.say(
                uid,
                "Сбросить этот текст к стандартному?",
                reply_markup=keyboard(
                    [
                        [
                            ("Сбросить", f"reset:{chat}:{bits[2]}"),
                            ("Отмена", f"texts:{chat}"),
                        ]
                    ]
                ),
            )
        elif action == "reset" and bits[2] in TEMPLATES:
            self.db.text(chat, bits[2], TEMPLATES[bits[2]])
            self.db.log(chat, uid, 0, "text_reset", bits[2])
            await self.texts(uid, chat)
        elif action == "emojis":
            await self.emoji_list(uid, chat)
        elif action == "emojiadd":
            self.pending[uid] = (chat, "emoji", "", time.monotonic() + 900)
            await self.say(
                uid,
                "Пришли: имя ID\nПример: bao 5368324170671202286\nТакже можно прислать имя и сам премиум-эмодзи. Имя латиницей, до 24 символов.\n/cancel для отмены",
            )
        elif action == "limits":
            c = self.db.settings(chat)
            self.pending[uid] = (chat, "limits", "", time.monotonic() + 900)
            await self.say(
                uid,
                f'Варны: {c["warn_limit"]}, мут {duration(c["warn_mute"])}\nФлуд: {c["flood_count"]} сообщений за {c["flood_window"]} сек, мут {duration(c["flood_mute"])}\n\nПришли 5 чисел: лимит варнов, мут за варны в минутах, сообщения, окно в секундах, мут за флуд в минутах.\nПример: 3 60 6 10 10\n/cancel для отмены',
            )
        elif action == "logs":
            rows = self.db.logs(chat)
            text = "\n".join(
                f'{datetime.fromtimestamp(r["ts"],timezone.utc):%d.%m %H:%M} | {r["action"]} | {r["actor"]} → {r["target"]} | {r["reason"][:80]}'
                for r in rows
            )
            await self.say(
                uid, "Журнал, время UTC\n<pre>" + esc(text or "Пока пусто") + "</pre>"
            )
        elif action == "rights":
            m = await self.member(chat, self.me["id"])
            await self.say(
                uid,
                "Права бота\n"
                + esc(
                    "\n".join(
                        f"{k}: {m.get(k,False)}"
                        for k in (
                            "status",
                            "can_delete_messages",
                            "can_restrict_members",
                            "can_pin_messages",
                        )
                    )
                ),
            )

    async def emoji_list(self, uid, chat):
        items = self.db.emojis(chat)
        text = "\n".join(
            f"{name}: {id} | {{emoji:{name}}}" for name, (id, _) in items.items()
        )
        await self.say(
            uid,
            "Каталог эмодзи\n<pre>"
            + esc(text or "Пока пусто")
            + "</pre>\nУдаление: /emoji del имя",
            reply_markup=keyboard(
                [[("Добавить", f"emojiadd:{chat}"), ("Назад", f"panel:{chat}")]]
            ),
        )

    async def add_emoji(self, chat, payload, message):
        parts = payload.split()
        if len(parts) < 2 or not NAME.fullmatch(parts[0]):
            raise ValueError(
                "Нужно: имя ID или имя и премиум-эмодзи. Имя латиницей: bao, warn_icon"
            )
        name = parts[0]
        entities = emoji_entities(message)
        id = parts[1] if parts[1].isdigit() else entities[0][2] if entities else ""
        if not re.fullmatch(r"\d{1,24}", id):
            raise ValueError("Не найден ID премиум-эмодзи")
        current = self.db.emojis(chat)
        if name not in current and len(current) >= 40:
            raise ValueError("В каталоге максимум 40 эмодзи")
        stickers = await self.api.call("getCustomEmojiStickers", custom_emoji_ids=[id])
        if not stickers:
            raise ValueError("Telegram не нашёл эмодзи с этим ID")
        fallback = stickers[0].get("emoji") or "🐾"
        self.db.emoji(chat, name, id, fallback)
        return name

    async def edit_input(self, uid, message, chat, kind, key, payload=None):
        await self.require(chat, uid)
        body = capture(message) if payload is None else payload
        if kind == "text":
            if key not in TEMPLATES:
                raise ValueError("Неизвестный текст")
            validate(body, self.db.emojis(chat))
            self.db.text(chat, key, body)
            self.db.log(chat, uid, 0, "text", key)
            await self.say(uid, "Текст сохранён. Предпросмотр:")
            fields = {
                k: esc(v)
                for k, v in dict(
                    chat="Тестовая группа",
                    reason="Пример причины",
                    duration="30 мин",
                    count="1",
                    limit="3",
                    admin="Админ",
                ).items()
            }
            fields["user"] = mention(message["from"])
            rich = render(body, self.db.emojis(chat), fields)
            try:
                await self.say(uid, rich)
            except TelegramError as e:
                if e.code != 400 or "<tg-emoji " not in rich:
                    raise
                await self.say(
                    uid, render(body, self.db.emojis(chat), fields, custom=False)
                )
                await self.say(
                    uid,
                    "Telegram отклонил отправку премиум-эмодзи. Сохранён их ID, в сообщении показана обычная замена.",
                )
        elif kind == "emoji":
            name = await self.add_emoji(chat, body, message)
            self.db.log(chat, uid, 0, "emoji", name)
            await self.say(
                uid, "Сохранено. Вставляй в текст: <code>{emoji:" + name + "}</code>"
            )
        elif kind == "limits":
            await self.require(chat, uid, "can_restrict_members")
            try:
                values = list(map(int, body.split()))
            except ValueError:
                raise ValueError("Нужны пять целых чисел")
            ranges = [(1, 20), (1, 525600), (3, 100), (2, 60), (1, 525600)]
            if len(values) != 5 or any(
                not lo <= v <= hi for v, (lo, hi) in zip(values, ranges)
            ):
                raise ValueError(
                    "Диапазоны: варны 1..20, муты 1..525600 мин, сообщения 3..100, окно 2..60 сек"
                )
            for k, v in zip(
                (
                    "warn_limit",
                    "warn_mute",
                    "flood_count",
                    "flood_window",
                    "flood_mute",
                ),
                values,
            ):
                self.db.set(chat, k, v * 60 if k.endswith("mute") else v)
            self.db.log(chat, uid, 0, "limits", body)
            await self.say(uid, "Лимиты сохранены")
        self.pending.pop(uid, None)

    async def emoji_ids(self, message):
        source = message.get("reply_to_message") or message
        ids = list(
            dict.fromkeys(
                (id, fallback) for _, _, id, fallback in emoji_entities(source)
            )
        )
        sticker = source.get("sticker", {})
        if sticker.get("custom_emoji_id"):
            ids.append((sticker["custom_emoji_id"], sticker.get("emoji", "🐾")))
        if not ids:
            return await self.say(
                message["chat"]["id"],
                "Ответь командой /emojiid на сообщение с премиум-эмодзи. Обычные эмодзи не имеют custom_emoji_id.",
            )
        await self.say(
            message["chat"]["id"],
            "\n".join(
                f"{esc(fallback)} <code>{id}</code>" for id, fallback in ids[:40]
            ),
        )

    async def target(self, message, args):
        reply = message.get("reply_to_message")
        if reply:
            if reply.get("sender_chat") or not reply.get("from"):
                raise ValueError("Нельзя выбрать анонимного отправителя или канал")
            return reply["from"], args
        parts = args.split(maxsplit=1)
        if not parts or not parts[0].isdigit():
            raise ValueError("Ответь на сообщение участника или укажи его числовой ID")
        member = await self.member(message["chat"]["id"], int(parts[0]))
        return member["user"], parts[1] if len(parts) > 1 else ""

    async def safe_target(self, chat, target, actor):
        if target["id"] in (actor, self.me["id"]):
            raise ValueError("Нельзя применить эту команду к себе или Bao Admin")
        member = await self.member(chat, target["id"])
        if member["status"] in ADMIN:
            raise ValueError("Администраторы защищены от наказаний")

    async def mute(self, chat, uid, n):
        info = await self.api.call("getChat", chat_id=chat)
        if info.get("type") != "supergroup":
            raise ValueError("Для мута нужна супергруппа Telegram")
        await self.api.call(
            "restrictChatMember",
            chat_id=chat,
            user_id=uid,
            permissions={"can_send_messages": False},
            use_independent_chat_permissions=True,
            until_date=int(time.time()) + n,
        )

    async def moderate(self, message, command, args):
        chat = message["chat"]["id"]
        actor = message["from"]
        uid = actor["id"]
        right = (
            "can_delete_messages"
            if command == "del"
            else (
                "can_pin_messages"
                if command in ("pin", "unpin")
                else "can_restrict_members"
            )
        )
        await self.require(chat, uid, right)
        if command in ("del", "pin", "unpin"):
            await self.bot_right(chat, right)
            reply = message.get("reply_to_message")
            if not reply:
                raise ValueError("Используй команду ответом на сообщение")
            method = {
                "del": "deleteMessage",
                "pin": "pinChatMessage",
                "unpin": "unpinChatMessage",
            }[command]
            params = {"chat_id": chat, "message_id": reply["message_id"]}
            if command == "pin":
                params["disable_notification"] = True
            await self.api.call(method, **params)
            self.db.log(chat, uid, reply.get("from", {}).get("id", 0), command)
            return await self.say(chat, "Готово")
        target, tail = await self.target(message, args)
        if command == "warns":
            return await self.say(
                chat,
                f'{mention(target)}: предупреждений {self.db.warns(chat,target["id"])}',
            )
        await self.safe_target(chat, target, uid)
        reason = tail[:300] or "Нарушение правил"
        if command == "unwarn":
            self.db.warns(chat, target["id"], reset=True)
        else:
            await self.bot_right(chat, "can_restrict_members")
            if command == "warn":
                cfg = self.db.settings(chat)
                count = self.db.warns(chat, target["id"], 1)
                self.db.log(chat, uid, target["id"], "warn", reason)
                await self.emit(
                    chat,
                    "warn",
                    target,
                    actor,
                    reason,
                    count=count,
                    limit=cfg["warn_limit"],
                )
                if count >= cfg["warn_limit"]:
                    await self.mute(chat, target["id"], cfg["warn_mute"])
                    self.db.warns(chat, target["id"], reset=True)
                    self.db.log(chat, uid, target["id"], "warn_mute", reason)
                    await self.emit(
                        chat,
                        "mute",
                        target,
                        actor,
                        "Достигнут лимит предупреждений",
                        duration=duration(cfg["warn_mute"]),
                    )
                return
            if command == "mute":
                parts = tail.split(maxsplit=1)
                if not parts:
                    raise ValueError("Укажи срок: /mute 30m причина")
                n = seconds(parts[0])
                reason = parts[1][:300] if len(parts) > 1 else "Нарушение правил"
                await self.mute(chat, target["id"], n)
            elif command == "unmute":
                info = await self.api.call("getChat", chat_id=chat)
                if info.get("type") != "supergroup":
                    raise ValueError("Для мута нужна супергруппа")
                perms = info.get("permissions")
                if not perms:
                    raise ValueError("Не удалось прочитать стандартные права группы")
                await self.api.call(
                    "restrictChatMember",
                    chat_id=chat,
                    user_id=target["id"],
                    permissions=perms,
                    use_independent_chat_permissions=True,
                )
            elif command == "ban":
                await self.api.call("banChatMember", chat_id=chat, user_id=target["id"])
            elif command == "unban":
                await self.api.call(
                    "unbanChatMember",
                    chat_id=chat,
                    user_id=target["id"],
                    only_if_banned=True,
                )
        self.db.log(chat, uid, target["id"], command, reason)
        extra = {"duration": duration(n)} if command == "mute" else {}
        await self.emit(chat, command, target, actor, reason, **extra)

    async def automated(self, message):
        chat = message["chat"]["id"]
        user = message["from"]
        cfg = self.db.settings(chat)
        if not cfg["enabled"] or self.db.meta("maintenance") == "on":
            return
        if not (cfg["antiflood"] or cfg["antilink"]):
            return
        member = await self.member(chat, user["id"])
        if member["status"] in ADMIN or user.get("is_bot"):
            return
        body = message.get("text", message.get("caption", ""))
        entities = message.get("entities", message.get("caption_entities", []))
        if cfg["antilink"] and (
            any(e["type"] in ("url", "text_link") for e in entities)
            or re.search(r"(?i)(https?://|www\.|t\.me/|telegram\.me/)", body)
        ):
            await self.bot_right(chat, "can_delete_messages")
            await self.api.call(
                "deleteMessage", chat_id=chat, message_id=message["message_id"]
            )
            self.db.log(chat, self.me["id"], user["id"], "link_delete")
            return True
        if cfg["antiflood"]:
            now = time.monotonic()
            key = (chat, user["id"])
            queue = self.flood[key]
            while queue and now - queue[0] > cfg["flood_window"]:
                queue.popleft()
            queue.append(now)
            if len(queue) >= cfg["flood_count"]:
                await self.bot_right(chat, "can_restrict_members")
                await self.mute(chat, user["id"], cfg["flood_mute"])
                queue.clear()
                self.db.log(chat, self.me["id"], user["id"], "flood_mute")
                await self.emit(
                    chat,
                    "mute",
                    user,
                    reason="Флуд",
                    duration=duration(cfg["flood_mute"]),
                )
                try:
                    await self.api.call(
                        "deleteMessage", chat_id=chat, message_id=message["message_id"]
                    )
                except TelegramError:
                    pass
                return True
        return False

    async def report(self, message, reason):
        if not message.get("reply_to_message"):
            raise ValueError("Ответь /report на сообщение нарушителя")
        chat = message["chat"]["id"]
        uid = message["from"]["id"]
        key = (chat, uid)
        now = time.monotonic()
        if now - self.report_times.get(key, -1000) < 60:
            raise ValueError("Жалобу можно отправлять раз в минуту")
        self.report_times[key] = now
        admins = await self.api.call("getChatAdministrators", chat_id=chat)
        tags = " ".join(
            mention(a["user"]) for a in admins if not a["user"].get("is_bot")
        )
        await self.say(
            chat,
            f'🚩 Жалоба от {mention(message["from"])}\n{esc(reason[:300] or "Проверьте сообщение")}\n{tags}',
            reply_parameters={
                "message_id": message["reply_to_message"]["message_id"],
                "allow_sending_without_reply": True,
            },
        )
        self.db.log(
            chat,
            uid,
            message["reply_to_message"].get("from", {}).get("id", 0),
            "report",
            reason,
        )

    async def message(self, m, edited=False):
        chat = m["chat"]
        cid = chat["id"]
        user = m.get("from")
        group = chat["type"] in ("group", "supergroup")
        if group:
            self.db.chat(chat)
        if not user or m.get("sender_chat"):
            return
        uid = user["id"]
        if user.get("is_bot"):
            return
        cfg = self.db.settings(cid)
        if group and m.get("new_chat_members"):
            if (
                cfg["enabled"]
                and cfg["welcome"]
                and self.db.meta("maintenance") != "on"
            ):
                for new in m["new_chat_members"]:
                    if not new.get("is_bot"):
                        await self.emit(cid, "welcome", new)
            return
        if edited:
            # Edits can contain links; do not count one edited message as new flood traffic.
            if (
                group
                and cfg["antilink"]
                and cfg["enabled"]
                and self.db.meta("maintenance") != "on"
            ):
                member = await self.member(cid, uid)
                entities = m.get("entities", m.get("caption_entities", []))
                body = m.get("text", m.get("caption", ""))
                if member["status"] not in ADMIN and (
                    any(e["type"] in ("url", "text_link") for e in entities)
                    or re.search(r"(?i)(https?://|www\.|t\.me/|telegram\.me/)", body)
                ):
                    await self.api.call(
                        "deleteMessage", chat_id=cid, message_id=m["message_id"]
                    )
                    self.db.log(cid, self.me["id"], uid, "edited_link_delete")
            return
        if group and await self.automated(m):
            return
        text = m.get("text", "")
        bits = text.split(maxsplit=1)
        first = bits[0].lower() if bits else ""
        args = bits[1] if len(bits) > 1 else ""
        if first.startswith("/"):
            name = first[1:].split("@")
            if len(name) > 1 and name[1] != self.me.get("username", "").lower():
                return
            command = name[0]
        else:
            command = ALIASES.get(first, "") if group else ""
        command = ALIASES.get(command, command)
        if command == "cancel":
            self.pending.pop(uid, None)
            return await self.say(cid, "Ввод отменён")
        if not group and uid in self.pending and not first.startswith("/"):
            target, kind, key, expires = self.pending[uid]
            if time.monotonic() > expires:
                self.pending.pop(uid, None)
                raise ValueError("Время ввода истекло. Открой /admin заново")
            return await self.edit_input(uid, m, target, kind, key)
        if command == "maintenance":
            if uid not in self.owners:
                raise ValueError("Команда доступна владельцу бота")
            if args not in ("on", "off"):
                raise ValueError("/maintenance on или /maintenance off")
            self.db.meta("maintenance", args)
            return await self.say(cid, "Техработы: " + args)
        if command in ("start", "help"):
            if command == "start" and not group and args.startswith("admin_"):
                try:
                    target = int(args[6:])
                except ValueError:
                    raise ValueError("Некорректная ссылка")
                return await self.panel(uid, target)
            return await self.say(cid, HELP)
        if command == "id":
            reply = m.get("reply_to_message", {}).get("from", user)
            return await self.say(
                cid,
                f'Пользователь: <code>{reply["id"]}</code>\nЧат: <code>{cid}</code>',
            )
        if command == "emojiid":
            return await self.emoji_ids(m)
        if command == "admin":
            if group:
                await self.require(cid, uid)
                url = f'https://t.me/{self.me["username"]}?start=admin_{cid}'
                return await self.say(
                    cid,
                    "🐾 Админ-консоль находится в личке",
                    reply_markup={
                        "inline_keyboard": [[{"text": "Открыть Bao Admin", "url": url}]]
                    },
                )
            return await self.groups(uid)
        if not group:
            if command in ("emoji", "text", "limits"):
                target = self.selected.get(uid)
                if target is None:
                    raise ValueError("Сначала выбери группу через /admin")
                await self.require(target, uid)
                if command == "limits":
                    return await self.edit_input(uid, m, target, "limits", "", args)
                if command == "text":
                    body = capture(m).split(maxsplit=2)
                    if len(body) < 3:
                        raise ValueError(
                            "/text ключ текст. Ключи: " + ", ".join(TEMPLATES)
                        )
                    return await self.edit_input(
                        uid, m, target, "text", body[1], body[2]
                    )
                parts = args.split(maxsplit=1)
                if not parts or parts[0] == "list":
                    return await self.emoji_list(uid, target)
                if len(parts) < 2:
                    raise ValueError("/emoji add имя ID или /emoji del имя")
                if parts[0] == "add":
                    return await self.edit_input(uid, m, target, "emoji", "", parts[1])
                if parts[0] == "del":
                    name = parts[1]
                    if name not in self.db.emojis(target):
                        raise ValueError("Такого имени нет")
                    if any(
                        "{emoji:" + name + "}" in self.db.template(target, key)
                        for key in TEMPLATES
                    ):
                        raise ValueError("Сначала убери этот эмодзи из шаблонов")
                    self.db.execute(
                        "DELETE FROM emojis WHERE chat=? AND name=?", (target, name)
                    )
                    self.db.log(target, uid, 0, "emoji_delete", name)
                    return await self.say(uid, "Эмодзи удалён")
                raise ValueError("/emoji add имя ID, /emoji del имя или /emoji list")
            if command in MOD:
                raise ValueError("Команды модерации выполняются в группе")
            return
        if command in MOD:
            # Admin commands remain available while automatic moderation is disabled.
            return await self.moderate(m, command, args)
        if not cfg["enabled"]:
            return
        if self.db.meta("maintenance") == "on":
            if command:
                await self.require(cid, uid)
            else:
                return
        if command == "rules":
            return await self.emit(cid, "rules", user)
        if command == "report":
            return await self.report(m, args)

    async def handle(self, update):
        now = time.monotonic()
        if now - self.last_prune > 120:
            self.flood = defaultdict(
                deque, {k: v for k, v in self.flood.items() if v and now - v[-1] < 120}
            )
            self.report_times = {
                k: v for k, v in self.report_times.items() if now - v < 120
            }
            self.pending = {k: v for k, v in self.pending.items() if now < v[3]}
            self.last_prune = now
        q = update.get("callback_query")
        m = update.get("message") or update.get("edited_message")
        dest = q["from"]["id"] if q else m["chat"]["id"] if m else None
        try:
            if q:
                await self.callback(q)
            elif m:
                await self.message(m, edited="edited_message" in update)
            elif "my_chat_member" in update:
                chat = update["my_chat_member"]["chat"]
                if chat["type"] in ("group", "supergroup"):
                    self.db.chat(chat)
        except ValueError as e:
            if dest:
                await self.say(dest, esc(str(e)))
        except TelegramError as e:
            log.warning("Telegram rejected operation, code=%s", e.code)
            if dest:
                await self.say(
                    dest,
                    "Ошибка Telegram. Если команда уже применилась, не повторяй её. Проверь состояние участника и права бота.\n"
                    + esc(e.description[:250]),
                )


async def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Заполни BOT_TOKEN в .env или переменных окружения")
    owners = {
        int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()
    }
    store = Store(os.getenv("DATABASE_PATH", "data/bao_admin.sqlite3"))
    api = API(token)
    app = BaoAdmin(api, store, owners)
    try:
        app.me = await api.call("getMe")
        webhook = await api.call("getWebhookInfo")
        if webhook.get("url"):
            raise SystemExit(
                "У бота включён webhook. Отключи прежнее приложение перед запуском polling"
            )
        await api.call(
            "setMyCommands",
            commands=[
                {"command": k, "description": v}
                for k, v in [
                    ("admin", "Админ-меню"),
                    ("help", "Команды"),
                    ("rules", "Правила группы"),
                    ("warn", "Предупреждение"),
                    ("mute", "Мут"),
                    ("unmute", "Снять мут"),
                    ("ban", "Бан"),
                    ("unban", "Разбан"),
                    ("unwarn", "Сброс предупреждений"),
                    ("warns", "Предупреждения участника"),
                    ("report", "Жалоба"),
                    ("emojiid", "ID премиум-эмодзи"),
                    ("id", "ID пользователя и чата"),
                    ("cancel", "Отменить ввод"),
                ]
            ],
        )
        offset = int(store.meta("offset") or 0)
        log.info("Bao Admin started, id=%s", app.me["id"])
        while True:
            try:
                updates = await api.call(
                    "getUpdates",
                    offset=offset,
                    timeout=25,
                    allowed_updates=[
                        "message",
                        "edited_message",
                        "callback_query",
                        "my_chat_member",
                    ],
                )
                for update in updates:
                    # Persist before side effects: avoid duplicate punishments after a crash.
                    offset = update["update_id"] + 1
                    store.meta("offset", offset)
                    try:
                        await app.handle(update)
                    except (TelegramError, httpx.HTTPError) as e:
                        log.warning(
                            "Update %s not completed, error=%s",
                            update["update_id"],
                            type(e).__name__,
                        )
                    except Exception:
                        log.exception("Update handler failed")
            except TelegramError as e:
                if e.code in (401, 409):
                    raise SystemExit(
                        "Проверь токен и убедись, что запущен один экземпляр бота"
                    )
                await asyncio.sleep(min(e.retry_after, 30))
            except (httpx.HTTPError, ValueError):
                log.warning("Polling connection failed; reconnecting")
                await asyncio.sleep(3)
    finally:
        await api.close()
        store.db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
