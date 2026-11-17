"""Transaction projection: period filtering, grouping and diffing.

The projection is the single place where events are turned into per-canonical-
account groups.  It is used by both sides of the audit:

* ``as_booked`` reads events resolved at ``close_generation`` and identity
  resolved at ``close_generation``; the resulting groups are frozen.
* ``restated`` reads events resolved at the *current* generation and identity
  resolved at the *current* generation, then is diffed against the frozen
  ``as_booked`` groups.

Because the diff is ``restated - as_booked`` computed per account, an event
that is moved, deleted or added always shows as a negative delta on the old
group and a positive delta on the new one; nothing is merely appended.
"""
from . import identity


def _byte_key(value):
    return value.encode("utf-8")


def project(events_by_id, links_by_id, generation, start, end):
    """Group period events by canonical account at ``generation``.

    ``events_by_id`` and ``links_by_id`` are the full revision histories.  The
    report period is the half-open interval ``[start, end)`` over
    ``event_time``.
    """
    events = {
        event_id: event
        for event_id, event in identity.resolve_events(
            events_by_id, generation
        ).items()
        if start <= event["event_time"] < end
    }
    links = identity.resolve_links(links_by_id, generation)
    resolved = identity.canonical_accounts(events, links)

    grouped = {}
    for event_id, account in resolved.items():
        grouped.setdefault(account, []).append(events[event_id])
    return {
        account: sorted(
            rows, key=lambda event: _byte_key(event["event_id"])
        )
        for account, rows in grouped.items()
    }


def summarize(grouped):
    """Canonical account rows: sorted account IDs, event IDs and totals."""
    rows = []
    for account in sorted(grouped, key=_byte_key):
        events = grouped[account]
        event_ids = sorted(
            (event["event_id"] for event in events), key=_byte_key
        )
        by_id = {event["event_id"]: event for event in events}
        rows.append(
            {
                "account_id": account,
                "event_ids": event_ids,
                "event_count": len(event_ids),
                "amount_minor": sum(
                    by_id[event_id]["amount_minor"] for event_id in event_ids
                ),
            }
        )
    return rows


def diff(as_booked_rows, restated_rows):
    """Per-account ``restated - as_booked`` movement.

    Only accounts whose membership, count or amount actually changed appear.
    The old group's reduction (negative) and the new group's increase
    (positive) are both present when an event moves, so the diff always
    rebalances to the change in period totals.  Both event-id lists are
    included so an audit can see exactly which transactions left and entered
    each group, even when a swap leaves count and amount unchanged.
    """
    booked = {row["account_id"]: row for row in as_booked_rows}
    restated = {row["account_id"]: row for row in restated_rows}
    accounts = sorted(set(booked) | set(restated), key=_byte_key)
    changes = []
    for account in accounts:
        before = booked.get(account)
        after = restated.get(account)
        before_count = 0 if before is None else before["event_count"]
        after_count = 0 if after is None else after["event_count"]
        before_amount = 0 if before is None else before["amount_minor"]
        after_amount = 0 if after is None else after["amount_minor"]
        before_ids = [] if before is None else list(before["event_ids"])
        after_ids = [] if after is None else list(after["event_ids"])
        delta_count = after_count - before_count
        delta_amount = after_amount - before_amount
        if delta_count == 0 and delta_amount == 0 and before_ids == after_ids:
            continue
        changes.append(
            {
                "account_id": account,
                "as_booked_event_ids": before_ids,
                "restated_event_ids": after_ids,
                "as_booked_event_count": before_count,
                "as_booked_amount_minor": before_amount,
                "restated_event_count": after_count,
                "restated_amount_minor": after_amount,
                "delta_event_count": delta_count,
                "delta_amount_minor": delta_amount,
            }
        )
    return changes
