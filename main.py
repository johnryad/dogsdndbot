"""Точка входа: на Render — веб-сервер с вебхуком Telegram, локально — опрос."""
import os
import signal
import asyncio
import logging

from aiohttp import web

from dnd.bot import from_env, webhook_base_url

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


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health_check)
    return app


async def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN") or os.getenv("DND_BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN не установлен!")
    dnd = from_env(token)
    port = int(os.getenv("PORT", 8080))
    base_url = webhook_base_url()
    app = make_app()

    if base_url:
        # Render free усыпляет сервис без запросов; вебхук будит его входящим сообщением
        dnd.mount_webhook(app, base_url)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logging.info(f"Режим вебхука: {base_url}, порт {port}")
        stop = asyncio.Event()
        try:
            # Render останавливает сервис через SIGTERM — закрываем базу и сессию аккуратно
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        except (NotImplementedError, AttributeError):
            pass
        try:
            await stop.wait()
        finally:
            await runner.cleanup()
        return

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info(f"Режим опроса, health-эндпоинт на порту {port}")
    await dnd.run(handle_signals=True)


if __name__ == "__main__":
    asyncio.run(main())
