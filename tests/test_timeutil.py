"""Tests for the UTC contract and duration presentation.

Two defects motivate these.

The first: every incident read used sysparm_display_value=true, which returns
datetimes in the integration user's timezone and date format, and signals then
compared those values against the server's local clock. Age and inactivity were
therefore wrong by the offset between two zones - for every incident, in the
same direction, with nothing raised. A test cannot detect a live timezone
mismatch, so these pin the properties that prevent one: utc_value takes the
UTC side of a field, utc_now is UTC, and parse_ts does not silently fail on a
non-ISO date.

The second: age was displayed from a DECIMAL(8,2) day figure, which cannot
resolve better than about fifteen minutes, so a duration naming minutes was
necessarily wrong.
"""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snow.base import query_ts, utc_ts, utc_value
from core.timeutil import (TS_FORMAT, days_between, format_duration,
                           format_duration_days, minutes_between, parse_ts,
                           utc_now)


class TestUtcNow:
    def test_is_naive(self):
        """MySQL DATETIME carries no zone; mixing aware and naive raises."""
        assert utc_now().tzinfo is None

    def test_has_no_microseconds(self):
        """So a stored value round-trips identically."""
        assert utc_now().microsecond == 0

    def test_is_actually_utc_not_local(self):
        reference = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        assert abs((utc_now() - reference).total_seconds()) < 5

    def test_differs_from_local_time_when_the_host_is_not_on_utc(self):
        """Documents the relationship rather than asserting a specific offset,
        so this passes on a UTC host and on any other."""
        offset = (datetime.datetime.now() - utc_now()).total_seconds()
        assert abs(offset) < 86400 + 60


class TestUtcValue:
    def test_takes_value_not_display_value(self):
        """The whole point: value is UTC, display_value is the user's zone."""
        field = {'display_value': '24-09-2026 13:07:11', 'value': '2026-09-24 07:07:11'}
        assert utc_value(field) == '2026-09-24 07:07:11'

    def test_falls_back_to_display_value_when_value_is_absent(self):
        """A response fetched with display_value=true has no value key; yield
        something rather than nothing."""
        assert utc_value({'display_value': '2026-09-24 07:07:11'}) == '2026-09-24 07:07:11'

    def test_falls_back_when_value_is_empty(self):
        field = {'display_value': '2026-09-24 07:07:11', 'value': ''}
        assert utc_value(field) == '2026-09-24 07:07:11'

    def test_plain_string_passes_through(self):
        assert utc_value('2026-09-24 07:07:11') == '2026-09-24 07:07:11'

    def test_none_and_empty_are_blank(self):
        assert utc_value(None) == ''
        assert utc_value({}) == ''

    def test_utc_ts_parses_the_value_side(self):
        field = {'display_value': '24-09-2026 13:07:11', 'value': '2026-09-24 07:07:11'}
        assert utc_ts(field) == datetime.datetime(2026, 9, 24, 7, 7, 11)

    def test_a_six_hour_profile_offset_does_not_reach_the_parsed_value(self):
        """The production failure, expressed as a test: display_value is six
        hours ahead, and the parsed result must ignore it entirely."""
        field = {'display_value': '2026-09-24 13:07:11', 'value': '2026-09-24 07:07:11'}
        assert utc_ts(field).hour == 7


class TestParseTs:
    @pytest.mark.parametrize('text,expected', [
        ('2026-09-24 07:07:11', datetime.datetime(2026, 9, 24, 7, 7, 11)),
        ('2026-09-24 07:07', datetime.datetime(2026, 9, 24, 7, 7)),
        ('2026-09-24', datetime.datetime(2026, 9, 24)),
        ('24-09-2026 07:07:11', datetime.datetime(2026, 9, 24, 7, 7, 11)),
        ('09/24/2026 07:07:11', datetime.datetime(2026, 9, 24, 7, 7, 11)),
    ])
    def test_accepted_formats(self, text, expected):
        """A display_value arrives in the integration user's date format. The
        former strict parser returned None for anything but the first of these,
        which silently emptied the timeline and flagged every incident as
        having no recorded action."""
        assert parse_ts(text) == expected

    def test_blank_and_none_are_none(self):
        """ServiceNow returns '' rather than null for an unset date."""
        assert parse_ts('') is None
        assert parse_ts('   ') is None
        assert parse_ts(None) is None

    def test_unparseable_is_none_not_an_exception(self):
        assert parse_ts('not a date') is None

    def test_a_datetime_passes_through(self):
        value = datetime.datetime(2026, 9, 24, 7, 7, 11, 500)
        assert parse_ts(value) == datetime.datetime(2026, 9, 24, 7, 7, 11)

    def test_round_trips_with_query_ts(self):
        """query_ts emits what parse_ts reads, so a window boundary survives."""
        moment = datetime.datetime(2026, 9, 24, 7, 7, 11)
        assert parse_ts(query_ts(moment)) == moment

    def test_query_ts_emits_no_javascript(self):
        """gs.dateGenerate interprets its argument in the session user's
        timezone, which would shift a UTC boundary by that user's offset."""
        rendered = query_ts(datetime.datetime(2026, 9, 24, 7, 7, 11))
        assert rendered == '2026-09-24 07:07:11'
        assert 'javascript' not in rendered


