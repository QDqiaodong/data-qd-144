"""Strict validation for the energy-audit source document.

The source top level is exactly four arrays::

    {
      "meters":  [ {"id","modulus","max_power",
                    "readings":[{"t","value"} ...]} ... ],
      "runs":    [ {"id","meter_id","plan_id","bar_id","start","end"} ... ],
      "tariffs": [ {"meter_id","start","end","price_num","price_den"} ... ],
      "budgets": [ {"work_order","min_energy","max_energy"} ... ]
    }

Validation here is purely structural and value-range based; it never opens a
warehouse.  Booleans are never accepted as integers, unknown fields and
duplicate identifiers are rejected, and every quantity is an exact integer.

Wrap-around candidate sets are *computed* here but an empty candidate set is
not a format error: it makes the global interpretation set empty (the audit
reports ``infeasible``) because the readings themselves are well-formed
observations that simply admit no physically possible consumption.
"""
from cutting_stock.common import ident

MAX_TIME = 10**9
MAX_MODULUS = 10**6
MAX_POWER = 10**6
MAX_INTERVALS = 10
MAX_RUNS = 20
MAX_PRICE_NUM = 10**6
MAX_PRICE_DEN = 10**6
MAX_BUDGET_ENERGY = 10**12


class AuditError(ValueError):
    """Contract violation carrying a stable error code for CLI exit 2."""

    code = "invalid_request"


class WarehouseStateError(AuditError):
    """The warehouse cannot serve the requested finished-plan history."""

    code = "warehouse_state"


def _strict_int(value, minimum, maximum, field):
    if type(value) is not int or isinstance(value, bool):
        raise AuditError(
            f"{field} must be a strict integer in [{minimum},{maximum}]"
        )
    if not minimum <= value <= maximum:
        raise AuditError(f"{field} outside allowed bounds [{minimum},{maximum}]")
    return value


def _require_fields(obj, fields, context):
    if not isinstance(obj, dict):
        raise AuditError(f"{context} must be an object")
    if set(obj) != set(fields):
        raise AuditError(
            f"{context} expects exactly fields {sorted(fields)}, "
            f"got {sorted(obj)}"
        )


def _ident(value, field):
    try:
        return ident(value)
    except ValueError as exc:
        raise AuditError(f"{field}: {exc}") from exc


def _array(value, context, limit=None):
    if not isinstance(value, list):
        raise AuditError(f"{context} must be an array")
    if limit is not None and len(value) > limit:
        raise AuditError(f"{context} allows at most {limit} entries")
    return value


def _byte_key(value):
    return value.encode("utf-8")


# --- meters ------------------------------------------------------------------


def _parse_meter(raw, seen_ids):
    _require_fields(raw, "id modulus max_power readings".split(), "meter")
    meter_id = _ident(raw["id"], "meter id")
    if meter_id in seen_ids:
        raise AuditError("duplicate meter id: " + meter_id)
    seen_ids.add(meter_id)
    modulus = _strict_int(raw["modulus"], 2, MAX_MODULUS, "modulus")
    max_power = _strict_int(raw["max_power"], 0, MAX_POWER, "max_power")
    readings_raw = _array(raw["readings"], "readings", MAX_INTERVALS + 1)
    readings = []
    for item in readings_raw:
        _require_fields(item, "t value".split(), "reading")
        t = _strict_int(item["t"], 0, MAX_TIME, "reading t")
        value = _strict_int(item["value"], 0, modulus - 1, "reading value")
        readings.append((t, value))
    readings.sort(key=lambda pair: pair[0])
    if len(readings) < 2:
        raise AuditError("meter " + meter_id + " needs at least two readings")
    for (t0, _), (t1, _) in zip(readings, readings[1:]):
        if t0 == t1:
            raise AuditError(
                "duplicate reading time on meter " + meter_id + f": t={t0}"
            )
    intervals = []
    for (t0, v0), (t1, v1) in zip(readings, readings[1:]):
        dt = t1 - t0
        cap = max_power * dt
        if cap > 2 * modulus:
            raise AuditError(
                f"meter {meter_id} interval [{t0},{t1}) allows more than "
                "two wraparounds (max_power*dt > 2*modulus)"
            )
        delta = (v1 - v0) % modulus
        # d = delta + k*modulus for k = 0,1,2 (zero, one or two wraparounds),
        # bounded by the physical cap max_power*dt.  May be empty: that is a
        # metering infeasibility, not a format error.
        candidates = [d for d in range(delta, cap + 1, modulus)]
        intervals.append(
            {
                "start": t0,
                "end": t1,
                "dt": dt,
                "delta": delta,
                "cap": cap,
                "candidates": candidates,
            }
        )
    return {
        "id": meter_id,
        "modulus": modulus,
        "max_power": max_power,
        "readings": readings,
        "intervals": intervals,
        "first_t": readings[0][0],
        "last_t": readings[-1][0],
    }


