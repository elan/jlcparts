import gzip
import json

import pytest

from jlcparts.kilncatalog import FIELDS, F, BuildRefused, Registry, check, snapshotOps, writeSnapshot


def row(lcsc, category, attrs=()):
    r = [None] * len(FIELDS)
    r[F["lcsc"]] = lcsc
    r[F["category"]] = category
    r[F["attributes"]] = list(attrs)
    return r


def test_snapshot_lists_only_entries_in_use_before_the_parts(tmp_path):
    reg = Registry()
    res = reg.catId("Resistors", "Chip Resistor")
    reg.catId("Unused", "Gone")
    ohm = reg.attrId("Resistance", {"v": 10})
    reg.attrId("Unused", {"v": 0})
    assert reg.catId("Resistors", "Chip Resistor") == res, "the same pair keeps its ID"

    ops = snapshotOps({"C2": row("C2", res, [ohm]), "C1": row("C1", res)}, reg)
    assert ops == [["c", res, "Resistors", "Chip Resistor"], ["a", ohm, "Resistance", {"v": 10}],
                   ["u", row("C1", res)], ["u", row("C2", res, [ohm])]]

    meta = writeSnapshot(tmp_path / "s.jsonl.gz", "b1", ops)
    lines = gzip.open(tmp_path / "s.jsonl.gz", "rt").read().splitlines()
    assert json.loads(lines[0]) == {"format": 1, "kind": "snapshot", "build": "b1", "fields": FIELDS}
    assert [json.loads(l) for l in lines[1:]] == ops
    assert meta["bytes"] > 0 and len(meta["sha256"]) == 64


def test_size_checks():
    check(0, 700_000, False, print)
    with pytest.raises(BuildRefused):
        check(0, 36_705, False, print)
    with pytest.raises(BuildRefused):
        check(700_000, 600_000, False, print)
    check(700_000, 650_000, False, print)
    check(700_000, 600_000, True, print)
