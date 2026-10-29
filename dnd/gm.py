"""Мозг ведущего: состояние партии, механика (инструменты для ИИ) и розыгрыш раунда."""
import json
import logging

from . import rules
from .dice import roll, double_dice, DiceError
from .llm import LLMError
from .scenarios import SCENARIOS, scenario_block

HISTORY_KEEP = 6        # сколько последних раундов идёт в промпт дословно
HISTORY_COMPRESS = 12   # с какого размера истории старое сжимается в саммари
MAX_TOOL_STEPS = 8

SYSTEM_PROMPT = """Ты — ведущий (Мастер) партии Dungeons & Dragons 5e в групповом чате Telegram. Пишешь только по-русски.

Характер: циничный, ироничный рассказчик с чёрным юмором. Подкалываешь игроков за глупые решения и провалы, комментируешь их зло, но смешно, как старый мастер, который видел всё. Сюжет и правила при этом держишь: мир живёт по своей логике, у поступков есть последствия, злодеи не глупеют ради удобства игроков. Юмор не отменяет опасности: бой может убить.

Правила механики (строго):
- Вызывай сразу все нужные в этом ходе инструменты одним пакетом, а не по одному — так быстрее.
- Ты НИКОГДА не придумываешь результаты бросков, урон и хиты. Любая проверка, атака, урон, лечение — только через инструменты. Чего не вернул инструмент — того не было.
- Проверку навыка/характеристики/спасбросок — ability_check со Сложностью (лёгкая 10, средняя 15, трудная 20). Не проси проверку на то, что точно получится или точно нет.
- Атаки персонажей и монстров — attack (бонусы и урон код знает сам). Заклинание с броском атаки — attack с его названием; заклинания с уроном по площади — roll_dice + ability_check(save:...) каждой цели + change_hp.
- Урон от ловушек, падений, лечение зельями и заклинаниями — change_hp. Предметы и золото — add_item. Состояния (отравлен, сбит с ног, без сознания...) — set_condition.
- Начало боя — start_combat со списком врагов (srd_index — английский индекс SRD, напр. goblin, wolf, bandit, skeleton, orc). Инициативу и очередь ведёт код. В ответе, где начинается бой, только опиши, как враги появились: никаких атак, ходы пойдут по очереди.
- Не объявляй, чей ход и кого ты ждёшь — это делает бот сам. Бой заканчивается сам, когда враги мертвы; если враги сбежали или сдались — end_combat.
- Если не уверен в правиле, монстре или заклинании — srd_lookup (по-английски).
- Опыт за решённые задачи и хорошую игру — award_xp; за убитых в бою код начисляет сам.
- Когда партия сменила место или ситуацию — update_scene коротко.
- Если идёт сценарий: держись его канвы, когда цель акта достигнута — advance_plot. Важные факты (обещания, долги, имена, кто кого обидел) — remember.
- У персонажей есть внешность, характер и предыстория — используй их в описаниях и подколках.

Как вести:
- Отвечай на заявки всех игроков в одном связном ответе. Не решай за персонажей, что они делают или говорят. За NPC говори сам, живо.
- Если игрок заявил невозможное или бред — пусть пробует и получает последствия (с соответствующей подколкой).
- В бою описывай только тех, кто указан в блоке «Ход»: действие текущего персонажа и затем ходы перечисленных монстров (каждый монстр атакует через attack). Не отыгрывай ходы других персонажей.
- Объём: 500–1200 знаков, без вступлений вроде «Отлично!». Можно **жирный** для имён и важного. Без списков и заголовков — это рассказ.
- Вне боя заканчивай ситуацией, на которую игроки могут реагировать.
"""

