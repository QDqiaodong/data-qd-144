"""Exact global cutting-plan optimizer for the bounded offline stock warehouse.

The optimizer is deliberately *not* first-fit-decreasing: a best-fit-decreasing
pass only seeds an initial incumbent, while an exact depth-first search proves
the lexicographic optimum of the stock contract.  Hard bounds (enforced by the
API layer): at most 8 open work orders, 12 available bars and 18 pending
pieces.

Search strategy — first-piece depth-first branch and bound
-----------------------------------------------------------
Two index spaces are kept strictly separate:

* ``assign[j]`` is indexed by piece and records the bar holding piece j;
* ``bar_used[i]`` / ``bar_count[i]`` are indexed by bar and record consumed
  length and piece count of bar i.

At each step the longest remaining piece (UTF-8 byte id as the deterministic
tie) is placed either onto an already opened compatible bar or into an unused
compatible bar.  Piece permutations are never expanded because the same
canonical piece is always chosen first from a given remaining set.  The search
is kept exact but small by:

* a best-fit-decreasing incumbent giving a tight initial bar-count upper
  bound, tightened in place whenever a better complete solution is found;
* optimistic capacity and grade-threshold lower bounds computed only from the
  remaining pieces and the bars currently available, so they tighten as the
  search deepens and never over-prune (kerf and grade splitting are ignored
  where that can only make the bound smaller);
* equal-shape bar collapsing (same consumed length, piece count and grade for
  opened bars; same length and grade for new bars), so symmetric assignments
  are explored once;
* a canonical state memo over the remaining pieces and the multiset of
  occupied/unused bar shapes, which merges equivalent revisits while the B
  tie-break is still decided exactly at leaves using concrete bar ids.

For a fixed piece set the minimized packing objective is
``(used bar count, used bar length sum, B tie-break tuple)``; the length sum
is equivalent to minimizing total remainder because piece lengths and kerf
loss are fixed for the chosen piece set.  :func:`plan_open_orders` wraps this
with the outer priority-sum / accepted-count / A-tuple ordering across all
work-order subsets.  The problem is NP-hard, so the resource documentation
reports measured time and memory at the contractual size limits rather than
assuming polynomial behaviour.
"""


def byte_key(value):
    """UTF-8 byte ordering key (stable, locale independent)."""
    return value.encode("utf-8")


def sort_by_id(values):
    return sorted(values, key=lambda item: byte_key(item["id"]))


def _bfd_incumbent(lengths, grades, bar_lengths, bar_grades, n_bars,
                   placement_order, kerf):
    """Best-fit-decreasing feasible upper bound on the number of used bars."""
    bar_used = [0] * n_bars
    bar_count = [0] * n_bars
    open_count = 0
    for piece_idx in placement_order:
        best_bar = -1
        best_slack = None
        for bar_idx in range(open_count):
            if bar_grades[bar_idx] < grades[piece_idx]:
                continue
            extra = lengths[piece_idx] + (kerf if bar_count[bar_idx] else 0)
            if bar_used[bar_idx] + extra > bar_lengths[bar_idx]:
                continue
            slack = bar_lengths[bar_idx] - (bar_used[bar_idx] + extra)
            if best_slack is None or slack < best_slack:
                best_slack = slack
                best_bar = bar_idx
        if best_bar >= 0:
            extra = lengths[piece_idx] + (kerf if bar_count[best_bar] else 0)
            bar_used[best_bar] += extra
            bar_count[best_bar] += 1
            continue
        placed = False
        for bar_idx in range(open_count, n_bars):
            if bar_grades[bar_idx] >= grades[piece_idx] and \
                    lengths[piece_idx] <= bar_lengths[bar_idx]:
                bar_used[bar_idx] = lengths[piece_idx]
                bar_count[bar_idx] = 1
                open_count = bar_idx + 1
                placed = True
                break
        if not placed:
            return None
    return sum(1 for c in bar_count if c > 0)


