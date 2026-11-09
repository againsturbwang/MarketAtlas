"""Assert the trading calendar at the moments that matter."""
import sys
from datetime import datetime, timezone

sys.path.insert(0, r"A:\Scraper\EastMoney")
import sessions


def ts(y, m, d, H, M, tz=timezone.utc):
    return datetime(y, m, d, H, M, tzinfo=tz).timestamp()


CASES = [
    # (label, spec, utc datetime, expected open?)
    ("CN pre-open 09:20 CST", "CN", ts(2026, 11, 5, 1, 20), False),
    ("CN open 09:31 CST", "CN", ts(2026, 11, 5, 1, 31), True),
    ("CN lunch 12:00 CST", "CN", ts(2026, 11, 5, 4, 0), False),
    ("CN afternoon 13:05 CST", "CN", ts(2026, 11, 5, 5, 5), True),
    ("CN closed 15:01 CST", "CN", ts(2026, 11, 5, 7, 1), False),
    ("CN weekend", "CN", ts(2026, 11, 7, 2, 0), False),
    ("HK open 10:00 HKT", "HK", ts(2026, 11, 5, 2, 0), True),
    ("HK lunch 12:30 HKT", "HK", ts(2026, 11, 5, 4, 30), False),
    ("US open 10:00 EST", "US", ts(2026, 11, 5, 15, 0), True),
    ("US closed 16:30 EST", "US", ts(2026, 11, 5, 21, 30), False),
    ("US pre 09:00 EST", "US", ts(2026, 11, 5, 14, 0), False),
    ("JP open 09:30 JST", "JP", ts(2026, 11, 5, 0, 30), True),
    ("JP lunch 12:00 JST", "JP", ts(2026, 11, 5, 3, 0), False),
    ("FX Sunday 23:00 UTC", "FX", ts(2026, 11, 8, 23, 0), True),
    ("FX Friday 21:00 UTC", "FX", ts(2026, 11, 6, 21, 0), True),
    ("FX Friday 23:00 UTC", "FX", ts(2026, 11, 6, 23, 0), False),
    ("FX Saturday 10:00 UTC", "FX", ts(2026, 11, 7, 10, 0), False),
    ("CN futures night 22:00 CST", "CN_FUT", ts(2026, 11, 5, 14, 0), True),
    ("CN futures 03:00 CST (after 02:30)", "CN_FUT", ts(2026, 11, 5, 19, 0), False),
    ("Europe open 10:00 CET", "DE", ts(2026, 11, 5, 9, 0), True),
    ("Europe closed 18:00 CET", "DE", ts(2026, 11, 5, 17, 0), False),
    ("ASIA basket (IN open, CN closed)", "ASIA", ts(2026, 11, 5, 7, 30), True),
]

failures = 0
for label, spec, when, expected in CASES:
    st = sessions.state(spec, when)
    ok = st["open"] == expected
    if not ok:
        failures += 1
    print(f"{'ok ' if ok else 'FAIL'} {label:42} open={st['open']} phase={st.get('phase')}")

# final snapshot after the bell
now = ts(2026, 11, 5, 7, 5)   # 15:05 CST
do, _ = sessions.should_collect("CN", now, last_ts=ts(2026, 11, 5, 6, 55), interval=300)
print(f"{'ok ' if do else 'FAIL'} final snapshot taken after close (last 14:55)")
do2, _ = sessions.should_collect("CN", now, last_ts=ts(2026, 11, 5, 7, 2), interval=300)
print(f"{'ok ' if not do2 else 'FAIL'} no refetch once the close is captured")
do3, _ = sessions.should_collect("CN", ts(2026, 11, 5, 10, 0), last_ts=ts(2026, 11, 5, 7, 2), interval=300)
print(f"{'ok ' if not do3 else 'FAIL'} idle two hours after close")

print("\nfailures:", failures)
