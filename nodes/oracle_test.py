"""Independent oracle: worked examples transcribed from RFC 5545 itself.

Every expectation here comes from the *expected output printed in RFC 5545
section 3.8.5.3*, not from running python-dateutil and recording what it did.
That is what makes this an oracle rather than a change-detector: the authority
is the standards document, and it was written years before this package existed.

Each case keeps the RFC's own DTSTART, its rule, and its stated occurrences.
Several span the US EDT -> EST transition, where the RFC's expected output stays
at 9:00 AM local, so they pin the wall clock across a DST boundary.

They do NOT, on their own, prove the zone is applied: occurrences are emitted as
local wall-clock strings, so a wall-clock assertion survives `tzid` being
ignored. The RFC annotates each occurrence with its zone (EDT vs EST) and this
transcription cannot carry that dimension. `test_tzid_actually_changes_the_answer`
covers it instead, by comparing against an absolute UTC window -- the one place
the offset becomes observable.
"""

import pytest

from gen.messages_pb2 import BetweenRequest, ExpandRequest
from nodes.between import between
from nodes.expand import expand
from nodes.testkit import NY, FakeContext, recurrence


def first_n(rrule, dtstart, n, tzid=NY):
    result = expand(
        FakeContext(),
        ExpandRequest(recurrence=recurrence(rrule, dtstart, tzid=tzid), limit=n),
    )
    assert result.error.code == "", result.error.message
    return list(result.occurrences)


# RFC 5545 p.123 -- "Daily for 10 occurrences"
#   ==> (1997 9:00 AM EDT) September 2-11
def test_rfc_daily_for_10_occurrences():
    assert first_n("FREQ=DAILY;COUNT=10", "19970902T090000", 20) == [
        f"199709{day:02d}T090000" for day in range(2, 12)
    ]


# RFC 5545 p.123 -- "Every 10 days, 5 occurrences"
#   ==> (1997 9:00 AM EDT) September 2,12,22; October 2,12
def test_rfc_every_10_days_5_occurrences():
    assert first_n("FREQ=DAILY;INTERVAL=10;COUNT=5", "19970902T090000", 20) == [
        "19970902T090000",
        "19970912T090000",
        "19970922T090000",
        "19971002T090000",
        "19971012T090000",
    ]


# RFC 5545 p.126 -- "Monthly on the first Friday for 10 occurrences"
#   ==> (1997 EDT) September 5; October 3
#       (1997 EST) November 7; December 5
#       (1998 EST) January 2; February 6; March 6; April 3
#       (1998 EDT) May 1; June 5
# Crosses EDT->EST->EDT and every occurrence stays 9:00 AM local.
def test_rfc_monthly_first_friday_10_occurrences():
    assert first_n("FREQ=MONTHLY;COUNT=10;BYDAY=1FR", "19970905T090000", 20) == [
        "19970905T090000",
        "19971003T090000",
        "19971107T090000",
        "19971205T090000",
        "19980102T090000",
        "19980206T090000",
        "19980306T090000",
        "19980403T090000",
        "19980501T090000",
        "19980605T090000",
    ]


# RFC 5545 p.130 -- "The third instance into the month of one of Tuesday,
# Wednesday, or Thursday, for the next 3 months"
#   ==> (1997 EDT) September 4; October 7   (1997 EST) November 6
def test_rfc_bysetpos_third_tue_wed_thu():
    assert first_n(
        "FREQ=MONTHLY;COUNT=3;BYDAY=TU,WE,TH;BYSETPOS=3", "19970904T090000", 20
    ) == ["19970904T090000", "19971007T090000", "19971106T090000"]


# RFC 5545 p.130 -- "The second-to-last weekday of the month"
#   ==> (1997 EDT) September 29  (1997 EST) October 30; November 27; December 30
#       (1998 EST) January 29; February 26; March 30
# The rule is unbounded; the RFC prints the first seven, so only those are pinned.
def test_rfc_bysetpos_second_to_last_weekday():
    assert first_n(
        "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2", "19970929T090000", 7
    ) == [
        "19970929T090000",
        "19971030T090000",
        "19971127T090000",
        "19971230T090000",
        "19980129T090000",
        "19980226T090000",
        "19980330T090000",
    ]


# The DST claim the tzid field makes: a weekly 09:00 meeting in New York stays
# at 09:00 local across the March 2026 EST->EDT transition.
#
# Note what this test alone does NOT prove. Occurrences are emitted as local
# wall-clock strings, so this assertion holds even if tzid were ignored outright
# -- see test_tzid_actually_changes_the_answer below for the case that fails if
# the zone is not applied. Both are needed: this one pins the wall clock, that
# one pins that the zone is real.
def test_wall_clock_survives_dst_transition():
    occurrences = first_n("FREQ=WEEKLY;COUNT=3", "20260305T090000", 5)
    assert occurrences == [
        "20260305T090000",  # EST (-05:00)
        "20260312T090000",  # EDT (-04:00) -- offset moved, wall clock did not
        "20260319T090000",
    ]


