"""Сценарий партии без настоящего Telegram и Gemini: python -m pytest tests -q

Telegram подменён фейковой сессией aiogram, ИИ — скриптом, который вызывает инструменты
так же, как это делает Gemini. Проверяем: /newgame, два /join, ходы с бросками, бой, рестарт.
"""
import os
import re
import asyncio
import datetime
import tempfile

from aiogram.client.session.base import BaseSession
from aiogram.types import Update, User, Chat, Message

from dnd import rules, dice
from dnd.bot import DndBot
from dnd.scenarios import SCENARIOS, scenario_block
from dnd.storage import SqliteStorage

CHAT_ID = -100500
VASYA = User(id=11, is_bot=False, first_name="Вася", username="vasya")
PETYA = User(id=22, is_bot=False, first_name="Петя", username="petya")

GOBLIN = {
    "index": "goblin", "name": "Goblin", "armor_class": [{"value": 15}], "hit_points": 7, "xp": 50,
    "strength": 8, "dexterity": 14, "constitution": 10, "intelligence": 10, "wisdom": 8, "charisma": 8,
    "actions": [{"name": "Scimitar", "attack_bonus": 4, "damage": [{"damage_dice": "1d6+2"}]}],
}


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[str] = []
        self.next_id = 1000
        self.webhook = None

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        if name == "SetWebhook":
            self.webhook = method.url
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="ГМ", username="gm_bot")
        if name in ("SendMessage", "EditMessageText"):
            self.next_id += 1
            self.sent.append(method.text)
            return Message(message_id=self.next_id, date=datetime.datetime.now(),
                           chat=Chat(id=CHAT_ID, type="supergroup"), text=method.text)
        return True

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError

    async def close(self):
        pass


class FakeLLM:
    """Ведёт себя как Gemini: на заявку отвечает вызовами инструментов, на их результаты — текстом."""

    def __init__(self):
        self.calls = 0

    async def generate(self, system, contents, tools=None, temperature=1.0):
        self.calls += 1
        last = contents[-1]["parts"]
        if "functionResponse" in last[0]:
            results = "; ".join(f"{p['functionResponse']['name']}" for p in last)
            return {"role": "model", "parts": [{"text": f"**Ведущий** язвительно описывает итог ({results})."}]}
        prompt = last[0]["text"]
        if "журнал кампании" in system:
            return {"role": "model", "parts": [{"text": "Краткое содержание: партия шла и страдала."}]}
        if not tools:
            return {"role": "model", "parts": [{"text": "Эпилог: все умерли от скуки."}]}
        turn = prompt.split("[ХОД]", 1)[1]
        calls = []
        if "Начни приключение" in turn:
            calls.append(("update_scene", {"scene": "Таверна «Блевотный пони», ночь"}))
        if "обыскиваю труп" in turn:
            calls.append(("advance_plot", {"summary": "нашли след жениха"}))
            calls.append(("remember", {"fact": "Груня обещала 20 золотых"}))
        if "обыскиваю" in turn:
            calls.append(("ability_check", {"who": "Торин", "check": "Внимательность", "dc": 12}))
            calls.append(("add_item", {"character": "Торин", "item": "ржавый ключ", "quantity": 1}))
        if "болтаю" in turn:
            calls.append(("ability_check", {"who": "Лиса", "check": "Убеждение", "dc": 15, "advantage": "adv"}))
            calls.append(("add_item", {"character": "Лиса", "item": "золото", "quantity": 5}))
        if "бью гоблинов" in turn:
            calls.append(("start_combat", {"enemies": [{"name": "Гоблин", "srd_index": "goblin", "count": 2}]}))
        m = re.search(r"Ход персонажа (\S+):", turn)
        if m:
            target = re.search(r"монстр id=(\S+):", prompt)
            if target:
                calls.append(("attack", {"attacker": m.group(1), "target": target.group(1)}))
        m = re.search(r"Затем ходят монстры: (.+?)\. ", turn)
        if m:
            hero = re.search(r"^- (\S+) \(", prompt.split("Партия:")[1], re.M).group(1)
            for mid in re.findall(r"(\w+_\d+) \(", m.group(1)):
                calls.append(("attack", {"attacker": mid, "target": hero}))
        if not calls:
            return {"role": "model", "parts": [{"text": "Ничего не происходит. Скучно."}]}
        return {"role": "model", "parts": [{"functionCall": {"name": n, "args": a}} for n, a in calls]}


