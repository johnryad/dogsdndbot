"""Telegram-обвязка ДнД-ведущего: команды, сбор заявок, таймеры, очередь боя."""
import os
import re
import html
import json
import hashlib
import random
import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand

from . import rules
from .dice import roll, DiceError
from .gm import (GameMaster, new_state, characters, can_act, current_actor, advance, take_monster_run,
                 death_save, finish_combat, living_monsters, stat, join_combat)
from .llm import GeminiClient, LLMError
from .scenarios import SCENARIOS
from .storage import make_storage

TG_LIMIT = 4000
MAX_ACTION_LEN = 600
# Сколько ходов подряд игрок может молчать, прежде чем бот сочтёт, что он отошёл
AFK_LIMIT = 2

# Меню команд, которое Telegram показывает при вводе «/»
COMMANDS = [
    ("newgame", "новая партия: выбрать сценарий"),
    ("join", "создать героя или вернуться в игру"),
    ("go", "начать / ведущий, отвечай без остальных"),
    ("status", "партия, сцена, чей ход"),
    ("sheet", "лист героя"),
    ("me", "мой профиль и прошлые герои"),
    ("log", "мои последние действия"),
    ("roll", "бросок кубиков: /roll 1d20+5"),
    ("leave", "выйти из партии (герой подождёт)"),
    ("back", "вернуться в партию"),
    ("afk", "отметить отошедшего: /afk Имя"),
    ("pause", "пауза / продолжить"),
    ("timeout", "сколько ждать заявки, сек"),
    ("endgame", "эпилог и конец партии"),
    ("help", "все команды"),
]

FAIL_GM = [
    "🍺 Мастер отошёл за пивом и застрял в очереди",
    "🎲 Мастер уронил кубики под диван и полез за ними",
    "📜 Мастер ищет правило в книге и, кажется, уснул",
]

HELP = """<b>ДнД-ведущий — команды</b>

<b>Партия</b>
/newgame — новая партия: 5 сценариев кнопками или свой мир (<code>/newgame мир и тон</code>)
/go — начать приключение; потом — ведущий отвечает, не дожидаясь остальных
/pause — пауза и продолжить, /endgame — эпилог и конец

<b>Герой</b>
/join — создать героя, бот проведёт по шагам
/sheet — лист героя, /me — профиль и прошлые герои, /log — твои последние действия

<b>Пришёл, ушёл</b>
/leave — выйти посреди игры, партия идёт дальше, герой ждёт
/back или /join — вернуться в любой момент, даже посреди боя
/afk Имя — товарищ пропал? Отметь, и партия не будет его ждать
Кто молчит 2 хода подряд, того бот сам считает отошедшим; вернётся, когда напишет в чат.

<b>Как играть</b>
Пишите в чат, что делает герой. Ведущий отвечает, когда заявились все, по /go или через 2 минуты (/timeout).
В бою ходят по инициативе: бот позовёт того, чей ход.
<code>//</code> или <code>(</code> в начале — разговор вне игры, ведущий не читает.
/roll 1d20+5 adv — бросок с преимуществом (dis — с помехой), /status — что происходит"""


