"""Offline finished-work-order energy attribution and metering-uncertainty audit.

This package adds the read-only ``energy-audit`` command on top of the
existing cutting-plan warehouse.  It never plans, never mutates inventory,
never posts financial documents and never matches procurement data; it only
reads finished-plan history from a single read-only SQLite snapshot and
combines it with a strictly validated meter/run/tariff/budget source document.
"""