def best_packing(pieces, bars, kerf, memo=None):
    """Find the contract-best packing of ``pieces`` into ``bars``.

    Returns ``None`` when infeasible.  Each piece must carry its work order id
    (field ``order``) so the B tie-break is fully determined.  ``memo`` is
    accepted for API compatibility; the search is stateless per call.
    """
    pieces = sort_by_id(pieces)
    bars = sort_by_id(bars)
    n = len(pieces)
    if n == 0:
        return {
            "bar_count": 0,
            "bar_length_sum": 0,
            "b_tuple": (),
            "patterns": (),
            "pieces": tuple(pieces),
            "bars": tuple(bars),
        }
    n_bars = len(bars)
    lengths = [p["length"] for p in pieces]
    grades = [p["min_grade"] for p in pieces]
    bar_lengths = [b["length"] for b in bars]
    bar_grades = [b["grade"] for b in bars]
    bar_byte_ids = [byte_key(b["id"]) for b in bars]
    placement_order = sorted(
        range(n), key=lambda j: (-lengths[j], byte_key(pieces[j]["id"]))
    )
    piece_bit = [1 << j for j in range(n)]
    full_mask = (1 << n) - 1
    canonical = sorted(
        range(n),
        key=lambda j: (byte_key(pieces[j]["order"]), byte_key(pieces[j]["id"])),
    )
    for j in range(n):
        if not any(bar_grades[i] >= grades[j] and bar_lengths[i] >= lengths[j]
                   for i in range(n_bars)):
            return None

    incumbent_count = _bfd_incumbent(
        lengths, grades, bar_lengths, bar_grades, n_bars,
        placement_order, kerf,
    )
    if incumbent_count is None:
        incumbent_count = n_bars + 1
    incumbent_holder = [incumbent_count]
    max_bar_length = max(bar_lengths)
    distinct_grades = sorted(set(grades))

    bar_used = [0] * n_bars
    bar_count = [0] * n_bars
    assign = [-1] * n
    state_memo = set()
    best = {"obj": None}

    def fits(bar_idx, piece_idx):
        if bar_grades[bar_idx] < grades[piece_idx]:
            return False
        extra = lengths[piece_idx] + (kerf if bar_count[bar_idx] else 0)
        return bar_used[bar_idx] + extra <= bar_lengths[bar_idx]

    def current_objective():
        used_indices = [i for i in range(n_bars) if bar_count[i] > 0]
        length_sum = sum(bar_lengths[i] for i in used_indices)
        b_tuple = tuple(bar_byte_ids[assign[j]] for j in canonical)
        return (len(used_indices), length_sum, b_tuple)

    def lower_bound(remaining_mask):
        """Optimistic total-bar lower bound; ignores kerf, never over-prunes.

        Computed from remaining pieces and currently available bars (opened
        bar slack plus unused bars), with one capacity bound per grade
        threshold, so it tightens as the search deepens.
        """
        remaining_idxs = [
            j for j in placement_order if remaining_mask & piece_bit[j]
        ]
        demand = sum(lengths[j] for j in remaining_idxs)
        opened_slack = sum(
            bar_lengths[i] - bar_used[i]
            for i in range(n_bars) if bar_count[i] > 0
        )
        unused_capacity = sum(
            bar_lengths[i] for i in range(n_bars) if bar_count[i] == 0
        )
        if demand > opened_slack + unused_capacity:
            return n_bars + 1
        used_bar_count = sum(1 for c in bar_count if c > 0)
        residual = max(0, demand - opened_slack)
        bound = used_bar_count + (
            (residual + max_bar_length - 1) // max_bar_length
        )
        for g in distinct_grades:
            high_demand = sum(
                lengths[j] for j in remaining_idxs if grades[j] >= g
            )
            if not high_demand:
                continue
            high_slack = sum(
                bar_lengths[i] - bar_used[i]
                for i in range(n_bars)
                if bar_count[i] > 0 and bar_grades[i] >= g
            )
            high_unused_caps = [
                bar_lengths[i] for i in range(n_bars)
                if bar_count[i] == 0 and bar_grades[i] >= g
            ]
            if high_slack < high_demand and not high_unused_caps:
                return n_bars + 1
            cap = max(high_unused_caps, default=max_bar_length)
            extra = max(0, high_demand - high_slack)
            need_new = (extra + cap - 1) // cap
            bound = max(bound, used_bar_count + need_new)
        return bound

    def dfs(remaining_mask):
        used_bar_count = sum(1 for c in bar_count if c > 0)
        if used_bar_count > incumbent_holder[0]:
            return
        bound = lower_bound(remaining_mask)
        if bound > incumbent_holder[0]:
            return
        if not remaining_mask:
            obj = current_objective()
            if best["obj"] is None or obj < best["obj"]:
                best["obj"] = obj
                best["assignment"] = tuple(assign)
                if obj[0] < incumbent_holder[0]:
                    incumbent_holder[0] = obj[0]
            return
        occupied_signature = tuple(sorted(
            (bar_grades[i], bar_used[i], bar_count[i])
            for i in range(n_bars) if bar_count[i] > 0
        ))
        unused_signature = tuple(sorted(
            (bar_grades[i], bar_lengths[i])
            for i in range(n_bars) if bar_count[i] == 0
        ))
        key = (remaining_mask, occupied_signature, unused_signature)
        if key in state_memo:
            return
        state_memo.add(key)
        piece_idx = next(
            j for j in placement_order if remaining_mask & piece_bit[j]
        )
        bit = piece_bit[piece_idx]
        # Existing opened bars; collapse equal (consumed, count, grade) shapes.
        seen_shapes = set()
        for bar_idx in range(n_bars):
            if bar_count[bar_idx] == 0 or not fits(bar_idx, piece_idx):
                continue
            shape = (bar_used[bar_idx], bar_count[bar_idx],
                     bar_grades[bar_idx])
            if shape in seen_shapes:
                continue
            seen_shapes.add(shape)
            extra = lengths[piece_idx] + (kerf if bar_count[bar_idx] else 0)
            bar_used[bar_idx] += extra
            bar_count[bar_idx] += 1
            assign[piece_idx] = bar_idx
            dfs(remaining_mask ^ bit)
            assign[piece_idx] = -1
            bar_count[bar_idx] -= 1
            bar_used[bar_idx] -= extra
        # Open an unused compatible bar; collapse equal (length, grade) shapes.
        # No new bar once the incumbent bar count is reached.
        if used_bar_count < incumbent_holder[0]:
            seen_new = set()
            for bar_idx in range(n_bars):
                if bar_count[bar_idx] > 0:
                    continue
                if bar_grades[bar_idx] < grades[piece_idx] or \
                        lengths[piece_idx] > bar_lengths[bar_idx]:
                    continue
                shape = (bar_lengths[bar_idx], bar_grades[bar_idx])
                if shape in seen_new:
                    continue
                seen_new.add(shape)
                bar_used[bar_idx] = lengths[piece_idx]
                bar_count[bar_idx] = 1
                assign[piece_idx] = bar_idx
                dfs(remaining_mask ^ bit)
                assign[piece_idx] = -1
                bar_count[bar_idx] = 0
                bar_used[bar_idx] = 0

    dfs(full_mask)
    if best["obj"] is None or "assignment" not in best:
        return None
    final_assignment = best["assignment"]
    patterns = {}
    for piece_idx, bar_idx in enumerate(final_assignment):
        patterns.setdefault(bar_idx, 0)
        patterns[bar_idx] |= piece_bit[piece_idx]
    pattern_tuple = tuple(
        (bar_idx, mask) for bar_idx, mask in sorted(patterns.items())
    )
    return {
        "bar_count": best["obj"][0],
        "bar_length_sum": best["obj"][1],
        "b_tuple": best["obj"][2],
        "patterns": pattern_tuple,
        "pieces": tuple(pieces),
        "bars": tuple(bars),
    }


