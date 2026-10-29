"""Правила D&D 5e из SRD 5.1 (CC-BY-4.0): характеристики, навыки, пресеты персонажей и справочник."""
import html
import logging
import aiohttp

ABILITIES = {
    "str": "Сила", "dex": "Ловкость", "con": "Телосложение",
    "int": "Интеллект", "wis": "Мудрость", "cha": "Харизма",
}
# Русские и английские синонимы, чтобы ИИ мог писать как угодно
ABILITY_ALIASES = {
    "сила": "str", "strength": "str", "ловкость": "dex", "dexterity": "dex",
    "телосложение": "con", "constitution": "con", "интеллект": "int", "intelligence": "int",
    "мудрость": "wis", "wisdom": "wis", "харизма": "cha", "charisma": "cha",
}

SKILLS = {
    "athletics": ("Атлетика", "str"),
    "acrobatics": ("Акробатика", "dex"),
    "sleight_of_hand": ("Ловкость рук", "dex"),
    "stealth": ("Скрытность", "dex"),
    "arcana": ("Магия", "int"),
    "history": ("История", "int"),
    "investigation": ("Анализ", "int"),
    "nature": ("Природа", "int"),
    "religion": ("Религия", "int"),
    "animal_handling": ("Уход за животными", "wis"),
    "insight": ("Проницательность", "wis"),
    "medicine": ("Медицина", "wis"),
    "perception": ("Внимательность", "wis"),
    "survival": ("Выживание", "wis"),
    "deception": ("Обман", "cha"),
    "intimidation": ("Запугивание", "cha"),
    "performance": ("Выступление", "cha"),
    "persuasion": ("Убеждение", "cha"),
}
SKILL_ALIASES = {ru.lower(): key for key, (ru, _) in SKILLS.items()}
SKILL_ALIASES.update({key.replace("_", " "): key for key in SKILLS})

# Порог опыта для уровней 1–10 (SRD)
XP_LEVELS = [0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000]

STANDARD_ARRAY = [15, 14, 13, 12, 10, 8]

RACES = {
    "human": {"name": "Человек", "about": "+1 ко всем характеристикам, универсал", "bonus": {a: 1 for a in ABILITIES}, "speed": 30},
    "elf": {"name": "Эльф", "about": "+2 Ловкость, +1 Интеллект; изящный и высокомерный", "bonus": {"dex": 2, "int": 1}, "speed": 30},
    "dwarf": {"name": "Дварф", "about": "+2 Телосложение, +1 Мудрость; крепкий, медленный, бородатый", "bonus": {"con": 2, "wis": 1}, "speed": 25},
    "halfling": {"name": "Полурослик", "about": "+2 Ловкость, +1 Харизма; маленький и везучий", "bonus": {"dex": 2, "cha": 1}, "speed": 25},
    "half_orc": {"name": "Полуорк", "about": "+2 Сила, +1 Телосложение; бьёт больно, думает потом", "bonus": {"str": 2, "con": 1}, "speed": 30},
    "tiefling": {"name": "Тифлинг", "about": "+2 Харизма, +1 Интеллект; рога, хвост, дурная репутация", "bonus": {"cha": 2, "int": 1}, "speed": 30},
    "dragonborn": {"name": "Драконорождённый", "about": "+2 Сила, +1 Харизма; чешуя и гордость", "bonus": {"str": 2, "cha": 1}, "speed": 30},
    "gnome": {"name": "Гном", "about": "+2 Интеллект, +1 Телосложение; мелкий гений с прибабахом", "bonus": {"int": 2, "con": 1}, "speed": 25},
}

