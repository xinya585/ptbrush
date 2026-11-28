#!/usr/bin/env bash
# PTBrush 一键管理菜单。安装后可直接运行：sudo ptbrush
set -uo pipefail

ENVFILE="/etc/default/ptbrush"
PREFIX="/opt/ptbrush"
UNIT="/etc/systemd/system/ptbrush.service"
BINLINK="/usr/local/bin/ptbrush"
BACKUP_DIR="/var/backups/ptbrush"
PORT="44445"
DATA="/var/lib/ptbrush"

QB_VERSION="4.3.9"
QB_RELEASE="release-4.3.9_v1.2.15"
QB_BIN="/usr/local/bin/qbittorrent-nox"
QB_UNIT="/etc/systemd/system/qbittorrent-nox.service"
QB_ENVFILE="/etc/ptbrush-qbittorrent.env"
QB_DATA="/var/lib/qbittorrent"
QB_SERVICE="qbittorrent-nox"
QB_OS_USER="qbittorrent"

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$@"
fi

if [ -f "$ENVFILE" ]; then
    # shellcheck disable=SC1090
    . "$ENVFILE" || true
    PORT="${PTBRUSH_PORT:-$PORT}"
    DATA="${PTBRUSH_DATA:-$DATA}"
fi

c() { printf '\033[36m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
err() { printf '\033[31m%s\033[0m\n' "$*"; }
pause() { read -r -p "按回车继续..." _ || true; }

svc_active() { systemctl is-active ptbrush 2>/dev/null || true; }
local_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }
app_version() { curl -fsS "http://127.0.0.1:${PORT}/api/version" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'; }
health() {
    for _ in $(seq 1 20); do
        if curl -fsS "http://127.0.0.1:${PORT}/api/version" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    return 1
}

show_status() {
    c "PTBrush 状态"
    printf '  服务   : %s\n' "$(svc_active)"
    printf '  端口   : %s\n' "$PORT"
    printf '  地址   : http://%s:%s\n' "$(local_ip)" "$PORT"
    printf '  数据   : %s（%s）\n' "$DATA" "$(du -sh "$DATA" 2>/dev/null | awk '{print $1}')"
    printf '  程序   : %s\n' "$PREFIX/app"
    local ver; ver="$(app_version)"; [ -n "$ver" ] || ver="服务未响应"
    printf '  版本   : %s\n' "$ver"
    printf '  开机自启: %s\n' "$(systemctl is-enabled ptbrush 2>/dev/null || true)"
    if [ -f "$QB_ENVFILE" ]; then
        local qb_port
        qb_port="$(sed -n 's/^QB_WEBUI_PORT=//p' "$QB_ENVFILE" | tail -n 1)"
        [ -n "$qb_port" ] || qb_port="8080"
        printf '  qBittorrent: %s  http://%s:%s\n' "$(systemctl is-active "$QB_SERVICE" 2>/dev/null || true)" "$(local_ip)" "$qb_port"
    fi
}

do_start()   { systemctl start ptbrush; ok "已启动"; }
do_stop()    { systemctl stop ptbrush; ok "已停止"; }
do_restart() { systemctl restart ptbrush; health && ok "已重启，服务正常" || warn "已重启，但健康检查没通过"; }

do_upgrade() {
    [ -f "$PREFIX/install.sh" ] || { err "找不到 $PREFIX/install.sh，请重新跑一次安装脚本"; return; }
    warn "将从 GitHub 拉取最新版本，数据目录不动"
    bash "$PREFIX/install.sh" --yes || { err "升级失败"; return; }
    ok "升级完成，当前版本 $(app_version)"
}

do_port() {
    read -r -p "新端口（当前 $PORT）: " np || true
    case "${np:-}" in ''|*[!0-9]*) err "端口无效"; return ;; esac
    [ "$np" -ge 1 ] && [ "$np" -le 65535 ] || { err "端口超出范围"; return; }
    local ports
    ports="$(ss -ltn 2>/dev/null | awk '{print $4}' || true)"
    if grep -q ":$np$" <<< "$ports"; then err "端口 $np 已被占用"; return; fi
    sed -i "s/^PTBRUSH_PORT=.*/PTBRUSH_PORT=$np/" "$ENVFILE"
    PORT="$np"
    systemctl restart ptbrush
    health && ok "端口已改为 $PORT" || warn "已改端口，但服务没起来，检查日志"
}

