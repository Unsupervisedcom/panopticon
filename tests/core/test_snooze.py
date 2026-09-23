"""The operator snooze predicate (``core.snooze``) — pure arithmetic, clock passed in.

Two callers gate on this — the dashboard (mute the row) and the session service (stop the
container) — so it's pinned on its own: an unreadable or lapsed deadline must read *inactive*
rather than raise, or a bad recorded fact would stall a spawn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from panopticon.core.snooze import INDEFINITE_UNTIL, is_snoozed, snooze_remaining

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def test_finite_deadline_in_the_future_is_active() -> None:
    until = (NOW + timedelta(hours=3)).isoformat()
    assert snooze_remaining(until, NOW) == 3 * 3600
    assert is_snoozed(until, NOW)


def test_lapsed_deadline_is_inactive() -> None:
    """The wake signal: expiry is clock-only (no event), so the predicate alone reports it."""
    until = (NOW - timedelta(seconds=1)).isoformat()
    assert snooze_remaining(until, NOW) is None
    assert not is_snoozed(until, NOW)
    assert not is_snoozed(NOW.isoformat(), NOW)  # the deadline itself is no longer in the future


def test_sticky_sentinel_never_expires() -> None:
    assert snooze_remaining(INDEFINITE_UNTIL, NOW) == float("inf")
    assert is_snoozed(INDEFINITE_UNTIL, datetime(9999, 1, 1, tzinfo=UTC))


def test_no_deadline_is_inactive() -> None:
    assert snooze_remaining(None, NOW) is None
    assert not is_snoozed(None, NOW)


def test_unparseable_deadline_is_inactive_not_an_error() -> None:
    assert snooze_remaining("not a timestamp", NOW) is None
    assert snooze_remaining(12345, NOW) is None  # type: ignore[arg-type]  # a non-string fact


def test_naive_datetimes_are_read_as_utc() -> None:
    """Either side may be naive (a hand-written fact, a naive display clock) — both mean UTC."""
    assert is_snoozed("2026-08-06T15:00:00", NOW)  # naive deadline vs aware now
    assert is_snoozed((NOW + timedelta(hours=1)).isoformat(), NOW.replace(tzinfo=None))
