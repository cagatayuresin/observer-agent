#!/usr/bin/env bash
set -euo pipefail

#───────────────────────────────────────────────────────────────────────────────
# 1) Parametreler
#───────────────────────────────────────────────────────────────────────────────
DEFAULT_AGENT_ID="$(hostname)"
read -rp "Agent ID [${DEFAULT_AGENT_ID}]: " AGENT_ID
AGENT_ID="${AGENT_ID:-$DEFAULT_AGENT_ID}"

DEFAULT_RMQ="amqp://guest:guest@rabbitmq:5672/"
read -rp "RabbitMQ URL [${DEFAULT_RMQ}]: " RABBITMQ_URL
RABBITMQ_URL="${RABBITMQ_URL:-$DEFAULT_RMQ}"

#───────────────────────────────────────────────────────────────────────────────
# 2) Dizini oluştur
#───────────────────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/observer-agent"
mkdir -p "$INSTALL_DIR"

#───────────────────────────────────────────────────────────────────────────────
# 3) Sistem paketleri
#───────────────────────────────────────────────────────────────────────────────
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl

# kubectl (K8s dışı host’ta bile pod metrikleri okunabilsin)
if ! command -v kubectl &>/dev/null; then
  curl -L --output /usr/local/bin/kubectl \
    https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl
  chmod +x /usr/local/bin/kubectl
fi

#───────────────────────────────────────────────────────────────────────────────
# 4) Kodları çek
#───────────────────────────────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone https://github.com/cagatayuresin/observer-agent.git "$INSTALL_DIR"
fi

#───────────────────────────────────────────────────────────────────────────────
# 5) Python venv
#───────────────────────────────────────────────────────────────────────────────
cd "$INSTALL_DIR"
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt

#───────────────────────────────────────────────────────────────────────────────
# 6) Kubeconfig otomatik algılama
#───────────────────────────────────────────────────────────────────────────────
KUBECONFIG_DEST=""
for CANDIDATE in \
  "$HOME/.kube/config" \
  "/etc/kubernetes/admin.conf" \
  "/etc/rancher/k3s/k3s.yaml"
do
  if [[ -f "$CANDIDATE" ]]; then
    KUBECONFIG_DEST="$INSTALL_DIR/cluster-admin.conf"
    cp "$CANDIDATE" "$KUBECONFIG_DEST"
    chmod 600 "$KUBECONFIG_DEST"
    echo "✅ Kubeconfig bulundu ve kopyalandı: $CANDIDATE → $KUBECONFIG_DEST"
    break
  fi
done

if [[ -z "$KUBECONFIG_DEST" ]]; then
  echo "⚠️  Kubernetes kubeconfig bulunamadı. Agent host-only modda çalışacak."
fi

#───────────────────────────────────────────────────────────────────────────────
# 7) .env dosyası
#───────────────────────────────────────────────────────────────────────────────
cat > .env <<EOF
RABBITMQ_URL=${RABBITMQ_URL}
METRICS_QUEUE=metrics
AGENT_ID=${AGENT_ID}
INTERVAL=30
$( [[ -n "$KUBECONFIG_DEST" ]] && echo "KUBECONFIG=${KUBECONFIG_DEST}" )
EOF

#───────────────────────────────────────────────────────────────────────────────
# 8) systemd servisi
#───────────────────────────────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/observer-agent.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Observer Agent
After=network.target

[Service]
Type=simple
EnvironmentFile=/opt/observer-agent/.env
Environment=PYTHONPATH=/opt/observer-agent
ExecStart=${INSTALL_DIR}/venv/bin/python -m app.agent
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo rm -f /opt/observer-agent/VERSION
echo "2.0.0" | sudo tee /opt/observer-agent/VERSION
systemctl daemon-reload
systemctl enable --now observer-agent

#───────────────────────────────────────────────────────────────────────────────
# 9) Bilgilendirme
#───────────────────────────────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Agent kuruldu ve başlatıldı."
echo "  • Agent ID     : ${AGENT_ID}"
echo "  • RabbitMQ URL : ${RABBITMQ_URL}"
if [[ -n "$KUBECONFIG_DEST" ]]; then
  echo "  • Kubeconfig   : ${KUBECONFIG_DEST}"
else
  echo "  • Kubeconfig   : bulunamadı (Kubernetes metrikleri toplanmayacak)"
fi
echo "  • Status       : $(systemctl is-active observer-agent)"
echo "Logları izlemek için: journalctl -u observer-agent -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
