"""Кубики. Бросает только код — ИИ получает готовые числа."""
import re
import random
from dataclasses import dataclass, field

# 2d6+3, d20, 4d6kh3, 1d20+5-1d4 — кубики и числа через + и -
TERM_RE = re.compile(r"([+-]?)\s*(?:(\d*)d(\d+)(?:(kh|kl)(\d+))?|(\d+))", re.I)
MAX_DICE = 100
MAX_SIDES = 1000

rng = random.SystemRandom()


class DiceError(ValueError):
    pass


@dataclass
class Roll:
    expr: str
    total: int
    parts: list[str] = field(default_factory=list)
    # Значение «натурального» d20 (для критов), если в броске был ровно один d20
    natural: int | None = None

    def __str__(self) -> str:
        return f"{self.expr}: {' '.join(self.parts)} = **{self.total}**"


def roll(expr: str, advantage: str = "none") -> Roll:
    """advantage: none | adv | dis — касается первого d20 в выражении."""
    src = expr.replace(" ", "").lower()
    if not src:
        raise DiceError("пустой бросок")
    pos = 0
    total = 0
    parts: list[str] = []
    natural = None
    d20_seen = False
    while pos < len(src):
        m = TERM_RE.match(src, pos)
        if not m or m.end() == pos:
            raise DiceError(f"не понял бросок «{expr}»")
        sign = -1 if m.group(1) == "-" else 1
        if pos > 0 and not m.group(1):
            raise DiceError(f"не понял бросок «{expr}»")
        prefix = "−" if sign < 0 else ("+" if parts else "")
        if m.group(6):
            value = int(m.group(6))
            parts.append(f"{prefix}{value}")
        else:
            count = int(m.group(2) or 1)
            sides = int(m.group(3))
            if not (1 <= count <= MAX_DICE and 2 <= sides <= MAX_SIDES):
                raise DiceError("слишком много кубиков, это не казино")
            if sides == 20 and count == 1 and not d20_seen and advantage in ("adv", "dis"):
                a, b = rng.randint(1, 20), rng.randint(1, 20)
                value = max(a, b) if advantage == "adv" else min(a, b)
                mark = "преим." if advantage == "adv" else "помеха"
                parts.append(f"{prefix}[{a}, {b} → {value} {mark}]")
            else:
                rolls = [rng.randint(1, sides) for _ in range(count)]
                kept = rolls
                if m.group(4):
                    k = max(1, min(int(m.group(5)), count))
                    kept = sorted(rolls, reverse=m.group(4) == "kh")[:k]
                value = sum(kept)
                shown = ", ".join(map(str, rolls))
                parts.append(f"{prefix}[{shown}]" + (f"→{value}" if kept is not rolls else ""))
            if sides == 20 and count == 1 and not d20_seen:
                d20_seen = True
                natural = value
        total += sign * value
        pos = m.end()
    return Roll(expr=expr.strip(), total=total, parts=parts, natural=natural)


def double_dice(expr: str) -> str:
    """Для крита: 1d8+3 → 2d8+3."""
    return re.sub(r"(\d*)d(\d+)", lambda m: f"{int(m.group(1) or 1) * 2}d{m.group(2)}", expr)
