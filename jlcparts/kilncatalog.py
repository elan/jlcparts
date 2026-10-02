"""
The catalog as Kiln consumes it: a weekly snapshot plus one small change file per day.

Both files are the same gzipped JSON-lines op stream. The first line is a header; every later line
is one operation:

    ["a", id, name, value]          attribute table entry (id is permanent)
    ["c", id, category, subcategory]
    ["u", [field values...]]        add or replace a part, fields in header["fields"] order
    ["d", "C123"]                   remove a part
    ["s", "C123", stock]
    ["p", "C123", [[qFrom, qTo, price], ...]]

A snapshot is that stream applied to an empty catalog; a change file applies on top of the build
named in its header's "base". index.json names the current snapshot and the change files since it.

The published files are also the builder's state: it replays them to learn what an up-to-date
client holds, including the permanent attribute and category IDs, and writes the difference.
"""

import gzip
import hashlib
import json
import os
import time

import click

from .datatables import COMPONENT_SOURCE_SCHEMA, extractComponent, _isUsableCategory
from .sourceDb import SourceDb
from .taxonomy import normalize_category_pair

FORMAT = 1
# Only what Kiln reads: it resolves datasheets through EasyEDA and builds product links from the
# LCSC code, so those (~15% of the snapshot) stay out.
FIELDS = ["lcsc", "category", "mfr", "manufacturer", "package", "tier", "joints", "description",
          "img", "attributes", "stock", "price", "attrition", "assembly"]
F = {name: i for i, name in enumerate(FIELDS)}

PRICE_TOLERANCE = 0.01      # JLC's USD prices drift daily by fractions of a percent
FLOOR = 100_000             # no real build comes anywhere near this
MIN_RATIO = 0.9             # a build under this share of the published catalog is refused
MAX_REMOVED = 0.05          # so is a change file removing more than this share of parts


class BuildRefused(RuntimeError):
    pass


