"""Расписание в формате cron (5 полей): разбор, предыдущий и следующий запуск.

Поддерживается: `*`, числа, диапазоны `a-b`, шаг `*/n` и `a-b/n`, списки через запятую, день недели
0–7 (0 и 7 — воскресенье), макросы `@hourly`, `@daily`, `@weekly`, `@monthly`. Если ограничены и день
месяца, и день недели, срабатывает любое из условий (как в cron). Время — в заданном часовом поясе.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}
# Поиск запуска ограничен: «31 февраля» и подобное никогда не срабатывает — это ошибка расписания.
_SEARCH_DAYS = 4 * 366 + 1


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = понедельник … 6 = воскресенье (как date.weekday())
    days_restricted: bool
    weekdays_restricted: bool

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = day.weekday() in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return dom or dow
        if self.days_restricted:
            return dom
        if self.weekdays_restricted:
            return dow
        return True

    def times(self) -> list[time]:
        return sorted(time(h, m) for h in self.hours for m in self.minutes)


def _field(text: str, low: int, high: int, name: str) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        step = 1
        part = item
        if "/" in item:
            part, step_text = item.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"некорректный шаг в поле «{name}»")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"некорректный диапазон в поле «{name}»")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = high
        else:
            raise CronError(f"некорректное значение в поле «{name}»")
        if start < low or end > high or start > end:
            raise CronError(f"значение вне диапазона {low}–{high} в поле «{name}»")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse(expression: str) -> CronSpec:
    text = MACROS.get(expression.strip().lower(), expression.strip())
    parts = text.split()
    if len(parts) != 5:
        raise CronError("нужно 5 полей: минута час день месяц день_недели")
    minute, hour, day, month, weekday = parts
    raw_weekdays = _field(weekday, 0, 7, "день недели")
    spec = CronSpec(
        minutes=_field(minute, 0, 59, "минута"),
        hours=_field(hour, 0, 23, "час"),
        days=_field(day, 1, 31, "день месяца"),
        months=_field(month, 1, 12, "месяц"),
        # cron: 0 и 7 — воскресенье; date.weekday(): 6 — воскресенье
        weekdays=frozenset((d - 1) % 7 for d in raw_weekdays),
        days_restricted=day != "*",
        weekdays_restricted=weekday != "*",
    )
    if next_fire(spec, datetime(2000, 1, 1, tzinfo=ZoneInfo("UTC")), ZoneInfo("UTC")) is None:
        raise CronError("расписание никогда не срабатывает")
    return spec


def _at(day: date, moment: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz)


def previous_fire(spec: CronSpec, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Последний момент запуска не позже `now`."""
    local = now.astimezone(tz)
    times = spec.times()
    for offset in range(_SEARCH_DAYS):
        day = local.date() - timedelta(days=offset)
        if not spec.matches_day(day):
            continue
        for moment in reversed(times):
            candidate = _at(day, moment, tz)
            if candidate <= local:
                return candidate
    return None


def next_fire(spec: CronSpec, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Ближайший момент запуска позже `now`."""
    local = now.astimezone(tz)
    times = spec.times()
    for offset in range(_SEARCH_DAYS):
        day = local.date() + timedelta(days=offset)
        if not spec.matches_day(day):
            continue
        for moment in times:
            candidate = _at(day, moment, tz)
            if candidate > local:
                return candidate
    return None