class Harness:
    def __init__(self, db_path):
        self.session = FakeSession()
        self.llm = FakeLLM()
        self.dnd = DndBot("42:TEST", self.llm, SqliteStorage(db_path), session=self.session)
        self.update_id = 0
        self.message_id = 0

    async def start(self):
        await self.dnd.storage.connect()
        await self.dnd.restore()

    async def say(self, user: User, text: str):
        self.update_id += 1
        self.message_id += 1
        entities = None
        if text.startswith("/"):
            entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        update = Update.model_validate({
            "update_id": self.update_id,
            "message": {"message_id": self.message_id, "date": int(datetime.datetime.now().timestamp()),
                        "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Подземелье"},
                        "from": user.model_dump(), "text": text, "entities": entities},
        }, context={"bot": self.dnd.bot})
        await self.dnd.dp.feed_update(self.dnd.bot, update)
        await asyncio.sleep(0.01)

    async def click(self, user: User, data: str):
        self.update_id += 1
        update = Update.model_validate({
            "update_id": self.update_id,
            "callback_query": {
                "id": str(self.update_id), "from": user.model_dump(), "chat_instance": "x", "data": data,
                "message": {"message_id": 1, "date": int(datetime.datetime.now().timestamp()),
                            "chat": {"id": CHAT_ID, "type": "supergroup"}, "text": "кнопки"},
            },
        }, context={"bot": self.dnd.bot})
        await self.dnd.dp.feed_update(self.dnd.bot, update)
        await asyncio.sleep(0.01)

    @property
    def state(self):
        return self.dnd.states[CHAT_ID]

    def last(self, n=1):
        return "\n---\n".join(self.session.sent[-n:])


def test_dice():
    for _ in range(200):
        r = dice.roll("2d6+3")
        assert 5 <= r.total <= 15
        r = dice.roll("1d20+5", "adv")
        assert "преим" in str(r) and 6 <= r.total <= 25 and r.natural
        assert 3 <= dice.roll("4d6kh3").total <= 18
    assert dice.roll("1d20-1d4+2").total <= 21
    assert dice.double_dice("1d8+3") == "2d8+3"
    for bad in ("", "d", "1d20++", "1000d6", "abc"):
        try:
            dice.roll(bad)
        except dice.DiceError:
            continue
        raise AssertionError(f"должен был упасть: {bad}")


def test_character_build():
    char = rules.build_character("Торин", "fighter", "dwarf")
    assert char["abilities"]["str"] == 15 and char["abilities"]["con"] == 16
    assert char["max_hp"] == 13 and char["ac"] == 18
    assert rules.check_bonus(char, "Атлетика") == (4, "Атлетика")
    assert rules.check_bonus(char, "save:con")[0] == 5
    assert rules.attack_damage(char, char["attacks"][0]) == "1d8+2"


