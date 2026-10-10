#!/bin/bash
set -e

# ============ Autovisor Web 一键部署脚本 ============
# 适用系统: Ubuntu 22.04 / 24.04 LTS (x86_64)
# 用法: sudo bash deploy/install.sh
# ====================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# --- 检查 root ---
if [ "$EUID" -ne 0 ]; then
    error "请用 root 运行: sudo bash deploy/install.sh"
fi

# --- 配置变量 ---
INSTALL_DIR="/opt/Nlauto"
WEB_DIR="${INSTALL_DIR}/web"
VENV_DIR="${INSTALL_DIR}/venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
MAX_CONCURRENT="${MAX_CONCURRENT:-3}"
SECRET_KEY="${SECRET_KEY:-$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)}"
SWAP_SIZE="${SWAP_SIZE:-4G}"

info "安装目录: ${INSTALL_DIR}"
info "最大并发: ${MAX_CONCURRENT}"

# --- 1. 安装系统依赖 ---
info "安装系统依赖..."
apt-get update -qq
apt-get install -y \
    python3 python3-venv python3-pip \
    xvfb \
    nginx \
    fonts-liberation fonts-noto-cjk \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libxfixes3

info "系统依赖安装完成"

# --- 2. 配置 Swap ---
MEM_TOTAL=$(free -g | awk '/^Mem:/{print $2}')
if [ "${MEM_TOTAL}" -lt 4 ]; then
    if ! swapon --show | grep -q '/swapfile'; then
        info "内存 ${MEM_TOTAL}G < 4G，创建 ${SWAP_SIZE} swap..."
        fallocate -l "${SWAP_SIZE}" /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
        swapon /swapfile
        if ! grep -q '/swapfile' /etc/fstab; then
            echo '/swapfile none swap sw 0 0' >> /etc/fstab
        fi
        info "Swap 创建完成"
    else
        info "Swap 已存在，跳过"
    fi
else
    info "内存 ${MEM_TOTAL}G >= 4G，无需 swap"
fi

# --- 3. 确定项目源 ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

if [ -f "${PROJECT_DIR}/Autovisor.py" ]; then
    info "从本地目录部署: ${PROJECT_DIR}"
    if [ "${PROJECT_DIR}" != "${INSTALL_DIR}" ]; then
        info "复制项目到 ${INSTALL_DIR}..."
        mkdir -p "${INSTALL_DIR}"
        rsync -a --exclude='venv' --exclude='__pycache__' \
              --exclude='data' --exclude='logs' \
              --exclude='.git' --exclude='*.rar' \
              --exclude='.codeartsdoer' --exclude='.merkle-snapshot.json' \
              "${PROJECT_DIR}/" "${INSTALL_DIR}/"
    fi
else
    if [ -d "${INSTALL_DIR}/.git" ]; then
        info "从 git 更新项目..."
        cd "${INSTALL_DIR}" && git pull
    else
        error "未找到项目文件。请先 git clone 到 ${INSTALL_DIR} 或在项目目录内运行此脚本"
    fi
fi

mkdir -p "${INSTALL_DIR}/data" "${INSTALL_DIR}/logs"

# --- 4. Python 虚拟环境 ---
info "创建 Python 虚拟环境..."
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
fi

info "安装 Python 依赖..."
${VENV_PYTHON} -m pip install --upgrade pip -q
${VENV_PYTHON} -m pip install -q \
    httpx pillow playwright requests \
    numpy opencv-python-headless \
    flask flask-socketio eventlet \
    openai

info "Python 依赖安装完成"

# --- 5. Playwright Chromium ---
info "安装 Playwright Chromium..."
${VENV_DIR}/bin/playwright install chromium
${VENV_DIR}/bin/playwright install-deps chromium 2>/dev/null || true
info "Playwright 安装完成"

# --- 6. 下载 socket.io.min.js ---
SOCKETIO_PATH="${WEB_DIR}/static/socket.io.min.js"
if [ ! -f "${SOCKETIO_PATH}" ]; then
    info "下载 socket.io.min.js..."
    curl -sL -o "${SOCKETIO_PATH}" \
        "https://cdn.socket.io/4.7.5/socket.io.min.js" \
        || wget -q -O "${SOCKETIO_PATH}" \
        "https://cdn.socket.io/4.7.5/socket.io.min.js"
    if [ ! -s "${SOCKETIO_PATH}" ]; then
        warn "socket.io 下载失败，请手动下载放到 ${SOCKETIO_PATH}"
    fi
fi

# --- 7. systemd 服务 ---
info "配置 systemd 服务..."

# Xvfb
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

# Autovisor Web
cat > /etc/systemd/system/autovisor-web.service << EOF
[Unit]
Description=Autovisor Web Service
After=xvfb.service network.target
Requires=xvfb.service

[Service]
WorkingDirectory=${WEB_DIR}
Environment=DISPLAY=:99
Environment=MAX_CONCURRENT=${MAX_CONCURRENT}
Environment=SECRET_KEY=${SECRET_KEY}
ExecStart=${VENV_PYTHON} app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable xvfb autovisor-web

info "systemd 服务配置完成"

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
nginx -t 2>/dev/null
systemctl enable nginx
systemctl restart nginx

info "Nginx 配置完成"

# --- 9. 启动服务 ---
info "启动服务..."
systemctl restart xvfb
sleep 1
systemctl restart autovisor-web
sleep 2

# --- 10. 状态检查 ---
echo ""
echo "=========================================="
echo -e "${GREEN} 部署完成!${NC}"
echo "=========================================="
echo ""
echo "服务状态:"

for svc in xvfb autovisor-web nginx; do
    if systemctl is-active --quiet "$svc"; then
        echo -e "  ${GREEN}✓${NC} $svc: running"
    else
        echo -e "  ${RED}✗${NC} $svc: failed"
    fi
done

echo ""
echo "配置信息:"
echo "  安装目录:  ${INSTALL_DIR}"
echo "  并发数:    ${MAX_CONCURRENT}"
echo "  虚拟环境:  ${VENV_DIR}"
echo "  数据目录:  ${INSTALL_DIR}/data"
echo "  日志目录:  ${INSTALL_DIR}/logs"
echo ""
echo "访问地址:"
SERVER_IP=$(hostname -I | awk '{print $1}')
echo "  http://${SERVER_IP}"
echo ""
echo "常用命令:"
echo "  查看日志:   journalctl -u autovisor-web -f"
echo "  重启服务:   systemctl restart autovisor-web"
echo "  修改并发:   systemctl edit autovisor-web"
echo "=========================================="
