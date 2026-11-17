"""Bitemporal identity resolution for customer-account equivalence.

Two independent time axes are involved:

* **valid time** – a link ``[valid_from, valid_to)`` says two accounts belonged
  to the same customer *during that interval* of the transaction timeline.
  It is consulted per ``event_time``; a link never applies outside its half
  open interval.
* **knowledge time** – link (and event) revisions are applied in generations.
  For a fixed generation we first pick, for each ``link_id``, the highest
  revision that was already known at that generation.  A delete revision is a
  retraction *in knowledge time*: it hides the link from that generation
  onwards but never erases the earlier ``put`` revision, so a close taken
  before the retraction keeps its historical grouping.

Only after those two selections do we build the undirected graph for one
distinct ``event_time``: every effective link whose interval contains the
instant contributes an edge, arbitrary-length connectivity (including cycles
and transitive chains) is collapsed, and the canonical account of a component
is the lexicographically (UTF-8 bytes) smallest node.  An account with no
effective edge at that instant stays its own canonical account.

Implementation
--------------
Resolving every instant independently would be quadratic in the worst case
(thousands of distinct ``event_time`` values times thousands of links).  The
same result is computed with *offline dynamic connectivity*: each link's
valid interval is inserted into a segment tree over the sorted event times and
a DFS over that tree maintains a rollback union-find.  Every instant sees
exactly the links whose half-open interval brackets it, at
``O((L log T + T) α)`` total work, and components are still independent per
instant because the DSU is rolled back when leaving a subtree.
"""
from bisect import bisect_left


def _byte_key(value):
    return value.encode("utf-8")


def select_revisions(revisions, generation):
    """Highest revision whose apply generation is <= ``generation``.

    ``revisions`` is ordered by ascending revision (and therefore by ascending
    apply generation).  Returns ``None`` when nothing was known yet.
    """
    chosen = None
    for revision in revisions:
        if revision["generation"] <= generation:
            chosen = revision
    return chosen


def resolve_events(revisions_by_id, generation):
    """Active event content at ``generation`` keyed by ``event_id``.

    The selected highest revision is authoritative: if it is a ``delete`` the
    event is inactive and contributes nothing to that generation's projection.
    """
    active = {}
    for event_id, revisions in revisions_by_id.items():
        chosen = select_revisions(revisions, generation)
        if chosen is None or chosen["op"] == "delete":
            continue
        active[event_id] = {
            "event_id": event_id,
            "account_id": chosen["account_id"],
            "event_time": chosen["event_time"],
            "amount_minor": chosen["amount_minor"],
        }
    return active


def resolve_links(revisions_by_id, generation):
    """Effective ``put`` links at ``generation``, ordered by valid time."""
    links = []
    for link_id, revisions in revisions_by_id.items():
        chosen = select_revisions(revisions, generation)
        if chosen is None or chosen["op"] == "delete":
            continue
        links.append(
            {
                "link_id": link_id,
                "left_account": chosen["left_account"],
                "right_account": chosen["right_account"],
                "valid_from": chosen["valid_from"],
                "valid_to": chosen["valid_to"],
            }
        )
    links.sort(
        key=lambda link: (
            link["valid_from"], link["valid_to"],
            _byte_key(link["left_account"]),
            _byte_key(link["right_account"]), _byte_key(link["link_id"]),
        )
    )
    return links


class _RollbackDisjointSet:
    """Union-find with no path compression and an undo stack.

    Each root tracks its component size and its UTF-8 smallest member, so a
    component's canonical account is available in O(1) after the unions.
    Union by size keeps the tree depth logarithmic; rollback restores exactly
    the state at the latest checkpoint.
    """

    def __init__(self):
        self._parent = {}
        self._size = {}
        self._minimum = {}
        self._history = []

    def __contains__(self, node):
        return node in self._parent

    def add(self, node):
        if node not in self._parent:
            self._parent[node] = node
            self._size[node] = 1
            self._minimum[node] = node

    def find(self, node):
        parent = self._parent
        while parent[node] != node:
            node = parent[node]
        return node

    def union(self, left, right):
        self.add(left)
        self.add(right)
        root_left, root_right = self.find(left), self.find(right)
        if root_left == root_right:
            self._history.append(None)
            return
        if self._size[root_left] < self._size[root_right]:
            root_left, root_right = root_right, root_left
        self._history.append(
            (
                root_right, root_left,
                self._size[root_left], self._minimum[root_left],
            )
        )
        self._parent[root_right] = root_left
        self._size[root_left] += self._size[root_right]
        if _byte_key(self._minimum[root_right]) < _byte_key(
            self._minimum[root_left]
        ):
            self._minimum[root_left] = self._minimum[root_right]

    def checkpoint(self):
        return len(self._history)

    def rollback(self, checkpoint):
        while len(self._history) > checkpoint:
            entry = self._history.pop()
            if entry is None:
                continue
            child, parent, parent_size, parent_minimum = entry
            self._parent[child] = child
            self._size[parent] = parent_size
            self._minimum[parent] = parent_minimum

    def minimum_of(self, node):
        return self._minimum[self.find(node)]


def _insert_interval(tree, size, left, right, payload):
    """Insert ``payload`` into an iterative segment tree over ``[left,right)``."""
    left += size
    right += size
    while left < right:
        if left & 1:
            tree[left].append(payload)
            left += 1
        if right & 1:
            right -= 1
            tree[right].append(payload)
        left >>= 1
        right >>= 1


def canonical_accounts(events, links):
    """Map ``event_id`` to its canonical account for every active event.

    Every distinct ``event_time`` sees the graph induced by exactly the links
    whose half-open interval brackets that instant; using one end-of-period
    component for the whole history would wrongly merge accounts across
    interval boundaries.
    """
    if not events:
        return {}
    by_time = {}
    for event in events.values():
        by_time.setdefault(event["event_time"], []).append(event)
    times = sorted(by_time)

    size = 1
    while size < len(times):
        size <<= 1
    tree = [[] for _ in range(2 * size)]
    for link in links:
        left = bisect_left(times, link["valid_from"])
        right = bisect_left(times, link["valid_to"])
        if left < right:
            _insert_interval(tree, size, left, right, link)

    dsu = _RollbackDisjointSet()
    resolved = {}

    def visit(node):
        checkpoint = dsu.checkpoint()
        for link in tree[node]:
            dsu.union(link["left_account"], link["right_account"])
        if node >= size:
            index = node - size
            if index < len(times):
                for event in by_time[times[index]]:
                    account = event["account_id"]
                    resolved[event["event_id"]] = (
                        dsu.minimum_of(account) if account in dsu else account
                    )
        else:
            visit(node * 2)
            visit(node * 2 + 1)
        dsu.rollback(checkpoint)

    visit(1)
    return resolved
