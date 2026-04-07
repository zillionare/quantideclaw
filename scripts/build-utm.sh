#!/bin/bash
###############################################################################
# build-utm.sh - Build OpenClaw Debian VM using UTM on macOS ARM
#
# This script helps you create an OpenClaw VM using UTM.
# Since UTM doesn't have a full CLI API, this script:
# 1. Guides you through VM creation in UTM
# 2. Copies guest/ and runs the single provision entrypoint via SSH after Debian is installed
#
# Prerequisites:
# - UTM installed (https://mac.getutm.app)
# - SSH enabled in Debian
#
# Usage:
#   ./scripts/build-utm.sh
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Configuration
VM_NAME="openclaw-debian-13"

SSH_USER="${SSH_USER:-root}"
SSH_PASS="${SSH_PASS:-root}"
SSH_PORT=22
BUILD_USER="${BUILD_USER:-quantide}"
BUILD_USER_PASSWORD="${BUILD_USER_PASSWORD:-quantide}"
REMOTE_STAGE_DIR="/root/openclaw-image"

###############################################################################
# Helper functions
###############################################################################
log_info() {
    echo "[INFO] $(date '+%Y-%m-%d %H:%M:%S') - $*"
}

log_error() {
    echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') - $*" >&2
}

log_step() {
    echo ""
    echo "============================================"
    echo "  $*"
    echo "============================================"
}

check_prerequisites() {
    log_step "Step 1: Checking prerequisites"

    # Check UTM installation
    if ! ls /Applications/UTM.app/Contents/MacOS/utmctl &>/dev/null; then
        log_error "UTM is not installed"
        log_error "Please install UTM from: https://mac.getutm.app"
        exit 1
    fi
    log_info "✓ UTM found"

    # Check sshpass (required for non-interactive SSH)
    if ! command -v sshpass &>/dev/null; then
        log_error "'sshpass' is required for automated SSH login but not found."
        log_info "Manual SSH works because it prompts for a password, but this script needs 'sshpass'."
        log_info "Please install it with: brew install sshpass"
        exit 1
    fi
    log_info "✓ sshpass found"
}

create_vm_guide() {
    log_step "Step 2: Create VM in UTM"

    cat << 'EOF'
Please create a new VM in UTM with these settings:

1. Open UTM and click "Create a New Virtual Machine"
2. Select "Virtualize" (not Emulate)
3. Select "Linux"
4. Configure:
   - Memory: 8192 MB
   - CPU Cores: 4
   - Storage: 64 GB
5. Under "Drives", add your Debian 13 ARM64 netinst ISO:
   - Click "New Drive" → "Import"
    - Select your local `.iso` file
   - Mark as "Removable"
6. Under "Network", ensure "Shared Network" is selected
7. Click "Save" and name it: openclaw-debian-13

Then start the VM and install Debian:
  - Use "Graphical install"
  - Language: English
  - Keyboard: US
  - Hostname: openclaw
  - Domain: (leave empty)
        - Root password: root
        - User account: optional during install; the provision scripts will create the final desktop user (quantide) automatically
  - Timezone: Asia/Shanghai
  - Partitioning: Guided - use entire disk
  - Software selection:
    ✓ SSH server
    ✓ standard system utilities
    ✗ DO NOT select any desktop environment

After installation completes and VM reboots:
  1. Remove the ISO from CD drive (UTM settings)
    2. Login with: root / root
  3. Check SSH is running: systemctl status ssh
  4. Note the IP address: ip addr show
  5. Press ENTER to continue

EOF

    read -rp "Press ENTER when Debian is installed and you can SSH into the VM..."
}

test_ssh() {
    log_step "Step 3: Testing SSH connection"

    # Ask for VM IP
    local vm_ip=""
    while [[ -z "$vm_ip" ]]; do
        read -rp "Enter VM IP address (from 'ip addr' in the VM): " vm_ip
        if [[ -z "$vm_ip" ]]; then
            log_error "IP address cannot be empty"
        fi
    done

    log_info "Testing SSH connection to ${SSH_USER}@${vm_ip}..."

    if sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        -o UserKnownHostsFile=/dev/null \
        -p "$SSH_PORT" \
        "${SSH_USER}@${vm_ip}" \
        "echo 'SSH connection successful!'" &>/dev/null; then
        log_info "✓ SSH connection verified"
        VM_IP="$vm_ip"
        return 0
    else
        log_error "Cannot connect via SSH as ${SSH_USER}"
        log_info "Please ensure OpenSSH server is installed and running: systemctl restart ssh"
        exit 1
    fi
}

run_provision() {
    log_step "Step 4: Uploading guest payload and running provision-manual.sh"

    log_info ">>> Preparing remote staging directory..."
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -p "$SSH_PORT" \
        "${SSH_USER}@${VM_IP}" \
        "rm -rf '${REMOTE_STAGE_DIR}' && mkdir -p '${REMOTE_STAGE_DIR}'"

    log_info ">>> Uploading guest resources..."
    sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -P "$SSH_PORT" \
        -r "$ROOT_DIR/guest" \
        "${SSH_USER}@${VM_IP}:${REMOTE_STAGE_DIR}/guest"

    log_info "✓ Resources uploaded to ${REMOTE_STAGE_DIR}"

    if [[ ! -f "$ROOT_DIR/guest/scripts/provision-manual.sh" ]]; then
        log_error "Script not found: guest/scripts/provision-manual.sh"
        exit 1
    fi

    log_info ">>> Executing provision-manual.sh..."
    if ! sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -p "$SSH_PORT" \
        "${SSH_USER}@${VM_IP}" \
        "env TARGET_ARCH=arm64 TARGET_PLATFORM=utm BUILD_USER='$BUILD_USER' BUILD_USER_PASSWORD='$BUILD_USER_PASSWORD' bash '${REMOTE_STAGE_DIR}/guest/scripts/provision-manual.sh'"; then
        log_error "provision-manual.sh execution failed!"
        exit 1
    fi

    log_info "✓ provision-manual.sh completed"
}

export_vm() {
    log_step "Step 5: Finalizing"

    cat << EOF

The VM has been provisioned successfully!

To export the VM for distribution:
1. Shut down the VM in UTM
2. Right-click the VM → "Export"
3. Save as: output/${VM_NAME}.utm

To compress for distribution:
  cd output
  tar -czf ${VM_NAME}.utm.tar.gz ${VM_NAME}.utm

The final file can be shared with users.

EOF

    read -rp "Press ENTER to exit..."
}

###############################################################################
# Main
###############################################################################
main() {
    log_info "=========================================="
    log_info "OpenClaw Debian VM Builder (UTM/macOS ARM)"
    log_info "=========================================="

    check_prerequisites
    create_vm_guide
    test_ssh
    run_provision
    export_vm

    log_info "Build completed successfully!"
}

main "$@"
