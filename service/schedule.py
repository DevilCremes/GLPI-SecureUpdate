"""Recorrências de consulta em horas corridas ou no calendário civil escolhido."""
import re
from calendar import monthrange
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common import require


def validate(rule):
    require(isinstance(rule, dict), "Regra de calendário inválida.")
    kind = rule.get("kind")
    options = {"daily": set(), "weekly": {"weekday"}, "first": set(),
               "monthly": {"day"}, "last": set()}
    require(isinstance(kind, str) and kind in options
            and set(rule) == {"kind", "at", "zone"} | options[kind], "Periodicidade inválida.")
    require(isinstance(rule["at"], str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", rule["at"]),
            "Informe um horário válido.")
    require(isinstance(rule["zone"], str) and 0 < len(rule["zone"]) <= 100, "Fuso inválido.")
    try:
        ZoneInfo(rule["zone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Fuso horário indisponível no servidor.") from None
    for key, minimum, maximum in (("weekday", 1, 7), ("day", 1, 31)):
        if key in rule:
            require(type(rule[key]) is int and minimum <= rule[key] <= maximum, "Dia inválido.")


def next_check(state, now):
    rule = state.get("schedule")
    if rule is None:
        return now + state["hours"] * 3600
    zone = ZoneInfo(rule["zone"])
    date = datetime.fromtimestamp(now, zone).date()
    clock = time.fromisoformat(rule["at"])
    for _ in range(370):
        kind = rule["kind"]
        due = (kind == "daily" or (kind == "weekly" and date.isoweekday() == rule["weekday"])
               or (kind == "monthly" and date.day == rule["day"])
               or (kind == "first" and date.day == 1)
               or (kind == "last" and date.day == monthrange(date.year, date.month)[1]))
        if due:
            wall = datetime.combine(date, clock)
            # Horário repetido ocorre uma vez. Horário inexistente é pulado.
            stamp = wall.replace(tzinfo=zone, fold=0).timestamp()
            if stamp > now and datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == wall:
                return stamp
        date += timedelta(days=1)
    raise ValueError("Não foi possível calcular a próxima consulta.")
