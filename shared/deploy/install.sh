#!/bin/bash

# ============ Autovisor Shared 一键部署脚本 ============
# 共享浏览器模式，支持 10-15 并发
# 用法: sudo bash shared/deploy/install.sh
# ====================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

if [ "$EUID" -ne 0 ]; then
    error "请用 root 运行: sudo bash shared/deploy/install.sh"
fi

INSTALL_DIR="/opt/Nlauto/shared"
WEB_DIR="${INSTALL_DIR}/web"
VENV_DIR="${INSTALL_DIR}/venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
MAX_CONCURRENT="${MAX_CONCURRENT:-10}"
SECRET_KEY="${SECRET_KEY:-$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)}"
SWAP_SIZE="${SWAP_SIZE:-4G}"

info "安装目录: ${INSTALL_DIR}"
info "最大并发: ${MAX_CONCURRENT}"

# --- 1. 系统依赖 ---
info "安装系统依赖..."
while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
    info "等待 dpkg 锁释放..."
    sleep 5
done
apt-get update || error "apt-get update 失败"
apt-get install -y \
    python3 python3-venv python3-pip \
    xvfb nginx dbus-x11 \
    fonts-liberation fonts-noto-cjk \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libxfixes3 \
    || error "apt-get install 失败"
info "系统依赖安装完成"

# --- 2. Swap ---
MEM_TOTAL=$(free -g | awk '/^Mem:/{print $2}')
if [ "${MEM_TOTAL}" -lt 4 ]; then
    if ! swapon --show | grep -q '/swapfile'; then
        info "创建 ${SWAP_SIZE} swap..."
        fallocate -l "${SWAP_SIZE}" /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
        swapon /swapfile
        grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
fi

# --- 3. 项目文件 ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

if [ -f "${PROJECT_DIR}/Autovisor_shared.py" ]; then
    if [ "${PROJECT_DIR}" != "${INSTALL_DIR}" ]; then
        info "复制项目到 ${INSTALL_DIR}..."
        mkdir -p "${INSTALL_DIR}"
        rsync -a --exclude='venv' --exclude='__pycache__' \
              --exclude='data' --exclude='logs' \
              --exclude='.git' --exclude='*.rar' \
              "${PROJECT_DIR}/" "${INSTALL_DIR}/"
    fi
fi
mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/logs"

# --- 4. Python venv ---
info "创建 Python 虚拟环境..."
[ ! -d "${VENV_DIR}" ] && python3 -m venv "${VENV_DIR}"
${VENV_PYTHON} -m pip install --upgrade pip -q
${VENV_PYTHON} -m pip install -q \
    httpx pillow playwright requests \
    numpy opencv-python-headless \
    flask flask-socketio eventlet \
    openai
info "Python 依赖安装完成"

# --- 5. Playwright ---
info "安装 Playwright Chromium..."
${VENV_DIR}/bin/playwright install chromium
${VENV_DIR}/bin/playwright install-deps chromium 2>/dev/null || true

# --- 6. socket.io ---
SOCKETIO_PATH="${WEB_DIR}/static/socket.io.min.js"
if [ ! -f "${SOCKETIO_PATH}" ]; then
    info "下载 socket.io.min.js..."
    curl -sL -o "${SOCKETIO_PATH}" "https://cdn.socket.io/4.7.5/socket.io.min.js" \
        || wget -q -O "${SOCKETIO_PATH}" "https://cdn.socket.io/4.7.5/socket.io.min.js"
fi

# --- 7. systemd 服务 ---
info "配置 systemd 服务..."

cat > /etc/systemd/system/xvfb.service << 'EOF'
[Unit]
Description=Xvfb Virtual Framebuffer (:99)
After=network.target
[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/chromium-shared.service << EOF
[Unit]
Description=Shared Chromium for Autovisor (CDP :9222)
After=xvfb.service network.target
Requires=xvfb.service
[Service]
Environment=DISPLAY=:99
ExecStart=${VENV_PYTHON} -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=False,args=['--no-sandbox','--start-maximized','--window-size=1920,1080','--remote-debugging-port=9222','--remote-allow-origins=*','--disable-gpu','--disable-dev-shm-usage']); print('Chromium ready on :9222'); b.wait_for_event('close',timeout=0)"
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/autovisor-web-shared.service << EOF
[Unit]
Description=Autovisor Web Service (Shared Browser Mode)
After=chromium-shared.service xvfb.service network.target
Requires=chromium-shared.service xvfb.service
[Service]
WorkingDirectory=${WEB_DIR}
Environment=DISPLAY=:99
Environment=MAX_CONCURRENT=${MAX_CONCURRENT}
Environment=SECRET_KEY=${SECRET_KEY}
Environment=AUTOVISOR_CDP_URL=http://localhost:9222
ExecStart=${VENV_PYTHON} app.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable xvfb chromium-shared autovisor-web-shared

# --- 8. Nginx ---
info "配置 Nginx..."
cat > /etc/nginx/sites-available/autovisor << 'EOF'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
    location /socket.io/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_read_timeout 86400;
    }
}
EOF
ln -sf /etc/nginx/sites-available/autovisor /etc/nginx/sites-enabled/autovisor
rm -f /etc/nginx/sites-enabled/default
nginx -t 2>/dev/null && systemctl enable nginx && systemctl restart nginx

# --- 9. 启动 ---
info "启动服务..."
systemctl restart xvfb
sleep 1
systemctl restart chromium-shared
sleep 3
systemctl restart autovisor-web-shared
sleep 2

# --- 10. 状态 ---
echo ""
echo "=========================================="
echo -e "${GREEN} 共享模式部署完成!${NC}"
echo "=========================================="
for svc in xvfb chromium-shared autovisor-web-shared nginx; do
    if systemctl is-active --quiet "$svc"; then
        echo -e "  ${GREEN}✓${NC} $svc: running"
    else
        echo -e "  ${RED}✗${NC} $svc: failed"
    fi
done
echo ""
echo "  并发数:    ${MAX_CONCURRENT} (共享浏览器)"
echo "  安装目录:  ${INSTALL_DIR}"
echo ""
SERVER_IP=$(hostname -I | awk '{print $1}')
echo "  访问: http://${SERVER_IP}"
echo "=========================================="