# Быстрые пресеты: порядок характеристик под стандартный набор, хиты, КД, атаки и стартовое барахло
CLASSES = {
    "fighter": {
        "about": "крепкий боец в тяжёлой броне, бьёт мечом, почти не умирает",
        "name": "Воин", "hit_die": 10, "priority": ["str", "con", "dex", "wis", "cha", "int"],
        "saves": ["str", "con"], "skills": ["athletics", "intimidation"],
        "armor": {"base": 16, "dex_max": 0, "shield": 2},
        "attacks": [{"name": "Длинный меч", "ability": "str", "damage": "1d8"},
                    {"name": "Лёгкий арбалет", "ability": "dex", "damage": "1d8"}],
        "items": ["кольчуга", "щит", "длинный меч", "лёгкий арбалет и 20 болтов", "набор путешественника"],
        "features": ["Второе дыхание (1/отдых: 1d10+уровень хитов)", "Боевой стиль: Оборона"],
        "spells": [],
    },
    "barbarian": {
        "about": "ярость, огромная секира, броня не нужна — есть мышцы",
        "name": "Варвар", "hit_die": 12, "priority": ["str", "con", "dex", "wis", "cha", "int"],
        "saves": ["str", "con"], "skills": ["athletics", "survival"],
        "armor": {"unarmored": "con"},
        "attacks": [{"name": "Секира", "ability": "str", "damage": "1d12"},
                    {"name": "Метательное копьё", "ability": "str", "damage": "1d6"}],
        "items": ["секира", "4 метательных копья", "набор путешественника"],
        "features": ["Ярость (2/долгий отдых: +2 к урону, сопротивление физ. урону)", "Защита без доспехов"],
        "spells": [],
    },
    "rogue": {
        "about": "скрытность, замки, ловушки и удар в спину на +1d6",
        "name": "Плут", "hit_die": 8, "priority": ["dex", "con", "int", "wis", "cha", "str"],
        "saves": ["dex", "int"], "skills": ["stealth", "sleight_of_hand", "deception", "perception"],
        "armor": {"base": 11},
        "attacks": [{"name": "Рапира", "ability": "dex", "damage": "1d8"},
                    {"name": "Короткий лук", "ability": "dex", "damage": "1d6"}],
        "items": ["кожаный доспех", "рапира", "короткий лук и 20 стрел", "воровские инструменты"],
        "features": ["Скрытая атака +1d6", "Воровской жаргон"],
        "spells": [],
    },
    "ranger": {
        "about": "лучник и следопыт, знает лес, стреляет издалека",
        "name": "Следопыт", "hit_die": 10, "priority": ["dex", "wis", "con", "str", "int", "cha"],
        "saves": ["str", "dex"], "skills": ["survival", "perception", "stealth"],
        "armor": {"base": 12},
        "attacks": [{"name": "Длинный лук", "ability": "dex", "damage": "1d8"},
                    {"name": "Короткий меч", "ability": "dex", "damage": "1d6"}],
        "items": ["проклёпанная кожа", "длинный лук и 20 стрел", "два коротких меча"],
        "features": ["Избранный враг", "Исследователь природы"],
        "spells": [],
    },
    "wizard": {
        "about": "хрупкий маг: огненные снаряды, сон, щит; думает головой",
        "name": "Волшебник", "hit_die": 6, "priority": ["int", "con", "dex", "wis", "cha", "str"],
        "saves": ["int", "wis"], "skills": ["arcana", "history"],
        "armor": {"base": 10},
        "attacks": [{"name": "Огненный снаряд", "ability": "int", "damage": "1d10", "spell": True},
                    {"name": "Кинжал", "ability": "dex", "damage": "1d4"}],
        "items": ["книга заклинаний", "магическая фокусировка", "кинжал", "набор учёного"],
        "features": ["Ячейки 1 ур.: 2", "Магическое восстановление"],
        "spells": ["Fire Bolt", "Mage Hand", "Magic Missile", "Shield", "Sleep", "Burning Hands"],
    },
    "cleric": {
        "about": "лечит, благословляет и бьёт булавой во имя бога",
        "name": "Жрец", "hit_die": 8, "priority": ["wis", "con", "str", "cha", "dex", "int"],
        "saves": ["wis", "cha"], "skills": ["medicine", "religion"],
        "armor": {"base": 14, "dex_max": 2, "shield": 2},
        "attacks": [{"name": "Булава", "ability": "str", "damage": "1d6"},
                    {"name": "Священное пламя", "ability": "wis", "damage": "1d8", "spell": True, "save": "dex"}],
        "items": ["чешуйчатый доспех", "щит", "булава", "священный символ"],
        "features": ["Ячейки 1 ур.: 2", "Домен Жизни: лечение сильнее"],
        "spells": ["Sacred Flame", "Guidance", "Cure Wounds", "Bless", "Healing Word"],
    },
    "bard": {
        "about": "болтун и музыкант: вдохновляет своих, высмеивает врагов до крови",
        "name": "Бард", "hit_die": 8, "priority": ["cha", "dex", "con", "wis", "int", "str"],
        "saves": ["dex", "cha"], "skills": ["performance", "persuasion", "deception"],
        "armor": {"base": 11},
        "attacks": [{"name": "Злая насмешка", "ability": "cha", "damage": "1d4", "spell": True, "save": "wis"},
                    {"name": "Рапира", "ability": "dex", "damage": "1d8"}],
        "items": ["кожаный доспех", "рапира", "лютня", "набор артиста"],
        "features": ["Ячейки 1 ур.: 2", "Вдохновение барда (d6, ХАР раз за долгий отдых)"],
        "spells": ["Vicious Mockery", "Minor Illusion", "Healing Word", "Charm Person", "Dissonant Whispers"],
    },
    "paladin": {
        "about": "святой воин в латах, лечит руками, держит строй",
        "name": "Паладин", "hit_die": 10, "priority": ["str", "cha", "con", "wis", "dex", "int"],
        "saves": ["wis", "cha"], "skills": ["athletics", "persuasion"],
        "armor": {"base": 16, "dex_max": 0, "shield": 2},
        "attacks": [{"name": "Длинный меч", "ability": "str", "damage": "1d8"},
                    {"name": "Метательное копьё", "ability": "str", "damage": "1d6"}],
        "items": ["кольчуга", "щит", "длинный меч", "священный символ"],
        "features": ["Божественное чувство", "Наложение рук (5 × уровень хитов)"],
        "spells": [],
    },
}


