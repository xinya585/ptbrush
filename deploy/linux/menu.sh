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
    printf '  版本   : %s\n' "$(curl -fsS "http://127.0.0.1:${PORT}/api/version" 2>/dev/null || echo '服务未响应')"
    printf '  开机自启: %s\n' "$(systemctl is-enabled ptbrush 2>/dev/null || true)"
}

do_start()   { systemctl start ptbrush; ok "已启动"; }
do_stop()    { systemctl stop ptbrush; ok "已停止"; }
do_restart() { systemctl restart ptbrush; health && ok "已重启，服务正常" || warn "已重启，但健康检查没通过"; }

do_upgrade() {
    [ -f "$PREFIX/install.sh" ] || { err "找不到 $PREFIX/install.sh，请重新跑一次安装脚本"; return; }
    warn "将从 GitHub 拉取最新版本，数据目录不动"
    bash "$PREFIX/install.sh" --yes || { err "升级失败"; return; }
    ok "升级完成，当前版本 $(curl -fsS "http://127.0.0.1:${PORT}/api/version" 2>/dev/null)"
}

do_port() {
    read -r -p "新端口（当前 $PORT）: " np || true
    case "${np:-}" in ''|*[!0-9]*) err "端口无效"; return ;; esac
    [ "$np" -ge 1 ] && [ "$np" -le 65535 ] || { err "端口超出范围"; return; }
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$np$"; then err "端口 $np 已被占用"; return; fi
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
    url) printf 'http://%s:%s\n' "$(local_ip)" "$PORT" ;;
    ''|menu) menu ;;
    *) echo "用法：ptbrush [status|start|stop|restart|upgrade|backup|restore|logs|uninstall|url]"; exit 1 ;;
esac