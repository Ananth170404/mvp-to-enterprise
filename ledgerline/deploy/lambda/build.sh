#!/bin/sh
# Build the Lambda package: dependencies for Linux x86_64 / Python 3.12, the
# app code, and run.sh. Runs inside a Linux container so it works the same
# from Windows, macOS or Linux:
#
#   docker run --rm -v "${PWD}:/src" -w /src python:3.12-slim sh -c "sed 's/\r$//' deploy/lambda/build.sh | sh"
#
# (sed strips Windows line endings, in case git converted them.)
set -eu
out=build/lambda
rm -rf "$out"
mkdir -p "$out"
pip install --quiet --no-cache-dir --root-user-action=ignore -r requirements.txt -t "$out" \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
  --only-binary=:all: --python-version 3.12 --implementation cp
cp -r app "$out/"
sed 's/\r$//' deploy/lambda/run.sh > "$out/run.sh"   # Lambda can't run a script with Windows line endings
chmod 755 "$out/run.sh"
find "$out" -name "__pycache__" -type d -prune -exec rm -rf {} +

# Zip here, inside Linux, so run.sh keeps its "executable" permission. (Zipping
# on Windows, e.g. with Terraform's archive_file, loses it, and Lambda then
# fails with "permission denied".)
python - "$out" build/ledgerline-lambda.zip <<'PY'
import os, sys, zipfile
src, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        for name in sorted(files):
            path = os.path.join(root, name)
            z.write(path, os.path.relpath(path, src))  # keeps file modes
PY
echo "Built build/ledgerline-lambda.zip ($(du -h build/ledgerline-lambda.zip | cut -f1); unzipped $(du -sh "$out" | cut -f1))"
