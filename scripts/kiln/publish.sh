#!/usr/bin/env bash
# Publish the files Kiln reads (manifest, attribute table, browse shards) from a buildtables
# output directory to R2. Shards go first and the manifest last, so a client never reads a
# manifest that names a shard not yet uploaded; shards the new manifest dropped go after it.
#
# usage: publish.sh DATA_DIR s3://BUCKET/PREFIX
# Needs the AWS CLI configured for R2 (AWS_ENDPOINT_URL, keys, AWS_DEFAULT_REGION=auto).
set -euo pipefail

data=${1:?data dir}
dest=${2:?s3 destination}
dest=${dest%/}

# Shard names are stable per category but their contents change nightly with stock and price,
# so nothing here may be cached as immutable. Served as opaque gzip: no Content-Encoding, or
# clients would inflate them before Kiln's own gunzip sees the bytes.
common=(--no-progress --cache-control no-cache)

aws s3 sync "$data" "$dest" "${common[@]}" --content-type application/gzip \
    --exclude '*' --include 'browse-*.jsonl.gz' --include 'attributes-lut.json.gz'
aws s3 cp "$data/manifest.json" "$dest/manifest.json" "${common[@]}" --content-type application/json
aws s3 sync "$data" "$dest" "${common[@]}" --content-type application/gzip \
    --exclude '*' --include 'browse-*.jsonl.gz' --delete

shards=("$data"/browse-*.jsonl.gz)
echo "published ${#shards[@]} shards to $dest"
