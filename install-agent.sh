#!/usr/bin/env bash
set -euo pipefail

# 1) Parametreleri sor
DEFAULT_AGENT_ID="$(hostname)"
read -p "Agent ID [${DEFAULT_AGENT_ID}]: " AGENT_ID
AGENT_ID="${AGENT_ID:-$DEFAULT_AGENT_ID}"

DEFAULT_RMQ="amqp://guest:guest@rabbitmq:5672/"
read -p "RabbitMQ URL [${DEFAULT_RMQ}]: " RABBITMQ_URL
RABBITMQ_URL="${RABBITMQ_URL:-$DEFAULT_RMQ}"

# 2) Dizini oluştur
INSTALL_DIR="/opt/observer-agent"
if [ ! -d "$INSTALL_DIR" ]; then
  mkdir -p "$INSTALL_DIR"
fi

# 3) Gerekli paketleri yükle
apt-get update
apt-get install -y python3 python3-venv python3-pip git

# 4) Kodları çek (varsa güncelle)
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull
else
  git clone https://github.com/cagatayuresin/observer-agent.git "$INSTALL_DIR"
fi

# 5) Python sanal ortam ve bağımlılıklar
cd "$INSTALL_DIR"
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 6) .env dosyasını oluştur
cat > .env <<EOF
RABBITMQ_URL=${RABBITMQ_URL}
METRICS_QUEUE=metrics
AGENT_ID=${AGENT_ID}
INTERVAL=30
EOF

# 7) systemd servisini ayarla
SERVICE_FILE="/etc/systemd/system/observer-agent.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Observer Agent Service
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 8) Servisi başlat
systemctl daemon-reload
systemctl enable observer-agent
systemctl restart observer-agent

echo "Kurulum tamamlandı!"
echo "Agent ID: $AGENT_ID"
echo "RabbitMQ URL: $RABBITMQ_URL"
echo "Servis durumu: $(systemctl is-active observer-agent)"
echo "Loglar için: journalctl -u observer-agent -f"
echo "Kurulum dizini: $INSTALL_DIR"
echo "Observer Agent başarıyla kuruldu ve çalışıyor!"
echo "Lütfen RabbitMQ ve Observer Agent arayüzlerini kontrol edin."
echo "Herhangi bir sorunla karşılaşırsanız, lütfen logları kontrol edin."
echo "Kurulum tamamlandı!"
