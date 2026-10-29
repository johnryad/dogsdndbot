"""Клиент Gemini через REST (без SDK — меньше зависимостей и памяти на Render)."""
import time
import asyncio
import logging
import aiohttp

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(Exception):
    pass


class RateLimiter:
    """Не чаще rpm запросов в минуту на весь бот — чтобы не упереться в лимиты бесплатного тарифа."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / max(rpm, 1)
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


class GeminiClient:
    # Сколько секунд не трогать модель, которая ответила «перегружена» или «лимит»
    COOLDOWN_BUSY = 180
    COOLDOWN_RPM = 60
    COOLDOWN_DAY = 3600

    def __init__(self, api_key: str, models: list[str], rpm: int = 8, thinking_level: str | None = None):
        self.api_key = api_key
        self.models = models
        self.limiter = RateLimiter(rpm)
        self.thinking_level = thinking_level
        self.calls = 0
        self.cooldown: dict[str, float] = {}
        # Модели, которые не понимают thinkingLevel: им шлём запрос без него
        self.no_thinking: set[str] = set()

    def order(self) -> list[str]:
        """Сначала свободные модели в заданном порядке, потом те, что на перерыве."""
        now = time.monotonic()
        free = [m for m in self.models if self.cooldown.get(m, 0) <= now]
        return free + [m for m in self.models if m not in free]

    async def generate(self, system: str, contents: list[dict], tools: list[dict] | None = None,
                       temperature: float = 1.0) -> dict:
        """Возвращает content ответа модели ({"role": "model", "parts": [...]}).

        Бесплатные Flash-модели бывают перегружены (503) или упираются в лимит (429) —
        тогда сразу идём к следующей модели, а занятую на время пропускаем.
        """
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 8192},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        last_error = "нет моделей"
        timeout = aiohttp.ClientTimeout(total=60)
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(5)
            for model in self.order():
                await self.limiter.wait()
                self.calls += 1
                request = body
                if self.thinking_level and model not in self.no_thinking:
                    config = dict(body["generationConfig"], thinkingConfig={"thinkingLevel": self.thinking_level})
                    request = dict(body, generationConfig=config)
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            API_URL.format(model=model), json=request, headers={"x-goog-api-key": self.api_key}
                        ) as resp:
                            data = await resp.json(content_type=None)
                            status = resp.status
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_error = f"{model}: сеть: {e!r}"
                    logging.warning(f"Gemini {last_error}")
                    self.cooldown[model] = time.monotonic() + self.COOLDOWN_BUSY
                    continue

                if status == 200:
                    candidates = data.get("candidates") or []
                    content = candidates[0].get("content") if candidates else None
                    if content and content.get("parts"):
                        content["role"] = "model"
                        return content
                    reason = candidates[0].get("finishReason") if candidates else data.get("promptFeedback")
                    last_error = f"{model}: пустой ответ ({reason})"
                    logging.warning(f"Gemini {last_error}")
                    continue

                message = (data or {}).get("error", {}).get("message", "")[:300]
                last_error = f"{model}: HTTP {status}: {message}"
                logging.warning(f"Gemini {last_error}")
                if status == 429:
                    daily = "per day" in message.lower() or "PerDay" in message
                    pause = self.COOLDOWN_DAY if daily else self.COOLDOWN_RPM
                    self.cooldown[model] = time.monotonic() + pause
                elif status >= 500:
                    self.cooldown[model] = time.monotonic() + self.COOLDOWN_BUSY
                elif status == 400 and "thinking" in message.lower() and model not in self.no_thinking:
                    # Этот уровень размышлений модель не поддерживает — на следующем круге пойдёт без него
                    self.no_thinking.add(model)
        raise LLMError(last_error)
