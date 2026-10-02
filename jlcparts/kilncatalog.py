"""
The catalog as Kiln consumes it: one snapshot, rebuilt every night, named by index.json.

The snapshot is a gzipped JSON-lines op stream. The first line is a header; every later line is
one operation:

    ["c", id, category, subcategory]
    ["a", id, name, value]          attribute table entry
    ["u", [field values...]]        a part, fields in header["fields"] order

IDs are only meaningful within one snapshot: Kiln loads each snapshot into a fresh database. The
header's "kind" leaves room for change files ("delta") should a later format want them.
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

FLOOR = 100_000             # no real build comes anywhere near this
MIN_RATIO = 0.9             # a build under this share of the published catalog is refused


class BuildRefused(RuntimeError):
    pass


class Registry:
    """Assigns attribute and category IDs, in first-seen order."""

    def __init__(self):
        self.attrs, self.cats = {}, {}      # key -> id

    def attrId(self, name, value):
        return self.attrs.setdefault(json.dumps([name, value], separators=(",", ":"), sort_keys=True,
                                                ensure_ascii=False), len(self.attrs))

    def catId(self, category, subcategory):
        return self.cats.setdefault((category, subcategory), len(self.cats) + 1)


def _tier(component):
    if component.get("basic"):
        return "basic"
    return "preferred" if component.get("preferred") else "extended"


def currentRows(lib, registry, ignoreOldStock, report):
    """Every publishable part as a row, keyed by LCSC code."""
    lib.prepareBuild()
    rows = {}
    for category, subcategories in sorted(lib.categories().items()):
        for subcategory in sorted(subcategories):
            if not _isUsableCategory(category, subcategory):
                continue
            canonical = normalize_category_pair(category, subcategory)
            before = len(rows)
            for component in lib.iterCategoryComponents(category, subcategory,
                                                        stockNewerThan=ignoreOldStock, fetchSize=5000):
                values = dict(zip(COMPONENT_SOURCE_SCHEMA, extractComponent(component, COMPONENT_SOURCE_SCHEMA)))
                extra = component.get("jlc_extra") or {}
                attrition = extra.get("attrition") or {}
                rows[component["lcsc"]] = [
                    component["lcsc"], registry.catId(canonical.category, canonical.subcategory),
                    values["mfr"] or "", component.get("manufacturer") or "", component.get("package") or "",
                    _tier(component), values["joints"] or 0, values["description"] or "", values["img"],
                    sorted(registry.attrId(name, value) for name, value in values["attributes"].items()),
                    values["stock"] or 0,
                    [[t.get("qFrom"), t.get("qTo"), t.get("price")] for t in (values["price"] or [])],
                    [attrition.get("lossNumber"), attrition.get("leastPatchNumber"), attrition.get("minPurchaseNum")],
                    extra.get("assemblyProcess"),
                ]
            if len(rows) > before:
                report(f"  {canonical.category} / {canonical.subcategory}: {len(rows):,} parts so far")
    return rows


def snapshotOps(rows, registry):
    """The snapshot's operations: categories, then attributes, then parts (only entries in use)."""
    usedCats = {r[F["category"]] for r in rows.values()}
    usedAttrs = {a for r in rows.values() for a in r[F["attributes"]]}
    cats = sorted((i, c, s) for (c, s), i in registry.cats.items() if i in usedCats)
    attrs = sorted((i, *json.loads(k)) for k, i in registry.attrs.items() if i in usedAttrs)
    return ([["c", *c] for c in cats] + [["a", *a] for a in attrs]
            + [["u", rows[k]] for k in sorted(rows)])


def writeSnapshot(path, build, ops):
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        header = {"format": FORMAT, "kind": "snapshot", "build": build, "fields": FIELDS}
        f.write(json.dumps(header, separators=(",", ":")) + "\n")
        for op in ops:
            f.write(json.dumps(op, separators=(",", ":"), ensure_ascii=False) + "\n")
    with open(path, "rb") as f:
        return {"sha256": hashlib.sha256(f.read()).hexdigest(), "bytes": os.path.getsize(path)}


def check(previousParts, newParts, force, report):
    problems = []
    if newParts < FLOOR:
        problems.append(f"{newParts:,} parts is below the {FLOOR:,} floor")
    if previousParts and newParts < previousParts * MIN_RATIO:
        problems.append(f"{newParts:,} parts is under {MIN_RATIO:.0%} of the {previousParts:,} published")
    if problems:
        if not force:
            raise BuildRefused("; ".join(problems))
        report("building anyway (forced): " + "; ".join(problems))


@click.command("buildkiln")
@click.argument("db", type=click.Path(dir_okay=False, exists=True))
@click.argument("outdir", type=click.Path(file_okay=False))
@click.option("--previous", type=click.Path(dir_okay=False), default=None,
    help="The index.json being served, for the size check")
@click.option("--ignoreoldstock", type=int, default=120,
    help="Leave out parts that haven't been in stock for this many days")
@click.option("--force", is_flag=True, help="Publish even if the size check fails")
def buildKiln(db, outdir, previous, ignoreoldstock, force):
    """
    Write Kiln's catalog for DB into OUTDIR: a snapshot and the index.json naming it.
    """
    started = time.time()
    report = lambda s: print(s, flush=True)
    previousParts = 0
    if previous and os.path.exists(previous):
        with open(previous) as f:
            previousParts = json.load(f)["parts"]
        report(f"published: {previousParts:,} parts")

    registry = Registry()
    rows = currentRows(SourceDb(db, create=False), registry, ignoreoldstock, report)
    check(previousParts, len(rows), force, report)

    build = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    os.makedirs(outdir, exist_ok=True)
    name = f"snapshot-{build}.jsonl.gz"
    meta = writeSnapshot(os.path.join(outdir, name), build, snapshotOps(rows, registry))

    counts = {}
    for row in rows.values():
        counts[row[F["category"]]] = counts.get(row[F["category"]], 0) + 1
    index = {
        "format": FORMAT, "build": build, "parts": len(rows),
        "snapshot": {"file": name, "build": build, "parts": len(rows), **meta},
        "deltas": [],
        "categories": [{"id": i, "category": c, "subcategory": s, "count": counts[i]}
                       for (c, s), i in sorted(registry.cats.items(), key=lambda kv: kv[1]) if i in counts],
    }
    with open(os.path.join(outdir, "index.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))
    report(f"snapshot {name}: {len(rows):,} parts, {meta['bytes'] / 1e6:.1f} MB; built in {time.time() - started:.0f}s")