def _build_layouts(packing, kerf, reuse_min):
    """Turn a packing result into per-bar, per-piece traceable layouts."""
    pieces = packing["pieces"]
    bars = packing["bars"]
    layouts = []
    for bar_idx, placed_mask in packing["patterns"]:
        bar = bars[bar_idx]
        idxs = [j for j in range(len(pieces)) if placed_mask & (1 << j)]
        seq_pieces = sorted(
            (pieces[j] for j in idxs),
            key=lambda p: (-p["length"], byte_key(p["id"])),
        )
        used_length = (
            sum(p["length"] for p in seq_pieces)
            + kerf * (len(seq_pieces) - 1)
        )
        remainder = bar["length"] - used_length
        layouts.append(
            {
                "bar_id": bar["id"],
                "grade": bar["grade"],
                "length": bar["length"],
                "pieces": [
                    {
                        "id": p["id"],
                        "work_order": p["order"],
                        "length": p["length"],
                        "min_grade": p["min_grade"],
                        "seq": pos,
                    }
                    for pos, p in enumerate(seq_pieces)
                ],
                "used": used_length,
                "kerf_loss": kerf * (len(seq_pieces) - 1),
                "remainder": remainder,
                "planned_reusable": remainder >= reuse_min,
            }
        )
    return sorted(layouts, key=lambda layout: byte_key(layout["bar_id"]))


