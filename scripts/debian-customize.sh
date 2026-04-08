#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
# shellcheck source=./_remote_phase_lib.sh
source "${SCRIPT_DIR}/_remote_phase_lib.sh"

THEME_NAME="quantideclaw"
BUILD_USER="${BUILD_USER:-quantide}"
BUILD_USER_PASSWORD="${BUILD_USER_PASSWORD:-quantide}"
RUN_CLEANUP="${RUN_CLEANUP:-true}"
REMOTE_STAGE_DIR="${REMOTE_STAGE_DIR:-/root/quantideclaw-stage1}"

log_info() {
    phase_log_info "$@"
}

log_warn() {
    phase_log_warn "$@"
}

log_error() {
    phase_log_error "$@"
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

ensure_build_user() {
    log_info "Ensuring desktop user '${BUILD_USER}' exists..."

    if ! id -u "${BUILD_USER}" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "${BUILD_USER}"
        echo "${BUILD_USER}:${BUILD_USER_PASSWORD}" | chpasswd
        log_info "Created user ${BUILD_USER}"
    fi

    apt-get install -y --no-install-recommends sudo >/dev/null
    usermod -aG sudo,audio,video,plugdev,netdev "${BUILD_USER}" || true
}

switch_apt_mirror() {
    local codename
    local source_file
    local sources_file="/etc/apt/sources.list"

    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-trixie}")"
    log_info "Switching APT mirror to Tsinghua for ${codename}..."

    if [[ -f "${sources_file}" ]]; then
        cp "${sources_file}" "${sources_file}.bak.$(date +%Y%m%d%H%M%S)"
    fi

    for source_file in /etc/apt/sources.list.d/*.sources; do
        [[ -f "${source_file}" ]] || continue
        cp "${source_file}" "${source_file}.bak" 2>/dev/null || true
        mv "${source_file}" "${source_file}.disabled-by-quantideclaw"
    done

    cat >"${sources_file}" <<EOF
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename} main contrib non-free non-free-firmware
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename}-updates main contrib non-free non-free-firmware
deb https://mirrors.tuna.tsinghua.edu.cn/debian/ ${codename}-backports main contrib non-free non-free-firmware
deb https://mirrors.tuna.tsinghua.edu.cn/debian-security/ ${codename}-security main contrib non-free non-free-firmware
EOF

    apt-get update
}

install_desktop_packages() {
    local core_packages=(
        sudo ca-certificates curl wget git jq unzip zip xz-utils gnupg lsb-release
        dbus-x11 xauth xdg-utils python3 python3-pil
        fonts-noto-cjk fonts-wqy-zenhei
    )
    local desktop_packages=(
        xorg accountsservice lightdm lightdm-gtk-greeter xfce4 xfce4-goodies
        network-manager pavucontrol mesa-utils plymouth plymouth-themes
    )
    local audio_pkg=""
    local browser_pkg=""

    log_info "Installing desktop packages..."

    if apt-cache show polkitd >/dev/null 2>&1; then
        core_packages+=(polkitd)
    elif apt-cache show policykit-1 >/dev/null 2>&1; then
        core_packages+=(policykit-1)
    else
        log_warn "Neither polkitd nor policykit-1 is available."
    fi

    if apt-cache show pkexec >/dev/null 2>&1; then
        core_packages+=(pkexec)
    fi

    if apt-cache show pulseaudio >/dev/null 2>&1; then
        audio_pkg="pulseaudio"
    elif apt-cache show pipewire-pulse >/dev/null 2>&1; then
        audio_pkg="pipewire-pulse"
    fi

    if [[ -n "${audio_pkg}" ]]; then
        desktop_packages+=("${audio_pkg}")
    else
        log_warn "Neither pulseaudio nor pipewire-pulse is available."
    fi

    if apt-cache show firefox-esr >/dev/null 2>&1; then
        browser_pkg="firefox-esr"
    elif apt-cache show firefox >/dev/null 2>&1; then
        browser_pkg="firefox"
    fi

    if [[ -n "${browser_pkg}" ]]; then
        desktop_packages+=("${browser_pkg}")
    else
        log_warn "Neither firefox-esr nor firefox is available."
    fi

    apt-get install -y --no-install-recommends "${core_packages[@]}"
    apt-get install -y --no-install-recommends "${desktop_packages[@]}"

    systemctl enable accounts-daemon 2>/dev/null || true
    systemctl start accounts-daemon 2>/dev/null || true
    systemctl enable NetworkManager 2>/dev/null || true
    systemctl enable lightdm 2>/dev/null || true
}

configure_cn_runtime_mirrors() {
    log_info "Configuring system-wide pip mirror..."

    cat >/etc/pip.conf <<'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
timeout = 120
EOF
}

configure_lightdm() {
    log_info "Configuring LightDM autologin..."

    install -d -m 0755 /etc/lightdm/lightdm.conf.d
    cat >/etc/lightdm/lightdm.conf.d/50-quantideclaw-autologin.conf <<EOF
[Seat:*]
autologin-user=${BUILD_USER}
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
EOF

    cat >"/home/${BUILD_USER}/.dmrc" <<'EOF'
[Desktop]
Session=xfce
EOF
    chown "${BUILD_USER}:${BUILD_USER}" "/home/${BUILD_USER}/.dmrc"
    chmod 0644 "/home/${BUILD_USER}/.dmrc"

    systemctl set-default graphical.target 2>/dev/null || true
    systemctl disable getty@tty1.service 2>/dev/null || true
    systemctl mask getty@tty1.service 2>/dev/null || true
}

configure_grub() {
    log_info "Configuring GRUB..."

    if [[ ! -f /etc/default/grub ]]; then
        log_warn "Missing /etc/default/grub, skipping GRUB update."
        return 0
    fi

    cp /etc/default/grub /etc/default/grub.bak.$(date +%Y%m%d%H%M%S)
    cat >/etc/default/grub <<'EOF'
GRUB_DEFAULT=0
GRUB_GFXMODE=auto
GRUB_GFXPAYLOAD_LINUX=keep
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=0
GRUB_DISABLE_OS_PROBER=true
GRUB_DISTRIBUTOR="`lsb_release -i -s 2> /dev/null || echo Debian`"
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash plymouth.ignore-serial-consoles loglevel=0 systemd.log_level=0 systemd.show_status=false vt.global_cursor_default=0"
GRUB_CMDLINE_LINUX=""
EOF

    if command -v update-grub >/dev/null 2>&1; then
        update-grub
    elif command -v grub-mkconfig >/dev/null 2>&1; then
        if [[ -d /boot/grub ]]; then
            grub-mkconfig -o /boot/grub/grub.cfg
        elif [[ -d /boot/grub2 ]]; then
            grub-mkconfig -o /boot/grub2/grub.cfg
        else
            log_warn "Cannot find GRUB output directory, skipped grub-mkconfig."
        fi
    fi
}

append_initramfs_module() {
    local module_name="$1"
    local modules_file="/etc/initramfs-tools/modules"

    install -d -m 0755 "$(dirname "${modules_file}")"
    touch "${modules_file}"

    if ! grep -q "^${module_name}$" "${modules_file}" 2>/dev/null; then
        echo "${module_name}" >>"${modules_file}"
    fi
}

install_plymouth_theme() {
    local theme_dir="/usr/share/plymouth/themes/${THEME_NAME}"
    local theme_image_source

    if ! theme_image_source="$(find_payload_file "quantideclaw-boot.png")"; then
        if ! theme_image_source="$(find_payload_file "logo.png")"; then
            log_warn "No brand image found, falling back to spinner theme."
            return 1
        fi
    fi

    log_info "Installing Plymouth theme from ${theme_image_source}..."

    install -d -m 0755 "${theme_dir}"
    install -m 0644 "${theme_image_source}" "${theme_dir}/brand.png"

    cat >"${theme_dir}/${THEME_NAME}.plymouth" <<EOF
[Plymouth Theme]
Name=QuantideClaw
Description=QuantideClaw branded boot splash
ModuleName=script

[script]
ImageDir=${theme_dir}
ScriptFile=${theme_dir}/${THEME_NAME}.script
EOF

    cat >"${theme_dir}/${THEME_NAME}.script" <<'EOF'
Window.SetBackgroundTopColor(0.0, 0.0, 0.0);
Window.SetBackgroundBottomColor(0.0, 0.0, 0.0);

brand.image = Image("brand.png");
brand.sprite = Sprite(brand.image);
brand.sprite.SetOpacity(1);
brand.sprite.SetZ(100);

fun refresh_callback ()
  {
    brand.sprite.SetX(Window.GetWidth() / 2 - brand.image.GetWidth() / 2);
    brand.sprite.SetY(Window.GetHeight() / 2 - brand.image.GetHeight() / 2);
  }

refresh_callback();
Plymouth.SetRefreshFunction(refresh_callback);
EOF

    if command -v plymouth-set-default-theme >/dev/null 2>&1; then
        plymouth-set-default-theme "${THEME_NAME}"
    fi

    return 0
}

configure_boot_splash() {
    log_info "Configuring Plymouth boot splash..."

    if ! install_plymouth_theme; then
        if command -v plymouth-set-default-theme >/dev/null 2>&1; then
            plymouth-set-default-theme spinner || true
        fi
    fi

    append_initramfs_module "drm"
    append_initramfs_module "simpledrm"
    append_initramfs_module "virtio_gpu"

    install -d -m 0755 /etc/initramfs-tools/conf.d
    cat >/etc/initramfs-tools/conf.d/splash <<'EOF'
FRAMEBUFFER=y
EOF

    if command -v update-initramfs >/dev/null 2>&1; then
        update-initramfs -u -k all || true
    fi
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

    log_info "=========================================="
    log_info "QuantideClaw Debian Base Customizer"
    log_info "Build user: ${BUILD_USER}"
    log_info "Cleanup: ${RUN_CLEANUP}"
    log_info "=========================================="

    switch_apt_mirror
    install_desktop_packages
    configure_cn_runtime_mirrors
    ensure_build_user
    configure_lightdm
    configure_grub
    configure_boot_splash
    cleanup_system

    log_info "Base image customization complete."
    log_info "Reboot the VM to verify branded boot and automatic XFCE login."
}

host_main() {
    local upload_items=(
        "scripts/debian-customize.sh"
        "scripts/_remote_phase_lib.sh"
    )

    phase_require_host_tools

    if [[ -f "${ROOT_DIR}/guest/assets/quantideclaw-boot.png" ]]; then
        upload_items+=("guest/assets/quantideclaw-boot.png")
    fi

    if [[ -f "${ROOT_DIR}/guest/assets/logo.png" ]]; then
        upload_items+=("guest/assets/logo.png")
    fi

    cat <<'EOF'
Stage 1 will:
1. Connect to the Debian VM over SSH.
2. Upload this stage script and its required branding assets.
3. Execute the customization inside the VM as root.

You only need:
- The VM powered on
- SSH enabled in the VM
- The VM SSH login information
EOF

    phase_collect_ssh_inputs
    phase_test_connection
    phase_upload_items "${REMOTE_STAGE_DIR}" "${upload_items[@]}"
    phase_run_remote_script "${REMOTE_STAGE_DIR}" "scripts/debian-customize.sh" \
        "BUILD_USER=${BUILD_USER}" \
        "BUILD_USER_PASSWORD=${BUILD_USER_PASSWORD}" \
        "RUN_CLEANUP=${RUN_CLEANUP}"
    phase_cleanup_remote_stage "${REMOTE_STAGE_DIR}"

    log_info "Stage 1 complete."
    log_info "Reboot the VM and run the acceptance checks."
}

main() {
    case "$(uname -s)" in
        Darwin)
            host_main "$@"
            ;;
        *)
            guest_main "$@"
            ;;
    esac
}

main "$@"
