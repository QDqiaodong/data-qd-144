"""Offline, auditable customer-activity ledger with bitemporal identity.

This package adds a new command family on top of the existing repository::

    activity-init   --db DB
    activity-apply  --db DB --request JSON
    activity-close  --db DB --request JSON
    activity-report --db DB --close-id ID --output JSON

It never touches the stock warehouse, the AP ledger or the energy audit.  All
persistence lives in one SQLite database created by ``activity-init``.  Strict
JSON (duplicate keys / non-finite numbers), the shared identifier rule and the
atomic, symlink-rejecting report publication are reused from
:mod:`cutting_stock.common`.
"""
