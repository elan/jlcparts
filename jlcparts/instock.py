"""
Walk every in-stock part through the JLCPCB website's parts search, in bulk.

The OpenAPI crawl covers the whole catalog (~7M parts) but reaches each one only every few
months, and it lacks the Preferred flag, attrition and minimum order. The website's list endpoint
returns all of that, parameters and price tiers included, 1000 in-stock parts per request, so a
complete in-stock pass takes ~700 requests. Running it every night keeps stock, price and tier
current for every part worth publishing.
"""

import math
import time
from concurrent.futures import ThreadPoolExecutor

import requests

API = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"
PAGE_SIZE = 1000
PAGE_CAP = 100          # the endpoint returns nothing beyond page 100
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class IncompleteWalk(RuntimeError):
    pass


def _body(**extra):
    body = {"currentPage": 1, "pageSize": 1, "keyword": None, "componentLibraryType": None,
            "stockFlag": True, "stockSort": None, "searchSource": "search", "searchType": 3,
            "sortASC": "", "sortMode": "", "componentBrandList": [], "componentSpecificationList": [],
            "componentAttributeList": [], "paramList": [], "startStockNumber": None}
    body.update(extra)
    return body


def _call(session, body, attempts=5):
    """POST with backoff. The endpoint occasionally 502s or throttles; a failed page must not lose a run."""
    for attempt in range(attempts):
        try:
            resp = session.post(API, json=body, timeout=90)
            if resp.status_code == 200:
                data = resp.json().get("data")
                if data is not None:
                    return data
        except (requests.RequestException, ValueError):
            pass
        if attempt < attempts - 1:
            time.sleep(5 * 2 ** attempt)
    raise IncompleteWalk(f"page failed after {attempts} attempts: {body.get('firstSortName')}/"
                         f"{body.get('secondSortName')} p{body.get('currentPage')}")


def partitions(session):
    """The category facet, split so every partition fits inside the 100-page window.

    Returns [(family, subcategory|None, expected_count)]. The facet's counts sum exactly to the
    in-stock total, which is what makes this an exhaustive partition rather than a guess.
    """
    facets = _call(session, _body(searchType=1)).get("sortAndCountVoList") or []
    if not facets:
        raise IncompleteWalk("no category facet returned; the API shape has changed")
    out = []
    for f in facets:
        count = f.get("componentCount") or 0
        if count <= PAGE_SIZE * PAGE_CAP:
            out.append((f["sortName"], None, count))
            continue
        children = [c for c in (f.get("childSortList") or []) if isinstance(c, dict)]
        covered = sum(c.get("componentCount") or 0 for c in children)
        if covered < count:
            raise IncompleteWalk(f"{f['sortName']}: children cover {covered} of {count}")
        for c in children:
            out.append((f["sortName"], c.get("sortName"), c.get("componentCount") or 0))
    return out


def walk(session, family, sub, expected, throttle):
    """Page through one partition, returning the raw website rows."""
    extra = {"firstSortName": family}
    if sub:
        extra["secondSortName"] = sub
    pages = max(1, math.ceil(expected / PAGE_SIZE))
    if pages > PAGE_CAP:
        raise IncompleteWalk(f"{family}/{sub}: {expected} parts needs {pages} pages, over the cap")
    rows = []
    for page in range(1, pages + 1):
        data = _call(session, _body(currentPage=page, pageSize=PAGE_SIZE, **extra))
        got = (data.get("componentPageInfo") or {}).get("list") or []
        if not got:
            break
        rows += got
        time.sleep(throttle)
    return rows


