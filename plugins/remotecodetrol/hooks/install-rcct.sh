#!/usr/bin/env bash
# install-rcct.sh — symlink ~/.local/bin/rcct → ${CLAUDE_PLUGIN_ROOT}/bin/rcct
#
# Idempotent: skips when the symlink is already correct. Updates when the
# target has changed (e.g. plugin update with a new install path). Runs on
# every Claude Code SessionStart so a freshly-installed plugin works
# immediately in the SAME session — no restart required.
#
# If ~/.local/bin doesn't exist on PATH, prints a one-time stderr warning
# suggesting the user add it. The plugin still works (Claude can use the
# absolute fallback path), just less ergonomically.

set -e

# ${CLAUDE_PLUGIN_ROOT} is provided by Claude Code when the plugin is loaded.
SOURCE="${CLAUDE_PLUGIN_ROOT}/bin/rcct"
TARGET_DIR="${HOME}/.local/bin"
TARGET="${TARGET_DIR}/rcct"

if [[ ! -f "${SOURCE}" ]]; then
  # Plugin install incomplete — bail without erroring.
  exit 0
fi

mkdir -p "${TARGET_DIR}"

# Update symlink if missing or pointing somewhere else.
CURRENT="$(readlink "${TARGET}" 2>/dev/null || true)"
if [[ "${CURRENT}" != "${SOURCE}" ]]; then
  ln -sf "${SOURCE}" "${TARGET}"
fi

# Best-effort PATH check. If ~/.local/bin isn't on PATH, surface a hint
# (once per session) so the user can add it. Hint goes to stderr; doesn't
# block anything.
case ":${PATH}:" in
  *":${TARGET_DIR}:"*)
    ;;
  *)
    echo "[remotecodetrol] note: ${TARGET_DIR} is not on your PATH." >&2
    echo "[remotecodetrol] add this to ~/.zprofile (or shell init): export PATH=\"\${HOME}/.local/bin:\${PATH}\"" >&2
    echo "[remotecodetrol] or invoke directly: ${SOURCE}" >&2
    ;;
esac

exit 0
