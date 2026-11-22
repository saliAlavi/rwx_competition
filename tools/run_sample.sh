#!/bin/sh
# Build and run the image on the sample under the scorer's flags, then score.
# Usage: tools/run_sample.sh [image] [data_dir]
set -e
IMAGE=${1:-br-dev}
D=${2:-/tmp/br_data/sample}
docker run --rm --network none --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges \
  --user 65534:65534 --env HOME=/tmp \
  -v $D/commits:/data/commits:ro -v $D/tests.json:/data/tests.json:ro \
  $IMAGE > /tmp/br_data/output.jsonl 2> /tmp/br_data/stderr.log
python3 score.py $D $D/answers.json /tmp/br_data/output.jsonl
