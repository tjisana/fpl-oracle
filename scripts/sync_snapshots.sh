#!/usr/bin/env bash
# Pull the cloud price snapshots (written daily by the Lambda in infra/)
# down into data/snapshots/. Copies ONLY dates missing locally: a day both
# jobs wrote (laptop open at 3 AM) keeps the local file, since the two are
# the same prices with different `taken_at` stamps. Never deletes, never
# overwrites, in either direction. Needs an `aws login` session.
set -eu
cd /Users/tjisanakerr/Dev/fpl-oracle

BUCKET=$(aws cloudformation describe-stacks --stack-name FplOracle \
  --query "Stacks[0].Outputs[?OutputKey=='DataBucketName'].OutputValue" --output text)
if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "sync_snapshots: could not read the DataBucketName output of stack FplOracle (logged in? deployed?)" >&2
  exit 1
fi

mkdir -p data/snapshots
copied=0
for key in $(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix snapshots/ \
              --query "Contents[].Key" --output text); do
  name=$(basename "$key")
  case "$name" in *.json) ;; *) continue ;; esac
  if [ ! -f "data/snapshots/$name" ]; then
    aws s3 cp "s3://$BUCKET/$key" "data/snapshots/$name" >/dev/null
    echo "sync_snapshots: pulled $name"
    copied=$((copied + 1))
  fi
done
echo "sync_snapshots: $copied new; data/snapshots/ holds $(ls data/snapshots/*.json | wc -l | tr -d ' ') files"
