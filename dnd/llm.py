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
    def __init__(self, api_key: str, models: list[str], rpm: int = 8, thinking_level: str | None = None):
        self.api_key = api_key
        self.models = models
        self.limiter = RateLimiter(rpm)
        self.thinking_level = thinking_level
        self.calls = 0

    async def generate(self, system: str, contents: list[dict], tools: list[dict] | None = None,
                       temperature: float = 1.0) -> dict:
        """Возвращает content ответа модели ({"role": "model", "parts": [...]}).

        При 429/5xx повторяет с паузой, потом переходит на запасную модель.
        """
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 8192},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        if self.thinking_level:
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": self.thinking_level}

        last_error = "нет моделей"
        timeout = aiohttp.ClientTimeout(total=90)
        for model in self.models:
            for attempt in range(3):
                await self.limiter.wait()
                self.calls += 1
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            API_URL.format(model=model), json=body, headers={"x-goog-api-key": self.api_key}
                        ) as resp:
                            data = await resp.json(content_type=None)
                            status = resp.status
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_error = f"{model}: сеть: {e}"
                    logging.warning(f"Gemini {last_error}")
                    await asyncio.sleep(2 * (attempt + 1))
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
                    # Дневной лимит не переждать — сразу на запасную модель, минутный — подождать
                    if "per day" in message.lower() or "PerDay" in message:
                        break
                    await asyncio.sleep(15 * (attempt + 1))
                elif status >= 500:
                    await asyncio.sleep(3 * (attempt + 1))
                else:
                    break
        raise LLMError(last_error)
