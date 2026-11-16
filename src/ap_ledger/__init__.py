"""Offline accounts-payable voucher matching and posting ledger.

The package is independent of the cutting-stock warehouse: procurement data is
entered through its own strict JSON source and never reads the stock planner
database.  It reuses the shared strict-JSON, identifier and atomic publication
rules in :mod:`cutting_stock.common`.
"""

SCHEMA_VERSION = 1