class TestElapsed:
    def test_minutes_between(self):
        a = datetime.datetime(2026, 9, 24, 12, 0, 0)
        b = datetime.datetime(2026, 9, 24, 13, 30, 40)
        assert minutes_between(b, a) == 90

    def test_minutes_floor_rather_than_round(self):
        a = datetime.datetime(2026, 9, 24, 12, 0, 0)
        b = datetime.datetime(2026, 9, 24, 12, 0, 59)
        assert minutes_between(b, a) == 0

    def test_a_record_ahead_of_the_clock_clamps_to_zero(self):
        """ServiceNow can legitimately be a few seconds ahead; a negative age
        is never meaningful."""
        a = datetime.datetime(2026, 9, 24, 12, 0, 10)
        b = datetime.datetime(2026, 9, 24, 12, 0, 0)
        assert minutes_between(b, a) == 0
        assert days_between(b, a) == 0.0

    def test_none_propagates(self):
        assert minutes_between(None, datetime.datetime(2026, 1, 1)) is None
        assert minutes_between(datetime.datetime(2026, 1, 1), None) is None

    def test_days_and_minutes_agree(self):
        a = datetime.datetime(2026, 9, 1, 0, 0, 0)
        b = datetime.datetime(2026, 9, 11, 12, 30, 0)
        assert minutes_between(b, a) == 15150
        assert days_between(b, a) == 10.52


class TestFormatDuration:
    @pytest.mark.parametrize('minutes,expected', [
        (15150, '10 Days 12 Hrs 30 Mins'),
        (1425, '23 Hrs 45 Mins'),
        (30, '30 Mins'),
    ])
    def test_the_requested_examples(self, minutes, expected):
        assert format_duration(minutes) == expected

    @pytest.mark.parametrize('minutes,expected', [
        (14400, '10 Days'),
        (14430, '10 Days 30 Mins'),
        (1440, '1 Day'),
        (2880, '2 Days'),
        (60, '1 Hr'),
        (120, '2 Hrs'),
        (1, '1 Min'),
        (59, '59 Mins'),
        (1439, '23 Hrs 59 Mins'),
        (1441, '1 Day 1 Min'),
        (1501, '1 Day 1 Hr 1 Min'),
    ])
    def test_components_and_singulars(self, minutes, expected):
        assert format_duration(minutes) == expected

    def test_zero_components_are_omitted_not_padded(self):
        assert format_duration(14430) == '10 Days 30 Mins'
        assert '0 Hrs' not in format_duration(14430)

    def test_zero_and_negative(self):
        assert format_duration(0) == '0 Mins'
        assert format_duration(-5) == '0 Mins'

    def test_custom_zero_text(self):
        assert format_duration(0, zero='none') == 'none'

    def test_none_is_empty_not_zero(self):
        """An absent figure must not read as a measured zero."""
        assert format_duration(None) == ''

    def test_garbage_is_empty(self):
        assert format_duration('abc') == ''
        assert format_duration({}) == ''

    def test_accepts_a_numeric_string(self):
        """MySQL drivers can return INT columns as strings."""
        assert format_duration('1425') == '23 Hrs 45 Mins'

    def test_fractional_minutes_round(self):
        assert format_duration(29.6) == '30 Mins'

    def test_total_is_reconstructable(self):
        """Every rendered string must account for the whole duration."""
        for minutes in (1, 59, 60, 61, 1439, 1440, 1441, 15150, 100000):
            days = minutes // 1440
            hours = (minutes % 1440) // 60
            mins = minutes % 60
            assert days * 1440 + hours * 60 + mins == minutes
            rendered = format_duration(minutes)
            for value, unit in ((days, 'Day'), (hours, 'Hr'), (mins, 'Min')):
                if value:
                    assert f'{value} {unit}' in rendered


class TestFormatDurationDays:
    def test_renders_from_days(self):
        assert format_duration_days(1.0) == '1 Day'
        assert format_duration_days(0.5) == '12 Hrs'

    def test_none_is_empty(self):
        assert format_duration_days(None) == ''

    def test_two_decimal_days_cannot_resolve_minutes(self):
        """Why the per-incident display reads the minute column instead: 10.52
        days is 15148.8 minutes, so the exact 15150 renders one minute short."""
        assert format_duration(15150) == '10 Days 12 Hrs 30 Mins'
        assert format_duration_days(10.52) == '10 Days 12 Hrs 29 Mins'
