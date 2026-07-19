"""Independent oracle: worked examples transcribed from RFC 5545 itself.

Every expectation here comes from the *expected output printed in RFC 5545
section 3.8.5.3*, not from running python-dateutil and recording what it did.
That is what makes this an oracle rather than a change-detector: the authority
is the standards document, and it was written years before this package existed.

Each case keeps the RFC's own DTSTART, its rule, and its stated occurrences.
Several deliberately span the US EDT -> EST transition, where the RFC's expected
output stays at 9:00 AM local -- so these also pin wall-clock preservation
across a DST boundary, which is the whole reason `tzid` exists.
"""

import pytest

from gen.messages_pb2 import ExpandRequest
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


# The DST claim the tzid field makes, stated directly: a weekly 09:00 meeting
# in New York stays at 09:00 local across the March 2026 EST->EDT transition,
# even though its UTC offset changes from -05:00 to -04:00.
def test_wall_clock_survives_dst_transition():
    occurrences = first_n("FREQ=WEEKLY;COUNT=3", "20260305T090000", 5)
    assert occurrences == [
        "20260305T090000",  # EST (-05:00)
        "20260312T090000",  # EDT (-04:00) -- offset moved, wall clock did not
        "20260319T090000",
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
