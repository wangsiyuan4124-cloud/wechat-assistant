#!/bin/bash
# 可选：在自建服务器上部署 frps（仅当你不使用「本机出站轮询」模式时需要）
# 用法：编辑下方占位符后执行 bash deploy-frps.example.sh
set -e

FRP_VERSION=0.61.1
FRP_TOKEN="<REPLACE_WITH_YOUR_FRP_TOKEN>"
BIND_PORT=7000

cd /opt
if [ ! -d "frp_${FRP_VERSION}_linux_amd64" ]; then
  wget -q "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz" \
    || wget -q "https://ghfast.top/https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz"
  tar -xzf "frp_${FRP_VERSION}_linux_amd64.tar.gz"
fi

cat > "frp_${FRP_VERSION}_linux_amd64/frps.toml" <<EOF
bindPort = ${BIND_PORT}
auth.token = "${FRP_TOKEN}"
EOF

cat > /etc/systemd/system/frps.service <<EOF
[Unit]
Description=frp server
After=network.target

[Service]
Type=simple
ExecStart=/opt/frp_${FRP_VERSION}_linux_amd64/frps -c /opt/frp_${FRP_VERSION}_linux_amd64/frps.toml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable frps
systemctl restart frps
sleep 2
systemctl status frps --no-pager | head -5
echo "OK: frps 已启动。请在云服务器安全组放行 TCP ${BIND_PORT} 和 8787 端口"