def toPayload(row, family, joints=None):
    """A website row in the OpenAPI detail shape, so both sources share one import path.

    The row's own sort names are the reverse of the request's: `firstSortName` is the
    subcategory and `secondSortName` the family.
    """
    return {
        "componentCode": row.get("componentCode"),
        "firstTypeName": row.get("secondSortName") or family,
        "secondTypeName": row.get("firstSortName") or row.get("componentTypeEn") or "",
        "componentModel": row.get("componentModelEn") or "",
        "componentSpecification": row.get("componentSpecificationEn") or "",
        "solderJointCount": joints,
        "manufacturer": row.get("componentBrandEn") or "",
        "libraryType": row.get("componentLibraryType"),
        "description": row.get("describe") or "",
        "dataManualUrl": row.get("dataManualUrl"),
        "dataManualOfficialLink": row.get("dataManualOfficialLink"),
        "stockCount": row.get("stockCount") or 0,
        "priceRanges": [
            {"startQuantity": p.get("startNumber"), "endQuantity": p.get("endNumber"),
             "unitPrice": p.get("productPrice")}
            for p in (row.get("componentPrices") or [])
        ],
        "parameters": [
            {"parameterName": a.get("attribute_name_en"), "parameterValue": a.get("attribute_value_name")}
            for a in (row.get("attributes") or [])
        ],
        "rohsFlag": row.get("rohsFlag"),
        "assemblyComponentFlag": row.get("assemblyComponentFlag"),
        "lossNumber": row.get("lossNumber"),
        "leastPatchNumber": row.get("leastPatchNumber"),
        "minPurchaseNum": row.get("minPurchaseNum"),
    }


def fetchInStock(lib, workers=2, throttle=0.3, officialDetails=None, minCoverage=0.98,
                 report=print):
    """Refresh every in-stock part in `lib` from the website, then mark the rest out of stock.

    `officialDetails(codes) -> [payload]` fills in joint counts and ECCN for parts the database
    has never seen; without it they're stored with zero joints until the OpenAPI crawl reaches
    them. Raises IncompleteWalk, before touching stock of unseen parts, if the walk covered less
    than `minCoverage` of what JLC reports in stock.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Content-Type": "application/json"})
    started = time.time()
    stockedBefore = lib.conn.execute("SELECT COUNT(*) FROM jlc_components WHERE stock != 0").fetchone()[0]
    plan = partitions(session)
    expected = sum(p[2] for p in plan)
    report(f"{len(plan)} partitions, {expected:,} in-stock parts expected")
    # Coverage is judged against JLC's own count, so that count has to be believable too: a
    # truncated facet would otherwise pass the check and zero the stock of everything it left out.
    if expected < stockedBefore * 0.5:
        raise IncompleteWalk(f"JLC reports {expected:,} in stock but the database holds "
                             f"{stockedBefore:,}; refusing to trust the category facet")

    seen, preferred, walked = set(), set(), 0

    def run(p):
        family, sub, count = p
        return p, walk(session, family, sub, count, throttle)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (family, sub, count), rows in pool.map(run, plan):
            walked += len(rows)
            rows = [r for r in rows if r.get("componentCode") and r["componentCode"] not in seen]
            codes = [r["componentCode"] for r in rows]
            joints = lib.solderJoints(codes)
            new = [c for c in codes if int(c[1:]) not in joints]
            if new and officialDetails is not None:
                try:
                    with lib.startTransaction():
                        for payload in officialDetails(new):
                            lib.updateJlcPayload(payload)
                    joints = lib.solderJoints(codes)
                except Exception as e:
                    report(f"  official details for {len(new)} new parts failed: {e}")
            with lib.startTransaction():
                for r in rows:
                    code = r["componentCode"]
                    lib.updateJlcPayload(toPayload(r, family, joints.get(int(code[1:]))))
                    if r.get("preferredComponentFlag"):
                        preferred.add(code)
            seen.update(codes)
            report(f"  {family}/{sub or '*'}: {len(rows)} parts, {len(new)} new (JLC says {count})")

    if walked < expected * minCoverage:
        raise IncompleteWalk(f"walked only {walked:,} of {expected:,} in-stock parts")
    zeroed = lib.zeroStockExcept(seen)
    lib.setPreferred(preferred)
    report(f"walked {walked:,} in-stock parts ({len(seen):,} distinct, {len(preferred):,} preferred), "
           f"{zeroed:,} marked out of stock, {time.time() - started:.0f}s")
    return len(seen)
