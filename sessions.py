"""Trading calendars for every market the platform covers.

Hours come from the exchanges' published schedules (cross-checked against
Wikipedia's [list of major stock exchanges], marketshours.com and iTick). Times
are local to the exchange and resolved with IANA zones, so daylight saving is
handled by the tz database rather than by hand.

Public holidays are deliberately *not* modelled: markets simply look open on a
weekday, and the collector's empty-response handling covers the handful of
holiday days. Weekends and the 24/5 FX/futures close are handled exactly.

[list of major stock exchanges]: https://en.wikipedia.org/wiki/List_of_major_stock_exchanges
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class Session:
    key: str
    label: str
    tz: str
    windows: tuple[tuple[int, int], ...] = ()   # (start, end) minutes after local midnight
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday = 0
    kind: str = "daily"                          # "daily" | "fx"
    close_hour_utc: int = 22                     # fx: weekly close on Friday


def _hm(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _w(*pairs: str) -> tuple[tuple[int, int], ...]:
    return tuple((_hm(a), _hm(b)) for a, b in (p.split("-") for p in pairs))


SESSIONS: dict[str, Session] = {
    # --- Asia ---------------------------------------------------------
    "CN": Session("CN", "沪深京", "Asia/Shanghai", _w("09:30-11:30", "13:00-15:00")),
    "HK": Session("HK", "香港", "Asia/Hong_Kong", _w("09:30-12:00", "13:00-16:00")),
    "JP": Session("JP", "东京", "Asia/Tokyo", _w("09:00-11:30", "12:30-15:30")),
    "KR": Session("KR", "首尔", "Asia/Seoul", _w("09:00-15:30")),
    "TW": Session("TW", "台北", "Asia/Taipei", _w("09:00-13:30")),
    "SG": Session("SG", "新加坡", "Asia/Singapore", _w("09:00-12:00", "13:00-17:00")),
    "IN": Session("IN", "印度", "Asia/Kolkata", _w("09:15-15:30")),
    "ID": Session("ID", "雅加达", "Asia/Jakarta", _w("09:00-12:00", "13:30-15:50")),
    "TH": Session("TH", "曼谷", "Asia/Bangkok", _w("10:00-12:30", "14:30-16:30")),
    "MY": Session("MY", "吉隆坡", "Asia/Kuala_Lumpur", _w("09:00-12:30", "14:30-17:00")),
    "VN": Session("VN", "胡志明", "Asia/Ho_Chi_Minh", _w("09:15-11:30", "13:00-14:45")),
    # --- Europe -------------------------------------------------------
    "UK": Session("UK", "伦敦", "Europe/London", _w("08:00-16:30")),
    "DE": Session("DE", "法兰克福", "Europe/Berlin", _w("09:00-17:30")),
    "FR": Session("FR", "巴黎", "Europe/Paris", _w("09:00-17:30")),
    "CH": Session("CH", "苏黎世", "Europe/Zurich", _w("09:00-17:30")),
    "SE": Session("SE", "斯德哥尔摩", "Europe/Stockholm", _w("09:00-17:30")),
    "RU": Session("RU", "莫斯科", "Europe/Moscow", _w("10:00-18:50")),
    "TR": Session("TR", "伊斯坦布尔", "Europe/Istanbul", _w("10:00-18:00")),
    # --- Americas -----------------------------------------------------
    "US": Session("US", "纽约", "America/New_York", _w("09:30-16:00")),
    "CA": Session("CA", "多伦多", "America/Toronto", _w("09:30-16:00")),
    "BR": Session("BR", "圣保罗", "America/Sao_Paulo", _w("10:00-17:00")),
    "MX": Session("MX", "墨西哥城", "America/Mexico_City", _w("08:30-15:00")),
    # --- Oceania / Africa --------------------------------------------
    "AU": Session("AU", "悉尼", "Australia/Sydney", _w("10:00-16:00")),
    "NZ": Session("NZ", "新西兰", "Pacific/Auckland", _w("10:00-16:45")),
    "ZA": Session("ZA", "约翰内斯堡", "Africa/Johannesburg", _w("09:00-17:00")),
    "SA": Session("SA", "利雅得", "Asia/Riyadh", _w("10:00-15:00")),
    # --- 24/5 ---------------------------------------------------------
    "FX": Session("FX", "外汇 24/5", "UTC", (), kind="fx"),
    "GLOB_FUT": Session("GLOB_FUT", "国际期货", "UTC", (), kind="fx", close_hour_utc=21),
    # --- CN futures (day + night session) -----------------------------
    "CN_FUT": Session("CN_FUT", "国内期货", "Asia/Shanghai",
                      _w("09:00-11:30", "13:30-15:00", "21:00-26:30")),
}

# Index baskets span several exchanges: they are "open" while any leg trades.
BASKETS: dict[str, tuple[str, ...]] = {
    "ASIA": ("CN", "HK", "JP", "KR", "TW", "SG", "IN", "ID", "TH", "MY", "VN"),
    "EUROPE": ("UK", "DE", "FR", "CH", "SE", "RU"),
    "AMERICA": ("US", "CA", "BR", "MX"),
    "OCEANIA": ("AU", "NZ"),
    "WORLD": ("CN", "HK", "JP", "US", "UK", "DE", "FR", "AU", "IN", "BR", "CA"),
}

# How long after the close we still take one final snapshot.
FINAL_SNAPSHOT_GRACE = 25 * 60


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _window_bounds(day: datetime, start_min: int, end_min: int, tz) -> tuple[datetime, datetime]:
    midnight = datetime.combine(day.date(), time(0), tzinfo=tz)
    return midnight + timedelta(minutes=start_min), midnight + timedelta(minutes=end_min)


def _daily_state(session: Session, now: float) -> dict:
    tz = _zone(session.tz)
    now_local = datetime.fromtimestamp(now, tz)
    open_now = None
    next_open = None
    last_close = None
    for offset in range(-1, 9):
        day = now_local + timedelta(days=offset)
        if day.weekday() not in session.weekdays:
            continue
        for start_min, end_min in session.windows:
            start, end = _window_bounds(day, start_min, end_min, tz)
            if start <= now_local < end:
                open_now = (start, end)
            elif end <= now_local and (last_close is None or end > last_close):
                last_close = end
            elif start > now_local and next_open is None:
                next_open = start
        if open_now:
            break
    if open_now:
        start, end = open_now
        return {
            "open": True, "phase": "open",
            "open_at": start.timestamp(), "close_at": end.timestamp(),
            "seconds_to_close": max(0, int(end.timestamp() - now)),
            "next_open": next_open.timestamp() if next_open else None,
            "last_close": last_close.timestamp() if last_close else None,
            "local_time": now_local.strftime("%H:%M"),
            "local_date": now_local.strftime("%Y-%m-%d"),
        }
    # closed: distinguish lunch break, after-hours and pre-open
    phase = "closed"
    traded_today = last_close is not None and last_close.date() == now_local.date()
    if now_local.weekday() >= 5:
        phase = "weekend"
    elif next_open is None:
        phase = "closed"
    elif next_open.date() == now_local.date():
        gap = (next_open - now_local).total_seconds()
        if traded_today and gap <= 2 * 3600:
            phase = "lunch"
        elif traded_today:
            phase = "closed"
        else:
            phase = "pre"
    elif traded_today:
        phase = "closed"
    else:
        phase = "pre"
    return {
        "open": False, "phase": phase,
        "open_at": None, "close_at": None,
        "seconds_to_open": max(0, int(next_open.timestamp() - now)) if next_open else None,
        "next_open": next_open.timestamp() if next_open else None,
        "last_close": last_close.timestamp() if last_close else None,
        "seconds_since_close": max(0, int(now - last_close.timestamp())) if last_close else None,
        "local_time": now_local.strftime("%H:%M"),
        "local_date": now_local.strftime("%Y-%m-%d"),
    }


def _fx_state(session: Session, now: float) -> dict:
    """Sunday 22:00 UTC -> Friday close_hour UTC, closed all weekend."""
    dt = datetime.fromtimestamp(now, UTC)
    weekday = dt.weekday()          # Monday = 0 ... Sunday = 6
    hour = dt.hour + dt.minute / 60.0
    close_hour = session.close_hour_utc

    def next_sunday_2200(ref: datetime) -> datetime:
        days_ahead = (6 - ref.weekday()) % 7
        if days_ahead == 0 and hour >= 22:
            days_ahead = 7
        return datetime.combine((ref + timedelta(days=days_ahead)).date(), time(22), tzinfo=UTC)

    is_open = (
        (0 <= weekday <= 3)
        or (weekday == 4 and hour < close_hour)
        or (weekday == 6 and hour >= 22)
    )
    if is_open:
        if weekday == 4:
            close_dt = datetime.combine(dt.date(), time(close_hour), tzinfo=UTC)
        elif weekday == 6:
            close_dt = datetime.combine((dt + timedelta(days=5)).date(), time(close_hour), tzinfo=UTC)
        else:
            close_dt = datetime.combine((dt + timedelta(days=4 - weekday)).date(),
                                        time(close_hour), tzinfo=UTC)
        return {
            "open": True, "phase": "open",
            "open_at": None, "close_at": close_dt.timestamp(),
            "seconds_to_close": max(0, int(close_dt.timestamp() - now)),
            "next_open": None, "last_close": None,
            "local_time": dt.strftime("%H:%M"), "local_date": dt.strftime("%Y-%m-%d"),
        }
    next_open_dt = next_sunday_2200(dt)
    last_close = None
    if weekday == 4 and hour >= close_hour:
        last_close = datetime.combine(dt.date(), time(close_hour), tzinfo=UTC)
    elif weekday == 5:
        last_close = datetime.combine((dt - timedelta(days=1)).date(), time(close_hour), tzinfo=UTC)
    return {
        "open": False, "phase": "weekend" if weekday >= 4 else "closed",
        "open_at": None, "close_at": None,
        "seconds_to_open": max(0, int(next_open_dt.timestamp() - now)),
        "next_open": next_open_dt.timestamp(),
        "last_close": last_close.timestamp() if last_close else None,
        "seconds_since_close": max(0, int(now - last_close.timestamp())) if last_close else None,
        "local_time": dt.strftime("%H:%M"), "local_date": dt.strftime("%Y-%m-%d"),
    }


def state(spec: str | tuple[str, ...] | list[str], now: float) -> dict:
    """Resolve a session spec (key, basket or list) into a status dict."""
    keys: tuple[str, ...]
    if isinstance(spec, str):
        if spec in BASKETS:
            keys = BASKETS[spec]
        else:
            keys = (spec,)
    else:
        keys = tuple(spec)

    states = []
    for key in keys:
        session = SESSIONS.get(key)
        if session is None:
            continue
        states.append(_fx_state(session, now) if session.kind == "fx" else _daily_state(session, now))
    if not states:
        return {"open": False, "phase": "unknown", "label": "未知", "sessions": []}

    opened = [s for s in states if s["open"]]
    if opened:
        current = min(opened, key=lambda s: s["seconds_to_close"])
        out = dict(current)
        out["seconds_to_open"] = None
    else:
        current = min((s for s in states if s.get("next_open")),
                      key=lambda s: s["next_open"], default=states[0])
        out = dict(current)
    out["label"] = SESSIONS[keys[0]].label if len(keys) == 1 else "多市场"
    out["sessions"] = [SESSIONS[k].key for k in keys if k in SESSIONS]
    return out


def describe(spec: str | tuple[str, ...], now: float) -> dict:
    """Compact state for the API/UI."""
    st = state(spec, now)
    phase = st.get("phase", "closed")
    text = {
        "open": "交易中", "lunch": "午间休市", "pre": "待开盘",
        "closed": "已收盘", "weekend": "周末休市", "unknown": "未知",
    }.get(phase, phase)
    return {
        "open": phase == "open",
        "phase": phase,
        "text": text,
        "local_time": st.get("local_time"),
        "close_in": st.get("seconds_to_close"),
        "open_in": st.get("seconds_to_open"),
        "next_open": st.get("next_open"),
        "close_at": st.get("close_at"),
        "sessions": st.get("sessions"),
    }


def should_collect(spec, now: float, last_ts: float, interval: int) -> tuple[bool, dict]:
    """Whether a market needs a fetch now, plus its state for logging/UI."""
    st = state(spec, now)
    if st["open"]:
        return now - last_ts >= interval, st
    # one last snapshot after the bell so the board shows the official close
    since = st.get("seconds_since_close")
    last_close = st.get("last_close")
    if since is not None and since <= FINAL_SNAPSHOT_GRACE and last_close and last_ts < last_close:
        return True, st
    return False, st


def all_sessions() -> dict[str, dict]:
    now = datetime.now(UTC).timestamp()
    return {key: describe(key, now) for key in SESSIONS}