def test_full_scenario(monkeypatch):
    async def fake_srd(category, query):
        return GOBLIN if query == "goblin" else None
    monkeypatch.setattr(rules, "srd_find", fake_srd)
    db_path = os.path.join(tempfile.mkdtemp(), "dnd.sqlite3")

    async def scenario():
        h = Harness(db_path)
        await h.start()

        await h.say(VASYA, "/newgame")
        assert "Похмелье в Гнилом Броде" in h.last() and "Ограбление века" in h.last()
        await h.click(VASYA, "ng:hangover")
        assert h.state["status"] == "lobby" and h.state["scenario"] == "hangover"

        # Вася — все шаги по инструкции, характеристики вручную
        await h.say(VASYA, "/join")
        assert "Шаг 1" in h.last()
        await h.say(VASYA, "Торин")
        assert "Шаг 2" in h.last() and "Воин" in h.last()
        await h.click(VASYA, "cls:11:fighter")
        await h.click(PETYA, "race:11:elf")  # чужая кнопка не сработает
        await h.click(VASYA, "race:11:dwarf")
        assert "Шаг 4" in h.last()
        await h.click(VASYA, "st:11:manual")
        for ability in ("str", "con", "dex", "wis", "cha", "int"):
            await h.click(VASYA, f"ab:11:{ability}")
        assert "Шаг 5" in h.last()
        await h.say(VASYA, "Рыжий дварф с подбитым глазом, ворчун")
        await h.say(VASYA, "Был пивоваром, пока не сжёг пивоварню")
        # Петя — имя сразу, характеристики по рекомендации, без описания
        await h.say(PETYA, "/join Лиса")
        await h.click(PETYA, "cls:22:rogue")
        await h.click(PETYA, "race:22:halfling")
        await h.click(PETYA, "st:22:rec")
        await h.say(PETYA, "-")
        await h.say(PETYA, "-")
        torin = h.state["players"]["11"]["char"]
        assert torin["race"] == "dwarf" and torin["abilities"]["con"] == 16 and torin["max_hp"] == 13
        assert torin["look"].startswith("Рыжий") and "пивоварню" in torin["backstory"]
        assert h.state["players"]["22"]["char"]["class"] == "rogue"
        assert h.state["players"]["22"]["char"]["look"] == ""
        assert "Герой готов" in h.last()

        await h.say(VASYA, "/go")
        assert h.state["status"] == "active" and h.state["scene"]

        # Разговор вне игры не считается заявкой
        await h.say(VASYA, "// пиццу заказали?")
        await h.say(PETYA, "(я на минутку)")
        assert h.state["pending"] == {}

        # Ход 1: ответ, когда заявились оба
        await h.say(VASYA, "обыскиваю труп трактирщика")
        assert "11" in h.state["pending"]
        await h.say(PETYA, "болтаю с барменшей")
        assert h.state["pending"] == {}
        assert "ржавый ключ" in h.state["players"]["11"]["char"]["inventory"]
        assert h.state["players"]["22"]["char"]["gold"] == 15
        assert "Внимательность" in h.last() and "Убеждение" in h.last()
        # Акт сценария сменился, опыт за акт начислен, факт запомнен
        assert h.state["act"] == 2 and torin["xp"] == 150
        assert "Груня обещала 20 золотых" in h.state["plot_notes"]
        assert "Болотная тропа" in h.last()

        # Ход 2: только один игрок + /go
        await h.say(PETYA, "болтаю опять")
        await h.say(VASYA, "/go")
        assert h.state["pending"] == {}

        # Таймаут: заявка одного игрока разыгрывается сама
        await h.say(VASYA, "обыскиваю подвал")
        h.dnd.arm_timer(CHAT_ID, 0.05)
        await asyncio.sleep(0.2)
        assert h.state["pending"] == {}

        # Бой
        await h.say(VASYA, "бью гоблинов топором")
        await h.say(PETYA, "прячусь за стойкой")
        assert h.state["combat"] and len(h.state["monsters"]) == 2
        assert any("Ход:" in s for s in h.session.sent[-3:])
        calls_before_restart = h.llm.calls

        # Перезапуск посреди боя: новое приложение, та же БД
        h.dnd.cancel_timer(CHAT_ID)
        await h.dnd.storage.close()
        h2 = Harness(db_path)
        await h2.start()
        await h2.say(VASYA, "/status")
        assert "Бой" in h2.last()
        state = await h2.dnd.get(CHAT_ID)
        assert state["players"]["11"]["char"]["inventory"].get("ржавый ключ") == 2  # подвал тоже обыскали

        users = {"11": VASYA, "22": PETYA}
        from dnd.gm import current_actor
        for _ in range(60):
            if not state["combat"]:
                break
            entry = current_actor(state)
            key = entry["key"][2:]
            await h2.say(users[key], "рублю ближайшего гоблина")
            await asyncio.sleep(0.02)
        assert state["combat"] is None, "бой не закончился"
        assert "Бой окончен" in "\n".join(h2.session.sent)
        h2.dnd.cancel_timer(CHAT_ID)

        # /roll, /sheet, /pause, /endgame
        await h2.say(PETYA, "/roll 1d20+5 adv")
        assert "преим" in h2.last()
        await h2.say(PETYA, "/sheet")
        assert "Лиса" in h2.last()
        await h2.say(VASYA, "/sheet")
        assert "пивоварню" in h2.last()
        await h2.say(VASYA, "/me")
        assert "Сейчас играет: Торин" in h2.last() and "Подвиги: заявок" in h2.last()
        await h2.say(VASYA, "/log")
        assert "✍️ " in h2.last() and "Торин" in h2.last()
        await h2.say(VASYA, "/pause")
        assert state["status"] == "paused"
        await h2.say(VASYA, "обыскиваю всё подряд")
        assert state["pending"] == {}
        await h2.say(VASYA, "/pause")
        assert state["status"] == "active"
        await h2.say(VASYA, "/endgame")
        await h2.click(VASYA, "end:yes")
        assert state["status"] == "ended"
        await h2.say(VASYA, "/me")
        assert "Прошлые герои" in h2.last() and "Похмелье в Гнилом Броде" in h2.last()

        print("\n=== Последние сообщения бота ===\n" + h2.last(6))
        # DND_TRANSCRIPT=путь — сохранить всю переписку бота, чтобы глазами посмотреть форматирование
        if os.getenv("DND_TRANSCRIPT"):
            with open(os.environ["DND_TRANSCRIPT"], "w", encoding="utf-8") as f:
                f.write("\n\n#####\n".join(h.session.sent + ["===== РЕСТАРТ ====="] + h2.session.sent))
        print(f"\nВызовов ИИ: {calls_before_restart} до рестарта, {h2.llm.calls} после")
        await h2.dnd.storage.close()

    asyncio.run(scenario())