TOOLS = [
    {
        "name": "roll_dice",
        "description": "Бросить кубики по формуле (например 2d6+3, 1d20, 8d6). Для урона по площади, случайных таблиц и т.п.",
        "parameters": {"type": "OBJECT", "properties": {
            "expression": {"type": "STRING", "description": "формула, например 3d6+2"},
            "reason": {"type": "STRING", "description": "зачем бросок"},
        }, "required": ["expression", "reason"]},
    },
    {
        "name": "ability_check",
        "description": "Проверка навыка, характеристики или спасбросок персонажа или монстра против Сложности.",
        "parameters": {"type": "OBJECT", "properties": {
            "who": {"type": "STRING", "description": "имя персонажа или id монстра"},
            "check": {"type": "STRING", "description": "навык (Скрытность, Внимательность, Атлетика...), характеристика (Сила...) или спасбросок в виде save:dex"},
            "dc": {"type": "INTEGER", "description": "Сложность"},
            "advantage": {"type": "STRING", "enum": ["none", "adv", "dis"]},
        }, "required": ["who", "check", "dc"]},
    },
    {
        "name": "attack",
        "description": "Атака: бросок попадания против КД цели и урон (урон применяется автоматически).",
        "parameters": {"type": "OBJECT", "properties": {
            "attacker": {"type": "STRING", "description": "имя персонажа или id монстра"},
            "target": {"type": "STRING", "description": "имя персонажа или id монстра"},
            "attack_name": {"type": "STRING", "description": "название атаки из листа/списка; можно пусто — возьмётся первая"},
            "advantage": {"type": "STRING", "enum": ["none", "adv", "dis"]},
            "extra_damage": {"type": "STRING", "description": "доп. урон, например 1d6 за скрытую атаку"},
            "attack_bonus": {"type": "INTEGER", "description": "только для импровизированной атаки, которой нет в списке"},
            "damage": {"type": "STRING", "description": "только для импровизированной атаки: формула урона"},
        }, "required": ["attacker", "target"]},
    },
    {
        "name": "change_hp",
        "description": "Изменить хиты: отрицательное число — урон, положительное — лечение.",
        "parameters": {"type": "OBJECT", "properties": {
            "target": {"type": "STRING"},
            "amount": {"type": "INTEGER"},
            "reason": {"type": "STRING"},
        }, "required": ["target", "amount", "reason"]},
    },
    {
        "name": "add_item",
        "description": "Дать (quantity > 0) или забрать (quantity < 0) предмет. Для денег item = 'золото'.",
        "parameters": {"type": "OBJECT", "properties": {
            "character": {"type": "STRING"},
            "item": {"type": "STRING"},
            "quantity": {"type": "INTEGER"},
        }, "required": ["character", "item", "quantity"]},
    },
    {
        "name": "set_condition",
        "description": "Наложить или снять состояние (отравлен, сбит с ног, ослеплён, испуган, схвачен...).",
        "parameters": {"type": "OBJECT", "properties": {
            "target": {"type": "STRING"},
            "condition": {"type": "STRING"},
            "active": {"type": "BOOLEAN"},
        }, "required": ["target", "condition", "active"]},
    },
    {
        "name": "start_combat",
        "description": "Начать бой. Код бросит инициативу всем и поведёт очередь ходов.",
        "parameters": {"type": "OBJECT", "properties": {
            "enemies": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING", "description": "как назвать по-русски"},
                "srd_index": {"type": "STRING", "description": "индекс монстра SRD по-английски (goblin, wolf...), если есть"},
                "count": {"type": "INTEGER"},
                "hp": {"type": "INTEGER", "description": "если монстра нет в SRD"},
                "ac": {"type": "INTEGER", "description": "если монстра нет в SRD"},
                "attack_bonus": {"type": "INTEGER", "description": "если монстра нет в SRD"},
                "damage": {"type": "STRING", "description": "если монстра нет в SRD, например 1d6+2"},
            }, "required": ["name"]}},
        }, "required": ["enemies"]},
    },
    {
        "name": "end_combat",
        "description": "Закончить бой досрочно (враги сбежали, сдались, партия удрала).",
        "parameters": {"type": "OBJECT", "properties": {"reason": {"type": "STRING"}}, "required": ["reason"]},
    },
    {
        "name": "award_xp",
        "description": "Начислить опыт партии (делится поровну между живыми).",
        "parameters": {"type": "OBJECT", "properties": {
            "amount": {"type": "INTEGER"}, "reason": {"type": "STRING"},
        }, "required": ["amount", "reason"]},
    },
    {
        "name": "rest",
        "description": "Отдых партии: short — короткий (час), long — долгий (8 часов, полное восстановление).",
        "parameters": {"type": "OBJECT", "properties": {
            "kind": {"type": "STRING", "enum": ["short", "long"]},
        }, "required": ["kind"]},
    },
    {
        "name": "srd_lookup",
        "description": "Найти правило, монстра, заклинание или предмет в SRD 5.1 (запрос по-английски).",
        "parameters": {"type": "OBJECT", "properties": {
            "category": {"type": "STRING", "enum": ["monsters", "spells", "equipment", "magic-items", "conditions"]},
            "name": {"type": "STRING"},
        }, "required": ["category", "name"]},
    },
    {
        "name": "advance_plot",
        "description": "Цель текущего акта сценария достигнута — перейти к следующему акту.",
        "parameters": {"type": "OBJECT", "properties": {
            "summary": {"type": "STRING", "description": "чем закончился акт, одно предложение"},
        }, "required": ["summary"]},
    },
    {
        "name": "remember",
        "description": "Запомнить важный факт сюжета надолго: обещание, долг, имя NPC, найденную улику.",
        "parameters": {"type": "OBJECT", "properties": {"fact": {"type": "STRING"}}, "required": ["fact"]},
    },
    {
        "name": "update_scene",
        "description": "Записать, где сейчас партия и что вокруг (1–3 предложения).",
        "parameters": {"type": "OBJECT", "properties": {"scene": {"type": "STRING"}}, "required": ["scene"]},
    },
]