# The zone made observable. A UTC window is an ABSOLUTE instant, so comparing a
# zoned recurrence against one exposes the offset the wall clock hides: 09:00 in
# New York is 14:00Z in January but 13:00Z in July. The identical UTC window
# therefore catches the occurrence in winter and misses it in summer -- an
# assertion that fails if tzid is dropped, defaulted, or treated as UTC.
def test_tzid_actually_changes_the_answer():
    def window(dtstart, start, end):
        result = between(
            FakeContext(),
            BetweenRequest(
                recurrence=recurrence("FREQ=DAILY", dtstart, tzid=NY),
                start=start, end=end,
            ),
        )
        assert result.error.code == "", result.error.message
        return list(result.occurrences)

    # January: the 09:00 EST occurrence is 14:00Z, inside [13:30Z, 14:30Z).
    assert window("20260105T090000", "20260105T133000Z", "20260105T143000Z") == [
        "20260105T090000"
    ]
    # July: the 09:00 EDT occurrence is 13:00Z, so the SAME window misses it.
    assert window("20260706T090000", "20260706T133000Z", "20260706T143000Z") == []
    # It is found an hour earlier in UTC, confirming the offset really moved.
    assert window("20260706T090000", "20260706T123000Z", "20260706T133000Z") == [
        "20260706T090000"
    ]


# A floating anchor (no tzid) has no zone at all, so it cannot shift.
def test_floating_anchor_has_no_zone_shift():
    assert first_n("FREQ=WEEKLY;COUNT=2", "20260305T090000", 5, tzid="") == [
        "20260305T090000",
        "20260312T090000",
    ]


# A UTC anchor round-trips in UTC form, keeping the trailing Z.
def test_utc_anchor_round_trips_in_utc_form():
    assert first_n("FREQ=DAILY;COUNT=3", "19970902T090000Z", 5, tzid="") == [
        "19970902T090000Z",
        "19970903T090000Z",
        "19970904T090000Z",
    ]


# A DATE-valued anchor yields DATE-valued occurrences, not midnight date-times.
def test_date_anchor_yields_date_form():
    assert first_n("FREQ=DAILY;COUNT=3", "19970902", 5, tzid="") == [
        "19970902",
        "19970903",
        "19970904",
    ]


# RFC 5545 p.128 -- "An example where the days generated makes a difference
# because of WKST". The RFC prints BOTH results to show WKST changes the answer,
# which makes it a rare self-contained oracle for a part that is easy to ignore.
#
#   DTSTART;TZID=America/New_York:19970805T090000
#   RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO
#   ==> (1997 EDT) August 5,10,19,24
def test_rfc_wkst_monday():
    assert first_n(
        "FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO", "19970805T090000", 10
    ) == [
        "19970805T090000",
        "19970810T090000",
        "19970819T090000",
        "19970824T090000",
    ]


# Same rule with WKST=SU -- the RFC's point is that this yields DIFFERENT dates.
#   ==> (1997 EDT) August 5,17,19,31
def test_rfc_wkst_sunday_changes_the_answer():
    monday = first_n(
        "FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO", "19970805T090000", 10
    )
    sunday = first_n(
        "FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=SU", "19970805T090000", 10
    )
    assert sunday == [
        "19970805T090000",
        "19970817T090000",
        "19970819T090000",
        "19970831T090000",
    ]
    assert sunday != monday, "WKST must change the answer, per the RFC's own example"


# RFC 5545 p.127 -- "Monday of week number 20 (where the default start of the
# week is Monday), forever"
#   DTSTART;TZID=America/New_York:19970512T090000
#   RRULE:FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO
#   ==> (1997 9:00 AM EDT) May 12  (1998 EDT) May 11  (1999 EDT) May 17
def test_rfc_byweekno_monday_of_week_20():
    assert first_n("FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO", "19970512T090000", 3) == [
        "19970512T090000",
        "19980511T090000",
        "19990517T090000",
    ]


# RFC 5545 p.127 -- "Every 20th Monday of the year, forever"
#   DTSTART;TZID=America/New_York:19970519T090000
#   RRULE:FREQ=YEARLY;BYDAY=20MO
#   ==> (1997 9:00 AM EDT) May 19  (1998 EDT) May 18  (1999 EDT) May 17
def test_rfc_twentieth_monday_of_the_year():
    assert first_n("FREQ=YEARLY;BYDAY=20MO", "19970519T090000", 3) == [
        "19970519T090000",
        "19980518T090000",
        "19990517T090000",
    ]
