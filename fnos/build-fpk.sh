#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
STAGE_DIR="${SCRIPT_DIR}/app/server"
MANIFEST_FILE="${SCRIPT_DIR}/manifest"
APP_VERSION=$(awk -F '=' '/^version[[:space:]]*=/ { gsub(/[[:space:]]/, "", $2); print $2; exit }' "${MANIFEST_FILE}")

command -v fnpack >/dev/null 2>&1 || {
    echo "缺少 fnpack。请先安装飞牛官方 fnpack 1.2.3 或更高版本。" >&2
    exit 1
}
command -v uv >/dev/null 2>&1 || {
    echo "缺少 uv，无法准备 Linux x86_64 原生依赖。" >&2
    exit 1
}

# fnOS 1.2.0800 的应用中心会在读取空向导数组时触发 10111
# （nil pointer dereference），并在 UI 中误报“应用包不符合系统要求”。
for wizard_file in "${SCRIPT_DIR}"/wizard/*; do
    [ -f "${wizard_file}" ] || continue
    wizard_compact=$(tr -d '[:space:]' <"${wizard_file}")
    if [ "${wizard_compact}" = "[]" ]; then
        echo "向导文件不能是空数组，请删除文件或至少提供一个步骤：${wizard_file}" >&2
        exit 1
    fi
done

cleanup() {
    for _attempt in 1 2 3 4 5 6 7 8 9 10; do
        rm -rf "${STAGE_DIR}" 2>/dev/null || true
        [ ! -e "${STAGE_DIR}" ] && break
        sleep 0.2
    done
    [ ! -e "${STAGE_DIR}" ] || echo "警告：临时构建目录未完全清理：${STAGE_DIR}" >&2
}
trap cleanup EXIT

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"
cp "${REPO_DIR}/README.md" \
   "${REPO_DIR}/main.py" "${STAGE_DIR}/"
cp -R "${REPO_DIR}/sequoia_x" "${STAGE_DIR}/sequoia_x"
cp "${SCRIPT_DIR}/requirements-native.txt" "${STAGE_DIR}/"

echo "准备 Linux x86_64 / Python 3.12 原生依赖..."
UV_HTTP_TIMEOUT=300 uv pip install \
    --target "${STAGE_DIR}/vendor" \
    --python-platform x86_64-manylinux_2_28 \
    --python-version 3.12 \
    -r "${SCRIPT_DIR}/requirements-native.txt"

rm -rf "${STAGE_DIR}/vendor/bin"
find "${STAGE_DIR}" -name '.DS_Store' -o -name '__pycache__' | while read -r path; do
    rm -rf "$path"
done

(cd "${SCRIPT_DIR}" && fnpack build)
mv "${SCRIPT_DIR}/fn-sequoia-x.fpk" \
   "${SCRIPT_DIR}/fn-sequoia-x_${APP_VERSION}_native_x86.fpk"
echo "FPK 构建完成：${SCRIPT_DIR}/fn-sequoia-x_${APP_VERSION}_native_x86.fpk"