def mod(score: int) -> int:
    return (score - 10) // 2


def prof_bonus(level: int) -> int:
    return 2 + (max(level, 1) - 1) // 4


def fmt_mod(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def resolve_ability(name: str) -> str | None:
    key = (name or "").strip().lower()
    if key in ABILITIES:
        return key
    return ABILITY_ALIASES.get(key)


def resolve_skill(name: str) -> str | None:
    key = (name or "").strip().lower().replace("-", " ")
    if key.replace(" ", "_") in SKILLS:
        return key.replace(" ", "_")
    return SKILL_ALIASES.get(key)


def calc_ac(cls: dict, abilities: dict) -> int:
    armor = cls["armor"]
    dex = mod(abilities["dex"])
    if "unarmored" in armor:
        return 10 + dex + mod(abilities[armor["unarmored"]])
    dex_max = armor.get("dex_max")
    dex_part = dex if dex_max is None else min(dex, dex_max)
    return armor["base"] + dex_part + armor.get("shield", 0)


def recommended_scores(class_key: str) -> dict:
    """Стандартный набор 15/14/13/12/10/8, разложенный под класс."""
    return dict(zip(CLASSES[class_key]["priority"], STANDARD_ARRAY))


def build_character(name: str, class_key: str, race_key: str, base: dict | None = None,
                    look: str = "", backstory: str = "") -> dict:
    """base — характеристики до расовых бонусов; без них раскладка по классу."""
    cls = CLASSES[class_key]
    race = RACES[race_key]
    base = base or recommended_scores(class_key)
    abilities = {a: base[a] + race["bonus"].get(a, 0) for a in ABILITIES}
    max_hp = cls["hit_die"] + mod(abilities["con"])
    return {
        "name": name,
        "class": class_key,
        "race": race_key,
        "level": 1,
        "xp": 0,
        "abilities": abilities,
        "max_hp": max_hp,
        "hp": max_hp,
        "ac": calc_ac(cls, abilities),
        "speed": race["speed"],
        "saves": list(cls["saves"]),
        "skills": list(cls["skills"]),
        "attacks": [dict(a) for a in cls["attacks"]],
        "inventory": {item: 1 for item in cls["items"]},
        "gold": 10,
        "features": list(cls["features"]),
        "spells": list(cls["spells"]),
        "conditions": [],
        "death": {"success": 0, "fail": 0, "stable": False, "dead": False},
        "look": look,
        "backstory": backstory,
        "stats": {},
    }


def attack_bonus(char: dict, attack: dict) -> int:
    return mod(char["abilities"][attack["ability"]]) + prof_bonus(char["level"])


def attack_damage(char: dict, attack: dict) -> str:
    bonus = mod(char["abilities"][attack["ability"]])
    # Модификатор характеристики к урону заговоров в 5e не добавляется
    if attack.get("spell") or bonus == 0:
        return attack["damage"]
    return f"{attack['damage']}{fmt_mod(bonus)}"


def spell_dc(char: dict, ability: str) -> int:
    return 8 + prof_bonus(char["level"]) + mod(char["abilities"][ability])


def check_bonus(char: dict, what: str) -> tuple[int, str] | None:
    """Модификатор проверки навыка, характеристики или спасброска (save:dex)."""
    what = (what or "").strip().lower()
    pb = prof_bonus(char["level"])
    if what.startswith(("save:", "спас:", "спасбросок:")):
        ability = resolve_ability(what.split(":", 1)[1])
        if not ability:
            return None
        bonus = mod(char["abilities"][ability]) + (pb if ability in char["saves"] else 0)
        return bonus, f"спасбросок {ABILITIES[ability]}"
    skill = resolve_skill(what)
    if skill:
        ru, ability = SKILLS[skill]
        bonus = mod(char["abilities"][ability]) + (pb if skill in char["skills"] else 0)
        return bonus, ru
    ability = resolve_ability(what)
    if ability:
        return mod(char["abilities"][ability]), ABILITIES[ability]
    return None


def level_for_xp(xp: int) -> int:
    level = 1
    for i, threshold in enumerate(XP_LEVELS):
        if xp >= threshold:
            level = i + 1
    return level


def sheet_text(char: dict) -> str:
    cls = CLASSES[char["class"]]
    race = RACES[char["race"]]
    ab = " · ".join(
        f"{ABILITIES[a][:3]} {s} ({fmt_mod(mod(s))})" for a, s in char["abilities"].items()
    )
    skills = ", ".join(SKILLS[s][0] for s in char["skills"])
    attacks = "; ".join(
        f"{a['name']} {fmt_mod(attack_bonus(char, a))}, {attack_damage(char, a)}" for a in char["attacks"]
    )
    items = ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in char["inventory"].items()) or "пусто"
    lines = [
        f"<b>{html.escape(char['name'])}</b> — {race['name']}, {cls['name']} {char['level']} ур. (опыт {char['xp']})",
        f"❤️ {char['hp']}/{char['max_hp']}  🛡 КД {char['ac']}  👣 {char['speed']} фт.  бонус мастерства +{prof_bonus(char['level'])}",
        ab,
        f"Навыки: {skills}",
        f"Атаки: {attacks}",
        f"Вещи: {html.escape(items)}; золото: {char['gold']}",
        f"Умения: {'; '.join(char['features'])}",
    ]
    if char["spells"]:
        lines.append(f"Заклинания: {', '.join(char['spells'])}")
    if char.get("look"):
        lines.append(f"Внешность и характер: {html.escape(char['look'])}")
    if char.get("backstory"):
        lines.append(f"Предыстория: {html.escape(char['backstory'])}")
    if char["conditions"]:
        lines.append(f"Состояния: {html.escape(', '.join(char['conditions']))}")
    if char["death"]["dead"]:
        lines.append("☠️ Мёртв. Соболезнуем, но не очень.")
    return "\n".join(lines)


