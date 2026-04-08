#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

BUILD_USER="${BUILD_USER:-quantide}"
BUILD_USER_PASSWORD="${BUILD_USER_PASSWORD:-quantide}"
TARGET_ARCH="${TARGET_ARCH:-$(dpkg --print-architecture)}"
TARGET_PLATFORM="${TARGET_PLATFORM:-manual}"
INSTALL_DESKTOP="${INSTALL_DESKTOP:-true}"
RUN_CLEANUP="${RUN_CLEANUP:-true}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_HOME="/opt/openclaw-firstboot"
OPENCLAW_ASSET_DIR="${OPENCLAW_HOME}/assets"
INSTALLER_ENV="${OPENCLAW_HOME}/installer.env"
MARKER_DIR="/var/lib/openclaw-firstboot"
STATUS_DIR="/var/lib/openclaw-build"
STATUS_FILE="${STATUS_DIR}/browser-status.txt"

log_info() {
    echo "[INFO] $(date '+%Y-%m-%d %H:%M:%S') - $*"
}

log_error() {
    echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') - $*" >&2
}

version_ge() {
    dpkg --compare-versions "$1" ge "$2"
}

current_node_version() {
    node --version 2>/dev/null | sed 's/^v//'
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

is_wsl() {
    [[ -f /run/WSL || "${TARGET_PLATFORM}" == "wsl2" ]]
}

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        log_error "This script must run as root."
        exit 1
    fi
}