def new_state(setting: str, scenario: str | None = None) -> dict:
    return {
        "status": "lobby",
        "setting": setting,
        "scenario": scenario,
        "act": 1,
        "plot_notes": [],
        "scene": "",
        "summary": "",
        "history": [],
        "players": {},
        "pending": {},
        "combat": None,
        "monsters": {},
        "round": 0,
        "timeout": 120,
        "combat_timeout": 120,
        "turn_label": "",
    }


def characters(state: dict) -> dict[str, dict]:
    """player_key → персонаж, только активные игроки с готовым персонажем."""
    return {k: p["char"] for k, p in state["players"].items() if p.get("char") and p.get("active", True)}


def can_act(char: dict) -> bool:
    return char["hp"] > 0 and not char["death"]["dead"]


class Mechanics:
    """Исполнитель инструментов: меняет состояние и пишет понятный лог бросков."""

    def __init__(self, state: dict):
        self.state = state
        self.log: list[str] = []
        self.combat_started = False

    # --- поиск целей ---

    def find(self, name: str) -> tuple[str, dict] | None:
        """('char', персонаж) или ('monster', монстр) по имени/ид, без учёта регистра."""
        key = (name or "").strip().lower()
        if not key:
            return None
        if key in self.state["monsters"]:
            return "monster", self.state["monsters"][key]
        chars = [c for c in characters(self.state).values()]
        for c in chars:
            if c["name"].lower() == key:
                return "char", c
        for mid, m in self.state["monsters"].items():
            if m["name"].lower() == key and not m.get("dead"):
                return "monster", m
        for c in chars:
            if key in c["name"].lower() or c["name"].lower() in key:
                return "char", c
        for mid, m in self.state["monsters"].items():
            if not m.get("dead") and (key in m["name"].lower() or key in mid):
                return "monster", m
        return None

    def _not_found(self, name: str) -> dict:
        known = [c["name"] for c in characters(self.state).values()]
        known += [mid for mid, m in self.state["monsters"].items() if not m.get("dead")]
        return {"error": f"не нашёл «{name}». Есть: {', '.join(known) or 'никого'}"}

    # --- инструменты ---

    async def call(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"tool_{name}", None)
        if not handler:
            return {"error": f"нет такого инструмента: {name}"}
        try:
            return await handler(**args)
        except DiceError as e:
            return {"error": str(e)}
        except TypeError as e:
            return {"error": f"неверные аргументы: {e}"}
        except Exception as e:
            logging.exception(f"ДнД: инструмент {name} упал")
            return {"error": f"инструмент сломался: {e}"}

    async def tool_roll_dice(self, expression, reason="", advantage="none"):
        r = roll(expression, advantage)
        self.log.append(f"🎲 {reason}: {r}")
        return {"total": r.total, "rolls": " ".join(r.parts)}

    async def tool_ability_check(self, who, check, dc, advantage="none"):
        found = self.find(who)
        if not found:
            return self._not_found(who)
        kind, obj = found
        if kind == "char":
            res = rules.check_bonus(obj, check)
            if not res:
                return {"error": f"не понял проверку «{check}»"}
            bonus, label = res
        else:
            ability = rules.resolve_ability(check.split(":", 1)[-1]) or rules.resolve_ability(
                rules.SKILLS.get(rules.resolve_skill(check) or "", ("", "dex"))[1]
            ) or "dex"
            bonus = rules.mod(obj.get("abilities", {}).get(ability, 10))
            label = check
        r = roll(f"1d20{rules.fmt_mod(bonus)}", advantage)
        success = r.total >= int(dc)
        if kind == "char":
            stat(obj, "checks")
            if success:
                stat(obj, "checks_ok")
        mark = "✅" if success else "❌"
        self.log.append(f"{mark} {obj['name']}, {label} (Сл {dc}): {r}")
        return {"total": r.total, "natural": r.natural, "dc": dc, "success": success}

    async def tool_attack(self, attacker, target, attack_name="", advantage="none", extra_damage="",
                          attack_bonus=None, damage=None):
        a = self.find(attacker)
        t = self.find(target)
        if not a:
            return self._not_found(attacker)
        if not t:
            return self._not_found(target)
        a_kind, a_obj = a
        t_kind, t_obj = t

        attacks = a_obj.get("attacks", [])
        chosen = None
        if attack_name:
            low = attack_name.lower()
            chosen = next((x for x in attacks if low in x["name"].lower() or x["name"].lower() in low), None)
        if chosen is None and attack_bonus is None and attacks:
            chosen = attacks[0]

        save_ability = None
        if chosen and a_kind == "char":
            bonus = rules.attack_bonus(a_obj, chosen)
            dmg = rules.attack_damage(a_obj, chosen)
            label = chosen["name"]
            save_ability = chosen.get("save")
        elif chosen:
            bonus, dmg, label = chosen["bonus"], chosen["damage"], chosen["name"]
        else:
            bonus = int(attack_bonus or 0)
            dmg = damage or "1d4"
            label = attack_name or "импровизированная атака"

        if extra_damage:
            dmg = f"{dmg}+{extra_damage.lstrip('+')}"

        # Заговор со спасброском (Священное пламя, Злая насмешка): цель спасается от Сл заклинания
        if save_ability:
            dc = rules.spell_dc(a_obj, chosen["ability"])
            if t_kind == "char":
                save_bonus = rules.check_bonus(t_obj, f"save:{save_ability}")[0]
            else:
                save_bonus = rules.mod(t_obj.get("abilities", {}).get(save_ability, 10))
            save = roll(f"1d20{rules.fmt_mod(save_bonus)}")
            if save.total >= dc:
                self.log.append(f"🛡 {a_obj['name']} → {t_obj['name']}, {label}: спасбросок {save} против Сл {dc} — мимо")
                return {"hit": False, "target_save": save.total, "dc": dc}
            dmg_roll = roll(dmg)
            result = self._apply_damage(t_kind, t_obj, dmg_roll.total)
            self._credit(a_kind, a_obj, t_kind, dmg_roll.total, result)
            self.log.append(
                f"💥 {a_obj['name']} → {t_obj['name']}, {label}: спасбросок {save.total} против Сл {dc} провален, "
                f"урон {dmg_roll}{result['note']}"
            )
            return {"hit": True, "damage": dmg_roll.total, **result["data"]}

        if t_kind == "char" and t_obj["hp"] <= 0:
            advantage = "adv"
        to_hit = roll(f"1d20{rules.fmt_mod(bonus)}", advantage)
        crit = to_hit.natural == 20
        fumble = to_hit.natural == 1
        hit = crit or (not fumble and to_hit.total >= t_obj["ac"])
        if a_kind == "char":
            stat(a_obj, "attacks")
            stat(a_obj, "hits", hit)
            stat(a_obj, "crits", crit)
            stat(a_obj, "fumbles", fumble)
        if not hit:
            word = "позорный промах (нат. 1)" if fumble else "мимо"
            self.log.append(f"🗡 {a_obj['name']} → {t_obj['name']}, {label}: {to_hit} против КД {t_obj['ac']} — {word}")
            return {"hit": False, "to_hit": to_hit.total, "natural": to_hit.natural, "target_ac": t_obj["ac"]}

        dmg_roll = roll(double_dice(dmg) if crit else dmg)
        total = max(dmg_roll.total, 0)
        result = self._apply_damage(t_kind, t_obj, total, crit=crit)
        self._credit(a_kind, a_obj, t_kind, total, result)
        word = "КРИТ!" if crit else "попал"
        self.log.append(
            f"🗡 {a_obj['name']} → {t_obj['name']}, {label}: {to_hit} против КД {t_obj['ac']} — {word}, "
            f"урон {dmg_roll}{result['note']}"
        )
        return {"hit": True, "crit": crit, "to_hit": to_hit.total, "damage": total, **result["data"]}

    @staticmethod
    def _credit(a_kind: str, a_obj: dict, t_kind: str, amount: int, result: dict):
        if a_kind != "char":
            return
        stat(a_obj, "damage_dealt", amount)
        if t_kind == "monster" and result["data"].get("target_dead"):
            stat(a_obj, "kills")

    def _apply_damage(self, kind: str, obj: dict, amount: int, crit: bool = False) -> dict:
        if kind == "char":
            stat(obj, "damage_taken", amount)
        if kind == "monster":
            obj["hp"] = max(obj["hp"] - amount, 0)
            if obj["hp"] == 0:
                obj["dead"] = True
                return {"note": f" → {obj['name']} повержен ☠️", "data": {"target_hp": 0, "target_dead": True}}
            return {"note": f" (ост. {obj['hp']}/{obj['max_hp']})", "data": {"target_hp": obj["hp"]}}

        death = obj["death"]
        if obj["hp"] == 0 and amount > 0:
            # Урон по лежащему без сознания — провал спасброска от смерти, крит — два
            death["fail"] += 2 if crit else 1
            death["stable"] = False
            if death["fail"] >= 3:
                death["dead"] = True
                return {"note": f" → {obj['name']} умирает ☠️", "data": {"target_dead": True}}
            return {"note": f" → провалов спасброска от смерти: {death['fail']}/3", "data": {"target_hp": 0}}
        obj["hp"] -= amount
        if obj["hp"] <= -obj["max_hp"]:
            obj["hp"] = 0
            death["dead"] = True
            return {"note": f" → {obj['name']} разорван на куски, мгновенная смерть ☠️", "data": {"target_dead": True}}
        if obj["hp"] <= 0:
            obj["hp"] = 0
            if "без сознания" not in obj["conditions"]:
                obj["conditions"].append("без сознания")
            stat(obj, "knockouts")
            return {"note": f" → {obj['name']} падает без сознания", "data": {"target_hp": 0, "unconscious": True}}
        return {"note": f" (ост. {obj['hp']}/{obj['max_hp']})", "data": {"target_hp": obj["hp"]}}

    async def tool_change_hp(self, target, amount, reason=""):
        t = self.find(target)
        if not t:
            return self._not_found(target)
        kind, obj = t
        amount = int(amount)
        if amount < 0:
            result = self._apply_damage(kind, obj, -amount)
            self.log.append(f"💢 {obj['name']}: {amount} хитов ({reason}){result['note']}")
            return {"hp": obj["hp"], **result["data"]}
        if kind == "char" and obj["death"]["dead"]:
            return {"error": f"{obj['name']} мёртв, лечение не поможет"}
        obj["hp"] = min(obj["hp"] + amount, obj["max_hp"])
        if kind == "char":
            stat(obj, "healed", amount)
        if kind == "char" and obj["hp"] > 0:
            obj["death"].update(success=0, fail=0, stable=False)
            if "без сознания" in obj["conditions"]:
                obj["conditions"].remove("без сознания")
        self.log.append(f"💚 {obj['name']}: +{amount} хитов ({reason}) → {obj['hp']}/{obj['max_hp']}")
        return {"hp": obj["hp"], "max_hp": obj["max_hp"]}

    async def tool_add_item(self, character, item, quantity=1):
        t = self.find(character)
        if not t or t[0] != "char":
            return self._not_found(character)
        char = t[1]
        quantity = int(quantity)
        if item.strip().lower() in ("золото", "gold", "зм", "gp"):
            if char["gold"] + quantity < 0:
                return {"error": f"у {char['name']} только {char['gold']} золота"}
            char["gold"] += quantity
            self.log.append(f"💰 {char['name']}: {quantity:+d} золота → {char['gold']}")
            return {"gold": char["gold"]}
        inv = char["inventory"]
        key = next((k for k in inv if k.lower() == item.strip().lower()), item.strip())
        have = inv.get(key, 0)
        if have + quantity < 0:
            return {"error": f"у {char['name']} нет столько: {key} ×{have}"}
        if have + quantity == 0:
            inv.pop(key, None)
        else:
            inv[key] = have + quantity
        sign = "+" if quantity > 0 else "−"
        self.log.append(f"🎒 {char['name']}: {sign} {key} ×{abs(quantity)}")
        return {"item": key, "now": inv.get(key, 0)}

    async def tool_set_condition(self, target, condition, active=True):
        t = self.find(target)
        if not t:
            return self._not_found(target)
        obj = t[1]
        conds = obj.setdefault("conditions", [])
        cond = condition.strip().lower()
        if active and cond not in conds:
            conds.append(cond)
        elif not active and cond in conds:
            conds.remove(cond)
        self.log.append(f"🌀 {obj['name']}: {'+' if active else '−'}{cond}")
        return {"conditions": conds}

    async def tool_start_combat(self, enemies):
        state = self.state
        if state["combat"]:
            # Подкрепление в идущий бой: добавляем в очередь
            order = state["combat"]["order"]
        else:
            state["monsters"] = {}
            order = []
            for key, char in characters(state).items():
                if char["death"]["dead"]:
                    continue
                init = roll(f"1d20{rules.fmt_mod(rules.mod(char['abilities']['dex']))}")
                order.append({"key": f"p:{key}", "name": char["name"], "init": init.total,
                              "tie": char["abilities"]["dex"]})
                self.log.append(f"⚡ Инициатива {char['name']}: {init}")
        added = []
        for enemy in enemies[:12]:
            count = max(1, min(int(enemy.get("count") or 1), 8))
            base = None
            if enemy.get("srd_index"):
                data = await rules.srd_find("monsters", enemy["srd_index"])
                if data:
                    base = rules.monster_from_srd(data)
                    base["abilities"] = {a: data.get(full, 10) for a, full in (
                        ("str", "strength"), ("dex", "dexterity"), ("con", "constitution"),
                        ("int", "intelligence"), ("wis", "wisdom"), ("cha", "charisma"))}
            if base is None:
                base = {"hp": int(enemy.get("hp") or 10), "ac": int(enemy.get("ac") or 12), "init_bonus": 1,
                        "attacks": [{"name": "Атака", "bonus": int(enemy.get("attack_bonus") or 3),
                                     "damage": enemy.get("damage") or "1d6+1"}],
                        "xp": 25, "abilities": {}}
                base["max_hp"] = base["hp"]
            # Без имени от ИИ берём русское из сценария, иначе английское из SRD
            scenario_names = {idx: ru for ru, idx in SCENARIOS.get(state.get("scenario") or "", {}).get("monsters", [])}
            name = enemy.get("name") or scenario_names.get(enemy.get("srd_index") or "") or base.get("name") or "Враг"
            slug = (enemy.get("srd_index") or "enemy").lower().replace(" ", "-")
            for i in range(count):
                n = 1
                while f"{slug}_{n}" in state["monsters"]:
                    n += 1
                mid = f"{slug}_{n}"
                monster = json.loads(json.dumps(base))
                monster["name"] = f"{name} {n}" if count > 1 or n > 1 else name
                init = roll(f"1d20{rules.fmt_mod(base['init_bonus'])}")
                state["monsters"][mid] = monster
                order.append({"key": f"m:{mid}", "name": monster["name"], "init": init.total,
                              "tie": 10 + base["init_bonus"] * 2})
                added.append(f"{mid} ({monster['name']}, HP {monster['hp']}, КД {monster['ac']})")
                self.log.append(f"⚡ Инициатива {monster['name']}: {init}")
        if not state["combat"]:
            order.sort(key=lambda x: (x["init"], x["tie"]), reverse=True)
            state["combat"] = {"order": order, "idx": 0, "round": 1}
            self.combat_started = True
        queue = ", ".join(f"{o['name']} ({o['init']})" for o in order)
        self.log.append(f"⚔️ Бой! Очередь: {queue}")
        return {"enemies": added, "order": queue,
                "note": "Код сам ведёт очередь. Сейчас только опиши появление врагов, НЕ атакуй в этом ответе: "
                        "ходы пойдут по инициативе, и бот сам объявит, чей ход."}

    async def tool_end_combat(self, reason=""):
        if not self.state["combat"]:
            return {"error": "боя и так нет"}
        finish_combat(self.state, self.log, reason)
        return {"ok": True}

    async def tool_award_xp(self, amount, reason=""):
        award_xp(self.state, int(amount), reason, self.log)
        return {"ok": True}

    async def tool_rest(self, kind="short"):
        for char in characters(self.state).values():
            if char["death"]["dead"]:
                continue
            if kind == "long":
                char["hp"] = char["max_hp"]
                char["conditions"] = [c for c in char["conditions"] if c == "мёртв"]
                char["death"].update(success=0, fail=0, stable=False)
            else:
                cls = rules.CLASSES[char["class"]]
                healed = roll(f"1d{cls['hit_die']}{rules.fmt_mod(rules.mod(char['abilities']['con']))}").total
                char["hp"] = min(char["max_hp"], char["hp"] + max(healed, 1))
                if char["hp"] > 0 and "без сознания" in char["conditions"]:
                    char["conditions"].remove("без сознания")
        word = "Долгий отдых: все восстановились" if kind == "long" else "Короткий отдых: кости хита потрачены"
        self.log.append(f"🏕 {word}")
        return {"party": [f"{c['name']} {c['hp']}/{c['max_hp']}" for c in characters(self.state).values()]}

    async def tool_srd_lookup(self, category, name):
        data = await rules.srd_find(category, name)
        if not data:
            return {"error": f"в SRD не нашлось «{name}» ({category}), решай сам по духу правил"}
        return {"text": rules.srd_summary(category, data)}

    async def tool_advance_plot(self, summary=""):
        sc = SCENARIOS.get(self.state.get("scenario") or "")
        if not sc:
            return {"error": "сценария нет, партия идёт в свободном режиме"}
        self.state["plot_notes"].append(f"Акт {self.state['act']} завершён: {summary}"[:300])
        if self.state["act"] >= len(sc["acts"]):
            self.log.append("📖 Сценарий пройден! Можно /endgame для эпилога или играть дальше")
            return {"finished": True, "note": "Это был последний акт: подведи к финалу."}
        self.state["act"] += 1
        act = sc["acts"][self.state["act"] - 1]
        self.log.append(f"📖 Акт {self.state['act']}: {act['title']}")
        # Опыт за акт каждому: 150, 300, 450 — к финалу партия дорастает до 3 уровня
        alive = [c for c in characters(self.state).values() if not c["death"]["dead"]]
        award_xp(self.state, 150 * (self.state["act"] - 1) * max(len(alive), 1), "за пройденный акт", self.log)
        return {"act": self.state["act"], "title": act["title"], "goal": act["goal"], "details": act["details"]}

    async def tool_remember(self, fact):
        self.state["plot_notes"].append(fact[:300])
        self.state["plot_notes"] = self.state["plot_notes"][-30:]
        return {"ok": True}

    async def tool_update_scene(self, scene):
        self.state["scene"] = scene[:600]
        return {"ok": True}


