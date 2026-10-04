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
import logging
import zoneinfo
from typing import Any, Optional

# Standard library logging rather than core.logging_setup: this module is
# imported by the pure signal functions, and importing the project's logging
# configuration from here would tie those to handler setup they do not need.
log = logging.getLogger('core.timeutil')

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


# ---------------------------------------------------------------------------
# Display timezone
#
# Everything above this line, and everything stored anywhere in this service,
# is naive UTC. That is deliberate and does not change: an elapsed time
# computed between two values on different clocks is wrong in a way nothing
# reports, and the only defence is one clock everywhere.
#
# This section is the single point where that contract is relaxed - at the
# edge, for a human reading a screen or an email. Nothing here is ever written
# back to the database, used in a comparison, or fed into an interval.
#
# Zones are IANA names, never fixed offsets. 'America/Chicago' is CST for part
# of the year and CDT for the rest; a stored -06:00 would silently be an hour
# out for eight months of every year, and the abbreviation shown beside each
# timestamp would be a lie.
# ---------------------------------------------------------------------------

DEFAULT_DISPLAY_TZ = 'America/Chicago'
DEFAULT_DATETIME_FORMAT = '%d %b %Y %H:%M'

# Offered in the navigation dropdown. A curated list rather than all 598 IANA
# zones: a long list is harder to use than a short one, and anything missing
# can be added through display.timezone_choices in conf.json.
COMMON_TIMEZONES = (
    ('America/Chicago', 'US Central'),
    ('America/New_York', 'US Eastern'),
    ('America/Denver', 'US Mountain'),
    ('America/Los_Angeles', 'US Pacific'),
    ('America/Sao_Paulo', 'Brazil'),
    ('Europe/London', 'UK'),
    ('Europe/Paris', 'Central Europe'),
    ('Africa/Johannesburg', 'South Africa'),
    ('Asia/Dubai', 'Gulf'),
    ('Asia/Kolkata', 'India'),
    ('Asia/Singapore', 'Singapore'),
    ('Asia/Shanghai', 'China'),
    ('Asia/Tokyo', 'Japan'),
    ('Australia/Sydney', 'Eastern Australia'),
    ('UTC', 'UTC'),
)


def get_zone(name: Optional[str]) -> datetime.tzinfo:
    """Resolve an IANA name, falling back to the default and then to UTC.

    Never raises. A bad zone reaching this point - a stale value in a user
    row, a typo in conf.json - must degrade to a readable timestamp rather
    than take out every page that renders a date.
    """
    for candidate in (name, DEFAULT_DISPLAY_TZ):
        if not candidate:
            continue
        try:
            return zoneinfo.ZoneInfo(str(candidate))
        except Exception:
            if candidate == name:
                log.warning('Unknown timezone %r - falling back', candidate)
    return datetime.timezone.utc


def is_valid_zone(name: Optional[str]) -> bool:
    """Whether a name can be trusted enough to store against an account."""
    if not name:
        return False
    try:
        zoneinfo.ZoneInfo(str(name))
        return True
    except Exception:
        return False


def to_zone(value: Any, zone: Any = None) -> Optional[datetime.datetime]:
    """Naive UTC (or a parseable string) -> an aware datetime in `zone`."""
    if value is None or value == '':
        return None

    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, datetime.date):
        # A plain date has no time to convert; midnight UTC would shift it
        # into the previous day for any western zone, which reads as an
        # off-by-one bug in the audit trail.
        return None
    else:
        moment = parse_ts(value)
        if moment is None:
            return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)

    target = zone if isinstance(zone, datetime.tzinfo) else get_zone(zone)
    return moment.astimezone(target)


def format_dt(value: Any, zone: Any = None, fmt: str = DEFAULT_DATETIME_FORMAT,
              with_zone: bool = False, empty: str = '') -> str:
    """Render one timestamp for a human, in `zone`.

    with_zone appends the abbreviation in force on that date - CST or CDT,
    GMT or BST - computed per value rather than per zone, so a January
    incident and a July one are each labelled correctly.
    """
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value.strftime(fmt.replace(' %H:%M', '').replace('%H:%M', '').strip())

    moment = to_zone(value, zone)
    if moment is None:
        return empty if value in (None, '') else str(value)

    rendered = moment.strftime(fmt)
    return f'{rendered} {moment.strftime("%Z")}' if with_zone else rendered


def zone_abbreviation(zone: Any = None, at: Optional[datetime.datetime] = None) -> str:
    """'CST' or 'CDT' for the given instant - today's, unless told otherwise."""
    moment = to_zone(at or utc_now(), zone)
    return moment.strftime('%Z') if moment else 'UTC'


def zone_label(zone: Any = None, at: Optional[datetime.datetime] = None) -> str:
    """'US Central (CDT, UTC-05:00)' - for the email disclaimer.

    The offset is spelled out because an abbreviation alone is not universally
    unambiguous: CST is also China Standard Time, eleven hours away from the
    one meant here.
    """
    name = zone if isinstance(zone, str) else getattr(zone, 'key', str(zone))
    friendly = dict(COMMON_TIMEZONES).get(name, name)

    moment = to_zone(at or utc_now(), zone)
    if moment is None:
        return friendly

    offset = moment.utcoffset() or datetime.timedelta(0)
    total = int(offset.total_seconds())
    sign = '+' if total >= 0 else '-'
    hours, minutes = divmod(abs(total) // 60, 60)
    abbrev = moment.strftime('%Z')

    if name == 'UTC' or (abbrev == 'UTC' and total == 0):
        return 'UTC'
    return f'{friendly} ({abbrev}, UTC{sign}{hours:02d}:{minutes:02d})'


def timezone_choices(extra: Any = None) -> list:
    """(iana_name, label) pairs for the dropdown, current offsets included."""
    pairs = list(COMMON_TIMEZONES)

    for entry in (extra or []):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            name, label = entry
        else:
            name = label = str(entry)
        if is_valid_zone(name) and name not in dict(pairs):
            pairs.append((name, label))

    now = utc_now()
    return [{'name': name, 'label': label,
             'detail': zone_label(name, at=now)} for name, label in pairs
            if is_valid_zone(name)]
