#!/usr/bin/env bash
# Ship v2 to FA LXC 124 next to v1 and restart only mobile-rpa-v2. v1 (/opt/mobile-rpa, port 8090) is not touched.
set -euo pipefail
NODE=root@192.168.100.20
KEY=~/.ssh/id_root
CT=124
cd "$(dirname "$0")/.."
tar czf /tmp/mobile-rpa-v2.tgz --exclude=.venv --exclude=data --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=docs --exclude=.superpowers .
scp -q -i $KEY /tmp/mobile-rpa-v2.tgz deploy/mobile-rpa-v2.service $NODE:/tmp/
ssh -i $KEY $NODE "pct push $CT /tmp/mobile-rpa-v2.tgz /tmp/mobile-rpa-v2.tgz && pct push $CT /tmp/mobile-rpa-v2.service /etc/systemd/system/mobile-rpa-v2.service && pct exec $CT -- bash -c '
  set -e; export PATH=/usr/local/bin:\$PATH
  mkdir -p /opt/mobile-rpa-v2 /var/lib/mobile-rpa-v2 && chmod 700 /var/lib/mobile-rpa-v2
  tar xzf /tmp/mobile-rpa-v2.tgz -C /opt/mobile-rpa-v2
  cd /opt/mobile-rpa-v2
  [ -d .venv ] || uv venv -q -p 3.13 .venv
  uv pip install -q -p .venv -e .
  systemctl daemon-reload && systemctl enable -q mobile-rpa-v2 && systemctl restart mobile-rpa-v2
  sleep 3 && systemctl is-active mobile-rpa-v2 && curl -fsS -o /dev/null -w \"http %{http_code}\n\" http://127.0.0.1:8091/
'"
