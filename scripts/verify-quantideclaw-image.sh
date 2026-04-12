#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
# shellcheck source=./_remote_phase_lib.sh
source "${SCRIPT_DIR}/_remote_phase_lib.sh"

BUILD_USER="${BUILD_USER:-quantide}"
VERIFY_WEIXIN_LOGIN="${VERIFY_WEIXIN_LOGIN:-1}"

run_remote_smoke_test() {
    phase_log_info "Running remote QuantideClaw smoke test"

    phase_ssh "env BUILD_USER=$(phase_shell_quote "${BUILD_USER}") VERIFY_WEIXIN_LOGIN=$(phase_shell_quote "${VERIFY_WEIXIN_LOGIN}") bash -s" <<'EOF'
set -euo pipefail

BUILD_USER="${BUILD_USER:-quantide}"
VERIFY_WEIXIN_LOGIN="${VERIFY_WEIXIN_LOGIN:-1}"
STATE_DIR="/home/${BUILD_USER}/.openclaw"
ASSET_DIR="/opt/quantideclaw-onboard/assets"
INSTALLER_ENV="/opt/quantideclaw-onboard/installer.env"
WRAPPER="/usr/local/bin/quantideclaw-openclaw"
SESSION_START="/usr/local/bin/quantideclaw-session-start"
VERIFY_CONFIG="$(mktemp /tmp/quantideclaw-verify-openclaw.XXXXXX.json)"
WEIXIN_LOGIN_OUTPUT="/tmp/quantideclaw-verify-weixin-login.out"
PLUGINS_OUTPUT="/tmp/quantideclaw-verify-plugins.out"

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

pass() {
    echo "[PASS] $*"
}

require_file() {
    local path="$1"
    [[ -e "${path}" ]] || fail "Missing required file: ${path}"
}

require_contains() {
    local path="$1"
    local pattern="$2"
    grep -q -- "${pattern}" "${path}" || fail "Expected '${pattern}' in ${path}"
}

require_file "/opt/quantideclaw-onboard/onboard.py"
require_file "${INSTALLER_ENV}"
require_file "${WRAPPER}"
require_file "${SESSION_START}"
require_file "/etc/xdg/autostart/quantideclaw-onboard.desktop"
require_file "/etc/xdg/autostart/quantideclaw-session-start.desktop"
require_file "/home/${BUILD_USER}/.config/autostart/quantideclaw-onboard.desktop"
require_file "/home/${BUILD_USER}/.config/autostart/quantideclaw-session-start.desktop"
require_file "${ASSET_DIR}/tencent-weixin-openclaw-weixin-2.1.7.tgz"
require_file "${ASSET_DIR}/tencent-connect-openclaw-qqbot-1.7.1.tgz"
pass "Onboard files and plugin packages are present"

require_contains "${INSTALLER_ENV}" "OPENCLAW_STATE_DIR=${STATE_DIR}"
if grep -q '^OPENCLAW_HOME=' "${INSTALLER_ENV}"; then
    fail "installer.env still exports OPENCLAW_HOME"
fi
require_contains "${WRAPPER}" 'OPENCLAW_STATE_DIR'
require_contains "/etc/xdg/autostart/quantideclaw-onboard.desktop" 'Exec=/usr/local/bin/quantideclaw-onboard-launch'
require_contains "/etc/xdg/autostart/quantideclaw-session-start.desktop" 'Exec=/usr/local/bin/quantideclaw-session-start'
pass "State directory wiring uses OPENCLAW_STATE_DIR"

if [[ -e "${STATE_DIR}/.openclaw" ]]; then
    fail "Nested state directory exists: ${STATE_DIR}/.openclaw"
fi
pass "Nested .openclaw directory is absent"

runuser -u "${BUILD_USER}" -- env \
    OPENCLAW_STATE_DIR="${STATE_DIR}" \
    OPENCLAW_ASSETS_DIR="${ASSET_DIR}" \
    "${WRAPPER}" plugins list >"${PLUGINS_OUTPUT}" 2>&1

grep -q 'openclaw-weixin' "${PLUGINS_OUTPUT}" || fail "Weixin plugin is not discoverable"
grep -Eq 'openclaw-qqbot|\bqqbot\b' "${PLUGINS_OUTPUT}" || fail "QQ plugin/channel is not discoverable"
pass "OpenClaw can discover Weixin and QQ plugins"

runuser -u "${BUILD_USER}" -- "${SESSION_START}" --restart-gateway >/tmp/quantideclaw-session-start.out 2>&1 || true
sleep 3
pgrep -u "${BUILD_USER}" -f 'openclaw-gateway|openclaw gateway' >/dev/null || fail "Gateway was not started by quantideclaw-session-start"
pass "Session-start helper launches the gateway"

if [[ "${VERIFY_WEIXIN_LOGIN}" == "1" ]]; then
    cat >"${VERIFY_CONFIG}" <<JSON
{
  "plugins": {
    "allow": [
      "openclaw-weixin",
      "openclaw-qqbot",
      "qqbot"
    ]
  }
}
JSON
    chown "${BUILD_USER}:${BUILD_USER}" "${VERIFY_CONFIG}"

    set +e
    runuser -u "${BUILD_USER}" -- env \
        OPENCLAW_STATE_DIR="${STATE_DIR}" \
        OPENCLAW_ASSETS_DIR="${ASSET_DIR}" \
        OPENCLAW_CONFIG_PATH="${VERIFY_CONFIG}" \
        timeout 25s script -qefc "${WRAPPER} channels login --channel openclaw-weixin" /dev/null >"${WEIXIN_LOGIN_OUTPUT}" 2>&1
    status=$?
    set -e

    if [[ "${status}" -ne 0 && "${status}" -ne 124 ]]; then
        sed -n '1,200p' "${WEIXIN_LOGIN_OUTPUT}" >&2 || true
        fail "Weixin login smoke test failed with exit code ${status}"
    fi

    if ! grep -Eq 'https://liteapp\.weixin\.qq\.com/q/|qrcode=' "${WEIXIN_LOGIN_OUTPUT}"; then
        sed -n '1,200p' "${WEIXIN_LOGIN_OUTPUT}" >&2 || true
        fail "Weixin login smoke test did not produce a QR URL"
    fi
    pass "Weixin login smoke test produced a QR URL"
fi

echo "[PASS] QuantideClaw stage-2 smoke test completed"
EOF
}

main() {
    phase_require_host_tools
    phase_collect_ssh_inputs
    phase_test_connection
    run_remote_smoke_test
}

main "$@"