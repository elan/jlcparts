#!/usr/bin/env bash
# Publish a buildkiln output directory: the new snapshot first (immutable, cached forever), then
# index.json (never cached), then remove files the new index no longer names. A client therefore
# never reads an index naming a file that isn't there yet; one that read the old index just before
# its snapshot was removed gets a 404 and picks up the new one on its next refresh.
#
# usage: publish_catalog.sh OUT_DIR s3://BUCKET/PREFIX
# Needs the AWS CLI configured for R2 (AWS_ENDPOINT_URL, keys, AWS_DEFAULT_REGION=auto).
set -euo pipefail

out=${1:?output dir}
dest=${2:?s3 destination}
dest=${dest%/}

for f in "$out"/snapshot-*.jsonl.gz; do
    [ -e "$f" ] || continue
    aws s3 cp --no-progress "$f" "$dest/$(basename "$f")" \
        --content-type application/gzip --cache-control "public, max-age=31536000, immutable"
done
aws s3 cp --no-progress "$out/index.json" "$dest/index.json" \
    --content-type application/json --cache-control no-cache

keep=$(python3 -c 'import json,sys; i=json.load(open(sys.argv[1])); print("\n".join([i["snapshot"]["file"]] + [d["file"] for d in i["deltas"]] + ["index.json"]))' "$out/index.json")
aws s3 ls "$dest/" | awk '{print $4}' | while read -r name; do
    [ -n "$name" ] || continue
    grep -qxF "$name" <<< "$keep" || aws s3 rm --quiet "$dest/$name"
done
echo "published $(python3 -c 'import json,sys; i=json.load(open(sys.argv[1])); print(i["build"], i["parts"], "parts,", len(i["deltas"]), "change files")' "$out/index.json")"
