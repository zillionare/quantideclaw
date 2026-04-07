OpenClaw Debian 12 VM image scaffold

Prerequisites on macOS:
  brew install hashicorp/tap/packer qemu

Build commands:
  ./scripts/build-amd64.sh
  ./scripts/build-arm64.sh

Notes:
- amd64 builds should run on an x86_64 Linux host for speed.
- arm64 builds can run on Apple Silicon with QEMU + HVF.
- The OpenClaw CLI commands in guest/scripts/provision-openclaw.sh are defaults and may need to be adjusted to match the real CLI.
- First boot auto-logs into XFCE and launches the Python wizard once.
- Uses Debian 12 netinst ISO (~400MB) for smaller image size.
- Final image size: ~1.5-2GB (compressed).