def brief_for_prompt(char: dict) -> str:
    """Короткая строка о персонаже для промпта ГМа."""
    cls = CLASSES[char["class"]]
    race = RACES[char["race"]]
    ab = ", ".join(f"{a.upper()} {s}" for a, s in char["abilities"].items())
    attacks = "; ".join(a["name"] for a in char["attacks"])
    state = f"HP {char['hp']}/{char['max_hp']}, AC {char['ac']}"
    if char["conditions"]:
        state += f", состояния: {', '.join(char['conditions'])}"
    if char["death"]["dead"]:
        state += ", МЁРТВ"
    spells = f"; заклинания: {', '.join(char['spells'])}" if char["spells"] else ""
    items = ", ".join(char["inventory"])
    flavor = ""
    if char.get("look"):
        flavor += f"; облик: {char['look'][:200]}"
    if char.get("backstory"):
        flavor += f"; предыстория: {char['backstory'][:200]}"
    return (
        f"{char['name']} ({race['name']} {cls['name']} {char['level']} ур.): {state}; {ab}; "
        f"атаки: {attacks}{spells}; вещи: {items}; золото {char['gold']}{flavor}"
    )


STAT_NAMES = [
    ("actions", "заявок"), ("checks", "проверок"), ("checks_ok", "из них успешных"), ("attacks", "атак"),
    ("hits", "попаданий"), ("crits", "критов"), ("fumbles", "позорных промахов"), ("kills", "убийств"),
    ("damage_dealt", "урона нанесено"), ("damage_taken", "урона получено"), ("healed", "вылечено хитов"),
    ("knockouts", "раз падал без сознания"),
]