def deferral_reasons(order, bars, kerf):
    """Structural reason codes for a deferred work order.

    Codes: ``grade`` (a piece has no bar of sufficient grade),
    ``length`` (a piece fits no grade-compatible bar, or the grade-compatible
    stock is shorter than the total demand), ``quantity`` (stock exists but is
    committed to the globally preferred accepted combination, or is too
    fragmented).  Several codes may apply; they are returned in canonical
    order.
    """
    solo = best_packing(
        [dict(piece, order=order["id"]) for piece in order["pieces"]],
        bars, kerf,
    )
    if solo is not None:
        return ["quantity"]
    reasons = []
    grade_blocked = any(
        all(bar["grade"] < piece["min_grade"] for bar in bars)
        for piece in order["pieces"]
    )
    if grade_blocked:
        reasons.append("grade")
    length_blocked = False
    for piece in order["pieces"]:
        compatible = [bar for bar in bars
                      if bar["grade"] >= piece["min_grade"]]
        if compatible and piece["length"] > max(bar["length"]
                                                for bar in compatible):
            length_blocked = True
    min_grade = min(piece["min_grade"] for piece in order["pieces"])
    compatible_bars = [bar for bar in bars if bar["grade"] >= min_grade]
    demand = (
        sum(piece["length"] for piece in order["pieces"])
        + kerf * (len(order["pieces"]) - 1)
    )
    if compatible_bars and demand > sum(bar["length"]
                                        for bar in compatible_bars):
        length_blocked = True
    if length_blocked:
        reasons.append("length")
    if not reasons:
        reasons.append("quantity")
    canonical_order = {"quantity": 0, "length": 1, "grade": 2}
    return sorted(reasons, key=lambda code: canonical_order[code])


def plan_open_orders(orders, bars, kerf, reuse_min):
    """Exact joint plan over all open work orders and available bars."""
    orders = sort_by_id(orders)
    bars = sort_by_id(bars)
    packing_cache = {}

    def packing_for(piece_list):
        key = frozenset(piece["id"] for piece in piece_list)
        if key not in packing_cache:
            packing_cache[key] = best_packing(piece_list, bars, kerf)
        return packing_cache[key]

    best = None
    empty_packing = packing_for(())
    best = (
        (0, 0, 0, 0, (), ()),
        (),
        empty_packing,
    )
    width = len(orders)
    for mask in range(1, 1 << width):
        subset = [orders[j] for j in range(width) if (mask >> j) & 1]
        pieces = [
            dict(piece, order=order["id"])
            for order in subset
            for piece in order["pieces"]
        ]
        packing = packing_for(pieces)
        if packing is None:
            continue
        priority_sum = sum(order["priority"] for order in subset)
        accepted_tuple = tuple(byte_key(order["id"]) for order in subset)
        objective = (
            -priority_sum,
            -len(subset),
            packing["bar_count"],
            packing["bar_length_sum"],
            accepted_tuple,
            packing["b_tuple"],
        )
        if objective < best[0]:
            best = (
                objective,
                tuple(j for j in range(width) if (mask >> j) & 1),
                packing,
            )

    accepted_indices = set(best[1])
    accepted = [orders[j]["id"] for j in sorted(accepted_indices)]
    deferred = []
    for j, order in enumerate(orders):
        if j in accepted_indices:
            continue
        deferred.append(
            {"id": order["id"],
             "reasons": deferral_reasons(order, bars, kerf)}
        )
    packing = best[2]
    layouts = _build_layouts(packing, kerf, reuse_min) if accepted else []
    priority_sum = sum(orders[j]["priority"] for j in accepted_indices)
    return {
        "accepted": accepted,
        "deferred": deferred,
        "layouts": layouts,
        "totals": {
            "priority_sum": priority_sum,
            "accepted_count": len(accepted),
            "bars_used": len(layouts),
            "total_remainder": sum(layout["remainder"] for layout in layouts),
        },
    }
