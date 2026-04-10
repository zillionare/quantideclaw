#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
# shellcheck source=./_remote_phase_lib.sh
source "${SCRIPT_DIR}/_remote_phase_lib.sh"

BUILD_USER="${BUILD_USER:-quantide}"
BUILD_USER_PASSWORD="${BUILD_USER_PASSWORD:-quantide}"
TARGET_ARCH="${TARGET_ARCH:-arm64}"
RUN_CLEANUP="${RUN_CLEANUP:-true}"
REMOTE_STAGE_DIR="${REMOTE_STAGE_DIR:-/root/quantideclaw-stage2}"
ONBOARD_HOME="/opt/quantideclaw-onboard"
ONBOARD_ASSET_DIR="${ONBOARD_HOME}/assets"
INSTALLER_ENV="${ONBOARD_HOME}/installer.env"
MARKER_DIR="/var/lib/quantideclaw-onboard"
STATUS_DIR="/var/lib/quantideclaw-build"
STATUS_FILE="${STATUS_DIR}/browser-status.txt"
OPENCLAW_WRAPPER="/usr/local/bin/quantideclaw-openclaw"

log_info() {
    echo "[INFO] $(date '+%Y-%m-%d %H:%M:%S') - $*"
}

log_warn() {
    echo "[WARN] $(date '+%Y-%m-%d %H:%M:%S') - $*" >&2
}

log_error() {
    echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') - $*" >&2
}

require_root() {
    if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
        log_error "This script must be run as root."
        exit 1
    fi
}