def stats_text(stats: dict) -> str:
    parts = [f"{label} {stats[key]}" for key, label in STAT_NAMES if stats.get(key)]
    return ", ".join(parts) or "пока ничего не сделал, только место занимает"


# --- Справочник SRD через dnd5eapi.co ---

SRD_API = "https://www.dnd5eapi.co/api/2014"
SRD_CATEGORIES = {"monsters", "spells", "equipment", "magic-items", "conditions", "rules", "rule-sections"}
_srd_cache: dict[str, dict | None] = {}


async def srd_fetch(path: str) -> dict | None:
    if path in _srd_cache:
        return _srd_cache[path]
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{SRD_API}/{path}") as resp:
                data = await resp.json() if resp.status == 200 else None
    except Exception as e:
        logging.warning(f"SRD {path} не ответил: {e}")
        return None
    _srd_cache[path] = data
    return data


async def srd_find(category: str, query: str) -> dict | None:
    """Ищет запись по английскому названию: сначала точный индекс, потом поиск по имени."""
    if category not in SRD_CATEGORIES:
        return None
    index = query.strip().lower().replace(" ", "-").replace("'", "")
    data = await srd_fetch(f"{category}/{index}")
    if data and "index" in data:
        return data
    found = await srd_fetch(f"{category}?name={query.strip()}")
    results = (found or {}).get("results") or []
    if not results:
        return None
    return await srd_fetch(f"{category}/{results[0]['index']}")


def _desc(value) -> str:
    if isinstance(value, list):
        return " ".join(map(str, value))
    return str(value or "")


def srd_summary(category: str, data: dict, limit: int = 1500) -> str:
    """Ужимает ответ SRD до важного, чтобы не раздувать промпт."""
    if category == "monsters":
        ac = data.get("armor_class") or [{}]
        actions = "; ".join(
            f"{a['name']}: {a.get('desc', '')}" for a in data.get("actions", [])
        )
        special = "; ".join(f"{a['name']}: {a.get('desc', '')}" for a in data.get("special_abilities", []))
        text = (
            f"{data['name']} (CR {data.get('challenge_rating')}, {data.get('xp')} XP): "
            f"AC {ac[0].get('value')}, HP {data.get('hit_points')} ({data.get('hit_dice')}), "
            f"STR {data.get('strength')} DEX {data.get('dexterity')} CON {data.get('constitution')} "
            f"INT {data.get('intelligence')} WIS {data.get('wisdom')} CHA {data.get('charisma')}. "
            f"Действия: {actions}. Особенности: {special}"
        )
    elif category == "spells":
        text = (
            f"{data['name']} (уровень {data.get('level')}, {data.get('school', {}).get('name')}), "
            f"время: {data.get('casting_time')}, дистанция: {data.get('range')}, "
            f"длительность: {data.get('duration')}. {_desc(data.get('desc'))} {_desc(data.get('higher_level'))}"
        )
    else:
        text = f"{data.get('name')}: {_desc(data.get('desc'))}"
        for key in ("damage", "armor_class", "cost", "weight"):
            if data.get(key):
                text += f" {key}: {data[key]}"
    return text[:limit]


def monster_from_srd(data: dict) -> dict:
    """Боевые параметры монстра для трекера: HP, КД, инициатива, атаки с бонусом и уроном."""
    attacks = []
    for action in data.get("actions", []):
        if action.get("attack_bonus") is None:
            continue
        dice = "+".join(d["damage_dice"] for d in action.get("damage", []) if d.get("damage_dice"))
        if dice:
            attacks.append({"name": action["name"], "bonus": action["attack_bonus"], "damage": dice})
    if not attacks:
        # У некоторых монстров SRD атаки описаны только текстом — даём базовую по Силе
        bonus = data.get("proficiency_bonus", 2) + mod(data.get("strength", 10))
        attacks.append({"name": "Атака", "bonus": bonus, "damage": f"1d6{fmt_mod(mod(data.get('strength', 10)))}"})
    ac = (data.get("armor_class") or [{"value": 12}])[0].get("value", 12)
    return {
        "name": data["name"],
        "srd": data["index"],
        "hp": data.get("hit_points", 10),
        "max_hp": data.get("hit_points", 10),
        "ac": ac,
        "init_bonus": mod(data.get("dexterity", 10)),
        "attacks": attacks,
        "xp": data.get("xp", 0),
    }