def _key(*parts):
    return json.dumps(parts, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


class Catalog:
    """What a fully synced client holds, plus the permanent ID registries."""

    def __init__(self):
        self.parts = {}                 # lcsc -> row (list, FIELDS order)
        self.attrs = {}                 # id -> [name, value]
        self.cats = {}                  # id -> [category, subcategory]
        self.attrIds = {}               # _key(name, value) -> id
        self.catIds = {}                # _key(category, subcategory) -> id
        self.nextAttr = 0
        self.nextCat = 1
        self.build = None

    def apply(self, path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            header = json.loads(f.readline())
            if header.get("format") != FORMAT or header.get("fields") != FIELDS:
                raise ValueError(f"{path}: format {header.get('format')} isn't readable here")
            if header["kind"] == "snapshot":
                self.__init__()
            elif header.get("base") != self.build:
                raise ValueError(f"{path} applies to {header.get('base')}, not {self.build}")
            # IDs are never reused, even for entries a snapshot dropped because nothing uses them.
            self.nextAttr = max(self.nextAttr, header["next"]["a"])
            self.nextCat = max(self.nextCat, header["next"]["c"])
            for line in f:
                op = json.loads(line)
                kind = op[0]
                if kind == "u":
                    self.parts[op[1][F["lcsc"]]] = op[1]
                elif kind == "s":
                    self.parts[op[1]][F["stock"]] = op[2]
                elif kind == "p":
                    self.parts[op[1]][F["price"]] = op[2]
                elif kind == "d":
                    self.parts.pop(op[1], None)
                elif kind == "a":
                    self.attrs[op[1]] = [op[2], op[3]]
                    self.attrIds[_key(op[2], op[3])] = op[1]
                    self.nextAttr = max(self.nextAttr, op[1] + 1)
                elif kind == "c":
                    self.cats[op[1]] = [op[2], op[3]]
                    self.catIds[_key(op[2], op[3])] = op[1]
                    self.nextCat = max(self.nextCat, op[1] + 1)
            self.build = header["build"]

    def attrId(self, name, value, created):
        k = _key(name, value)
        if k not in self.attrIds:
            self.attrIds[k] = self.nextAttr
            self.attrs[self.nextAttr] = [name, value]
            created.append(["a", self.nextAttr, name, value])
            self.nextAttr += 1
        return self.attrIds[k]

    def catId(self, category, subcategory, created):
        k = _key(category, subcategory)
        if k not in self.catIds:
            self.catIds[k] = self.nextCat
            self.cats[self.nextCat] = [category, subcategory]
            created.append(["c", self.nextCat, category, subcategory])
            self.nextCat += 1
        return self.catIds[k]


def _tier(component):
    if component.get("basic"):
        return "basic"
    return "preferred" if component.get("preferred") else "extended"


def _price(tiers):
    return [[t.get("qFrom"), t.get("qTo"), t.get("price")] for t in (tiers or [])]


def priceMoved(old, new, tolerance=PRICE_TOLERANCE):
    """True if a quantity break changed or any tier's price moved by more than `tolerance`."""
    if [t[:2] for t in old] != [t[:2] for t in new]:
        return True
    return any(o[2] != n[2] and (not o[2] or abs(n[2] - o[2]) > tolerance * abs(o[2]))
               for o, n in zip(old, new))


def currentRows(lib, catalog, created, ignoreOldStock, report):
    """Every publishable part as a row, assigning permanent IDs (recorded in `created`) as it goes."""
    lib.prepareBuild()
    rows = {}
    for category, subcategories in sorted(lib.categories().items()):
        for subcategory in sorted(subcategories):
            if not _isUsableCategory(category, subcategory):
                continue
            canonical = normalize_category_pair(category, subcategory)
            catId = None
            for component in lib.iterCategoryComponents(category, subcategory,
                                                        stockNewerThan=ignoreOldStock, fetchSize=5000):
                if catId is None:
                    catId = catalog.catId(canonical.category, canonical.subcategory, created)
                values = dict(zip(COMPONENT_SOURCE_SCHEMA, extractComponent(component, COMPONENT_SOURCE_SCHEMA)))
                attrIds = sorted(catalog.attrId(name, value, created)
                                 for name, value in values["attributes"].items())
                extra = component.get("jlc_extra") or {}
                attrition = extra.get("attrition") or {}
                rows[component["lcsc"]] = [
                    component["lcsc"], catId, values["mfr"] or "", component.get("manufacturer") or "",
                    component.get("package") or "", _tier(component), values["joints"] or 0,
                    values["description"] or "", values["img"],
                    attrIds, values["stock"] or 0, _price(values["price"]),
                    [attrition.get("lossNumber"), attrition.get("leastPatchNumber"), attrition.get("minPurchaseNum")],
                    extra.get("assemblyProcess"),
                ]
            if catId is not None:
                report(f"  {canonical.category} / {canonical.subcategory}: {len(rows):,} parts so far")
    return rows


def diff(catalog, rows):
    """Ops turning `catalog` into `rows`. Sub-tolerance price drift is left as published."""
    ops = []
    stock = price = 0
    for lcsc, row in rows.items():
        old = catalog.parts.get(lcsc)
        if old is None:
            ops.append(["u", row])
            continue
        if not priceMoved(old[F["price"]], row[F["price"]]):
            row[F["price"]] = old[F["price"]]
        same = all(old[i] == row[i] for i in range(len(FIELDS)) if i not in (F["stock"], F["price"]))
        if not same:
            ops.append(["u", row])
            continue
        if old[F["stock"]] != row[F["stock"]]:
            ops.append(["s", lcsc, row[F["stock"]]]); stock += 1
        if old[F["price"]] != row[F["price"]]:
            ops.append(["p", lcsc, row[F["price"]]]); price += 1
    removed = sorted(catalog.parts.keys() - rows.keys())
    ops += [["d", lcsc] for lcsc in removed]
    return ops, {"upserts": sum(1 for o in ops if o[0] == "u"), "stock": stock, "price": price,
                 "removed": len(removed)}


def _write(path, header, ops):
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(json.dumps(header, separators=(",", ":")) + "\n")
        for op in ops:
            f.write(json.dumps(op, separators=(",", ":"), ensure_ascii=False) + "\n")
    with open(path, "rb") as f:
        return {"sha256": hashlib.sha256(f.read()).hexdigest(), "bytes": os.path.getsize(path)}


def _check(previousParts, newParts, removed, force, report):
    problems = []
    if newParts < FLOOR:
        problems.append(f"{newParts:,} parts is below the {FLOOR:,} floor")
    if previousParts and newParts < previousParts * MIN_RATIO:
        problems.append(f"{newParts:,} parts is under {MIN_RATIO:.0%} of the {previousParts:,} published")
    if previousParts and removed > previousParts * MAX_REMOVED:
        problems.append(f"removing {removed:,} of {previousParts:,} parts")
    if problems:
        if not force:
            raise BuildRefused("; ".join(problems))
        report("building anyway (forced): " + "; ".join(problems))


@click.command("buildkiln")
@click.argument("db", type=click.Path(dir_okay=False, exists=True))
@click.argument("outdir", type=click.Path(file_okay=False))
@click.option("--previous", type=click.Path(file_okay=False), default=None,
    help="Directory holding the published index.json and the files it names")
@click.option("--ignoreoldstock", type=int, default=120,
    help="Leave out parts that haven't been in stock for this many days")
@click.option("--snapshot-every", type=int, default=7,
    help="Publish a fresh snapshot once this many builds share the current one")
@click.option("--snapshot", is_flag=True, help="Publish a snapshot regardless")
@click.option("--force", is_flag=True, help="Publish even if the size checks fail")
def buildKiln(db, outdir, previous, ignoreoldstock, snapshot_every, snapshot, force):
    """
    Write Kiln's catalog files for DB into OUTDIR: index.json and a snapshot or change file.
    """
    started = time.time()
    report = lambda s: print(s, flush=True)
    catalog, index = Catalog(), None
    if previous and os.path.exists(os.path.join(previous, "index.json")):
        with open(os.path.join(previous, "index.json")) as f:
            index = json.load(f)
        for entry in [index["snapshot"]] + index["deltas"]:
            catalog.apply(os.path.join(previous, entry["file"]))
        if catalog.build != index["build"]:
            raise ValueError(f"replayed to {catalog.build}, but the index says {index['build']}")
        report(f"previous build {index['build']}: {len(catalog.parts):,} parts, "
               f"{len(index['deltas'])} change files since its snapshot")
    previousParts = len(catalog.parts)

    created = []
    rows = currentRows(SourceDb(db, create=False), catalog, created, ignoreoldstock, report)
    build = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    os.makedirs(outdir, exist_ok=True)

    makeSnapshot = snapshot or index is None or len(index["deltas"]) + 1 >= snapshot_every
    if makeSnapshot:
        _check(previousParts, len(rows), 0, force, report)
        name = f"snapshot-{build}.jsonl.gz"
        usedCats = {r[F["category"]] for r in rows.values()}
        usedAttrs = {a for r in rows.values() for a in r[F["attributes"]]}
        ops = ([["c", i, *catalog.cats[i]] for i in sorted(usedCats)]
               + [["a", i, *catalog.attrs[i]] for i in sorted(usedAttrs)]
               + [["u", rows[k]] for k in sorted(rows)])
        meta = _write(os.path.join(outdir, name),
                      {"format": FORMAT, "kind": "snapshot", "build": build, "fields": FIELDS,
                       "next": {"a": catalog.nextAttr, "c": catalog.nextCat}}, ops)
        newIndex = {"snapshot": {"file": name, "build": build, "parts": len(rows), **meta}, "deltas": []}
        report(f"snapshot {name}: {len(rows):,} parts, {meta['bytes'] / 1e6:.1f} MB")
    else:
        ops, counts = diff(catalog, rows)
        _check(previousParts, len(rows), counts["removed"], force, report)
        name = f"delta-{build}.jsonl.gz"
        meta = _write(os.path.join(outdir, name),
                      {"format": FORMAT, "kind": "delta", "base": catalog.build, "build": build,
                       "fields": FIELDS, "next": {"a": catalog.nextAttr, "c": catalog.nextCat}},
                      created + ops)
        newIndex = {"snapshot": index["snapshot"],
                    "deltas": index["deltas"] + [{"file": name, "base": catalog.build, "build": build, **meta}]}
        report(f"change file {name}: {len(created):,} new IDs, {counts['upserts']:,} parts added or "
               f"changed, {counts['stock']:,} stock, {counts['price']:,} price, {counts['removed']:,} "
               f"removed; {meta['bytes'] / 1e6:.2f} MB")

    counts = {}
    for row in rows.values():
        counts[row[F["category"]]] = counts.get(row[F["category"]], 0) + 1
    newIndex = {
        "format": FORMAT, "build": build, "parts": len(rows), **newIndex,
        "categories": [{"id": i, "category": c, "subcategory": s, "count": counts[i]}
                       for i, (c, s) in sorted(catalog.cats.items()) if i in counts],
    }
    with open(os.path.join(outdir, "index.json"), "w") as f:
        json.dump(newIndex, f, separators=(",", ":"))
    report(f"built {build} in {time.time() - started:.0f}s")
