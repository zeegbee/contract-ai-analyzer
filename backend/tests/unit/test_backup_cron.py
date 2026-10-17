from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.backup.cron import CronError, next_fire, parse, previous_fire

UTC_TZ = ZoneInfo("UTC")
MSK = ZoneInfo("Europe/Moscow")


def at(year: int, month: int, day: int, hour: int, minute: int, tz: ZoneInfo = UTC_TZ) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def test_daily_at_three() -> None:
    spec = parse("0 3 * * *")
    now = at(2026, 10, 17, 14, 0)
    assert previous_fire(spec, now, UTC_TZ) == at(2026, 10, 17, 3, 0)
    assert next_fire(spec, now, UTC_TZ) == at(2026, 10, 18, 3, 0)
    # ровно в момент запуска — это предыдущий запуск, следующий — завтра
    assert previous_fire(spec, at(2026, 10, 17, 3, 0), UTC_TZ) == at(2026, 10, 17, 3, 0)


def test_timezone_is_applied() -> None:
    spec = parse("0 3 * * *")
    # 03:00 по Москве = 00:00 UTC
    now = datetime(2026, 10, 17, 1, 0, tzinfo=UTC)
    assert previous_fire(spec, now, MSK) == at(2026, 10, 17, 3, 0, tz=MSK)
    assert previous_fire(spec, now, MSK).astimezone(UTC) == datetime(2026, 10, 17, 0, 0, tzinfo=UTC)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("expr", "now", "expected_next"),
    [
        ("*/15 * * * *", at(2026, 10, 17, 10, 7), at(2026, 10, 17, 10, 15)),
        ("0 */6 * * *", at(2026, 10, 17, 10, 7), at(2026, 10, 17, 12, 0)),
        ("30 2 * * 0", at(2026, 10, 17, 10, 0), at(2026, 10, 18, 2, 30)),  # воскресенье
        ("30 2 * * 7", at(2026, 10, 17, 10, 0), at(2026, 10, 18, 2, 30)),  # 7 — тоже воскресенье
        ("0 4 1 * *", at(2026, 10, 17, 10, 0), at(2026, 11, 1, 4, 0)),
        ("0 4 1-5/2 * *", at(2026, 10, 2, 10, 0), at(2026, 10, 3, 4, 0)),
        ("0 1 * * 1-5", at(2026, 10, 17, 10, 0), at(2026, 10, 19, 1, 0)),  # суббота → понедельник
        ("@daily", at(2026, 10, 17, 10, 0), at(2026, 10, 18, 0, 0)),
        ("@weekly", at(2026, 10, 17, 10, 0), at(2026, 10, 18, 0, 0)),
        ("0 0 29 2 *", at(2026, 10, 17, 10, 0), at(2028, 2, 29, 0, 0)),
    ],
)
def test_next_fire(expr: str, now: datetime, expected_next: datetime) -> None:
    assert next_fire(parse(expr), now, UTC_TZ) == expected_next


def test_day_of_month_or_weekday() -> None:
    # Ограничены и день месяца, и день недели — срабатывает любое (как в cron).
    spec = parse("0 0 13 * 5")
    assert next_fire(spec, at(2026, 10, 10, 0, 0), UTC_TZ) == at(2026, 10, 13, 0, 0)  # 13-е
    assert next_fire(spec, at(2026, 10, 13, 0, 0), UTC_TZ) == at(2026, 10, 16, 0, 0)  # пятница


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "0 3 * *",
        "60 3 * * *",
        "0 24 * * *",
        "0 3 0 * *",
        "0 3 * 13 *",
        "0 3 * * 8",
        "a b c d e",
        "*/0 * * * *",
        "5-1 * * * *",
        "0 0 31 2 *",
    ],
)
def test_invalid_expressions(expr: str) -> None:
    with pytest.raises(CronError):
        parse(expr)
