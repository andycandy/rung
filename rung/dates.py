"""Date understanding for requests. Rule: if the request contains something that looks
like a date and we can't resolve it confidently, ask -- never fall back to "today"."""
from __future__ import annotations

import calendar
import os
import re
from datetime import date, datetime, timedelta

os.environ.setdefault("TZ", "Asia/Kolkata")
if os.environ.get("TZ") == "Asia/Calcutta":  # tzlocal rejects the legacy alias
    os.environ["TZ"] = "Asia/Kolkata"

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = [m.lower() for m in calendar.month_name[1:]] + [m.lower() for m in calendar.month_abbr[1:]]
DATEISH = re.compile(r"\b(" + "|".join(WEEKDAYS + MONTHS) + r")\b|\b\d{1,2}(st|nd|rd|th)\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}\b|"
                     r"\b(today|tomorrow|tonight|end of|next week|this week|next month|in \w+ (days|weeks))\b", re.I)


def has_date(text: str) -> bool:
    return bool(DATEISH.search(text or ""))


def resolve(text: str, today: date) -> tuple[str | None, str | None]:
    """-> (ISO date or None, the phrase it came from). None + phrase means 'date-like but unclear'."""
    low = (text or "").lower()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)
    if m:
        return m.group(1), m.group(1)
    m = re.search(r"\b(next|this|coming)\s+(" + "|".join(WEEKDAYS) + r")\b", low)
    if m:
        wd = WEEKDAYS.index(m.group(2))
        if m.group(1) == "next":  # "next Friday" = the Friday of next calendar week
            d = today + timedelta(days=7 - today.weekday() + wd)
        else:
            d = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        return d.isoformat(), m.group(0)
    m = re.search(r"\bend of (the |this )?month\b", low)
    if m:
        return today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat(), m.group(0)
    m = re.search(r"\b(today|tonight)\b", low)
    if m:
        return today.isoformat(), m.group(0)
    m = re.search(r"\btomorrow\b", low)
    if m:
        return (today + timedelta(days=1)).isoformat(), m.group(0)
    if not has_date(low):
        return None, None
    try:
        from dateparser.search import search_dates
        found = search_dates(text, languages=["en"], settings={
            "RELATIVE_BASE": datetime.combine(today, datetime.min.time()), "PREFER_DATES_FROM": "future"})
    except Exception:  # noqa: BLE001
        found = None
    if found:
        phrase, dt = found[-1]
        if re.search(r"\b(next|last|end|week|month)\b", phrase.lower()) or not has_date(phrase):
            return None, phrase  # relative phrases dateparser half-understood: ask
        return dt.date().isoformat(), phrase
    return None, DATEISH.search(low).group(0)
