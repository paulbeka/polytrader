#!/bin/sh
# Repeatable host setup. Application data and existing configuration are preserved.
set -eu
root=/srv/polytrader
install_dependencies=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) root=$2; shift 2 ;;
    --install-dependencies) install_dependencies=true; shift ;;
    *) echo "Usage: sudo sh deploy/bootstrap.sh [--root /srv/polytrader] [--install-dependencies]" >&2; exit 1 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "Run bootstrap with sudo." >&2; exit 1; }
case "$root" in /*) ;; *) echo "Root must be absolute" >&2; exit 1 ;; esac
case "$root" in *[!a-zA-Z0-9/_-]*|/|*/../*|*/..) echo "Use a simple absolute directory path" >&2; exit 1 ;; esac
root=$(realpath -m -- "$root")
[ "$root" != / ] || { echo "Cannot use the filesystem root" >&2; exit 1; }
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || {
  echo "Bootstrap currently supports Ubuntu 24.04; use the manual guide elsewhere." >&2; exit 1;
}
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$install_dependencies" = true ]; then
  apt-get update
  apt-get install -y ca-certificates curl git python3 python3-venv
  if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
      "$(dpkg --print-architecture)" "$VERSION_CODENAME" > /etc/apt/sources.list.d/polytrader-docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
docker compose version --short | python3 -c 'import sys; v=sys.stdin.read().strip().lstrip("v"); assert tuple(map(int,v.split(".")[:2])) >= (2,24), "Compose 2.24+ required"'
systemctl enable --now docker
docker info >/dev/null
mkdir -p "$root"
chmod 755 "$root"
for directory in configs releases locks catalog platform run control; do
  [ -d "$root/$directory" ] || install -d -m 755 "$root/$directory"
done
chmod 700 "$root/control"
for directory in data data/ops; do
  [ -d "$root/$directory" ] || install -d -m 755 -o 10001 -g 10001 "$root/$directory"
done
# Socket membership is numeric; it need not create a conflicting host UID/GID.
chown 0:10001 "$root/run"
chmod 750 "$root/run"
if systemctl is-active --quiet polytrader-controller; then
  echo "Waiting for the current management operation to finish before updating the host helper."
  systemctl stop polytrader-controller
  trap 'systemctl start polytrader-controller' EXIT
fi
python3 -m venv "$root/host-venv"
"$root/host-venv/bin/pip" install --no-deps "$repo"
install -m 644 "$repo/deploy/compose.yaml" "$root/platform/compose.yaml"
if [ ! -f "$root/platform/.env" ]; then
  install -m 600 /dev/null "$root/platform/.env"
fi
printf '#!/bin/sh\nexport POLYTRADER_ROOT="%s"\nexec "%s/host-venv/bin/python" -m polytrader.ops.control_cli "$@"\n' \
  "$root" "$root" > /usr/local/bin/polytraderctl
chmod 755 /usr/local/bin/polytraderctl
cat > /etc/systemd/system/polytrader-controller.service <<EOF
[Unit]
Description=Polytrader private bot controller
After=docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=$root/host-venv/bin/python -m polytrader.ops.controller --root $root
Restart=on-failure
RestartSec=5
UMask=0022
TimeoutStopSec=1200

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now polytrader-controller
trap - EXIT
echo "Host helper installed. Configs: $root/configs; data: $root/data"
echo "Next: sudo polytraderctl init --release-file ./release.json"
echo "Then add reviewed bot configs, run doctor, platform up, and deploy --all."
echo "Dashboard: http://127.0.0.1:8501 (after platform up). Use private Tailscale Serve for remote access."
