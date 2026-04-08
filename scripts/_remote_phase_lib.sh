#!/usr/bin/env bash

phase_log_info() {
    echo "[INFO] $(date '+%Y-%m-%d %H:%M:%S') - $*"
}

phase_log_warn() {
    echo "[WARN] $(date '+%Y-%m-%d %H:%M:%S') - $*" >&2
}

phase_log_error() {
    echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') - $*" >&2
}

phase_shell_quote() {
    printf '%q' "$1"
}

phase_require_host_tools() {
    local missing=0
    local tool

    for tool in ssh scp tar; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            phase_log_error "Missing required command: ${tool}"
            missing=1
        fi
    done

    if [[ "$missing" -ne 0 ]]; then
        exit 1
    fi
}

phase_collect_ssh_inputs() {
    if [[ -z "${VM_HOST:-}" ]]; then
        read -rp "VM IP or hostname: " VM_HOST
    fi
    if [[ -z "${VM_HOST:-}" ]]; then
        phase_log_error "VM IP or hostname cannot be empty."
        exit 1
    fi

    if [[ -z "${SSH_USER:-}" ]]; then
        read -rp "SSH user [root]: " SSH_USER
        SSH_USER="${SSH_USER:-root}"
    fi

    if [[ -z "${SSH_PORT:-}" ]]; then
        read -rp "SSH port [22]: " SSH_PORT
        SSH_PORT="${SSH_PORT:-22}"
    fi

    if [[ -z "${SSH_PASSWORD:-}" ]] && command -v sshpass >/dev/null 2>&1; then
        read -rsp "SSH password (leave empty for interactive prompts): " SSH_PASSWORD
        echo
    fi
}

phase_ssh() {
    if [[ -n "${SSH_PASSWORD:-}" ]] && command -v sshpass >/dev/null 2>&1; then
        sshpass -p "${SSH_PASSWORD}" ssh \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -p "${SSH_PORT}" \
            "${SSH_USER}@${VM_HOST}" "$@"
        return
    fi

    ssh \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -p "${SSH_PORT}" \
        "${SSH_USER}@${VM_HOST}" "$@"
}

phase_test_connection() {
    phase_log_info "Testing SSH connection: ${SSH_USER}@${VM_HOST}:${SSH_PORT}"
    if ! phase_ssh "echo connected" >/dev/null; then
        phase_log_error "SSH connection failed. Verify the VM is running, SSH is enabled, and the login info is correct."
        exit 1
    fi
    phase_log_info "SSH connection succeeded."
}

phase_upload_items() {
    local remote_stage_dir="$1"
    shift

    if [[ "$#" -eq 0 ]]; then
        phase_log_error "No files provided for upload."
        exit 1
    fi

    phase_log_info "Uploading stage files to ${SSH_USER}@${VM_HOST}:${remote_stage_dir}"

    (
        cd "${ROOT_DIR}"
        tar -cf - "$@"
    ) | phase_ssh "rm -rf $(phase_shell_quote "${remote_stage_dir}") && mkdir -p $(phase_shell_quote "${remote_stage_dir}") && tar -xf - -C $(phase_shell_quote "${remote_stage_dir}")"
}

phase_run_remote_script() {
    local remote_stage_dir="$1"
    local remote_script_rel="$2"
    shift 2

    local remote_script_path="${remote_stage_dir}/${remote_script_rel}"
    local remote_cmd="env QUANTIDECLAW_REMOTE_EXEC=1"
    local assignment

    for assignment in "$@"; do
        remote_cmd+=" $(phase_shell_quote "${assignment}")"
    done

    remote_cmd+=" bash $(phase_shell_quote "${remote_script_path}")"

    phase_log_info "Running ${remote_script_rel} inside the VM"
    if ! phase_ssh "${remote_cmd}"; then
        phase_log_error "Remote script failed. Preserved ${remote_stage_dir} for debugging."
        exit 1
    fi
    phase_log_info "Remote script finished."
}

phase_cleanup_remote_stage() {
    local remote_stage_dir="$1"
    phase_log_info "Cleaning temporary remote directory ${remote_stage_dir}"
    phase_ssh "rm -rf $(phase_shell_quote "${remote_stage_dir}")" >/dev/null || true
}