find_payload_file() {
    local name="$1"
    local candidate

    for candidate in \
        "${SCRIPT_DIR}/assets/${name}" \
        "${SCRIPT_DIR}/../assets/${name}" \
        "${SCRIPT_DIR}/../../assets/${name}" \
        "${SCRIPT_DIR}/../guest/assets/${name}" \
        "${SCRIPT_DIR}/../../guest/assets/${name}"; do
        if [[ -f "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    return 1
}

require_payload() {
    local name="$1"

    if ! find_payload_file "${name}" >/dev/null; then
        log_error "Missing required payload file: ${name}"
        log_error "Copy the script together with an assets directory containing the wizard Python files and images."
        exit 1
    fi
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

ensure_payload_files() {
    require_payload openclaw_firstboot.py
    require_payload edge_tts_proxy.py
    require_payload openclaw-firstboot.desktop
    require_payload openrouter.jpg
    require_payload quantfans.png
}

ensure_build_user() {
    log_info "Ensuring build user '${BUILD_USER}' exists..."

    if ! id -u "${BUILD_USER}" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "${BUILD_USER}"
        echo "${BUILD_USER}:${BUILD_USER_PASSWORD}" | chpasswd
        log_info "Created user ${BUILD_USER}"
    fi

    if is_wsl; then
        cat >/etc/wsl.conf <<EOF
[user]
default=${BUILD_USER}
EOF
    fi

    usermod -aG sudo,audio,video,plugdev,netdev "${BUILD_USER}"
}

switch_apt_mirror() {
    local codename
    local sources_file="/etc/apt/sources.list"
    local source_file

    codename="$(lsb_release -cs 2>/dev/null || echo 'trixie')"
    log_info "Switching APT mirrors to Tsinghua for ${codename}..."

    if [[ -f "${sources_file}" ]]; then
        cp "${sources_file}" "${sources_file}.bak" 2>/dev/null || true
    fi

    for source_file in /etc/apt/sources.list.d/*.sources; do
        [[ -f "${source_file}" ]] || continue
        cp "${source_file}" "${source_file}.bak" 2>/dev/null || true
        mv "${source_file}" "${source_file}.disabled-by-openclaw"
    done

    cat >"${sources_file}" <<EOF
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename} main contrib non-free non-free-firmware
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename}-updates main contrib non-free non-free-firmware
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename}-backports main contrib non-free non-free-firmware
deb https://mirrors.tuna.tsinghua.edu.cn/debian-security/ ${codename}-security main contrib non-free non-free-firmware
EOF
}

install_base_packages() {
    log_info "Installing base packages and XFCE desktop..."

    apt-get update
    apt-get install -y --no-install-recommends sudo
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget git jq unzip zip xz-utils build-essential \
        gnupg lsb-release dbus-x11 xauth xdg-utils \
        python3 python3-pip python3-tk python3-pil python3-pil.imagetk tk \
        fonts-noto-cjk fonts-wqy-zenhei

    if [[ "${INSTALL_DESKTOP}" == "true" ]]; then
        apt-get install -y --no-install-recommends \
            xorg accountsservice lightdm lightdm-gtk-greeter xfce4 xfce4-goodies \
            network-manager pulseaudio pavucontrol mesa-utils firefox-esr

        if ! is_wsl; then
            systemctl enable accounts-daemon 2>/dev/null || true
            systemctl start accounts-daemon 2>/dev/null || true
            systemctl enable lightdm 2>/dev/null || true
            systemctl enable NetworkManager 2>/dev/null || true
        fi
    fi
}

configure_cn_mirrors() {
    log_info "Configuring npm/pip/bun mirrors..."

    cat >/etc/npmrc <<'EONPM'
registry=https://registry.npmmirror.com/
fund=false
audit=false
EONPM

    cat >/etc/pip.conf <<'EOPIP'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
timeout = 120
EOPIP

    cat >/etc/bunfig.toml <<'EOBUN'
[install]
registry = "https://registry.npmmirror.com"
EOBUN
}

configure_python() {
    log_info "Configuring Python and edge-tts..."

    ln -sf "$(command -v python3)" /usr/local/bin/python
    ln -sf "$(command -v pip3)" /usr/local/bin/pip

    cat >/etc/profile.d/python-aliases.sh <<'EOALIAS'
if command -v python3 >/dev/null 2>&1; then
    alias python=python3
    alias pip=pip3
fi
EOALIAS
    chmod 0644 /etc/profile.d/python-aliases.sh

    python3 -m pip install --break-system-packages --no-cache-dir edge-tts qrcode[pil]
}

install_nodejs() {
    local required_version="22.14.0"
    local installed_version=""

    log_info "Installing Node.js (OpenClaw requires >= ${required_version})..."

    apt-get install -y --no-install-recommends nodejs npm || true

    if command -v node >/dev/null 2>&1; then
        installed_version="$(current_node_version)"
        if [[ -n "${installed_version}" ]] && version_ge "${installed_version}" "${required_version}"; then
            log_info "Using Debian mirror Node.js v${installed_version}"
            ensure_corepack
            return 0
        fi

        if [[ -n "${installed_version}" ]]; then
            log_info "Debian mirror Node.js v${installed_version} is below required >= ${required_version}; trying NodeSource 24.x..."
        fi
    else
        log_info "Debian mirror Node.js package not available; trying NodeSource 24.x..."
    fi

    if ! curl --retry 3 --retry-all-errors -fsSL https://deb.nodesource.com/setup_24.x | bash -; then
        log_error "NodeSource setup failed, and Debian mirror Node.js did not satisfy >= ${required_version}."
        exit 1
    fi

    apt-get install -y nodejs
    installed_version="$(current_node_version)"
    if [[ -z "${installed_version}" ]] || ! version_ge "${installed_version}" "${required_version}"; then
        log_error "Installed Node.js version is '${installed_version:-missing}', but OpenClaw requires >= ${required_version}."
        exit 1
    fi

    log_info "Using NodeSource Node.js v${installed_version}"
    ensure_corepack
}

install_bun() {
    log_info "Installing Bun from npm mirror..."
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

    log_info "Installing Google Chrome best-effort on amd64..."
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

validate_openclaw_runtime() {
    if ! openclaw plugins list >/tmp/openclaw-plugins-list.log 2>&1; then
        cat /tmp/openclaw-plugins-list.log >&2
        log_error "OpenClaw CLI failed runtime validation."
        exit 1
    fi
}

install_weixin_plugin_for_user() {
    su - "${BUILD_USER}" -s /bin/bash -c '
        set -euo pipefail
        plugin_root="$HOME/.openclaw"
        rm -rf "$plugin_root/extensions/openclaw-weixin" "$plugin_root/hook-packs/openclaw-weixin"
        mkdir -p "$plugin_root/extensions" "$plugin_root/hook-packs"
        openclaw plugins install @tencent-weixin/openclaw-weixin
    '
}

install_openclaw() {
    log_info "Installing OpenClaw CLI and plugin payload..."

    install -d -m 0755 "${OPENCLAW_HOME}" "${OPENCLAW_ASSET_DIR}" "${STATUS_DIR}"

    cat >"${INSTALLER_ENV}" <<'EOENV'
OPENCLAW_HOME=/opt/openclaw-firstboot
OPENCLAW_ASSETS_DIR=/opt/openclaw-firstboot/assets
OPENCLAW_CONFIG_PATH=~/.openclaw/openclaw.json
OPENCLAW_WORKSPACE=~/.openclaw/workspace
WEIXIN_PLUGIN_PACKAGE=@tencent-weixin/openclaw-weixin
WEIXIN_CHANNEL=openclaw-weixin
QQBOT_CHANNEL=qqbot
EDGE_TTS_PROXY_URL=http://127.0.0.1:18792/v1
EDGE_TTS_DEFAULT_VOICE=zh-CN-XiaoxiaoNeural
CHROME_STATUS_FILE=/var/lib/openclaw-build/browser-status.txt
EOENV

    npm install -g openclaw
    repair_openclaw_runtime_deps
    validate_openclaw_runtime
    install_weixin_plugin_for_user

    cat >"${OPENCLAW_HOME}/plugin-status.txt" <<'EOSTATUS'
weixin=installed package=@tencent-weixin/openclaw-weixin
qqbot=bundled package=qqbot
EOSTATUS

    chown -R "${BUILD_USER}:${BUILD_USER}" "${OPENCLAW_HOME}"
}

configure_desktop() {
    if [[ "${INSTALL_DESKTOP}" != "true" ]]; then
        return 0
    fi

    if is_wsl; then
        log_info "WSL2 detected - skipping LightDM autologin and graphical.target configuration."
        return 0
    fi

    log_info "Configuring direct boot into XFCE via LightDM..."

    install -d -m 0755 /etc/lightdm/lightdm.conf.d
    cat >/etc/lightdm/lightdm.conf.d/50-openclaw-autologin.conf <<EOF
[Seat:*]
autologin-user=${BUILD_USER}
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
EOF

    cat >"/home/${BUILD_USER}/.dmrc" <<'EODMRC'
[Desktop]
Session=xfce
EODMRC
    chown "${BUILD_USER}:${BUILD_USER}" "/home/${BUILD_USER}/.dmrc"
    chmod 0644 "/home/${BUILD_USER}/.dmrc"

    systemctl set-default graphical.target 2>/dev/null || true
    systemctl disable getty@tty1.service 2>/dev/null || true
    systemctl mask getty@tty1.service 2>/dev/null || true
}

configure_grub() {
    log_info "Configuring GRUB to hide boot menu and suppress boot messages..."

    # Backup original grub config
    if [[ -f /etc/default/grub ]]; then
        cp /etc/default/grub /etc/default/grub.bak.$(date +%Y%m%d%H%M%S)
    fi

    # Configure GRUB
    cat >/etc/default/grub <<'EOGRUB'
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=0
GRUB_DISABLE_OS_PROBER=true
GRUB_DISTRIBUTOR="`lsb_release -i -s 2> /dev/null || echo Debian`"
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash loglevel=0 systemd.log_level=0"
GRUB_CMDLINE_LINUX=""
EOGRUB

    # Update GRUB configuration
    if command -v update-grub >/dev/null 2>&1; then
        update-grub
    elif [[ -x /usr/sbin/update-grub ]]; then
        /usr/sbin/update-grub
    else
        log_info "update-grub not found, trying grub-mkconfig..."
        if [[ -d /boot/grub ]]; then
            grub-mkconfig -o /boot/grub/grub.cfg
        elif [[ -d /boot/grub2 ]]; then
            grub-mkconfig -o /boot/grub2/grub.cfg
        fi
    fi

    log_info "✓ GRUB configuration updated"
}

install_firstboot() {
    local user_autostart_dir="/home/${BUILD_USER}/.config/autostart"

    log_info "Installing first-boot wizard and XFCE autostart..."

    install -d -m 0755 "${OPENCLAW_HOME}" "${OPENCLAW_ASSET_DIR}" "${MARKER_DIR}" "${user_autostart_dir}"

    copy_payload_executable openclaw_firstboot.py "${OPENCLAW_HOME}/openclaw_firstboot.py"
    copy_payload_executable edge_tts_proxy.py "${OPENCLAW_HOME}/edge_tts_proxy.py"
    copy_payload openrouter.jpg "${OPENCLAW_ASSET_DIR}/openrouter.jpg"
    copy_payload quantfans.png "${OPENCLAW_ASSET_DIR}/quantfans.png"
    copy_payload openclaw-firstboot.desktop /etc/xdg/autostart/openclaw-firstboot.desktop
    copy_payload openclaw-firstboot.desktop "${user_autostart_dir}/openclaw-firstboot.desktop"

    cat >/etc/xdg/autostart/openclaw-session-start.desktop <<'EODESKTOP'
[Desktop Entry]
Type=Application
Version=1.0
Name=OpenClaw Session Start
Comment=Start the OpenClaw gateway services for this session
Exec=/usr/local/bin/openclaw-session-start
Terminal=false
OnlyShowIn=XFCE;
X-GNOME-Autostart-enabled=true
X-XFCE-Autostart-enabled=true
EODESKTOP
    chmod 0644 /etc/xdg/autostart/openclaw-session-start.desktop
    cp /etc/xdg/autostart/openclaw-session-start.desktop "${user_autostart_dir}/openclaw-session-start.desktop"
    chmod 0644 "${user_autostart_dir}/openclaw-session-start.desktop"

    cat >/usr/local/bin/openclaw-session-start <<'EOSTART'
#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${HOME}/.openclaw/openclaw.json"
LOG_DIR="${HOME}/.openclaw/logs"
RESTART_GATEWAY=false

if [[ "${1:-}" == "--restart-gateway" ]]; then
  RESTART_GATEWAY=true
fi

mkdir -p "$LOG_DIR"

if [[ "$RESTART_GATEWAY" == true ]]; then
  pkill -u "$(id -u)" -f "openclaw gateway" >/dev/null 2>&1 || true
  sleep 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  exit 0
fi

if ! pgrep -u "$(id -u)" -f "edge_tts_proxy.py" >/dev/null 2>&1; then
  nohup /usr/bin/python3 /opt/openclaw-firstboot/edge_tts_proxy.py >>"$LOG_DIR/edge-tts-proxy.log" 2>&1 &
fi

if ! pgrep -u "$(id -u)" -f "openclaw gateway" >/dev/null 2>&1; then
  nohup openclaw gateway --verbose >>"$LOG_DIR/gateway.log" 2>&1 &
fi
EOSTART
    chmod 0755 /usr/local/bin/openclaw-session-start

    cat >/usr/local/bin/openclaw-firstboot <<'EOWRAP'
#!/usr/bin/env bash
set -euo pipefail

MARKER_FILE=/var/lib/openclaw-firstboot/completed
if command -v /usr/local/bin/openclaw-session-start >/dev/null 2>&1; then
  /usr/local/bin/openclaw-session-start >/dev/null 2>&1 || true
fi
if [[ -f "$MARKER_FILE" ]]; then
  exit 0
fi
exec /usr/bin/python3 /opt/openclaw-firstboot/openclaw_firstboot.py
EOWRAP
    chmod 0755 /usr/local/bin/openclaw-firstboot

    cat >/usr/local/bin/openclaw-firstboot-launch <<'EOLAUNCH'
#!/usr/bin/env bash
set -euo pipefail

MARKER_FILE=/var/lib/openclaw-firstboot/completed

if [[ -f "$MARKER_FILE" ]]; then
  exit 0
fi

if pgrep -u "$(id -u)" -f "/opt/openclaw-firstboot/openclaw_firstboot.py" >/dev/null 2>&1; then
  exit 0
fi

nohup /usr/local/bin/openclaw-firstboot >/tmp/openclaw-firstboot-launch.log 2>&1 &
EOLAUNCH
    chmod 0755 /usr/local/bin/openclaw-firstboot-launch

        cat >/etc/profile.d/openclaw-wsl-firstboot.sh <<'EOWSL'
if [[ -f /run/WSL ]] && [[ $- == *i* ]] && [[ "$(id -u)" -ne 0 ]]; then
    /usr/local/bin/openclaw-session-start >/dev/null 2>&1 || true

    if [[ ! -f /var/lib/openclaw-firstboot/completed ]] && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && [[ -z "${OPENCLAW_FIRSTBOOT_SESSION_LAUNCHED:-}" ]]; then
        export OPENCLAW_FIRSTBOOT_SESSION_LAUNCHED=1
        nohup /usr/local/bin/openclaw-firstboot >/tmp/openclaw-firstboot-gui.log 2>&1 &
    fi
fi
EOWSL
        chmod 0644 /etc/profile.d/openclaw-wsl-firstboot.sh

    chown -R "${BUILD_USER}:${BUILD_USER}" "${OPENCLAW_HOME}" "${MARKER_DIR}" "/home/${BUILD_USER}/.config"
}

cleanup_system() {
    if [[ "${RUN_CLEANUP}" != "true" ]]; then
        return 0
    fi

    log_info "Cleaning system before image export..."

    apt-get autoremove -y --purge
    apt-get clean
    rm -rf /var/lib/apt/lists/*
    rm -rf /root/.cache "/home/${BUILD_USER}/.cache" /tmp/* /var/tmp/*
    find /var/log -type f -exec truncate -s 0 {} +
    find /usr/local/lib -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find /usr/lib -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    fstrim -av || true
}

main() {
    require_root
    ensure_payload_files

    log_info "=========================================="
    log_info "OpenClaw manual provisioning"
    log_info "Architecture: ${TARGET_ARCH}"
    log_info "Platform: ${TARGET_PLATFORM}"
    log_info "Build user: ${BUILD_USER}"
    log_info "Desktop: ${INSTALL_DESKTOP}"
    log_info "Cleanup: ${RUN_CLEANUP}"
    log_info "=========================================="

    ensure_build_user
    switch_apt_mirror
    install_base_packages
    configure_cn_mirrors
    configure_python
    install_nodejs
    install_bun
    install_chrome_best_effort
    install_openclaw
    configure_desktop
    configure_grub
    install_firstboot
    cleanup_system

    log_info "Provisioning complete. Reboot the VM and it should land directly in the XFCE desktop."
}

main "$@"