find_payload_file() {
    local name="$1"
    local candidate

    for candidate in \
        "${SCRIPT_DIR}/../guest/assets/${name}" \
        "${SCRIPT_DIR}/assets/${name}" \
        "${SCRIPT_DIR}/../assets/${name}" \
        "$(pwd)/guest/assets/${name}" \
        "$(pwd)/assets/${name}" \
        "/root/openclaw-image/guest/assets/${name}" \
        "/root/assets/${name}"; do
        if [[ -f "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    return 1
}

copy_payload() {
    local name="$1"
    local destination="$2"
    local source

    source="$(find_payload_file "${name}")"
    install -D -m 0644 "${source}" "${destination}"
}

copy_payload_executable() {
    local name="$1"
    local destination="$2"
    local source

    source="$(find_payload_file "${name}")"
    install -D -m 0755 "${source}" "${destination}"
}

require_payload() {
    local name="$1"

    if ! find_payload_file "${name}" >/dev/null; then
        log_error "Missing required payload file: ${name}"
        exit 1
    fi
}

ensure_payload_files() {
    require_payload onboard.py
    require_payload edge_tts_proxy.py
    require_payload openrouter.jpg
    require_payload quantfans.png
}

version_ge() {
    dpkg --compare-versions "$1" ge "$2"
}

current_node_version() {
    node --version 2>/dev/null | sed 's/^v//'
}

ensure_build_user() {
    log_info "Ensuring application user '${BUILD_USER}' exists..."

    if ! id -u "${BUILD_USER}" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "${BUILD_USER}"
        echo "${BUILD_USER}:${BUILD_USER_PASSWORD}" | chpasswd
    fi

    usermod -aG sudo,audio,video,plugdev,netdev "${BUILD_USER}" || true
}

configure_cn_runtime_mirrors() {
    log_info "Configuring npm/pip/bun mirrors..."

    cat >/etc/npmrc <<'EOF'
registry=https://registry.npmmirror.com/
fund=false
audit=false
EOF

    cat >/etc/pip.conf <<'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
timeout = 120
EOF

    cat >/etc/bunfig.toml <<'EOF'
[install]
registry = "https://registry.npmmirror.com"
EOF
}

configure_python() {
    log_info "Installing Python runtime dependencies..."

    apt-get update
    apt-get install -y --no-install-recommends python3 python3-pip python3-tk python3-pil python3-pil.imagetk tk

    ln -sf "$(command -v python3)" /usr/local/bin/python
    ln -sf "$(command -v pip3)" /usr/local/bin/pip

    python3 -m pip install --break-system-packages --no-cache-dir edge-tts qrcode[pil]
}

ensure_corepack() {
    if ! command -v npm >/dev/null 2>&1; then
        log_error "npm is not available after Node.js installation."
        exit 1
    fi

    npm config set registry https://registry.npmmirror.com/ --global

    if ! command -v corepack >/dev/null 2>&1; then
        npm install -g corepack
    fi

    corepack enable
}

install_nodejs() {
    local required_version="22.14.0"
    local installed_version=""

    log_info "Installing Node.js..."
    apt-get install -y --no-install-recommends nodejs npm || true

    if command -v node >/dev/null 2>&1; then
        installed_version="$(current_node_version)"
        if [[ -n "${installed_version}" ]] && version_ge "${installed_version}" "${required_version}"; then
            log_info "Using Debian Node.js v${installed_version}"
            ensure_corepack
            return 0
        fi
        log_info "Debian Node.js is insufficient, falling back to NodeSource 24.x..."
    fi

    curl --retry 3 --retry-all-errors -fsSL https://deb.nodesource.com/setup_24.x | bash -
    apt-get install -y nodejs
    installed_version="$(current_node_version)"

    if [[ -z "${installed_version}" ]] || ! version_ge "${installed_version}" "${required_version}"; then
        log_error "Installed Node.js version is '${installed_version:-missing}', but >= ${required_version} is required."
        exit 1
    fi

    ensure_corepack
}

install_bun() {
    log_info "Installing Bun..."
    npm install -g bun
}

record_status() {
    install -d -m 0755 "${STATUS_DIR}"
    printf '%s\n' "$*" >>"${STATUS_FILE}"
}

install_chrome_best_effort() {
    local chrome_deb="/tmp/google-chrome-stable.deb"

    install -d -m 0755 "${STATUS_DIR}"
    : >"${STATUS_FILE}"
    if [[ "${TARGET_ARCH}" != "amd64" ]]; then
        record_status "chrome=skipped reason=official_linux_amd64_only"
        return 0
    fi

    if ! curl -fsSL "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" -o "${chrome_deb}"; then
        record_status "chrome=skipped reason=download_failed"
        return 0
    fi

    if ! dpkg -i "${chrome_deb}"; then
        apt-get install -f -y || true
    fi
    rm -f "${chrome_deb}"

    if command -v google-chrome >/dev/null 2>&1; then
        record_status "chrome=installed"
    else
        record_status "chrome=skipped reason=package_unavailable_after_install"
    fi
}

repair_openclaw_runtime_deps() {
    local npm_root
    local openclaw_dir
    local vendored_carbon
    local target_namespace
    local target_link

    npm_root="$(npm root -g)"
    openclaw_dir="${npm_root}/openclaw"
    vendored_carbon="${openclaw_dir}/dist/extensions/discord/node_modules/@buape/carbon"
    target_namespace="${openclaw_dir}/node_modules/@buape"
    target_link="${target_namespace}/carbon"

    if [[ ! -d "${openclaw_dir}" ]]; then
        log_error "OpenClaw global install directory not found: ${openclaw_dir}"
        exit 1
    fi

    if [[ -d "${vendored_carbon}" ]]; then
        install -d -m 0755 "${target_namespace}"
        rm -rf "${target_link}"
        ln -s "${vendored_carbon}" "${target_link}"
        return 0
    fi

    npm install -g @buape/carbon@0.14.0
}

install_openclaw_wrapper() {
    cat >"${OPENCLAW_WRAPPER}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

INSTALLER_ENV=/opt/quantideclaw-onboard/installer.env

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "${value}"
}

strip_matching_quotes() {
    local value="$1"
    local first_char last_char
    if [[ ${#value} -lt 2 ]]; then
        printf '%s' "${value}"
        return
    fi
    first_char="${value:0:1}"
    last_char="${value: -1}"
    if [[ ( "${first_char}" == '"' || "${first_char}" == "'" ) && "${last_char}" == "${first_char}" ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s' "${value}"
}

expand_leading_tilde() {
    local value="$1"
    local home_dir="$2"
    if [[ "${value}" == "~" ]]; then
        printf '%s\n' "${home_dir}"
    elif [[ "${value}" == ~/* ]]; then
        printf '%s/%s\n' "${home_dir}" "${value#~/}"
    else
        printf '%s\n' "${value}"
    fi
}

if [[ -r "${INSTALLER_ENV}" ]]; then
    while IFS='=' read -r raw_key raw_value; do
        if [[ -z "${raw_value+x}" ]]; then
            continue
        fi
        key="$(trim "${raw_key}")"
        [[ -n "${key}" ]] || continue
        [[ "${key:0:1}" == "#" ]] && continue
        value="$(trim "${raw_value}")"
        value="$(strip_matching_quotes "${value}")"
        case "${key}" in
            OPENCLAW_HOME|OPENCLAW_ASSETS_DIR|OPENCLAW_CONFIG_PATH|OPENCLAW_WORKSPACE|WEIXIN_PLUGIN_PACKAGE|WEIXIN_CHANNEL|QQBOT_PLUGIN_PACKAGE|QQBOT_CHANNEL|EDGE_TTS_PROXY_URL|EDGE_TTS_DEFAULT_VOICE|CHROME_STATUS_FILE)
                export "${key}=${value}"
                ;;
        esac
    done <"${INSTALLER_ENV}"
fi

if [[ -z "${HOME:-}" ]]; then
    home_from_passwd="$(getent passwd "$(id -un)" | cut -d: -f6)"
    export HOME="${home_from_passwd:-/root}"
fi

export OPENCLAW_CONFIG_PATH="$(expand_leading_tilde "${OPENCLAW_CONFIG_PATH:-~/.openclaw/openclaw.json}" "${HOME}")"
export OPENCLAW_WORKSPACE="$(expand_leading_tilde "${OPENCLAW_WORKSPACE:-~/.openclaw/workspace}" "${HOME}")"
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

if command -v openclaw >/dev/null 2>&1; then
    exec openclaw "$@"
fi

if command -v npm >/dev/null 2>&1; then
    npm_root="$(npm root -g 2>/dev/null || true)"
    if [[ -n "${npm_root}" ]] && [[ -x "${npm_root}/.bin/openclaw" ]]; then
        exec "${npm_root}/.bin/openclaw" "$@"
    fi
fi

echo "quantideclaw-openclaw: openclaw executable not found" >&2
exit 127
EOF
    chmod 0755 "${OPENCLAW_WRAPPER}"
}

validate_openclaw_runtime() {
    if ! "${OPENCLAW_WRAPPER}" plugins list >/tmp/openclaw-plugins-list.log 2>&1; then
        cat /tmp/openclaw-plugins-list.log >&2
        log_error "OpenClaw CLI failed runtime validation."
        exit 1
    fi
}

install_weixin_plugin_for_user() {
    su - "${BUILD_USER}" -s /bin/bash -c '
        set -euo pipefail
        plugin_root="$HOME/.openclaw"
        plugin_package="${WEIXIN_PLUGIN_PACKAGE:-@tencent-weixin/openclaw-weixin}"
        install_log="$(mktemp)"
        trap "rm -f \"$install_log\"" EXIT
        mkdir -p "$plugin_root/extensions" "$plugin_root/hook-packs"

        for attempt in 1 2 3 4 5; do
            rm -rf "$plugin_root/extensions/openclaw-weixin" "$plugin_root/hook-packs/openclaw-weixin"
            if /usr/local/bin/quantideclaw-openclaw plugins install "$plugin_package" >"$install_log" 2>&1; then
                cat "$install_log"
                exit 0
            fi

            cat "$install_log" >&2
            if grep -Eiq "rate.?limit|too many requests|429" "$install_log" && [[ "$attempt" -lt 5 ]]; then
                sleep_seconds=$((attempt * 15))
                echo "[WARN] Weixin plugin install hit rate limit, retrying in ${sleep_seconds}s..." >&2
                sleep "$sleep_seconds"
                continue
            fi

            exit 1
        done
    '
}

install_qqbot_plugin_for_user() {
    su - "${BUILD_USER}" -s /bin/bash -c '
        set -euo pipefail
        plugin_root="$HOME/.openclaw"
        rm -rf "$plugin_root/extensions/openclaw-qqbot" "$plugin_root/hook-packs/openclaw-qqbot"
        mkdir -p "$plugin_root/extensions" "$plugin_root/hook-packs"
        /usr/local/bin/quantideclaw-openclaw plugins install @tencent-connect/openclaw-qqbot@latest
        /usr/local/bin/quantideclaw-openclaw plugins disable qqbot || true
    '
}

install_openclaw() {
    log_info "Installing OpenClaw..."

    install -d -m 0755 "${ONBOARD_HOME}" "${ONBOARD_ASSET_DIR}" "${STATUS_DIR}"

    cat >"${INSTALLER_ENV}" <<'EOF'
OPENCLAW_HOME=/opt/quantideclaw-onboard
OPENCLAW_ASSETS_DIR=/opt/quantideclaw-onboard/assets
OPENCLAW_CONFIG_PATH=~/.openclaw/openclaw.json
OPENCLAW_WORKSPACE=~/.openclaw/workspace
WEIXIN_PLUGIN_PACKAGE=@tencent-weixin/openclaw-weixin
WEIXIN_CHANNEL=openclaw-weixin
QQBOT_PLUGIN_PACKAGE=@tencent-connect/openclaw-qqbot@latest
QQBOT_CHANNEL=qqbot
EDGE_TTS_PROXY_URL=http://127.0.0.1:18792/v1
EDGE_TTS_DEFAULT_VOICE=zh-CN-XiaoxiaoNeural
CHROME_STATUS_FILE=/var/lib/quantideclaw-build/browser-status.txt
EOF

    npm install -g openclaw
    repair_openclaw_runtime_deps
    install_openclaw_wrapper
    validate_openclaw_runtime
    install_weixin_plugin_for_user
    install_qqbot_plugin_for_user

    cat >"${ONBOARD_HOME}/plugin-status.txt" <<'EOF'
weixin=installed package=@tencent-weixin/openclaw-weixin
qqbot=installed package=@tencent-connect/openclaw-qqbot@latest builtin=disabled
EOF

    chown -R "${BUILD_USER}:${BUILD_USER}" "${ONBOARD_HOME}"
}

install_onboard() {
    local user_autostart_dir="/home/${BUILD_USER}/.config/autostart"

    log_info "Installing onboard assets and autostart entries..."

    install -d -m 0755 "${ONBOARD_HOME}" "${ONBOARD_ASSET_DIR}" "${MARKER_DIR}" "${user_autostart_dir}"

    copy_payload_executable onboard.py "${ONBOARD_HOME}/onboard.py"
    copy_payload_executable edge_tts_proxy.py "${ONBOARD_HOME}/edge_tts_proxy.py"
    copy_payload openrouter.jpg "${ONBOARD_ASSET_DIR}/openrouter.jpg"
    copy_payload quantfans.png "${ONBOARD_ASSET_DIR}/quantfans.png"

    cat >/etc/xdg/autostart/quantideclaw-onboard.desktop <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=QuantideClaw Onboard
Comment=Run the QuantideClaw first-login onboarding flow
Exec=/usr/local/bin/quantideclaw-onboard-launch
Terminal=false
OnlyShowIn=XFCE;
X-GNOME-Autostart-enabled=true
X-XFCE-Autostart-enabled=true
EOF
    chmod 0644 /etc/xdg/autostart/quantideclaw-onboard.desktop
    cp /etc/xdg/autostart/quantideclaw-onboard.desktop "${user_autostart_dir}/quantideclaw-onboard.desktop"
    chmod 0644 "${user_autostart_dir}/quantideclaw-onboard.desktop"

    cat >/etc/xdg/autostart/quantideclaw-session-start.desktop <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=QuantideClaw Session Start
Comment=Start QuantideClaw background services for this session
Exec=/usr/local/bin/quantideclaw-session-start
Terminal=false
OnlyShowIn=XFCE;
X-GNOME-Autostart-enabled=true
X-XFCE-Autostart-enabled=true
EOF
    chmod 0644 /etc/xdg/autostart/quantideclaw-session-start.desktop
    cp /etc/xdg/autostart/quantideclaw-session-start.desktop "${user_autostart_dir}/quantideclaw-session-start.desktop"
    chmod 0644 "${user_autostart_dir}/quantideclaw-session-start.desktop"

    cat >/usr/local/bin/quantideclaw-session-start <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${HOME}/.openclaw/openclaw.json"
LOG_DIR="${HOME}/.openclaw/logs"
OPENCLAW_WRAPPER="/usr/local/bin/quantideclaw-openclaw"
RESTART_GATEWAY=false

if [[ "${1:-}" == "--restart-gateway" ]]; then
  RESTART_GATEWAY=true
fi

mkdir -p "${LOG_DIR}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  exit 0
fi

if [[ "${RESTART_GATEWAY}" == true ]]; then
  pkill -u "$(id -u)" -f "openclaw gateway" >/dev/null 2>&1 || true
  sleep 1
fi

if ! pgrep -u "$(id -u)" -f "edge_tts_proxy.py" >/dev/null 2>&1; then
  nohup /usr/bin/python3 /opt/quantideclaw-onboard/edge_tts_proxy.py >>"${LOG_DIR}/edge-tts-proxy.log" 2>&1 &
fi

if ! pgrep -u "$(id -u)" -f "openclaw gateway" >/dev/null 2>&1; then
  nohup openclaw gateway --verbose >>"${LOG_DIR}/gateway.log" 2>&1 &
fi
EOF
    chmod 0755 /usr/local/bin/quantideclaw-session-start

    cat >/usr/local/bin/quantideclaw-onboard <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

MARKER_FILE=/var/lib/quantideclaw-onboard/completed
if command -v /usr/local/bin/quantideclaw-session-start >/dev/null 2>&1; then
  /usr/local/bin/quantideclaw-session-start >/dev/null 2>&1 || true
fi
if [[ -f "${MARKER_FILE}" ]]; then
  exit 0
fi
exec /usr/bin/python3 /opt/quantideclaw-onboard/onboard.py
EOF
    chmod 0755 /usr/local/bin/quantideclaw-onboard

    cat >/usr/local/bin/quantideclaw-onboard-launch <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

MARKER_FILE=/var/lib/quantideclaw-onboard/completed

if [[ -f "${MARKER_FILE}" ]]; then
  exit 0
fi

if pgrep -u "$(id -u)" -f "/opt/quantideclaw-onboard/onboard.py" >/dev/null 2>&1; then
  exit 0
fi

nohup /usr/local/bin/quantideclaw-onboard >/tmp/quantideclaw-onboard-launch.log 2>&1 &
EOF
    chmod 0755 /usr/local/bin/quantideclaw-onboard-launch

    chown -R "${BUILD_USER}:${BUILD_USER}" "${ONBOARD_HOME}" "${MARKER_DIR}" "/home/${BUILD_USER}/.config"
}

cleanup_system() {
    if [[ "${RUN_CLEANUP}" != "true" ]]; then
        return 0
    fi

    log_info "Running safe cleanup..."

    apt-get autoremove -y --purge || true
    apt-get clean || true
    rm -rf /var/lib/apt/lists/*
    rm -rf /root/.cache "/home/${BUILD_USER}/.cache" /tmp/* /var/tmp/*
    find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
    find /usr/local/lib -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find /usr/lib -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
}

guest_main() {
    require_root
    ensure_payload_files

    log_info "=========================================="
    log_info "QuantideClaw Product Customizer"
    log_info "Build user: ${BUILD_USER}"
    log_info "Architecture: ${TARGET_ARCH}"
    log_info "Cleanup: ${RUN_CLEANUP}"
    log_info "=========================================="

    ensure_build_user
    configure_cn_runtime_mirrors
    configure_python
    install_nodejs
    install_bun
    install_chrome_best_effort
    install_openclaw
    install_onboard
    cleanup_system

    log_info "Product image customization complete."
    log_info "Reboot the VM and verify onboard.py launches once after desktop login."
}

host_main() {
    local upload_items=(
        "scripts/quantideclaw-customize.sh"
        "scripts/_remote_phase_lib.sh"
        "guest/assets/onboard.py"
        "guest/assets/edge_tts_proxy.py"
        "guest/assets/openrouter.jpg"
        "guest/assets/quantfans.png"
    )

    phase_require_host_tools

    cat <<'EOF'
Stage 2 will:
1. Connect to the VM over SSH.
2. Upload this stage script and its required product assets.
3. Execute the product customization inside the VM as root.

You only need:
- The VM powered on
- SSH enabled in the VM
- The VM SSH login information
EOF

    phase_collect_ssh_inputs
    phase_test_connection
    phase_upload_items "${REMOTE_STAGE_DIR}" "${upload_items[@]}"
    phase_run_remote_script "${REMOTE_STAGE_DIR}" "scripts/quantideclaw-customize.sh" \
        "BUILD_USER=${BUILD_USER}" \
        "BUILD_USER_PASSWORD=${BUILD_USER_PASSWORD}" \
        "TARGET_ARCH=${TARGET_ARCH}" \
        "RUN_CLEANUP=${RUN_CLEANUP}"
    phase_cleanup_remote_stage "${REMOTE_STAGE_DIR}"

    log_info "Stage 2 complete."
    log_info "Reboot the VM and verify onboard.py launches only on first login."
}

main() {
    case "$(uname -s)" in
        Darwin)
            host_main "$@"
            ;;
        *)
            if [[ "${TARGET_ARCH}" == "arm64" ]] && command -v dpkg >/dev/null 2>&1; then
                TARGET_ARCH="$(dpkg --print-architecture)"
            fi
            guest_main "$@"
            ;;
    esac
}

main "$@"
