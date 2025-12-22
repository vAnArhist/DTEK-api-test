#!/usr/bin/env bash
set -euo pipefail

ServiceName="dtek-api"
UnitPath="/etc/systemd/system/${ServiceName}.service"
EnvPath="/etc/default/${ServiceName}"

RepoDir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UserName="${SUDO_USER:-$USER}"

PythonPath="${RepoDir}/.venv/bin/python"
ScriptPath="${RepoDir}/bot.py"
TemplatePath="${RepoDir}/systemd/dtek-api.service.template"

need_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 $*"
    exit 1
  fi
}

render_unit() {
  sed \
    -e "s|{{USER}}|${UserName}|g" \
    -e "s|{{WORKDIR}}|${RepoDir}|g" \
    -e "s|{{ENVFILE}}|${EnvPath}|g" \
    -e "s|{{PYTHON}}|${PythonPath}|g" \
    -e "s|{{SCRIPT}}|${ScriptPath}|g" \
    "${TemplatePath}"
}

cmd_install() {
  need_root

  if [[ ! -x "${PythonPath}" ]]; then
    echo "ERROR: venv python not found/executable: ${PythonPath}"
    echo "Create venv: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 1
  fi

  if [[ ! -f "${ScriptPath}" ]]; then
    echo "ERROR: bot script not found: ${ScriptPath}"
    exit 1
  fi

  # Token: take from BOT_TOKEN env or ask
  if [[ -z "${BOT_TOKEN:-}" ]]; then
    read -r -s -p "Enter BOT_TOKEN: " BOT_TOKEN
    echo
  fi

  # Write env file (root-only readable)
  umask 077
  cat > "${EnvPath}" <<EOF
BOT_TOKEN=${BOT_TOKEN}
EOF
  chmod 600 "${EnvPath}"
  chown root:root "${EnvPath}"

  # Write unit
  render_unit > "${UnitPath}"
  chmod 644 "${UnitPath}"

  systemctl daemon-reload
  systemctl enable --now "${ServiceName}.service"

  echo "Installed and started: ${ServiceName}"
  systemctl --no-pager --full status "${ServiceName}.service" || true
}

cmd_restart() {
  need_root
  systemctl restart "${ServiceName}.service"
  systemctl --no-pager --full status "${ServiceName}.service" || true
}

cmd_stop() {
  need_root
  systemctl stop "${ServiceName}.service"
}

cmd_status() {
  systemctl --no-pager --full status "${ServiceName}.service" || true
}

cmd_logs() {
  journalctl -u "${ServiceName}.service" -f
}

cmd_uninstall() {
  need_root
  systemctl disable --now "${ServiceName}.service" || true
  rm -f "${UnitPath}"
  systemctl daemon-reload
  echo "Removed unit: ${UnitPath}"

  if [[ -f "${EnvPath}" ]]; then
    rm -f "${EnvPath}"
    echo "Removed env: ${EnvPath}"
  fi
}

case "${1:-}" in
  install)   cmd_install ;;
  restart)   cmd_restart ;;
  stop)      cmd_stop ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  uninstall) cmd_uninstall ;;
  *)
    echo "Usage: $0 {install|restart|stop|status|logs|uninstall}"
    echo
    echo "Install example:"
    echo "  BOT_TOKEN='xxx' sudo $0 install"
    exit 1
    ;;
esac