def fmt(text: str) -> str:
    """Экранирует текст ИИ и превращает **жирный** и *курсив* в HTML."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    return text


def split_message(text: str) -> list[str]:
    chunks = []
    while len(text) > TG_LIMIT:
        cut = text.rfind("\n", 0, TG_LIMIT)
        if cut < TG_LIMIT // 2:
            cut = text.rfind(" ", 0, TG_LIMIT)
        if cut <= 0:
            cut = TG_LIMIT
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    chunks.append(text)
    return [c for c in chunks if c.strip()]


def mention(player_key: str, name: str) -> str:
    return f'<a href="tg://user?id={player_key}">{html.escape(name)}</a>'


def clean_name(name: str) -> str:
    name = re.sub(r"[<>&\"*_`\[\]]", "", name or "").strip()
    return name[:40]


class DndBot:
    def __init__(self, token: str, llm, storage, allowed_chats: set[int] | None = None, session=None):
        self.bot = Bot(token=token, session=session) if session else Bot(token=token)
        self.dp = Dispatcher()
        self.gm = GameMaster(llm)
        self.storage = storage
        self.allowed_chats = allowed_chats
        self.states: dict[int, dict] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.timers: dict[int, asyncio.Task] = {}
        self.router = Router()
        self._register()
        self.dp.include_router(self.router)

    # --- состояние ---

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(chat_id, asyncio.Lock())

    async def get(self, chat_id: int) -> dict | None:
        if chat_id not in self.states:
            state = await self.storage.load(chat_id)
            if state is None:
                return None
            self.states[chat_id] = state
        return self.states[chat_id]

    async def save(self, chat_id: int):
        state = self.states.get(chat_id)
        if state is not None:
            await self.storage.save(chat_id, state)

    def allowed(self, chat_id: int) -> bool:
        return not self.allowed_chats or chat_id in self.allowed_chats

    async def send(self, chat_id: int, text: str, **kwargs):
        for chunk in split_message(text):
            await self.bot.send_message(chat_id, chunk, parse_mode="HTML", disable_web_page_preview=True, **kwargs)

    async def chronicle(self, chat_id: int, kind: str, text: str, user_id: int | None = None, char: str = ""):
        """Журнал всего, что было в игре. Ошибка записи не должна ломать игру."""
        state = self.states.get(chat_id) or {}
        try:
            await self.storage.log(chat_id, user_id, char, state.get("round", 0), kind, text)
        except Exception as e:
            logging.warning(f"ДнД: не записал в журнал: {e}")

    async def send_mech(self, chat_id: int, lines: list[str]):
        for line in lines:
            await self.chronicle(chat_id, "mech", line.replace("**", ""))
        await self.send(chat_id, "\n".join(fmt(line) for line in lines))

    async def send_gm(self, chat_id: int, narrative: str, log: list[str]):
        await self.chronicle(chat_id, "gm", narrative)
        for line in log:
            await self.chronicle(chat_id, "mech", line.replace("**", ""))
        text = fmt(narrative)
        if log:
            text += "\n\n<blockquote expandable>" + "\n".join(fmt(line) for line in log) + "</blockquote>"
        await self.send(chat_id, text)

    # --- таймеры ---

    def cancel_timer(self, chat_id: int):
        task = self.timers.pop(chat_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    def arm_timer(self, chat_id: int, seconds: int):
        self.cancel_timer(chat_id)
        self.timers[chat_id] = asyncio.create_task(self._timer(chat_id, seconds))

    async def _timer(self, chat_id: int, seconds: int):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self.timers.pop(chat_id, None)
        try:
            state = await self.get(chat_id)
            if not state or state["status"] != "active":
                return
            if state["combat"]:
                entry = current_actor(state)
                if entry and entry["key"].startswith("p:"):
                    await self.combat_turn(chat_id, entry["key"][2:], None)
            elif state["pending"]:
                await self.resolve_round(chat_id)
        except Exception:
            logging.exception("ДнД: ошибка в таймере")

    async def restore(self):
        """После рестарта: поднимаем таймеры для партий, где ждали заявок."""
        for chat_id in await self.storage.active_chats():
            state = await self.get(chat_id)
            if not state or state["status"] != "active":
                continue
            if state["combat"] or state["pending"]:
                self.arm_timer(chat_id, state["timeout"])
                logging.info(f"ДнД: восстановил партию в чате {chat_id}")

    # --- розыгрыш ---

    async def safe_narrate(self, chat_id: int, state: dict, turn_text: str, use_tools: bool = True):
        """Ход ведущего с откатом состояния, если ИИ упал посреди хода."""
        snapshot = json.loads(json.dumps(state))
        try:
            await self.bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        try:
            return await self.gm.narrate(state, turn_text, use_tools)
        except LLMError as e:
            logging.error(f"ДнД: ИИ не ответил в чате {chat_id}: {e}")
            state.clear()
            state.update(snapshot)
            await self.send(chat_id, f"{random.choice(FAIL_GM)}. Заявки сохранены — попробуйте /go через минуту.")
            return None

    def newcomers_note(self, state: dict) -> str:
        fresh = [c for c in characters(state).values() if not c.get("introduced")]
        for c in fresh:
            c["introduced"] = True
        if not fresh:
            return ""
        return ("\nНовые или вернувшиеся персонажи (введи их в сцену: догнали группу, вылезли из кустов, "
                "очнулись в канаве): " + ", ".join(rules.brief_for_prompt(c) for c in fresh))

    async def start_adventure(self, chat_id: int):
        async with self.lock(chat_id):
            state = await self.get(chat_id)
            if not state or state["status"] != "lobby":
                return
            state["status"] = "active"
            for c in characters(state).values():
                c["introduced"] = True
            turn = ("Начни приключение. Коротко и ярко представь мир по сеттингу, сведи всех персонажей партии "
                    "в первой сцене (используй их облик и предыстории), дай каждому по подколке, запиши сцену через "
                    "update_scene и закончи ситуацией, требующей действий.")
            sc = SCENARIOS.get(state.get("scenario") or "")
            if sc:
                turn += f" Завязка сценария: {sc['hook']}"
            result = await self.safe_narrate(chat_id, state, turn)
            if result is None:
                state["status"] = "lobby"
                return
            await self.send_gm(chat_id, result[0], result[1])
            await self.save(chat_id)

    async def resolve_round(self, chat_id: int):
        """Вне боя: ведущий отвечает на все заявки разом."""
        async with self.lock(chat_id):
            state = await self.get(chat_id)
            if not state or state["status"] != "active" or state["combat"]:
                return
            self.cancel_timer(chat_id)
            pending = state["pending"]
            if not pending:
                return
            chars = characters(state)
            lines = ["Заявки игроков:"]
            for key, text in pending.items():
                if key in chars:
                    lines.append(f"- {chars[key]['name']}: «{text}»")
            silent_keys = [k for k, c in chars.items() if k not in pending and can_act(c)]
            silent = [chars[k]["name"] for k in silent_keys]
            if silent:
                lines.append(f"Промолчали (просто следуют за группой): {', '.join(silent)}")
            gone = self.departed_note(state)
            if gone:
                lines.append(gone)
            turn = "\n".join(lines) + self.newcomers_note(state)
            state["pending"] = {}
            state["round"] += 1
            result = await self.safe_narrate(chat_id, state, turn)
            if result is None:
                state["pending"] = pending
                await self.save(chat_id)
                return
            text, log, combat_started = result
            await self.send_gm(chat_id, text, log)
            for key in silent_keys:
                await self.count_missed(chat_id, state, key)
            if combat_started and state["combat"]:
                await self.run_combat(chat_id, state)
            await self.save(chat_id)
            await self.gm.compress(state)
            await self.save(chat_id)

    async def combat_turn(self, chat_id: int, player_key: str, action: str | None):
        """Ход персонажа в бою и следом — ходы монстров до следующего персонажа."""
        async with self.lock(chat_id):
            state = await self.get(chat_id)
            if not state or state["status"] != "active" or not state["combat"]:
                return
            entry = current_actor(state)
            if not entry or entry["key"] != f"p:{player_key}":
                return
            self.cancel_timer(chat_id)
            char = state["players"][player_key]["char"]
            state["pending"].pop(player_key, None)
            missed = action is None
            if missed:
                action = "(игрок задумался и не успел — персонаж занимает оборону: действие Уклонение)"
            snapshot = json.loads(json.dumps(state))
            advance(state)
            monsters = take_monster_run(state)
            turn = f"Раунд {state['combat']['round']}. Ход персонажа {char['name']}: «{action}»."
            turn += self.monsters_note(state, monsters)
            gone = self.departed_note(state)
            if gone:
                turn += "\n" + gone
            result = await self.safe_narrate(chat_id, state, turn)
            if result is None:
                state.clear()
                state.update(snapshot)
                state["pending"][player_key] = action
                await self.save(chat_id)
                return
            await self.send_gm(chat_id, result[0], result[1])
            if missed:
                await self.count_missed(chat_id, state, player_key)
            else:
                state["players"][player_key]["missed"] = 0
            await self.run_combat(chat_id, state)
            await self.save(chat_id)

    # --- пришёл, ушёл ---

    async def count_missed(self, chat_id: int, state: dict, key: str):
        """Игрок пропустил ход. После AFK_LIMIT пропусков подряд партия перестаёт его ждать."""
        player = state["players"][key]
        player["missed"] = player.get("missed", 0) + 1
        if player["missed"] >= AFK_LIMIT and player.get("active", True):
            self.set_inactive(state, key, away=True)
            await self.chronicle(chat_id, "leave", f"{player['name']} молчит — отмечен отошедшим", int(key),
                                 player["char"]["name"])
            await self.send(chat_id, f"💤 {mention(key, player['name'])} молчит {player['missed']} хода — "
                                     f"<b>{html.escape(player['char']['name'])}</b> отстаёт от группы, партия идёт "
                                     "дальше. Вернуться — просто написать в чат или /back.")

    def set_inactive(self, state: dict, key: str, away: bool):
        player = state["players"][key]
        player["active"] = False
        player["away"] = away
        state["pending"].pop(key, None)
        if player.get("char"):
            state.setdefault("departed", []).append(player["char"]["name"])

    def departed_note(self, state: dict) -> str:
        """Кто ушёл из партии с прошлого хода — чтобы ведущий убрал их из сцены."""
        gone = state.pop("departed", [])
        if not gone:
            return ""
        return (f"Из партии ушли (игроки отошли): {', '.join(gone)}. Убери этих героев из сцены одной фразой "
                "(отстали, отвлеклись, ушли по делам) и больше за них не играй.")

    async def leave(self, chat_id: int, key: str, away: bool, by: str):
        """Уход посреди игры: партия не ждёт ушедшего, его ход в бою пропускается."""
        async with self.lock(chat_id):
            state = await self.get(chat_id)
            player = state["players"][key]
            char = player.get("char")
            was_current = bool(state["combat"]) and (current_actor(state) or {}).get("key") == f"p:{key}"
            self.set_inactive(state, key, away)
            await self.chronicle(chat_id, "leave", f"{player['name']} вышел ({by})", int(key), (char or {}).get("name", ""))
            hero = f"<b>{html.escape(char['name'])}</b>" if char else "Игрок"
            back = "вернётся, когда напишет в чат или /back" if away else "вернуться — /back или /join"
            await self.send(chat_id, f"🚪 {hero} покидает партию ({html.escape(by)}). Играем дальше без него, {back}.")
            if was_current and state["status"] == "active":
                self.cancel_timer(chat_id)
                await self.run_combat(chat_id, state)
            await self.save(chat_id)
        if not state["combat"] and state["status"] == "active" and state["pending"]:
            waiting = [k for k, c in characters(state).items() if can_act(c) and k not in state["pending"]]
            if not waiting:
                await self.resolve_round(chat_id)

    async def come_back(self, chat_id: int, state: dict, key: str) -> str:
        """Возвращает героя в партию; посреди боя — с броском инициативы."""
        player = state["players"][key]
        char = player["char"]
        player.update(active=True, away=False, missed=0)
        # Ведущий снова введёт героя в сцену
        char["introduced"] = False
        note = join_combat(state, key) if state["combat"] else None
        await self.chronicle(chat_id, "join", f"{player['name']} вернулся", int(key), char["name"])
        await self.save(chat_id)
        text = f"👋 <b>{html.escape(char['name'])}</b> возвращается в партию."
        if note:
            text += "\n" + fmt(note)
        elif state["status"] == "active" and not state["combat"]:
            text += " Пиши, что делает герой."
        return text

    def monsters_note(self, state: dict, monsters: list[dict]) -> str:
        alive = [m for m in monsters if not state["monsters"].get(m["key"][2:], {}).get("dead")]
        if not alive or not state["combat"]:
            return ""
        ids = ", ".join(f"{m['key'][2:]} ({m['name']})" for m in alive)
        return (f"\nЗатем ходят монстры: {ids}. Каждый действует по-своему разумно: атакует через attack "
                f"(выбери цель), убегает или делает пакость. Ходы персонажей не описывай.")

    async def run_combat(self, chat_id: int, state: dict):
        """Крутит очередь до хода живого персонажа: ходы монстров, спасброски от смерти, конец боя."""
        for _ in range(30):
            combat = state["combat"]
            if not combat:
                return
            if not living_monsters(state):
                log: list[str] = []
                finish_combat(state, log, "враги повержены")
                await self.send_mech(chat_id, log)
                return
            conscious = [c for c in characters(state).values() if can_act(c)]
            if not conscious:
                log = []
                finish_combat(state, log, "партия пала")
                result = await self.safe_narrate(
                    chat_id, state,
                    "Все персонажи без сознания или мертвы, бой проигран. Реши судьбу партии по логике сцены "
                    "(плен, ограбление, издевательства, смерть для мёртвых) и опиши, где они очнутся.")
                if result:
                    await self.send_gm(chat_id, result[0], log + result[1])
                return
            entry = current_actor(state)
            if entry is None:
                return
            if entry["key"].startswith("m:"):
                monsters = take_monster_run(state)
                gone = self.departed_note(state)
                result = await self.safe_narrate(
                    chat_id, state, f"Раунд {combat['round']}." + self.monsters_note(state, monsters)
                    + (f"\n{gone}" if gone else ""))
                if result is None:
                    return
                await self.send_gm(chat_id, result[0], result[1])
                continue
            player_key = entry["key"][2:]
            char = state["players"][player_key]["char"]
            if char["hp"] <= 0:
                advance(state)
                if not char["death"]["stable"]:
                    await self.send_mech(chat_id, [death_save(char)])
                continue
            name = state["players"][player_key]["name"]
            await self.send(
                chat_id,
                f"⚔️ Раунд {combat['round']}. Ход: <b>{html.escape(char['name'])}</b> ({mention(player_key, name)}), "
                f"❤️ {char['hp']}/{char['max_hp']}. У тебя {state['timeout']} сек.")
            if state["pending"].get(player_key):
                # Игрок заявил действие заранее — разыгрываем сразу после выхода из лока
                asyncio.create_task(self.combat_turn(chat_id, player_key, state["pending"][player_key]))
            else:
                self.arm_timer(chat_id, state["timeout"])
            return

    # --- обработчики ---

    def _register(self):
        r = self.router
        r.message.register(self.cmd_help, Command("help", "start"))
        r.message.register(self.cmd_newgame, Command("newgame"))
        r.message.register(self.cmd_join, Command("join"))
        r.message.register(self.cmd_sheet, Command("sheet"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_go, Command("go"))
        r.message.register(self.cmd_pause, Command("pause", "resume"))
        r.message.register(self.cmd_endgame, Command("endgame"))
        r.message.register(self.cmd_roll, Command("roll", "r"))
        r.message.register(self.cmd_timeout, Command("timeout"))
        r.message.register(self.cmd_leave, Command("leave"))
        r.message.register(self.cmd_back, Command("back"))
        r.message.register(self.cmd_afk, Command("afk"))
        r.message.register(self.cmd_me, Command("me", "profile"))
        r.message.register(self.cmd_log, Command("log"))
        r.message.register(self.on_text, F.text)
        r.callback_query.register(self.on_setting, F.data.startswith("ng:"))
        r.callback_query.register(self.on_class, F.data.startswith("cls:"))
        r.callback_query.register(self.on_race, F.data.startswith("race:"))
        r.callback_query.register(self.on_stats, F.data.startswith("st:"))
        r.callback_query.register(self.on_ability, F.data.startswith("ab:"))
        r.callback_query.register(self.on_endgame, F.data.startswith("end:"))

    async def cmd_help(self, message: Message):
        await message.answer(HELP, parse_mode="HTML")

    async def cmd_newgame(self, message: Message, command: CommandObject):
        if not self.allowed(message.chat.id):
            return
        state = await self.get(message.chat.id)
        if state and state["status"] in ("lobby", "active", "paused"):
            await message.answer("Партия уже идёт. Сначала /endgame — похороним эту как следует.")
            return
        if command.args:
            await self.create_game(message.chat.id, command.args.strip()[:500])
            return
        lines = ["<b>Выбирайте сценарий:</b>"]
        for i, sc in enumerate(SCENARIOS.values(), 1):
            lines.append(f"{i}. <b>{sc['title']}</b> — {sc['pitch']}")
        lines.append("\nИли свой мир без сценария: <code>/newgame мир и тон</code> — ведущий будет импровизировать.")
        buttons = [[InlineKeyboardButton(text=f"{i}. {sc['title']}", callback_data=f"ng:{key}")]
                   for i, (key, sc) in enumerate(SCENARIOS.items(), 1)]
        await message.answer("\n".join(lines), parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    async def on_setting(self, query: CallbackQuery):
        key = query.data.split(":", 1)[1]
        state = await self.get(query.message.chat.id)
        if key not in SCENARIOS or (state and state["status"] in ("lobby", "active", "paused")):
            await query.answer("Поздно, партия уже создана")
            return
        await query.answer()
        await query.message.edit_reply_markup(reply_markup=None)
        await self.create_game(query.message.chat.id, SCENARIOS[key]["setting"], key)

    async def create_game(self, chat_id: int, setting: str, scenario: str | None = None):
        async with self.lock(chat_id):
            old = await self.get(chat_id)
            state = new_state(setting, scenario)
            if old:
                state["timeout"] = old.get("timeout", 120)
            self.states[chat_id] = state
            await self.save(chat_id)
        logging.info(f"ДнД: новая партия в чате {chat_id}")
        title = f"«{SCENARIOS[scenario]['title']}»" if scenario else "свободная импровизация"
        await self.send(chat_id, f"🎲 Новая партия: <b>{html.escape(title)}</b>\n<i>{html.escape(setting)}</i>\n\n"
                                 "Каждый игрок — /join, бот проведёт по созданию героя. Когда все готовы — /go.")

    async def cmd_join(self, message: Message, command: CommandObject):
        state = await self.get(message.chat.id)
        if not state or state["status"] == "ended":
            await message.answer("Сначала /newgame.")
            return
        key = str(message.from_user.id)
        player = state["players"].get(key)
        if player and player.get("char") and not player["char"]["death"]["dead"]:
            if player.get("active", True):
                await message.answer(f"Ты уже в игре как {html.escape(player['char']['name'])}. /sheet — лист.",
                                     parse_mode="HTML")
                return
            async with self.lock(message.chat.id):
                text = await self.come_back(message.chat.id, state, key)
            await self.send(message.chat.id, text)
            return
        if player and player.get("char"):
            await self.archive_hero(message.chat.id, state, key, "погиб")
        state["players"][key] = {"name": message.from_user.first_name or message.from_user.username or "Игрок",
                                 "username": message.from_user.username,
                                 "char": None, "active": True, "draft": {"step": "name"}}
        name = clean_name(command.args or "")
        if name:
            await self.ask_class(message, state, key, name)
        else:
            await message.reply(
                "Создаём героя, шагов пять:\n1) имя  2) класс  3) раса  4) характеристики  5) облик и предыстория\n\n"
                "<b>Шаг 1.</b> Как зовут героя? Напиши имя следующим сообщением.", parse_mode="HTML")
        await self.save(message.chat.id)

    async def ask_class(self, message: Message, state: dict, key: str, name: str):
        state["players"][key]["draft"] = {"step": "class", "name": name}
        rows, row = [], []
        for cls_key, cls in rules.CLASSES.items():
            row.append(InlineKeyboardButton(text=cls["name"], callback_data=f"cls:{key}:{cls_key}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        rows.append(row + [InlineKeyboardButton(text="🎲 Случайно", callback_data=f"cls:{key}:random")])
        about = "\n".join(f"• <b>{c['name']}</b> — {c['about']}" for c in rules.CLASSES.values())
        await message.reply(f"<b>Шаг 2.</b> {html.escape(name)}, кем будешь?\n\n{about}\n\n"
                            "«Случайно» — класс и раса наугад, характеристики по рекомендации.",
                            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def on_class(self, query: CallbackQuery):
        _, key, cls_key = query.data.split(":")
        if str(query.from_user.id) != key:
            await query.answer("Это не твой персонаж, руки прочь")
            return
        state = await self.get(query.message.chat.id)
        player = (state or {}).get("players", {}).get(key)
        if not player or not player.get("draft") or player["draft"].get("step") != "class":
            await query.answer("Устарело")
            return
        await query.answer()
        if cls_key == "random":
            cls_key = random.choice(list(rules.CLASSES))
            race_key = random.choice(list(rules.RACES))
            player["draft"].update(cls=cls_key, race=race_key, base=rules.recommended_scores(cls_key))
            await query.message.edit_text(
                f"🎲 Судьба решила: {rules.RACES[race_key]['name']}, {rules.CLASSES[cls_key]['name']}.")
            await self.ask_look(query.message.chat.id, player)
            await self.save(query.message.chat.id)
            return
        player["draft"].update(step="race", cls=cls_key)
        buttons, row = [], []
        for race_key, race in rules.RACES.items():
            row.append(InlineKeyboardButton(text=race["name"], callback_data=f"race:{key}:{race_key}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        about = "\n".join(f"• <b>{r['name']}</b> — {r['about']}" for r in rules.RACES.values())
        await query.message.edit_text(
            f"<b>Шаг 3.</b> {html.escape(player['draft']['name'])} — {rules.CLASSES[cls_key]['name']}. А раса?\n\n{about}",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await self.save(query.message.chat.id)

    async def on_race(self, query: CallbackQuery):
        _, key, race_key = query.data.split(":")
        if str(query.from_user.id) != key:
            await query.answer("Это не твой персонаж, руки прочь")
            return
        state = await self.get(query.message.chat.id)
        player = (state or {}).get("players", {}).get(key)
        draft = (player or {}).get("draft") or {}
        if draft.get("step") != "race" or race_key not in rules.RACES:
            await query.answer("Устарело")
            return
        await query.answer()
        draft.update(step="stats", race=race_key)
        rec = rules.recommended_scores(draft["cls"])
        rec_text = ", ".join(f"{rules.ABILITIES[a]} {v}" for a, v in sorted(rec.items(), key=lambda x: -x[1]))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚡ Рекомендовано", callback_data=f"st:{key}:rec"),
            InlineKeyboardButton(text="✋ Распределю сам", callback_data=f"st:{key}:manual"),
        ]])
        await query.message.edit_text(
            f"<b>Шаг 4.</b> Характеристики. Стандартный набор чисел: 15, 14, 13, 12, 10, 8 — каждое число "
            f"ставится на одну характеристику, потом добавляются бонусы расы ({rules.RACES[race_key]['about']}).\n\n"
            f"Рекомендовано для класса {rules.CLASSES[draft['cls']]['name']}: {rec_text}.",
            parse_mode="HTML", reply_markup=kb)
        await self.save(query.message.chat.id)

    async def on_stats(self, query: CallbackQuery):
        _, key, mode = query.data.split(":")
        player = await self.own_draft(query, key, "stats")
        if not player:
            return
        draft = player["draft"]
        if mode == "rec":
            draft["base"] = rules.recommended_scores(draft["cls"])
            await query.message.edit_reply_markup(reply_markup=None)
            await self.ask_look(query.message.chat.id, player)
        else:
            draft.update(step="assign", base={})
            await self.ask_ability(query.message, key, draft)
        await self.save(query.message.chat.id)

    async def ask_ability(self, message: Message, key: str, draft: dict):
        value = rules.STANDARD_ARRAY[len(draft["base"])]
        free = [a for a in rules.ABILITIES if a not in draft["base"]]
        buttons = [[InlineKeyboardButton(text=rules.ABILITIES[a], callback_data=f"ab:{key}:{a}") for a in free[i:i + 3]]
                   for i in range(0, len(free), 3)]
        done = ", ".join(f"{rules.ABILITIES[a]} {v}" for a, v in draft["base"].items()) or "пока ничего"
        await message.edit_text(f"Уже: {done}.\nКуда ставим <b>{value}</b>?", parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    async def on_ability(self, query: CallbackQuery):
        _, key, ability = query.data.split(":")
        player = await self.own_draft(query, key, "assign")
        if not player:
            return
        draft = player["draft"]
        if ability in draft["base"] or ability not in rules.ABILITIES:
            return
        draft["base"][ability] = rules.STANDARD_ARRAY[len(draft["base"])]
        if len(draft["base"]) < len(rules.ABILITIES):
            await self.ask_ability(query.message, key, draft)
        else:
            await query.message.edit_text("Характеристики: " + ", ".join(
                f"{rules.ABILITIES[a]} {v}" for a, v in draft["base"].items()))
            await self.ask_look(query.message.chat.id, player)
        await self.save(query.message.chat.id)

    async def own_draft(self, query: CallbackQuery, key: str, step: str) -> dict | None:
        """Игрок, чья это кнопка, если он сейчас на нужном шаге создания."""
        if str(query.from_user.id) != key:
            await query.answer("Это не твой персонаж, руки прочь")
            return None
        state = await self.get(query.message.chat.id)
        player = (state or {}).get("players", {}).get(key)
        if not player or (player.get("draft") or {}).get("step") != step:
            await query.answer("Устарело")
            return None
        await query.answer()
        return player

    async def ask_look(self, chat_id: int, player: dict):
        player["draft"]["step"] = "look"
        await self.send(chat_id, f"<b>Шаг 5.</b> {html.escape(player['name'])}, опиши облик и характер героя "
                                 "одним-двумя предложениями (как выглядит, какой у него нрав). Ведущий это запомнит "
                                 "и будет использовать. Не хочешь — напиши «-».")

    async def finish_char(self, chat_id: int, key: str):
        state = await self.get(chat_id)
        player = state["players"][key]
        d = player["draft"]
        char = rules.build_character(d["name"], d["cls"], d["race"], d.get("base"),
                                     d.get("look", ""), d.get("backstory", ""))
        # Новичок посреди приключения — ведущий представит его в следующем ходе
        char["introduced"] = state["status"] == "lobby"
        player["char"] = char
        player["draft"] = None
        note = join_combat(state, key) if state["combat"] else None
        await self.save(chat_id)
        await self.touch_profile(int(key), player)
        await self.chronicle(chat_id, "join", f"{player['name']} создал героя: {rules.brief_for_prompt(char)}",
                             int(key), char["name"])
        await self.send(chat_id, "✅ Герой готов:\n" + rules.sheet_text(char) + (f"\n\n{fmt(note)}" if note else ""))

    async def cmd_sheet(self, message: Message, command: CommandObject):
        state = await self.get(message.chat.id)
        if not state:
            return
        if command.args:
            target = command.args.strip().lower()
            chars = [c for c in characters(state).values() if target in c["name"].lower()]
        else:
            player = state["players"].get(str(message.from_user.id))
            chars = [player["char"]] if player and player.get("char") else []
        if not chars:
            await message.answer("Персонажа нет. /join Имя")
            return
        await message.answer(rules.sheet_text(chars[0]), parse_mode="HTML")

    async def cmd_status(self, message: Message):
        state = await self.get(message.chat.id)
        if not state:
            await message.answer("Партии нет. /newgame")
            return
        status = {"lobby": "сбор партии", "active": "идёт", "paused": "на паузе", "ended": "окончена"}
        lines = [f"<b>Партия {status.get(state['status'], state['status'])}</b>, раунд {state['round']}, "
                 f"таймаут {state['timeout']} сек."]
        if state["scene"]:
            lines.append(f"📍 {html.escape(state['scene'])}")
        for key, player in state["players"].items():
            char = player.get("char")
            if not char:
                continue
            if not player.get("active", True):
                lines.append(f"💤 {html.escape(char['name'])} ({html.escape(player['name'])}): не в партии, /back")
                continue
            mark = "✍️" if key in state["pending"] else "…"
            if char["death"]["dead"]:
                mark = "☠️"
            lines.append(f"{mark} {html.escape(char['name'])} ({html.escape(player['name'])}): "
                         f"❤️ {char['hp']}/{char['max_hp']}, КД {char['ac']}")
        if state["combat"]:
            entry = current_actor(state)
            lines.append(f"⚔️ Бой, раунд {state['combat']['round']}, ходит: {html.escape(entry['name'] if entry else '?')}")
            for mid, m in state["monsters"].items():
                if not m.get("dead"):
                    lines.append(f"  👹 {html.escape(m['name'])}: {m['hp']}/{m['max_hp']}")
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_go(self, message: Message):
        state = await self.get(message.chat.id)
        if not state:
            await message.answer("Партии нет. /newgame")
            return
        if state["status"] == "lobby":
            if not characters(state):
                await message.answer("Некого вести. Хоть кто-нибудь — /join")
                return
            await self.start_adventure(message.chat.id)
        elif state["status"] == "active":
            if state["combat"]:
                entry = current_actor(state)
                if entry and entry["key"].startswith("p:"):
                    # В бою /go — пропустить зависшего игрока
                    await self.combat_turn(message.chat.id, entry["key"][2:], None)
            elif state["pending"]:
                await self.resolve_round(message.chat.id)
            else:
                await message.answer("Никто ничего не заявил. Ведущий не телепат, хотя и хотел бы.")
        elif state["status"] == "paused":
            await message.answer("Пауза. /pause — продолжить.")

    async def cmd_pause(self, message: Message):
        state = await self.get(message.chat.id)
        if not state or state["status"] not in ("active", "paused"):
            return
        if state["status"] == "active":
            state["status"] = "paused"
            self.cancel_timer(message.chat.id)
            await message.answer("⏸ Пауза. Персонажи замерли в нелепых позах. /pause — продолжить.")
        else:
            state["status"] = "active"
            await message.answer("▶️ Продолжаем.")
            if state["combat"]:
                async with self.lock(message.chat.id):
                    await self.run_combat(message.chat.id, state)
            elif state["pending"]:
                self.arm_timer(message.chat.id, state["timeout"])
        await self.save(message.chat.id)

    async def cmd_endgame(self, message: Message):
        state = await self.get(message.chat.id)
        if not state or state["status"] == "ended":
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, эпилог", callback_data="end:yes"),
            InlineKeyboardButton(text="Нет", callback_data="end:no"),
        ]])
        await message.answer("Точно закончить партию?", reply_markup=kb)

    async def on_endgame(self, query: CallbackQuery):
        chat_id = query.message.chat.id
        await query.answer()
        await query.message.edit_reply_markup(reply_markup=None)
        if query.data != "end:yes":
            await query.message.edit_text("Живём дальше.")
            return
        async with self.lock(chat_id):
            state = await self.get(chat_id)
            if not state or state["status"] == "ended":
                return
            self.cancel_timer(chat_id)
            if state["status"] != "lobby" and state["history"]:
                state["combat"] = None
                result = await self.safe_narrate(
                    chat_id, state,
                    "Партия заканчивается. Напиши ядовитый эпилог: что стало с каждым персонажем и миром после них.",
                    use_tools=False)
                if result:
                    await self.send_gm(chat_id, result[0], [])
            state["status"] = "ended"
            for key, player in state["players"].items():
                if player.get("char"):
                    fate = "погиб" if player["char"]["death"]["dead"] else "дожил до эпилога"
                    await self.archive_hero(chat_id, state, key, fate)
            await self.save(chat_id)
        await self.send(chat_id, "🏁 Партия окончена. Герои записаны в профили (/me). /newgame — новая.")

    # --- профили и журнал ---

    async def touch_profile(self, user_id: int, player: dict) -> dict:
        try:
            profile = await self.storage.load_profile(user_id) or {"heroes": []}
            profile.update(name=player["name"], username=player.get("username"))
            await self.storage.save_profile(user_id, profile)
            return profile
        except Exception as e:
            logging.warning(f"ДнД: профиль {user_id} не обновился: {e}")
            return {"heroes": []}

    async def archive_hero(self, chat_id: int, state: dict, key: str, fate: str):
        """Переносит героя в профиль игрока: кем был, чем кончил, что натворил."""
        char = state["players"][key].get("char")
        if not char or char.get("archived"):
            return
        char["archived"] = True
        sc = SCENARIOS.get(state.get("scenario") or "")
        record = {
            "name": char["name"], "class": char["class"], "race": char["race"], "level": char["level"],
            "xp": char["xp"], "fate": fate, "scenario": sc["title"] if sc else "свой мир",
            "act": state.get("act", 1), "stats": char.get("stats", {}),
        }
        try:
            profile = await self.storage.load_profile(int(key)) or {"heroes": []}
            profile.setdefault("heroes", []).append(record)
            await self.storage.save_profile(int(key), profile)
        except Exception as e:
            logging.warning(f"ДнД: не сохранил героя в профиль: {e}")

    async def cmd_me(self, message: Message):
        user_id = message.from_user.id
        state = await self.get(message.chat.id)
        player = (state or {}).get("players", {}).get(str(user_id))
        lines = [f"<b>Профиль {html.escape(message.from_user.first_name or 'игрока')}</b>"]
        char = (player or {}).get("char")
        if char and not char.get("archived"):
            lines.append(f"\nСейчас играет: {html.escape(char['name'])}, {rules.CLASSES[char['class']]['name']} "
                         f"{char['level']} ур., ❤️ {char['hp']}/{char['max_hp']}")
            lines.append(f"Подвиги: {rules.stats_text(char.get('stats', {}))}")
        profile = await self.storage.load_profile(user_id)
        heroes = (profile or {}).get("heroes", [])
        if heroes:
            lines.append("\n<b>Прошлые герои:</b>")
            for h in heroes[-10:]:
                lines.append(f"• {html.escape(h['name'])} — {rules.RACES[h['race']]['name']} "
                             f"{rules.CLASSES[h['class']]['name']} {h['level']} ур., «{html.escape(h['scenario'])}», "
                             f"{h['fate']}. {rules.stats_text(h.get('stats', {}))}")
        elif not char:
            lines.append("Героев пока не было. /join")
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_log(self, message: Message):
        state = await self.get(message.chat.id)
        player = (state or {}).get("players", {}).get(str(message.from_user.id))
        char = (player or {}).get("char")
        if not char:
            await message.answer("Героя нет — и вспоминать нечего. /join")
            return
        rows = await self.storage.recent_log(message.chat.id, message.from_user.id, char["name"], 20)
        if not rows:
            await message.answer("Пока пусто.")
            return
        lines = [f"<b>Последнее, что делал {html.escape(char['name'])}:</b>"]
        for kind, text in rows:
            lines.append(("✍️ " if kind == "action" else "   ") + html.escape(text[:300]))
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_roll(self, message: Message, command: CommandObject):
        args = (command.args or "1d20").split()
        adv = "none"
        if args and args[-1].lower() in ("adv", "dis", "преим", "помеха"):
            adv = "adv" if args.pop().lower() in ("adv", "преим") else "dis"
        try:
            r = roll("".join(args) or "1d20", adv)
        except DiceError as e:
            await message.reply(f"🎲 {e}")
            return
        await message.reply(f"🎲 {fmt(str(r))}", parse_mode="HTML")

    async def cmd_timeout(self, message: Message, command: CommandObject):
        state = await self.get(message.chat.id)
        if not state:
            return
        try:
            seconds = int(command.args)
        except (TypeError, ValueError):
            await message.answer(f"Сейчас {state['timeout']} сек. Поменять: /timeout 180")
            return
        state["timeout"] = max(20, min(seconds, 3600))
        await self.save(message.chat.id)
        await message.answer(f"⏱ Ждём заявки {state['timeout']} сек.")

    async def cmd_leave(self, message: Message):
        state = await self.get(message.chat.id)
        key = str(message.from_user.id)
        player = (state or {}).get("players", {}).get(key)
        if not player or not player.get("active", True) or state["status"] == "ended":
            await message.answer("Тебя и так нет в партии.")
            return
        if player.get("draft") and not player.get("char"):
            state["players"].pop(key)
            await self.save(message.chat.id)
            await message.answer("Создание героя отменено.")
            return
        await self.leave(message.chat.id, key, away=False, by="ушёл сам")

    async def cmd_back(self, message: Message):
        state = await self.get(message.chat.id)
        key = str(message.from_user.id)
        player = (state or {}).get("players", {}).get(key)
        if not player or not player.get("char") or player["char"]["death"]["dead"]:
            await message.answer("Возвращаться некому. /join — создать героя.")
            return
        if player.get("active", True):
            await message.answer("Ты и так в партии.")
            return
        async with self.lock(message.chat.id):
            text = await self.come_back(message.chat.id, state, key)
        await self.send(message.chat.id, text)

    async def cmd_afk(self, message: Message, command: CommandObject):
        state = await self.get(message.chat.id)
        if not state or state["status"] not in ("lobby", "active", "paused"):
            return
        if not command.args:
            await message.answer("Кого отметить? <code>/afk Имя героя</code>. Себя — /leave.", parse_mode="HTML")
            return
        target = command.args.strip().lower()
        found = [k for k, p in state["players"].items()
                 if p.get("char") and p.get("active", True) and target in p["char"]["name"].lower()]
        if not found:
            await message.answer("Такого героя в партии нет. /status — кто есть.")
            return
        await self.leave(message.chat.id, found[0], away=True, by=f"отметил {message.from_user.first_name}")

    async def on_text(self, message: Message):
        chat_id = message.chat.id
        text = (message.text or "").strip()
        if not text or text.startswith(("//", "(", "/")) or not self.allowed(chat_id) or not message.from_user:
            return
        state = await self.get(chat_id)
        if not state:
            return
        key = str(message.from_user.id)
        player = state["players"].get(key)
        if not player:
            return
        if not player.get("active", True):
            # Отошедший (не ушедший сам) возвращается, просто написав в чат
            if not player.get("away") or not player.get("char") or player["char"]["death"]["dead"]:
                return
            async with self.lock(chat_id):
                notice = await self.come_back(chat_id, state, key)
            await self.send(chat_id, notice)
        draft = player.get("draft")
        if draft and draft.get("step") == "name":
            name = clean_name(text)
            if name:
                await self.ask_class(message, state, key, name)
                await self.save(chat_id)
            return
        if draft and draft.get("step") == "look":
            draft.update(step="backstory", look="" if text == "-" else text[:400])
            await self.save(chat_id)
            await message.reply("Последнее: кем герой был до приключений и почему ввязался? Пара предложений, "
                                "можно с тёмными секретами — ведущий их припомнит. Или «-».")
            return
        if draft and draft.get("step") == "backstory":
            draft["backstory"] = "" if text == "-" else text[:600]
            await self.finish_char(chat_id, key)
            return
        char = player.get("char")
        if state["status"] != "active" or not char or not can_act(char):
            return
        text = text[:MAX_ACTION_LEN]

        await self.chronicle(chat_id, "action", text, message.from_user.id, char["name"])
        stat(char, "actions")
        player["missed"] = 0

        if state["combat"]:
            entry = current_actor(state)
            if entry and entry["key"] == f"p:{key}" and not self.lock(chat_id).locked():
                await self.combat_turn(chat_id, key, text)
            else:
                # Заявка на будущий ход: сыграет, когда до игрока дойдёт очередь
                state["pending"][key] = text
                await self.save(chat_id)
            return

        prev = state["pending"].get(key)
        state["pending"][key] = f"{prev} {text}"[:MAX_ACTION_LEN] if prev else text
        await self.save(chat_id)
        waiting = [k for k, c in characters(state).items() if can_act(c) and k not in state["pending"]]
        if not waiting:
            await self.resolve_round(chat_id)
        elif chat_id not in self.timers:
            self.arm_timer(chat_id, state["timeout"])

    # --- запуск ---

    async def startup(self):
        await self.storage.connect()
        await self.restore()
        me = await self.bot.get_me()
        try:
            await self.bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
        except Exception as e:
            logging.warning(f"ДнД: не смог задать меню команд: {e}")
        logging.info(f"ДнД-бот @{me.username} запущен")

    async def shutdown(self):
        await self.storage.close()
        await self.bot.session.close()

    async def run(self, handle_signals: bool = True):
        """Опрос Telegram — для локального запуска."""
        try:
            await self.startup()
            # Если на Render стоит вебхук, getUpdates не работает — снимаем его (Render поставит заново при старте)
            await self.bot.delete_webhook()
            await self.dp.start_polling(self.bot, handle_signals=handle_signals,
                                        allowed_updates=self.dp.resolve_used_update_types())
        finally:
            await self.shutdown()

    def webhook_secret(self) -> str:
        """Секрет вебхука выводится из токена: одинаковый между рестартами и без лишней переменной."""
        return hashlib.sha256(self.bot.token.encode()).hexdigest()[:48]

    def mount_webhook(self, app: web.Application, base_url: str):
        """Вебхук: Telegram сам присылает сообщения и будит уснувший сервис на Render free.

        Сервис работает только пока идёт игра, cron-пинг не нужен.
        """
        secret = self.webhook_secret()
        path = f"/telegram/{secret[:16]}"
        SimpleRequestHandler(self.dp, self.bot, secret_token=secret).register(app, path=path)

        async def on_startup(_):
            await self.startup()
            await self.bot.set_webhook(base_url.rstrip("/") + path, secret_token=secret,
                                       allowed_updates=self.dp.resolve_used_update_types())
            logging.info("ДнД: вебхук установлен")

        async def on_cleanup(_):
            for task in list(self.timers.values()):
                task.cancel()
            await self.shutdown()

        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)


def from_env(token: str) -> DndBot:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY не установлен!")
    models = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-3.5-flash,gemini-3.8-flash,gemini-3.5-flash-lite").split(",")
              if m.strip()]
    llm = GeminiClient(api_key, models, rpm=int(os.getenv("GEMINI_RPM", 8)),
                       thinking_level=os.getenv("GEMINI_THINKING", "low") or None)
    storage = make_storage(os.getenv("DATABASE_URL"), os.getenv("DND_SQLITE", "dnd.sqlite3"))
    allowed = {int(x) for x in os.getenv("DND_ALLOWED_CHATS", "").replace(" ", "").split(",") if x}
    return DndBot(token, llm, storage, allowed or None)


async def run_dnd(token: str, handle_signals: bool = True):
    await from_env(token).run(handle_signals=handle_signals)


def webhook_base_url() -> str | None:
    """Адрес сервиса для вебхука. Render сам задаёт RENDER_EXTERNAL_URL; DND_POLLING=1 — принудительно опрос."""
    if os.getenv("DND_POLLING") == "1":
        return None
    return os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
