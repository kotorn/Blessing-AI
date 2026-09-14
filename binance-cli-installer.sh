#!/bin/sh
set -eu

# Research-only helper. The downloaded binary is never placed in this repo and
# must be invoked through apps/trading_worker/research/binance_cli.py.
VERSION="2.1.1"
ARCHIVE="binance-cli-x86_64-unknown-linux-gnu.tar.xz"
SHA256="6b836a24f281abf590988207b0d19d4933971ca66dcf45a9e255cdc237c4deee"
URL="https://github.com/binance/binance-cli/releases/download/v${VERSION}/${ARCHIVE}"
INSTALL_DIR="${BINANCE_CLI_INSTALL_DIR:-${HOME}/.local/bin}"

usage() {
    echo "Usage: $0 [--install-dir PATH]"
    echo "Installs pinned Binance CLI ${VERSION} outside the repository."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-dir)
            [ "$#" -ge 2 ] || { echo "--install-dir requires a path" >&2; exit 2; }
            INSTALL_DIR=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[ "$(uname -s)" = "Linux" ] || { echo "Only Linux is supported by this pinned helper" >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "Only x86_64 is supported by this pinned helper" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { echo "sha256sum is required" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "tar is required" >&2; exit 1; }
command -v install >/dev/null 2>&1 || { echo "install is required" >&2; exit 1; }

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/blessing-binance-cli.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM
archive_path="${tmp_dir}/${ARCHIVE}"

curl --fail --silent --show-error --location "$URL" --output "$archive_path"
printf '%s  %s\n' "$SHA256" "$archive_path" | sha256sum --check --strict -
tar -xJf "$archive_path" -C "$tmp_dir"

binary_path=$(find "$tmp_dir" -type f -name binance-cli -print -quit)
[ -n "$binary_path" ] || { echo "Pinned archive did not contain binance-cli" >&2; exit 1; }
mkdir -p "$INSTALL_DIR"
install -m 0755 "$binary_path" "${INSTALL_DIR}/binance-cli"
echo "Installed ${INSTALL_DIR}/binance-cli"
echo "Use the read-only research wrapper; this script does not configure credentials or PATH."
