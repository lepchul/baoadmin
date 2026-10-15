import httpx


class TelegramError(Exception):
    def __init__(self, data):
        self.code = data.get("error_code", 0)
        self.description = data.get("description", "Telegram API error")
        self.retry_after = data.get("parameters", {}).get("retry_after", 3)
        super().__init__(self.description)


class API:
    def __init__(self, token):
        self.url = "https://api.telegram.org/bot" + token + "/"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10))

    async def call(self, method, **params):
        # Do not retry mutations: a timeout may happen after Telegram applied them.
        response = await self.client.post(self.url + method, json=params)
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(data)
        return data["result"]

    async def close(self):
        await self.client.aclose()