# --- runs --------------------------------------------------------------------


def _parse_run(raw, seen_ids, seen_pairs, meter_ids):
    _require_fields(raw, "id meter_id plan_id bar_id start end".split(), "run")
    run_id = _ident(raw["id"], "run id")
    if run_id in seen_ids:
        raise AuditError("duplicate run id: " + run_id)
    seen_ids.add(run_id)
    meter_id = _ident(raw["meter_id"], "run meter_id")
    if meter_id not in meter_ids:
        raise AuditError("run references unknown meter: " + meter_id)
    plan_id = _ident(raw["plan_id"], "run plan_id")
    bar_id = _ident(raw["bar_id"], "run bar_id")
    start = _strict_int(raw["start"], 0, MAX_TIME, "run start")
    end = _strict_int(raw["end"], 0, MAX_TIME, "run end")
    if not start < end:
        raise AuditError("run " + run_id + " must satisfy start < end")
    pair = (plan_id, bar_id)
    if pair in seen_pairs:
        raise AuditError(
            f"(plan_id, bar_id) pair appears in more than one run: {pair}"
        )
    seen_pairs.add(pair)
    return {
        "id": run_id,
        "meter_id": meter_id,
        "plan_id": plan_id,
        "bar_id": bar_id,
        "start": start,
        "end": end,
    }


# --- tariffs -----------------------------------------------------------------


def _parse_tariff(raw, meter_ids):
    _require_fields(
        raw, "meter_id start end price_num price_den".split(), "tariff"
    )
    meter_id = _ident(raw["meter_id"], "tariff meter_id")
    if meter_id not in meter_ids:
        raise AuditError("tariff references unknown meter: " + meter_id)
    start = _strict_int(raw["start"], 0, MAX_TIME, "tariff start")
    end = _strict_int(raw["end"], 0, MAX_TIME, "tariff end")
    if not start < end:
        raise AuditError("tariff must satisfy start < end")
    price_num = _strict_int(raw["price_num"], 0, MAX_PRICE_NUM, "price_num")
    price_den = _strict_int(raw["price_den"], 1, MAX_PRICE_DEN, "price_den")
    return {
        "meter_id": meter_id,
        "start": start,
        "end": end,
        "price_num": price_num,
        "price_den": price_den,
    }


# --- budgets -----------------------------------------------------------------


def _parse_budget(raw, seen_orders):
    _require_fields(raw, "work_order min_energy max_energy".split(), "budget")
    work_order = _ident(raw["work_order"], "budget work_order")
    if work_order in seen_orders:
        raise AuditError("duplicate budget work order: " + work_order)
    seen_orders.add(work_order)
    min_energy = _strict_int(
        raw["min_energy"], 0, MAX_BUDGET_ENERGY, "min_energy"
    )
    max_energy = _strict_int(
        raw["max_energy"], 0, MAX_BUDGET_ENERGY, "max_energy"
    )
    if min_energy > max_energy:
        raise AuditError("budget min_energy exceeds max_energy")
    return {
        "work_order": work_order,
        "min_energy": min_energy,
        "max_energy": max_energy,
    }