def test_scenarios():
    assert len(SCENARIOS) == 5
    for key, sc in SCENARIOS.items():
        assert len(sc["acts"]) == 4 and sc["monsters"] and sc["hook"]
        for act in (1, 3):
            block = scenario_block({"scenario": key, "act": act, "plot_notes": []})
            assert "▶ ТЕКУЩИЙ" in block and sc["acts"][act - 1]["details"] in block
            # Подробности чужих актов в промпт не попадают — экономим лимит
            other = sc["acts"][1 if act == 1 else 0]["details"]
            assert other not in block


KOLYA = User(id=33, is_bot=False, first_name="Коля", username="kolya")
MASHA = User(id=44, is_bot=False, first_name="Маша", username="masha")


def test_leave_and_return(monkeypatch):
    async def fake_srd(category, query):
        return GOBLIN if query == "goblin" else None
    monkeypatch.setattr(rules, "srd_find", fake_srd)
    db_path = os.path.join(tempfile.mkdtemp(), "dnd.sqlite3")

    async def make_hero(h, user, name):
        await h.say(user, f"/join {name}")
        await h.click(user, f"cls:{user.id}:random")
        await h.say(user, "-")
        await h.say(user, "-")

    async def scenario():
        h = Harness(db_path)
        await h.start()
        await h.say(VASYA, "/newgame Болото и тоска")
        for user, name in ((VASYA, "Торин"), (PETYA, "Лиса"), (KOLYA, "Гнида")):
            await make_hero(h, user, name)
        await h.say(VASYA, "/go")
        players = h.state["players"]

        # Коля не заявился и ушёл сам — партия не ждёт, ход разыгрывается сразу
        await h.say(VASYA, "болтаю с тиной")
        await h.say(PETYA, "болтаю с жабой")
        assert h.state["pending"]
        await h.say(KOLYA, "/leave")
        assert not players["33"]["active"] and h.state["pending"] == {}
        assert any("Гнида" in s and "покидает" in s for s in h.session.sent)
        assert "Гнида" in h.state["history"][-1]["actions"]  # ведущему сказали убрать героя из сцены
        # Пока ушёл сам, сообщения в чат не возвращают
        await h.say(KOLYA, "эй я тут")
        assert not players["33"]["active"]

        # Вернулся командой — ведущий введёт героя снова
        await h.say(KOLYA, "/back")
        assert players["33"]["active"] and "возвращается" in h.last()
        assert not players["33"]["char"]["introduced"]

        # Пропал без предупреждения: два хода молчит — бот перестаёт его ждать
        for _ in range(2):
            await h.say(VASYA, "болтаю с тиной")
            await h.say(PETYA, "болтаю с жабой")
            h.dnd.arm_timer(CHAT_ID, 0.05)
            await asyncio.sleep(0.2)
        assert not players["33"]["active"] and players["33"]["away"]
        assert any("💤" in s for s in h.session.sent)
        # Теперь Васи и Пети хватает, чтобы ход шёл без таймаута
        await h.say(VASYA, "болтаю с тиной")
        await h.say(PETYA, "болтаю с жабой")
        assert h.state["pending"] == {}

        # Отошедший возвращается, просто написав в чат, и его сообщение — уже заявка
        await h.say(KOLYA, "болтаю с комарами")
        assert players["33"]["active"] and h.state["pending"].get("33") == "болтаю с комарами"

        # Пометить пропавшего товарища
        await h.say(VASYA, "/afk лиса")
        assert not players["22"]["active"]
        await h.say(PETYA, "/back")
        assert players["22"]["active"]
        await h.say(VASYA, "/go")

        # Бой: тот, чей ход, уходит — очередь идёт дальше
        await h.say(VASYA, "бью гоблинов")
        await h.say(PETYA, "бью гоблинов")
        await h.say(KOLYA, "бью гоблинов")
        assert h.state["combat"]
        from dnd.gm import current_actor
        entry = current_actor(h.state)
        if entry and entry["key"].startswith("p:"):
            leaver = entry["key"][2:]
            await h.say({"11": VASYA, "22": PETYA, "33": KOLYA}[leaver], "/leave")
            if h.state["combat"]:
                after = current_actor(h.state)
                assert after is None or after["key"] != f"p:{leaver}"

        # Новый игрок посреди боя встаёт в очередь
        if h.state["combat"]:
            await make_hero(h, MASHA, "Мышь")
            assert any(o["key"] == "p:44" for o in h.state["combat"]["order"])
            assert "влетает в бой" in h.last()
        h.dnd.cancel_timer(CHAT_ID)

        await h.say(VASYA, "/status")
        await h.say(VASYA, "/help")
        assert "/afk" in h.last() and "/back" in h.last()
        await h.dnd.storage.close()

    asyncio.run(scenario())


