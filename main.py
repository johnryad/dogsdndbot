"""Точка входа: health-эндпоинт для Render и cron-пинга + ДнД-бот."""
import os
import asyncio
import logging

from aiohttp import web

from dnd.bot import run_dnd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def load_dotenv(path: str = ".env"):
    """Локально секреты удобно держать в .env (KEY=value). На Render их задают в Environment."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def health_check(request):
    return web.Response(text="DnD bot is running OK!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info(f"Health-эндпоинт на порту {port}")


async def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN") or os.getenv("DND_BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN не установлен!")
    await start_web_server()
    await run_dnd(token, handle_signals=True)


if __name__ == "__main__":
    asyncio.run(main())
