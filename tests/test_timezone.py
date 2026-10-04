"""Display timezone conversion.

Storage does not change and must not. Every timestamp written to the database,
read from ServiceNow's `value` field, or fed into a subtraction is naive UTC -
an elapsed time computed between two values recorded on different clocks is
wrong in a way nothing reports, and one clock everywhere is the only defence.

These tests cover the one place that contract is relaxed: the edge, where a
person reads a screen or an email.

The property that matters most is that conversion is a dead end. Nothing here
produces a value that can find its way back into a comparison or a write - the
helpers return strings, and the aware datetime from to_zone() exists only to
be formatted. A test suite cannot prove that directly, so the tests below pin
the behaviour that would break first if it stopped being true: durations stay
timezone-independent, and the stored side of every round trip is unchanged.

Daylight saving is the other half. 'America/Chicago' is CST for part of the
year and CDT for the rest, so the abbreviation has to be computed per
timestamp, not per zone - a January incident and a July one in the same table
carry different labels, and getting that wrong is a one-hour error that only
appears for eight months of the year.
"""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.timeutil import (DEFAULT_DISPLAY_TZ, format_dt, format_duration,
                           get_zone, is_valid_zone, minutes_between, to_zone,
                           timezone_choices, utc_now, zone_abbreviation,
                           zone_label)

CHICAGO = 'America/Chicago'

# Both 18:30 UTC. Chicago is UTC-6 in January and UTC-5 in July.
WINTER = datetime.datetime(2026, 1, 15, 18, 30, 0)
SUMMER = datetime.datetime(2026, 7, 15, 18, 30, 0)


class TestConversion:
    def test_winter_renders_as_central_standard_time(self):
        assert format_dt(WINTER, CHICAGO, '%d %b %Y %H:%M') == '15 Jan 2026 12:30'

    def test_summer_renders_as_central_daylight_time(self):
        """The same wall clock in UTC is an hour later in Chicago in July."""
        assert format_dt(SUMMER, CHICAGO, '%d %b %Y %H:%M') == '15 Jul 2026 13:30'

    def test_the_abbreviation_follows_the_date_not_the_zone(self):
        """CST and CDT are different offsets. A label fixed per zone would be
        wrong for eight months of every year."""
        assert format_dt(WINTER, CHICAGO, with_zone=True).endswith('CST')
        assert format_dt(SUMMER, CHICAGO, with_zone=True).endswith('CDT')

    def test_a_naive_value_is_read_as_utc(self):
        """Everything stored is naive UTC, so a value with no tzinfo must not
        be assumed to be server-local."""
        assert to_zone(SUMMER, 'UTC').hour == 18

    def test_an_aware_value_is_converted_not_relabelled(self):
        aware = SUMMER.replace(tzinfo=datetime.timezone.utc)
        assert to_zone(aware, CHICAGO).hour == 13

    def test_a_stored_string_converts_too(self):
        """ServiceNow timeline entries arrive as strings, not datetimes."""
        assert format_dt('2026-07-15 18:30:00', CHICAGO, '%H:%M') == '13:30'

    def test_india_carries_a_half_hour_offset(self):
        assert format_dt(SUMMER, 'Asia/Kolkata', '%d %b %H:%M') == '16 Jul 00:00'

    def test_utc_renders_unchanged(self):
        assert format_dt(SUMMER, 'UTC', '%d %b %Y %H:%M') == '15 Jul 2026 18:30'

    def test_conversion_can_cross_a_date_boundary(self):
        """23:30 UTC is the previous evening in Chicago. A reader comparing
        this against ServiceNow must see the same date ServiceNow shows."""
        late = datetime.datetime(2026, 7, 16, 2, 30)
        assert format_dt(late, CHICAGO, '%d %b %H:%M') == '15 Jul 21:30'


class TestFallbacks:
    def test_an_unknown_zone_falls_back_rather_than_raising(self):
        """A stale value in a user row must not take out every page that
        renders a date."""
        assert format_dt(SUMMER, 'Mars/Olympus', '%H:%M') == '13:30'   # the default

    def test_none_renders_empty(self):
        assert format_dt(None, CHICAGO) == ''

    def test_empty_string_renders_empty(self):
        assert format_dt('', CHICAGO) == ''

    def test_an_unparseable_value_is_passed_through_not_swallowed(self):
        """Showing the raw value beats showing nothing - a blank cell reads as
        'no data' when the truth is 'unexpected data'."""
        assert format_dt('not a date', CHICAGO) == 'not a date'

    def test_a_plain_date_is_not_shifted(self):
        """A date has no time to convert; treating it as midnight UTC would
        move it to the previous day in any western zone, which reads as an
        off-by-one in the audit trail."""
        assert format_dt(datetime.date(2026, 7, 15), CHICAGO) == '15 Jul 2026'

    def test_get_zone_never_raises(self):
        for candidate in (None, '', 'Nowhere/Nothing', 12345):
            assert get_zone(candidate) is not None

    @pytest.mark.parametrize('name,expected', [
        ('America/Chicago', True), ('UTC', True), ('Asia/Kolkata', True),
        ('Mars/Olympus', False), ('', False), (None, False), ('-06:00', False),
    ])
    def test_validation_accepts_only_real_iana_names(self, name, expected):
        """A fixed offset is rejected on purpose: it cannot express daylight
        saving, so it would be an hour wrong for part of every year."""
        assert is_valid_zone(name) is expected