def test_webhook():
    from aiohttp import web
    from aiohttp.test_utils import TestServer, TestClient

    async def scenario():
        h = Harness(os.path.join(tempfile.mkdtemp(), "dnd.sqlite3"))
        app = web.Application()
        h.dnd.mount_webhook(app, "https://dogs.onrender.com/")
        async with TestClient(TestServer(app)) as client:
            assert h.session.webhook.startswith("https://dogs.onrender.com/telegram/")
            path = "/" + h.session.webhook.split("/", 3)[3]
            update = {
                "update_id": 1,
                "message": {"message_id": 1, "date": int(datetime.datetime.now().timestamp()),
                            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Подземелье"},
                            "from": VASYA.model_dump(), "text": "/newgame",
                            "entities": [{"type": "bot_command", "offset": 0, "length": 8}]},
            }
            # Чужой запрос без секрета Telegram не проходит
            resp = await client.post(path, json=update)
            assert resp.status == 401
            resp = await client.post(path, json=update,
                                     headers={"X-Telegram-Bot-Api-Secret-Token": h.dnd.webhook_secret()})
            assert resp.status == 200
            await asyncio.sleep(0.2)
            assert "Похмелье в Гнилом Броде" in h.last()
            assert (await client.get("/")).status == 404  # health-эндпоинт добавляет main.py

    asyncio.run(scenario())