do_backup() {
    mkdir -p "$BACKUP_DIR"
    local f="$BACKUP_DIR/ptbrush-data-$(date +%F-%H%M%S).tar.gz"
    systemctl stop ptbrush >/dev/null 2>&1 || true
    if tar -czf "$f" -C "$(dirname "$DATA")" "$(basename "$DATA")"; then
        ok "备份完成：$f"
    else
        err "备份失败"
    fi
    systemctl start ptbrush >/dev/null 2>&1 || true
}

do_restore() {
    local files=()
    while IFS= read -r line; do files+=("$line"); done < <(ls -1t "$BACKUP_DIR"/*.tar.gz 2>/dev/null)
    [ "${#files[@]}" -gt 0 ] || { err "没有备份文件"; return; }
    c "可用备份："
    local i=1
    for f in "${files[@]}"; do printf '  %d) %s\n' "$i" "$(basename "$f")"; i=$((i+1)); done
    read -r -p "选择编号（回车取消）: " idx || true
    case "${idx:-}" in ''|*[!0-9]*) return ;; esac
    [ "$idx" -ge 1 ] && [ "$idx" -le "${#files[@]}" ] || { err "编号无效"; return; }
    local f="${files[$((idx-1))]}"
    warn "将用 $f 覆盖当前数据目录，先自动备份当前数据"
    read -r -p "确认恢复？输入 yes: " ans || true
    [ "${ans:-}" = "yes" ] || return
    systemctl stop ptbrush >/dev/null 2>&1 || true
    mkdir -p "$BACKUP_DIR"
    tar -czf "$BACKUP_DIR/before-restore-$(date +%F-%H%M%S).tar.gz" -C "$(dirname "$DATA")" "$(basename "$DATA")" 2>/dev/null || true
    find "$DATA" -mindepth 1 -delete
    tar -xzf "$f" -C "$(dirname "$DATA")"
    chown -R ptbrush:ptbrush "$DATA"
    systemctl start ptbrush
    health && ok "恢复完成" || warn "已恢复，但服务没起来"
}

do_uninstall() {
    warn "将停止并删除程序与 systemd 服务"
    read -r -p "确认卸载？输入 yes: " ans || true
    [ "${ans:-}" = "yes" ] || return
    systemctl stop ptbrush >/dev/null 2>&1 || true
    systemctl disable ptbrush >/dev/null 2>&1 || true
    rm -f "$UNIT"; systemctl daemon-reload
    rm -rf "$PREFIX"
    rm -f "$ENVFILE"
    read -r -p "同时删除数据目录 $DATA ？[y/N]: " purge || true
    if [ "${purge:-}" = "y" ] || [ "${purge:-}" = "Y" ]; then
        rm -rf "$DATA"
        warn "数据已删除"
    else
        ok "数据保留在 $DATA"
    fi
    rm -f "$BINLINK"
    ok "已卸载"
}

do_logs() { journalctl -u ptbrush -n 80 --no-pager 2>/dev/null || tail -n 80 "$DATA/ptbrush.log"; }

qb_random_password() {
    head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-16
}

qb_new_password() {
    local p
    p="$(qb_random_password)"
    [ "${#p}" -eq 16 ] || return 1
    printf '%s\n' "$p"
}

qb_port_in_use() {
    local p="$1" out
    command -v ss >/dev/null 2>&1 || return 1
    out="$(ss -ltn 2>/dev/null | awk '{print $4}' || true)"
    grep -q ":${p}$" <<< "$out"
}

qb_pick_port() {
    local p
    for p in $(seq 8080 8100); do
        qb_port_in_use "$p" || { printf '%s\n' "$p"; return 0; }
    done
    return 1
}

qb_api_login() {
    local pass="$1" jar="$2" out="$3"
    rm -f "$jar"
    curl -sS -c "$jar" -H "Referer: http://127.0.0.1:${QB_PORT}" \
        --data-urlencode "username=${QB_USERNAME}" \
        --data-urlencode "password=${pass}" \
        "http://127.0.0.1:${QB_PORT}/api/v2/auth/login" > "$out" 2>/dev/null || return 1
    grep -qx 'Ok.' "$out"
}

qb_wait() {
    local i
    for i in $(seq 1 30); do
        if curl -sS --max-time 2 -o /dev/null "http://127.0.0.1:${QB_PORT}/" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

qb_report() {
    local title="$1" ip
    ip="$(local_ip)"
    [ -n "$ip" ] || ip="<本机IP>"
    echo
    ok "$title"
    printf '  访问地址：http://%s:%s\n' "$ip" "$QB_PORT"
    printf '  账号    ：%s\n' "$QB_USERNAME"
    printf '  密码    ：%s\n' "${QB_PASS:-（见 $QB_ENVFILE）}"
    printf '  数据目录：%s\n' "$QB_DATA"
    printf '  服务    ：systemctl status %s\n' "$QB_SERVICE"
    printf '  密码文件：%s（仅 root 可读）\n' "$QB_ENVFILE"
}

do_install_qb() {
    local arch sha url tmp conf ans old_port old_pass temp_pass
    local QB_PORT QB_USERNAME QB_PASS
    QB_PORT="8080"
    QB_USERNAME="admin"
    QB_PASS=""

    case "$(uname -m)" in
        x86_64|amd64)
            arch="x86_64"
            sha="11d6fca4cf33821e96fe6a19555d280fdb22f302ddae484bb90455d94d91e769"
            ;;
        aarch64|arm64)
            arch="aarch64"
            sha="4ceb234ebd6cd9ab0170191f0151077ace2e1f9ae5f71787885cb5049db9fef1"
            ;;
        *)
            err "暂不支持 $(uname -m)，qBittorrent ${QB_VERSION} 只提供 x86_64 / aarch64"
            return 1
            ;;
    esac

    command -v curl >/dev/null 2>&1 || { err "需要 curl，请先安装 curl"; return 1; }
    command -v systemctl >/dev/null 2>&1 || { err "需要 systemd"; return 1; }

    if [ -f "$QB_ENVFILE" ]; then
        # shellcheck disable=SC1090
        . "$QB_ENVFILE" || true
        QB_PORT="${QB_WEBUI_PORT:-8080}"
        QB_USERNAME="${QB_WEBUI_USER:-admin}"
        QB_PASS="${QB_WEBUI_PASS:-}"
    fi

    if [ -f "$QB_ENVFILE" ] && systemctl is-active --quiet "$QB_SERVICE" 2>/dev/null; then
        if [ "${QB_FORCE:-0}" -eq 0 ]; then
            qb_report "qBittorrent ${QB_VERSION} 已安装"
            return 0
        fi
        warn "将覆盖重装 qBittorrent ${QB_VERSION}"
    fi

    if [ -f "$QB_ENVFILE" ] && [ -x "$QB_BIN" ] && [ -f "$QB_UNIT" ] && [ "${QB_FORCE:-0}" -eq 0 ]; then
        systemctl enable "$QB_SERVICE" >/dev/null 2>&1 || true
        systemctl start "$QB_SERVICE" >/dev/null 2>&1 || true
        if qb_wait; then
            qb_report "qBittorrent ${QB_VERSION} 已启动"
            return 0
        fi
        warn "已有 qBittorrent 启动失败，继续重装"
    fi

    old_port="$QB_PORT"
    if [ -f "$QB_ENVFILE" ]; then
        if qb_port_in_use "$QB_PORT" && ! systemctl is-active --quiet "$QB_SERVICE" 2>/dev/null; then
            QB_PORT="$(qb_pick_port)" || { err "8080-8100 端口都被占用"; return 1; }
            warn "原端口 ${old_port} 被占用，改用 ${QB_PORT}"
        fi
    else
        QB_PORT="$(qb_pick_port)" || { err "8080-8100 端口都被占用"; return 1; }
    fi

    if [ ! -f "$QB_ENVFILE" ] && { [ -e "$QB_BIN" ] || [ -e "$QB_UNIT" ]; }; then
        if [ "${QB_FORCE:-0}" -eq 0 ]; then
            warn "检测到已有 qBittorrent 安装（${QB_BIN} 或 ${QB_UNIT}）"
            if [ -t 0 ]; then
                read -r -p "覆盖为 ${QB_VERSION}？输入 yes: " ans || true
                [ "${ans:-}" = "yes" ] || { warn "已取消"; return 0; }
            else
                err "已有 qBittorrent 安装，未覆盖；确认重装请加 --force"
                return 1
            fi
        fi
    fi

    conf="$QB_DATA/qBittorrent/config/qBittorrent.conf"
    if [ -f "$conf" ] && [ ! -f "$QB_ENVFILE" ]; then
        err "检测到已有 qBittorrent 配置：$conf"
        err "为避免覆盖现有密码，安装已停止；请先备份并移走 $QB_DATA 后重试"
        return 1
    fi

    tmp="$(mktemp -d)" || { err "无法创建临时目录"; return 1; }
    url="https://github.com/userdocs/qbittorrent-nox-static/releases/download/${QB_RELEASE}/${arch}-qbittorrent-nox"
    c "下载 qBittorrent ${QB_VERSION}（${arch}）..."
    if ! curl -fL --retry 3 --connect-timeout 15 -o "$tmp/qbittorrent-nox" "$url"; then
        err "下载失败：$url"
        rm -rf "$tmp"
        return 1
    fi

    if command -v sha256sum >/dev/null 2>&1; then
        if ! printf '%s  %s\n' "$sha" "$tmp/qbittorrent-nox" | sha256sum -c - >/dev/null 2>&1; then
            err "sha256 校验失败，安装包可能损坏"
            rm -rf "$tmp"
            return 1
        fi
    else
        warn "缺少 sha256sum，跳过二进制校验"
    fi
    chmod +x "$tmp/qbittorrent-nox"

    if ! "$tmp/qbittorrent-nox" --version 2>/dev/null | grep -q "v${QB_VERSION}"; then
        err "qBittorrent 二进制版本校验失败"
        rm -rf "$tmp"
        return 1
    fi

    if ! getent group "$QB_OS_USER" >/dev/null 2>&1; then
        groupadd --system "$QB_OS_USER" 2>/dev/null || groupadd -r "$QB_OS_USER" 2>/dev/null || {
            err "创建系统组 ${QB_OS_USER} 失败"
            rm -rf "$tmp"
            return 1
        }
    fi
    if ! id -u "$QB_OS_USER" >/dev/null 2>&1; then
        useradd --system --gid "$QB_OS_USER" --home-dir "$QB_DATA" --shell /usr/sbin/nologin "$QB_OS_USER" 2>/dev/null \
            || useradd -r -g "$QB_OS_USER" -d "$QB_DATA" -s /usr/sbin/nologin "$QB_OS_USER" 2>/dev/null || {
                err "创建系统用户 ${QB_OS_USER} 失败"
                rm -rf "$tmp"
                return 1
            }
    fi
    if ! install -d -m 755 -o "$QB_OS_USER" -g "$QB_OS_USER" "$QB_DATA" "$QB_DATA/downloads"; then
        err "创建数据目录 $QB_DATA 失败"
        rm -rf "$tmp"
        return 1
    fi

    systemctl stop "$QB_SERVICE" >/dev/null 2>&1 || true
    if ! install -m 755 "$tmp/qbittorrent-nox" "$QB_BIN"; then
        err "安装 $QB_BIN 失败"
        rm -rf "$tmp"
        return 1
    fi

    if ! cat > "$QB_UNIT" <<EOF
[Unit]
Description=qBittorrent-nox ${QB_VERSION} (PTBrush)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${QB_OS_USER}
Group=${QB_OS_USER}
WorkingDirectory=${QB_DATA}
ExecStart=${QB_BIN} --profile=${QB_DATA} --webui-port=${QB_PORT}
Restart=always
RestartSec=5
LimitNOFILE=65535
UMask=022

[Install]
WantedBy=multi-user.target
EOF
    then
        err "写入 $QB_UNIT 失败"
        rm -rf "$tmp"
        return 1
    fi

    systemctl daemon-reload
    systemctl enable "$QB_SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$QB_SERVICE"
    if ! qb_wait; then
        err "qBittorrent 启动失败，查看：journalctl -u ${QB_SERVICE} -n 50"
        journalctl -u "$QB_SERVICE" -n 20 --no-pager 2>/dev/null || true
        rm -rf "$tmp"
        return 1
    fi

    old_pass="$QB_PASS"
    if [ -n "$old_pass" ] && qb_api_login "$old_pass" "$tmp/cookies.txt" "$tmp/login.txt"; then
        QB_PASS="$old_pass"
        ok "使用已有密码登录成功"
    elif qb_api_login "adminadmin" "$tmp/cookies.txt" "$tmp/login.txt"; then
        ok "使用 qBittorrent 默认密码登录成功"
        QB_PASS="$(qb_new_password)" || { err "生成随机密码失败"; rm -rf "$tmp"; return 1; }
    else
        temp_pass="$(journalctl -u "$QB_SERVICE" -n 80 --no-pager 2>/dev/null | sed -n 's/.*temporary password.*: //p' | tail -n 1)"
        if [ -n "$temp_pass" ] && qb_api_login "$temp_pass" "$tmp/cookies.txt" "$tmp/login.txt"; then
            ok "使用 qBittorrent 临时密码登录成功"
            QB_PASS="$(qb_new_password)" || { err "生成随机密码失败"; rm -rf "$tmp"; return 1; }
        else
            err "无法登录 qBittorrent WebUI，无法自动设置密码"
            err "请手动打开 http://$(local_ip):${QB_PORT} 或查看：journalctl -u ${QB_SERVICE} -n 50"
            rm -rf "$tmp"
            return 1
        fi
    fi

    if [ "$QB_PASS" != "$old_pass" ] || [ -z "$old_pass" ]; then
        if [ -z "$QB_PASS" ]; then
            QB_PASS="$(qb_new_password)" || { err "生成随机密码失败"; rm -rf "$tmp"; return 1; }
        fi
        local json code
        json="{\"web_ui_username\":\"${QB_USERNAME}\",\"web_ui_password\":\"${QB_PASS}\",\"save_path\":\"${QB_DATA}/downloads\"}"
        code="$(curl -sS -o "$tmp/set.txt" -w '%{http_code}' -b "$tmp/cookies.txt" \
            -H "Referer: http://127.0.0.1:${QB_PORT}" \
            --data-urlencode "json=${json}" \
            "http://127.0.0.1:${QB_PORT}/api/v2/app/setPreferences" 2>/dev/null || true)"
        if [ "$code" != "200" ]; then
            err "设置 qBittorrent 密码失败（HTTP ${code:-无响应}）"
            rm -rf "$tmp"
            return 1
        fi
        sleep 1
        if ! qb_api_login "$QB_PASS" "$tmp/cookies2.txt" "$tmp/login2.txt"; then
            err "新密码验证失败，请手动检查 qBittorrent WebUI"
            rm -rf "$tmp"
            return 1
        fi
    fi

    ( umask 077; cat > "$QB_ENVFILE" <<EOF
QB_WEBUI_PORT=${QB_PORT}
QB_WEBUI_USER=${QB_USERNAME}
QB_WEBUI_PASS=${QB_PASS}
QB_DATA=${QB_DATA}
EOF
    ) || {
        err "写入 $QB_ENVFILE 失败"
        rm -rf "$tmp"
        return 1
    }
    chmod 600 "$QB_ENVFILE"

    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw allow "${QB_PORT}/tcp" >/dev/null 2>&1 || true
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
        firewall-cmd --permanent --add-port="${QB_PORT}/tcp" >/dev/null 2>&1 || true
        firewall-cmd --reload >/dev/null 2>&1 || true
    fi

    rm -rf "$tmp"
    qb_report "qBittorrent ${QB_VERSION} 安装完成"
}

menu() {
    while true; do
        clear 2>/dev/null || true
        c "======================================"
        c "            PTBrush 管理菜单"
        c "======================================"
        show_status
        echo
        cat <<EOF
  1) 启动服务
  2) 停止服务
  3) 重启服务
  4) 升级到最新版
  5) 修改端口
  6) 查看日志
  7) 备份数据
  8) 从备份恢复
  9) 卸载
 10) 安装 qBittorrent 4.3.9（WebUI）
  0) 退出
EOF
        read -r -p "请选择: " choice || true
        case "${choice:-}" in
            1) do_start; pause ;;
            2) do_stop; pause ;;
            3) do_restart; pause ;;
            4) do_upgrade; pause ;;
            5) do_port; pause ;;
            6) do_logs; pause ;;
            7) do_backup; pause ;;
            8) do_restore; pause ;;
            9) do_uninstall; pause ;;
            10) do_install_qb; pause ;;
            0|'') exit 0 ;;
            *) err "无效选项"; sleep 1 ;;
        esac
    done
}

case "${1:-}" in
    status) show_status ;;
    start) do_start ;;
    stop) do_stop ;;
    restart) do_restart ;;
    upgrade) do_upgrade ;;
    backup) do_backup ;;
    restore) do_restore ;;
    logs) do_logs ;;
    uninstall) do_uninstall ;;
    install-qb)
        QB_FORCE=0
        [ "${2:-}" = "--force" ] && QB_FORCE=1
        do_install_qb
        ;;
    url) printf 'http://%s:%s\n' "$(local_ip)" "$PORT" ;;
    ''|menu) menu ;;
    *) echo "用法：ptbrush [status|start|stop|restart|upgrade|backup|restore|logs|uninstall|install-qb|url]"; exit 1 ;;
esac