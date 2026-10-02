import gzip
import json

import pytest

from jlcparts.kilncatalog import FIELDS, F, FORMAT, Catalog, BuildRefused, _check, diff, priceMoved


def row(lcsc, stock=10, price=((1, None, 1.0),), attrs=(), description="part"):
    r = [None] * len(FIELDS)
    r[F["lcsc"]] = lcsc
    r[F["category"]] = 1
    r[F["description"]] = description
    r[F["attributes"]] = list(attrs)
    r[F["stock"]] = stock
    r[F["price"]] = [list(t) for t in price]
    return r


def write(path, header, ops):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"format": FORMAT, "fields": FIELDS, **header}) + "\n")
        for op in ops:
            f.write(json.dumps(op) + "\n")
    return str(path)


def test_snapshot_then_delta_replays(tmp_path):
    c = Catalog()
    c.apply(write(tmp_path / "s", {"kind": "snapshot", "build": "b1", "next": {"a": 1, "c": 2}},
                  [["c", 1, "Resistors", "Chip"], ["a", 0, "Resistance", {"v": 1}],
                   ["u", row("C1", attrs=[0])], ["u", row("C2")]]))
    c.apply(write(tmp_path / "d", {"kind": "delta", "base": "b1", "build": "b2", "next": {"a": 2, "c": 2}},
                  [["a", 1, "Power", {"v": 2}], ["s", "C1", 5], ["p", "C2", [[1, None, 2.0]]],
                   ["u", row("C3", attrs=[1])], ["d", "C2"]]))
    assert c.build == "b2"
    assert sorted(c.parts) == ["C1", "C3"]
    assert c.parts["C1"][F["stock"]] == 5
    assert c.attrIds['["Power",{"v":2}]'] == 1


def test_delta_on_the_wrong_base_is_refused(tmp_path):
    c = Catalog()
    c.apply(write(tmp_path / "s", {"kind": "snapshot", "build": "b1", "next": {"a": 0, "c": 1}}, []))
    with pytest.raises(ValueError):
        c.apply(write(tmp_path / "d", {"kind": "delta", "base": "b0", "build": "b2", "next": {"a": 0, "c": 1}}, []))


def test_ids_dropped_by_a_snapshot_are_not_reused(tmp_path):
    c = Catalog()
    c.apply(write(tmp_path / "s", {"kind": "snapshot", "build": "b1", "next": {"a": 7, "c": 3}},
                  [["a", 0, "Resistance", {"v": 1}]]))
    assert c.attrId("Power", {"v": 2}, []) == 7
    assert c.catId("Capacitors", "MLCC", []) == 3


def test_price_tolerance():
    assert not priceMoved([[1, 9, 1.0]], [[1, 9, 1.005]])
    assert priceMoved([[1, 9, 1.0]], [[1, 9, 1.02]])
    assert priceMoved([[1, 9, 1.0]], [[1, 19, 1.0]]), "a changed quantity break always counts"
    assert priceMoved([[1, 9, 1.0]], [[1, 9, 1.0], [10, None, 0.9]])


def test_diff_keeps_published_price_through_small_drift():
    c = Catalog()
    c.parts = {"C1": row("C1", price=[[1, None, 1.0]]), "C2": row("C2"), "C3": row("C3")}
    new = {"C1": row("C1", price=[[1, None, 1.004]]),          # drift: stays as published
           "C2": row("C2", stock=3),                           # stock only
           "C4": row("C4")}                                    # added; C3 removed
    ops, counts = diff(c, new)
    assert new["C1"][F["price"]] == [[1, None, 1.0]]
    assert ["s", "C2", 3] in ops and ["d", "C3"] in ops and ["u", new["C4"]] in ops
    assert counts == {"upserts": 1, "stock": 1, "price": 0, "removed": 1}

    c.parts = {"C1": row("C1", price=[[1, None, 1.0]])}          # drift accumulates past 1%
    ops, _ = diff(c, {"C1": row("C1", price=[[1, None, 1.012]])})
    assert ops == [["p", "C1", [[1, None, 1.012]]]]


def test_size_checks():
    _check(0, 700_000, 0, False, print)
    with pytest.raises(BuildRefused):
        _check(0, 36_705, 0, False, print)
    with pytest.raises(BuildRefused):
        _check(700_000, 600_000, 0, False, print)
    with pytest.raises(BuildRefused):
        _check(700_000, 700_000, 40_000, False, print)
    _check(700_000, 600_000, 0, True, print)
