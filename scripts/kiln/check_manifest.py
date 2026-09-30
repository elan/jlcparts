#!/usr/bin/env python3
"""
Refuse to publish a build that has lost a large share of the catalog.

Upstream once published a build missing ~94% of its parts after its nightly job lost the cache
and republished only what it had re-fetched. This compares the new manifest with the one being
served and exits non-zero if the new one is implausibly small, or references files the build
didn't produce.

usage: check_manifest.py NEW_MANIFEST PUBLISHED_MANIFEST [--force]

PUBLISHED_MANIFEST is a local copy of the manifest being served; a missing file means nothing
has been published yet, and only the floor applies.
"""

import json
import os
import sys

FLOOR = 100_000        # no real build comes anywhere near this; the in-stock catalog is ~700k
MIN_RATIO = 0.9        # stricter than Kiln's own client-side check, which allows down to half


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    if len(args) != 2:
        sys.exit(__doc__)
    path, publishedPath = args
    new = load(path)
    outdir = os.path.dirname(path)

    wanted = {new["attributesLut"]}
    for c in new["categories"]:
        wanted.update(c.get("browseShards") or [])
    missing = sorted(f for f in wanted if not os.path.exists(os.path.join(outdir, f)))
    if missing:
        sys.exit(f"manifest references {len(missing)} files the build didn't write, e.g. {missing[:3]}")

    total = new["totalComponents"]
    old = load(publishedPath) if os.path.exists(publishedPath) else None
    previous = old["totalComponents"] if old else None
    print(f"new build: {total:,} parts in {len(new['categories'])} categories; "
          f"published: {'none' if previous is None else f'{previous:,}'}")

    problems = []
    if total < FLOOR:
        problems.append(f"{total:,} parts is below the {FLOOR:,} floor")
    if previous and total < previous * MIN_RATIO:
        problems.append(f"{total:,} parts is under {MIN_RATIO:.0%} of the {previous:,} published")
    if problems:
        if force:
            print("publishing anyway (forced): " + "; ".join(problems))
            return
        sys.exit("refusing to publish: " + "; ".join(problems))


if __name__ == "__main__":
    main()
