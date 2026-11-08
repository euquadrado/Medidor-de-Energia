from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Sao_Paulo")
PERIODS = {"dia": ("Hoje", 24), "semana": ("Esta semana", 7),
           "mes": ("Este mês", 31), "ano": ("Este ano", 12)}


def start_of(now, period):
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "semana":
        return day - timedelta(days=day.weekday())
    if period == "mes":
        return day.replace(day=1)
    if period == "ano":
        return day.replace(month=1, day=1)
    return day


def summarize(rows, now):
    starts = {p: start_of(now, p) for p in PERIODS}
    buckets = {p: [None] * size for p, (_, size) in PERIODS.items()}
    previous = None
    resets = gaps = 0
    for stamp, total in rows:
        local = stamp.astimezone(ZONE)
        if previous:
            before, old_total = previous
            delta = total - old_total
            if delta < 0:
                resets += 1
            else:
                if stamp - before > timedelta(minutes=5):
                    gaps += 1
                for p, start in starts.items():
                    if local >= start:
                        index = (local.hour if p == "dia" else local.month - 1
                                 if p == "ano" else (local.date() - start.date()).days)
                        group = buckets[p]
                        group[index] = (group[index] or 0) + delta
        previous = stamp, total
    return buckets, resets, gaps