class TestLabels:
    def test_the_label_spells_out_the_offset(self):
        """'CST' alone is ambiguous - it is also China Standard Time, eleven
        hours from the one meant here."""
        assert zone_label(CHICAGO, at=WINTER) == 'US Central (CST, UTC-06:00)'
        assert zone_label(CHICAGO, at=SUMMER) == 'US Central (CDT, UTC-05:00)'

    def test_half_hour_offsets_render_correctly(self):
        assert zone_label('Asia/Kolkata', at=SUMMER) == 'India (IST, UTC+05:30)'

    def test_utc_is_not_dressed_up(self):
        assert zone_label('UTC', at=SUMMER) == 'UTC'

    def test_abbreviation_tracks_daylight_saving(self):
        assert zone_abbreviation(CHICAGO, at=WINTER) == 'CST'
        assert zone_abbreviation(CHICAGO, at=SUMMER) == 'CDT'


class TestChoices:
    def test_central_is_offered_first(self):
        """It is the default, and the zone the ServiceNow instance uses."""
        assert timezone_choices()[0]['name'] == DEFAULT_DISPLAY_TZ

    def test_every_option_carries_a_current_offset(self):
        for option in timezone_choices():
            assert option['detail']
            assert option['name'] and option['label']

    def test_extra_zones_can_be_configured(self):
        names = [o['name'] for o in timezone_choices([['Europe/Madrid', 'Spain']])]
        assert 'Europe/Madrid' in names

    def test_a_bare_string_works_as_an_extra(self):
        assert 'Pacific/Auckland' in [
            o['name'] for o in timezone_choices(['Pacific/Auckland'])]

    def test_an_invalid_extra_is_dropped_rather_than_breaking_the_dropdown(self):
        before = len(timezone_choices())
        assert len(timezone_choices(['Mars/Olympus'])) == before

    def test_a_duplicate_extra_does_not_appear_twice(self):
        names = [o['name'] for o in timezone_choices([CHICAGO])]
        assert names.count(CHICAGO) == 1


class TestStorageIsUnaffected:
    """The contract this feature is not allowed to break."""

    def test_utc_now_is_naive(self):
        assert utc_now().tzinfo is None

    def test_elapsed_time_does_not_depend_on_the_display_zone(self):
        """Durations are computed from stored UTC on both sides. If a display
        conversion ever leaked into this path, age and idle would shift by the
        offset for every ticket at once - in the same direction, with nothing
        raised."""
        opened = datetime.datetime(2026, 7, 10, 18, 30)
        now = datetime.datetime(2026, 7, 15, 18, 30)
        assert minutes_between(now, opened) == 5 * 24 * 60

    def test_a_duration_renders_identically_whatever_the_reader_sees(self):
        assert format_duration(5 * 24 * 60) == '5 Days'

    def test_conversion_leaves_the_source_value_untouched(self):
        original = datetime.datetime(2026, 7, 15, 18, 30)
        to_zone(original, CHICAGO)
        assert original == datetime.datetime(2026, 7, 15, 18, 30)
        assert original.tzinfo is None

    def test_a_round_trip_through_a_zone_returns_the_same_instant(self):
        there = to_zone(SUMMER, CHICAGO)
        back = there.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        assert back == SUMMER


class TestDaylightSavingEdges:
    def test_the_spring_forward_hour(self):
        """02:00 local does not exist on 8 March 2026 in Chicago; 07:59 UTC is
        01:59 CST and 08:00 UTC is 03:00 CDT."""
        before = datetime.datetime(2026, 3, 8, 7, 59)
        after = datetime.datetime(2026, 3, 8, 8, 0)
        assert format_dt(before, CHICAGO, '%H:%M', with_zone=True) == '01:59 CST'
        assert format_dt(after, CHICAGO, '%H:%M', with_zone=True) == '03:00 CDT'

    def test_the_autumn_back_hour_is_labelled_distinguishably(self):
        """01:30 local happens twice on 1 November 2026. The abbreviation is
        what tells the two apart on screen."""
        first = datetime.datetime(2026, 11, 1, 6, 30)
        second = datetime.datetime(2026, 11, 1, 7, 30)
        assert format_dt(first, CHICAGO, '%H:%M', with_zone=True) == '01:30 CDT'
        assert format_dt(second, CHICAGO, '%H:%M', with_zone=True) == '01:30 CST'

    def test_the_elapsed_time_across_the_boundary_is_still_correct(self):
        """The whole reason storage stays UTC: the two timestamps above are an
        hour apart, and would be zero apart if durations used local time."""
        assert minutes_between(datetime.datetime(2026, 11, 1, 7, 30),
                               datetime.datetime(2026, 11, 1, 6, 30)) == 60
