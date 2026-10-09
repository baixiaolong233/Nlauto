#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }

INSTALL_DIR="/opt/Auto"

info "停止服务..."
systemctl stop autovisor-web xvfb 2>/dev/null || true
systemctl disable autovisor-web xvfb 2>/dev/null || true

info "移除 systemd 服务..."
rm -f /etc/systemd/system/xvfb.service
rm -f /etc/systemd/system/autovisor-web.service
rm -rf /etc/systemd/system/autovisor-web.service.d
systemctl daemon-reload

info "移除 Nginx 配置..."
rm -f /etc/nginx/sites-enabled/autovisor
rm -f /etc/nginx/sites-available/autovisor
systemctl reload nginx 2>/dev/null || true

read -p "是否删除项目目录 ${INSTALL_DIR}? (y/N) " -r
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "${INSTALL_DIR}"
    info "项目目录已删除"
fi

read -p "是否删除 swap /swapfile? (y/N) " -r
if [[ $REPLY =~ ^[Yy]$ ]]; then
    swapoff /swapfile 2>/dev/null || true
    rm -f /swapfile
    sed -i '/\/swapfile/d' /etc/fstab
    info "Swap 已删除"
fi

info "卸载完成"
