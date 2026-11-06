#!/bin/sh
# Сборка установочного zip аддона (Blender extension) в dist/
set -e
cd "$(dirname "$0")"
BLENDER="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
find corona_dr -name __pycache__ -type d -exec rm -rf {} +
mkdir -p dist
"$BLENDER" --command extension validate corona_dr
"$BLENDER" --command extension build --source-dir corona_dr --output-dir dist
ls -la dist
