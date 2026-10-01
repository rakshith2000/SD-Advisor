"""Time handling and duration presentation.

Deliberately dependency-free so the pure signal functions can import it
without pulling in requests or a database driver.

The contract for the whole project, in one sentence: every timestamp that
crosses a module boundary or reaches the database is naive UTC.

That is not an arbitrary preference. ServiceNow returns a datetime twice over -
the `value` field is UTC, and the `display_value` field is the integration
user's timezone rendered in the integration user's date format. Reading
display_value and then comparing it against this server's local clock produces
an age that is wrong by the offset between those two zones, for every incident,
in the same direction, with nothing raised. Reading `value` and comparing
against utc_now() cannot drift, because neither side depends on a profile
setting an administrator could change.

Conversion to a human timezone happens once, at presentation.
"""

import datetime
from typing import Any, Optional

TS_FORMAT = '%Y-%m-%d %H:%M:%S'

# Accepted inbound formats. The first is what ServiceNow's `value` field always
# returns; the rest exist because a display_value can arrive in the integration
# user's date format, and a parse failure here is far worse than a lenient
# parse - it silently becomes None and every downstream signal degrades.
_INBOUND_FORMATS = (
    TS_FORMAT,
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d',
    '%d-%m-%Y %H:%M:%S',
    '%d/%m/%Y %H:%M:%S',
    '%m/%d/%Y %H:%M:%S',
    '%m-%d-%Y %H:%M:%S',
    '%d-%m-%Y %H:%M',
    '%m/%d/%Y %H:%M',
)

MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 1440


def utc_now() -> datetime.datetime:
    """Current UTC time, naive, to the second.

    Naive rather than aware because MySQL DATETIME columns carry no zone, and
    a mix of aware and naive values raises on subtraction. Microseconds are
    dropped so stored values round-trip identically.

    Built from an aware value and then stripped, rather than via the
    deprecated datetime.utcnow().
    """
    return (datetime.datetime.now(datetime.timezone.utc)
            .replace(tzinfo=None, microsecond=0))


def parse_ts(value: Any) -> Optional[datetime.datetime]:
    """Parse a ServiceNow timestamp. Returns None for unset or unparseable.

    ServiceNow returns '' rather than null for an unset date, so an empty
    result is normal and not an error.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.replace(microsecond=0)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)

    text = str(value).strip()
    if not text:
        return None

    for fmt in _INBOUND_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def minutes_between(later: Optional[datetime.datetime],
                    earlier: Optional[datetime.datetime]) -> Optional[int]:
    """Whole minutes between two instants, floored at zero.

    Clamped because a ServiceNow record can legitimately carry a timestamp a
    few seconds ahead of this server's clock, and a negative age is never
    meaningful to a lead.
    """
    if later is None or earlier is None:
        return None
    return int(max(0.0, (later - earlier).total_seconds()) // 60)


def days_between(later: Optional[datetime.datetime],
                 earlier: Optional[datetime.datetime]) -> Optional[float]:
    """Elapsed days to two decimal places. Feeds the attention score."""
    if later is None or earlier is None:
        return None
    return round(max(0.0, (later - earlier).total_seconds()) / 86400.0, 2)


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

def format_duration(minutes: Any, zero: str = '0 Mins') -> str:
    """Render a duration in minutes as 'Days Hrs Mins'.

        15150 -> '10 Days 12 Hrs 30 Mins'
         1425 -> '23 Hrs 45 Mins'
           30 -> '30 Mins'
        14400 -> '10 Days'            (zero components omitted)
        14430 -> '10 Days 30 Mins'    (not '10 Days 0 Hrs 30 Mins')
         1441 -> '1 Day 1 Min'        (singularised at one)

    Zero components are omitted entirely rather than padded, so the string
    stays short for the common cases while remaining exact. Units are
    singularised at one, matching how ServiceNow renders its own durations.
    """
    if minutes is None:
        return ''

    try:
        total = int(round(float(minutes)))
    except (TypeError, ValueError):
        return ''

    if total <= 0:
        return zero

    days, remainder = divmod(total, MINUTES_PER_DAY)
    hours, mins = divmod(remainder, MINUTES_PER_HOUR)

    parts = []
    if days:
        parts.append(f"{days} {'Day' if days == 1 else 'Days'}")
    if hours:
        parts.append(f"{hours} {'Hr' if hours == 1 else 'Hrs'}")
    if mins:
        parts.append(f"{mins} {'Min' if mins == 1 else 'Mins'}")

    return ' '.join(parts)


def format_duration_days(days: Any, zero: str = '0 Mins') -> str:
    """Same rendering from a value expressed in days.

    Only for aggregates that are averaged in days and have no minute column -
    a per-incident figure should always come from the stored minutes, because
    a value rounded to two decimal places cannot resolve better than about
    fifteen minutes.
    """
    if days is None:
        return ''
    try:
        return format_duration(float(days) * MINUTES_PER_DAY, zero=zero)
    except (TypeError, ValueError):
        return ''
