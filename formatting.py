import html
import re

TOKEN = re.compile(r"\{([^{}]+)\}")
FIELDS = {"user", "chat", "reason", "duration", "count", "limit", "admin"}
NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
RAW_EMOJI = re.compile(r"^ce:(\d{1,24}):([^{}<>\n]{1,16})$")


def clean(text):
    return str(text).replace("\u2014", "-").replace("\u2013", "-")


def esc(text):
    return html.escape(clean(text), quote=True)


def mention(user):
    return f'<a href="tg://user?id={int(user["id"])}">{esc(user.get("first_name",user["id"]))}</a>'


def validate(body, emojis):
    if not body.strip() or len(body) > 1500:
        raise ValueError("Текст должен содержать от 1 до 1500 символов")
    for token in TOKEN.findall(body):
        if token in FIELDS or RAW_EMOJI.fullmatch(token):
            continue
        if token.startswith("emoji:") and token[6:] in emojis:
            continue
        raise ValueError(f"Неизвестная вставка: {{{token}}}")
    if "{" in TOKEN.sub("", body) or "}" in TOKEN.sub("", body):
        raise ValueError("Проверь фигурные скобки")
    worst = {
        key: "x" * size
        for key, size in {
            "user": 128,
            "chat": 256,
            "admin": 128,
            "reason": 600,
            "duration": 40,
            "count": 20,
            "limit": 20,
        }.items()
    }
    preview = html.unescape(render(body, emojis, worst, custom=False))
    if len(preview.encode("utf-16-le")) // 2 > 4000:
        raise ValueError(
            "Слишком много повторяющихся вставок. Сократи текст, чтобы он поместился в сообщение"
        )


def render(body, emojis, fields, custom=True):
    def replace(match):
        token = match.group(1)
        raw = RAW_EMOJI.fullmatch(token)
        if raw:
            id, fallback = raw.groups()
        elif token.startswith("emoji:") and token[6:] in emojis:
            id, fallback = emojis[token[6:]]
        else:
            return fields.get(token, "")
        return (
            f'<tg-emoji emoji-id="{id}">{esc(fallback)}</tg-emoji>'
            if custom
            else esc(fallback)
        )

    # Literal text is escaped, only validated placeholders can insert markup.
    pieces = []
    end = 0
    for match in TOKEN.finditer(body):
        pieces.extend((esc(body[end : match.start()]), replace(match)))
        end = match.end()
    return "".join(pieces) + esc(body[end:])


def emoji_entities(message):
    text = message.get("text", message.get("caption", ""))
    data = text.encode("utf-16-le")
    result = []
    for ent in message.get("entities", message.get("caption_entities", [])):
        if ent.get("type") == "custom_emoji":
            start = ent["offset"] * 2
            stop = start + ent["length"] * 2
            result.append(
                (
                    ent["offset"],
                    ent["length"],
                    str(ent["custom_emoji_id"]),
                    data[start:stop].decode("utf-16-le"),
                )
            )
    return result


def capture(message):
    text = message.get("text", message.get("caption", ""))
    data = text.encode("utf-16-le")
    for offset, length, id, fallback in sorted(emoji_entities(message), reverse=True):
        data = (
            data[: offset * 2]
            + f"{{ce:{id}:{fallback}}}".encode("utf-16-le")
            + data[(offset + length) * 2 :]
        )
    return clean(data.decode("utf-16-le"))


def seconds(value):
    m = re.fullmatch(r"(\d+)([mhdмчд])", value.lower())
    if not m:
        raise ValueError("Срок: 10m, 2h или 1d. От 1 минуты до 365 дней")
    result = (
        int(m[1])
        * {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}[m[2]]
    )
    if not 60 <= result <= 365 * 86400:
        raise ValueError("Срок должен быть от 1 минуты до 365 дней")
    return result


def duration(n):
    return (
        f"{n//86400} д"
        if n % 86400 == 0
        else f"{n//3600} ч" if n % 3600 == 0 else f"{n//60} мин"
    )
