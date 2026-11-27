#!/usr/bin/env bash
# PTBrush Linux 一键安装 / 升级脚本（原生二进制，不依赖 Docker）
# 用法：
#   sudo bash install.sh                 # 交互式，回车用默认值
#   sudo bash install.sh --yes           # 全默认，一键安装
#   sudo bash install.sh --port 8080 --data /srv/ptbrush --yes
#   sudo bash install.sh --asset ./ptbrush-linux-amd64.tar.gz --yes   # 离线包
set -euo pipefail

REPO="${PTBRUSH_REPO:-xinya585/ptbrush}"
PREFIX="${PTBRUSH_PREFIX:-/opt/ptbrush}"
UNIT="/etc/systemd/system/ptbrush.service"
ENVFILE="/etc/default/ptbrush"
BINLINK="/usr/local/bin/ptbrush"
PORT="44445"
DATA="/var/lib/ptbrush"
ASSET=""
YES=0
NO_START=0
FORCE=0

log()  { printf '\033[36m[PTBrush]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[PTBrush]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[PTBrush]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：
  sudo bash install.sh                    交互式安装（回车用默认值）
  sudo bash install.sh --yes              全默认一键安装
  sudo bash install.sh --port 8080 --data /srv/ptbrush --yes
  sudo bash install.sh --asset ./ptbrush-linux-amd64.tar.gz --yes   离线安装
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --data) DATA="$2"; shift 2 ;;
        --asset) ASSET="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --yes|-y) YES=1; shift ;;
        --no-start) NO_START=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage ;;
        *) die "未知参数：$1（用 --help 查看）" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "需要 root：请用 sudo bash install.sh"

# 已装过就沿用原来的端口与数据目录，升级不丢配置
if [ -f "$ENVFILE" ]; then
    # shellcheck disable=SC1090
    . "$ENVFILE" || true
    PORT="${PTBRUSH_PORT:-$PORT}"
    DATA="${PTBRUSH_DATA:-$DATA}"
fi

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) die "暂不支持 $ARCH_RAW，目前只提供 amd64 / arm64" ;;
esac

if [ -t 0 ] && [ "$YES" -eq 0 ]; then
    printf '安装目录：%s\n数据目录：%s\n' "$PREFIX" "$DATA"
    read -r -p "Web 端口 [$PORT]: " input_port || true
    [ -n "${input_port:-}" ] && PORT="$input_port"
    read -r -p "数据目录 [$DATA]: " input_data || true
    [ -n "${input_data:-}" ] && DATA="$input_data"
fi

case "$PORT" in
    ''|*[!0-9]*) die "端口必须是数字：$PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围：$PORT"

if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$PORT$"; then
    [ "$FORCE" -eq 1 ] || die "端口 $PORT 已被占用，换一个：--port 44446"
fi

command -v systemctl >/dev/null 2>&1 || die "没有 systemd，无法注册开机自启；请用 tar 包手动运行"
command -v curl >/dev/null 2>&1 || die "需要 curl，请先安装：apt install curl"
command -v tar >/dev/null 2>&1 || die "需要 tar"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -n "$ASSET" ]; then
    [ -f "$ASSET" ] || die "找不到离线包：$ASSET"
    pkg="$ASSET"
    log "使用离线包：$pkg"
else
    asset="ptbrush-linux-${ARCH}.tar.gz"
    url="https://github.com/${REPO}/releases/latest/download/${asset}"
    log "下载最新安装包：$url"
    curl -fL --retry 3 --connect-timeout 15 -o "$TMP/$asset" "$url" || die "下载失败，检查网络或改用 --asset 离线安装"
    pkg="$TMP/$asset"
    if curl -fL --retry 2 -o "$TMP/$asset.sha256" "${url}.sha256" 2>/dev/null; then
        ( cd "$TMP" && sha256sum -c "$asset.sha256" ) || die "校验失败，安装包可能损坏"
    else
        warn "没有校验文件，跳过 sha256 校验"
    fi
fi

log "解包..."
tar -xzf "$pkg" -C "$TMP" || die "解包失败"
APPDIR="$TMP/ptbrush"
[ -x "$APPDIR/ptbrush" ] || die "安装包结构不对：找不到 ptbrush 可执行文件"

if ! id ptbrush >/dev/null 2>&1; then
    log "创建系统用户 ptbrush"
    useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin ptbrush 2>/dev/null \
        || useradd -r -d "$DATA" -s /usr/sbin/nologin ptbrush
fi

install -d -m 755 "$PREFIX" "$DATA"
systemctl stop ptbrush >/dev/null 2>&1 || true

log "安装程序到 $PREFIX/app"
rm -rf "$PREFIX/app.new" "$PREFIX/app.prev"
cp -a "$APPDIR" "$PREFIX/app.new"
if [ -d "$PREFIX/app" ]; then mv "$PREFIX/app" "$PREFIX/app.prev"; fi
mv "$PREFIX/app.new" "$PREFIX/app"
if [ -f "$APPDIR/install.sh" ]; then install -m 755 "$APPDIR/install.sh" "$PREFIX/install.sh"; fi
if [ -f "$APPDIR/menu.sh" ]; then install -m 755 "$APPDIR/menu.sh" "$BINLINK"; fi
chmod 755 "$PREFIX/app/ptbrush"
chown -R ptbrush:ptbrush "$DATA"

cat > "$ENVFILE" <<EOF
PTBRUSH_PORT=$PORT
PTBRUSH_DATA=$DATA
EOF
chmod 644 "$ENVFILE"

cat > "$UNIT" <<'EOF'
[Unit]
Description=PTBrush - PT 站点刷流工具
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ptbrush
Group=ptbrush
WorkingDirectory=/opt/ptbrush/app
EnvironmentFile=-/etc/default/ptbrush
ExecStart=/opt/ptbrush/app/ptbrush --port ${PTBRUSH_PORT} --data ${PTBRUSH_DATA}
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ptbrush >/dev/null 2>&1
if [ "$NO_START" -eq 0 ]; then
    log "启动服务"
    systemctl restart ptbrush
    for i in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:${PORT}/api/version" >/dev/null 2>&1; then break; fi
        sleep 1
        [ "$i" -eq 30 ] && warn "30 秒内没等到服务就绪，看日志：journalctl -u ptbrush -n 50"
    done
else
    warn "已跳过启动（--no-start）"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="<本机IP>"
cat <<EOF

安装完成
  访问地址：http://${IP}:${PORT}
  数据目录：${DATA}
  管理菜单：sudo ptbrush
  查看日志：journalctl -u ptbrush -f
  systemd ：systemctl status ptbrush

EOF