#!/bin/sh
# Полная проверка: сборка zip, установка в чистый временный профиль Blender,
# тест аддона и тест сетевого рендера (два узла на портах 19410/19411).
set -e
cd "$(dirname "$0")/.."
BLENDER="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
./build.sh >/dev/null
ZIP=$(ls dist/corona_dr-*.zip | tail -1)
PROFILE=$(mktemp -d /tmp/corona_dr_profile.XXXX)
export BLENDER_USER_RESOURCES="$PROFILE"
echo "== установка $ZIP в чистый профиль $PROFILE"
"$BLENDER" --factory-startup --command extension install-file -r user_default -e "$ZIP"
echo "== тест аддона"
"$BLENDER" -b --python tests/test_addon.py 2>&1 | grep -E "^  (OK|FAIL)|TEST_|Traceback" || true
unset BLENDER_USER_RESOURCES
echo "== тест сетевого рендера"
"$BLENDER" -b --factory-startup --python tests/test_dr.py 2>&1 | grep -E "^  (OK|FAIL)|TEST_|найдено|разниц|скачок" || true
rm -rf "$PROFILE"