def stat(char: dict, key: str, amount: int | bool = 1):
    """Копит статистику героя для /me: атаки, криты, убийства, урон..."""
    stats = char.setdefault("stats", {})
    stats[key] = stats.get(key, 0) + int(amount)


def award_xp(state: dict, amount: int, reason: str, log: list[str]):
    alive = [c for c in characters(state).values() if not c["death"]["dead"]]
    if not alive or amount <= 0:
        return
    share = max(amount // len(alive), 1)
    log.append(f"⭐ +{share} опыта каждому ({reason})")
    for char in alive:
        char["xp"] += share
        new_level = rules.level_for_xp(char["xp"])
        while char["level"] < new_level:
            char["level"] += 1
            cls = rules.CLASSES[char["class"]]
            gain = max(cls["hit_die"] // 2 + 1 + rules.mod(char["abilities"]["con"]), 1)
            char["max_hp"] += gain
            char["hp"] += gain
            log.append(f"🆙 {char['name']} теперь {char['level']} уровня (+{gain} к макс. хитам)")


def finish_combat(state: dict, log: list[str], reason: str = ""):
    killed_xp = sum(m.get("xp", 0) for m in state["monsters"].values() if m.get("dead"))
    state["combat"] = None
    # Заявки, сделанные «на свой ход», после боя уже неактуальны
    state["pending"] = {}
    log.append(f"🏁 Бой окончен{': ' + reason if reason else ''}")
    if killed_xp:
        award_xp(state, killed_xp, "за поверженных врагов", log)
    state["monsters"] = {}


# --- Очередь боя ---

def actor_alive(state: dict, entry: dict) -> bool:
    kind, key = entry["key"].split(":", 1)
    if kind == "m":
        m = state["monsters"].get(key)
        return bool(m) and not m.get("dead")
    player = state["players"].get(key)
    return bool(player and player.get("char") and player.get("active", True)
                and not player["char"]["death"]["dead"])


def living_monsters(state: dict) -> list[str]:
    return [mid for mid, m in state["monsters"].items() if not m.get("dead")]


def current_actor(state: dict) -> dict | None:
    combat = state["combat"]
    if not combat:
        return None
    order = combat["order"]
    for _ in range(len(order)):
        entry = order[combat["idx"] % len(order)]
        if actor_alive(state, entry):
            return entry
        advance(state)
    return None


def advance(state: dict):
    combat = state["combat"]
    combat["idx"] += 1
    if combat["idx"] >= len(combat["order"]):
        combat["idx"] = 0
        combat["round"] += 1


def join_combat(state: dict, player_key: str) -> str | None:
    """Герой, который пришёл посреди боя, бросает инициативу и встаёт в конец очереди раунда."""
    combat = state["combat"]
    if not combat or any(o["key"] == f"p:{player_key}" for o in combat["order"]):
        return None
    char = state["players"][player_key]["char"]
    init = roll(f"1d20{rules.fmt_mod(rules.mod(char['abilities']['dex']))}")
    combat["order"].append({"key": f"p:{player_key}", "name": char["name"], "init": init.total,
                            "tie": char["abilities"]["dex"]})
    return f"⚡ {char['name']} влетает в бой, инициатива {init}"


def take_monster_run(state: dict) -> list[dict]:
    """Снимает с очереди подряд идущих монстров (они ходят одним вызовом ИИ)."""
    run = []
    while state["combat"]:
        entry = current_actor(state)
        if not entry or not entry["key"].startswith("m:") or entry in run:
            break
        run.append(entry)
        advance(state)
    return run


def death_save(char: dict) -> str:
    death = char["death"]
    r = roll("1d20")
    if r.natural == 20:
        char["hp"] = 1
        death.update(success=0, fail=0, stable=False)
        if "без сознания" in char["conditions"]:
            char["conditions"].remove("без сознания")
        return f"💀 {char['name']}, спасбросок от смерти: {r} — 20! Встаёт с 1 хитом"
    if r.natural == 1:
        death["fail"] += 2
    elif r.total >= 10:
        death["success"] += 1
    else:
        death["fail"] += 1
    if death["fail"] >= 3:
        death["dead"] = True
        return f"💀 {char['name']}, спасбросок от смерти: {r} — умирает ☠️"
    if death["success"] >= 3:
        death["stable"] = True
        return f"💀 {char['name']}, спасбросок от смерти: {r} — стабилизировался"
    return f"💀 {char['name']}, спасбросок от смерти: {r} (успехов {death['success']}/3, провалов {death['fail']}/3)"


# --- Промпт ---

def state_block(state: dict) -> str:
    lines = [f"Сеттинг и тон: {state['setting']}"]
    plot = scenario_block(state)
    if plot:
        lines.append(plot)
    if state["summary"]:
        lines.append(f"Что было раньше: {state['summary']}")
    if state["scene"]:
        lines.append(f"Текущая сцена: {state['scene']}")
    lines.append("Партия:")
    for player in state["players"].values():
        if player.get("char") and player.get("active", True):
            lines.append(f"- {rules.brief_for_prompt(player['char'])} [игрок {player['name']}]")
    combat = state["combat"]
    if combat:
        lines.append(f"ИДЁТ БОЙ, раунд {combat['round']}. Очередь: " + ", ".join(
            o["name"] for o in combat["order"] if actor_alive(state, o)))
        for mid, m in state["monsters"].items():
            if not m.get("dead"):
                attacks = ", ".join(f"{a['name']} {rules.fmt_mod(a['bonus'])}" for a in m["attacks"])
                lines.append(f"- монстр id={mid}: {m['name']}, HP {m['hp']}/{m['max_hp']}, КД {m['ac']}, атаки: {attacks}"
                             + (f", состояния: {', '.join(m['conditions'])}" if m.get("conditions") else ""))
    return "\n".join(lines)


def history_contents(state: dict) -> list[dict]:
    contents = []
    for item in state["history"][-HISTORY_KEEP:]:
        contents.append({"role": "user", "parts": [{"text": item["actions"]}]})
        gm_text = item["gm"]
        if item.get("mech"):
            gm_text += "\n[механика: " + "; ".join(item["mech"]) + "]"
        contents.append({"role": "model", "parts": [{"text": gm_text}]})
    return contents


def strip_marks(text: str) -> str:
    return text.replace("**", "")


class GameMaster:
    def __init__(self, llm):
        self.llm = llm

    async def narrate(self, state: dict, turn_text: str, use_tools: bool = True) -> tuple[str, list[str], bool]:
        """Один ход ведущего: ИИ вызывает инструменты, потом пишет рассказ.

        Возвращает (текст, лог механики, начался ли бой).
        """
        mech = Mechanics(state)
        prompt = f"[СОСТОЯНИЕ]\n{state_block(state)}\n\n[ХОД]\n{turn_text}"
        contents = history_contents(state) + [{"role": "user", "parts": [{"text": prompt}]}]
        texts: list[str] = []
        for step in range(MAX_TOOL_STEPS):
            tools = TOOLS if use_tools and step < MAX_TOOL_STEPS - 1 else None
            content = await self.llm.generate(SYSTEM_PROMPT, contents, tools)
            contents.append(content)
            calls = [p["functionCall"] for p in content["parts"] if "functionCall" in p]
            texts += [p["text"] for p in content["parts"] if p.get("text") and not p.get("thought")]
            if not calls:
                break
            responses = []
            for call in calls:
                result = await mech.call(call["name"], call.get("args") or {})
                logging.info(f"ДнД tool {call['name']}({call.get('args')}) → {result}")
                response = {"name": call["name"], "response": result}
                if call.get("id"):
                    response["id"] = call["id"]
                responses.append({"functionResponse": response})
            contents.append({"role": "user", "parts": responses})
        text = "\n\n".join(t.strip() for t in texts if t.strip())
        if not text:
            raise LLMError("ИИ не написал текст")

        state["history"].append({
            "actions": turn_text,
            "gm": text,
            "mech": [strip_marks(line) for line in mech.log],
        })
        return text, mech.log, mech.combat_started

    async def compress(self, state: dict):
        """Сворачивает старую историю в саммари, чтобы промпт не рос бесконечно."""
        if len(state["history"]) <= HISTORY_COMPRESS:
            return
        old = state["history"][:-HISTORY_KEEP]
        transcript = "\n\n".join(f"Игроки: {h['actions']}\nВедущий: {h['gm']}" for h in old)
        prompt = (
            f"Прежнее краткое содержание:\n{state['summary'] or '(нет)'}\n\nНовые события:\n{transcript}\n\n"
            "Сожми всё в одно краткое содержание кампании до 1500 знаков: важные события, NPC, долги, "
            "обещания, найденные зацепки, нерешённые угрозы. Только факты, без юмора."
        )
        try:
            content = await self.llm.generate(
                "Ты помогаешь вести журнал кампании D&D. Пишешь по-русски, сжато.",
                [{"role": "user", "parts": [{"text": prompt}]}], None, temperature=0.3,
            )
            summary = "".join(p.get("text", "") for p in content["parts"] if not p.get("thought")).strip()
        except LLMError as e:
            logging.warning(f"ДнД: не смог сжать историю: {e}")
            return
        if summary:
            state["summary"] = summary[:2500]
            state["history"] = state["history"][-HISTORY_KEEP:]