# --- whole document ------------------------------------------------------------


def parse_source(data):
    """Validate the source document and return a normalized, canonically
    ordered structure.  Array order in the input never affects the result."""
    _require_fields(data, "meters runs tariffs budgets".split(), "source")

    meters_raw = _array(data["meters"], "meters", MAX_INTERVALS)
    meter_ids = set()
    meters = [_parse_meter(raw, meter_ids) for raw in meters_raw]
    total_intervals = sum(len(meter["intervals"]) for meter in meters)
    if total_intervals > MAX_INTERVALS:
        raise AuditError(
            f"at most {MAX_INTERVALS} reading intervals across all meters"
        )
    if total_intervals < 1:
        raise AuditError("at least one reading interval is required")
    meters.sort(key=lambda meter: _byte_key(meter["id"]))
    meters_by_id = {meter["id"]: meter for meter in meters}

    runs_raw = _array(data["runs"], "runs", MAX_RUNS)
    run_ids, run_pairs = set(), set()
    runs = [
        _parse_run(raw, run_ids, run_pairs, meter_ids) for raw in runs_raw
    ]
    for run in runs:
        meter = meters_by_id[run["meter_id"]]
        if run["start"] < meter["first_t"] or run["end"] > meter["last_t"]:
            raise AuditError(
                "run " + run["id"] + " lies outside the reading domain of "
                "meter " + run["meter_id"]
            )
    runs.sort(key=lambda run: _byte_key(run["id"]))
    # Per-meter run windows must not overlap; touching edges are allowed.
    for meter in meters:
        windows = sorted(
            (run for run in runs if run["meter_id"] == meter["id"]),
            key=lambda run: (run["start"], run["end"], _byte_key(run["id"])),
        )
        for left, right in zip(windows, windows[1:]):
            if left["end"] > right["start"]:
                raise AuditError(
                    f"overlapping run windows on meter {meter['id']}: "
                    f"{left['id']} and {right['id']}"
                )

    tariffs_raw = _array(data["tariffs"], "tariffs")
    tariffs = [_parse_tariff(raw, meter_ids) for raw in tariffs_raw]
    tariffs_by_meter = {meter_id: [] for meter_id in meter_ids}
    for tariff in tariffs:
        tariffs_by_meter[tariff["meter_id"]].append(tariff)
    for meter in meters:
        segments = sorted(
            tariffs_by_meter[meter["id"]],
            key=lambda tariff: (tariff["start"], tariff["end"]),
        )
        if not segments:
            raise AuditError(
                "meter " + meter["id"] + " has no tariff coverage"
            )
        if segments[0]["start"] != meter["first_t"]:
            raise AuditError(
                "tariff coverage of meter " + meter["id"]
                + " does not start at the first reading"
            )
        if segments[-1]["end"] != meter["last_t"]:
            raise AuditError(
                "tariff coverage of meter " + meter["id"]
                + " does not end at the last reading"
            )
        for left, right in zip(segments, segments[1:]):
            if left["end"] != right["start"]:
                raise AuditError(
                    "tariff coverage of meter " + meter["id"]
                    + " has a gap or overlap"
                )
        tariffs_by_meter[meter["id"]] = segments

    budgets_raw = _array(data["budgets"], "budgets")
    budget_orders = set()
    budgets = [_parse_budget(raw, budget_orders) for raw in budgets_raw]
    budgets.sort(key=lambda budget: _byte_key(budget["work_order"]))

    return {
        "meters": meters,
        "meters_by_id": meters_by_id,
        "runs": runs,
        "tariffs_by_meter": tariffs_by_meter,
        "budgets": budgets,
    }
