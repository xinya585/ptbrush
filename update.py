"""自动更新：读版本清单、下载安装包、校验、替换、重启。

清单（latest.json）和安装包分开存放：
- 清单放站点（zta.cc.cd），体积小、更新快；
- 安装包放 Cloudflare R2，经站点 /dl/ 路径下载，避免 Pages 单文件 25 MiB 限制。
可用环境变量 PTBRUSH_UPDATE_URL 指向自己的清单地址。

多节点：
- 最新版本清单两个节点（CF / 海外）并发拉，谁先返回有效 JSON 用谁；
- 安装包按用户在「系统设置」里选的下载节点下载，失败自动换下一个节点重试。
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from paths import app_root, bundle_dir, data_dir
from version import get_version

DEFAULT_MANIFEST_URL = "https://zta.cc.cd/latest.json"
PLATFORM_KEY = "windows-x64"
LINUX_PLATFORM_KEY = "linux-x64"
LINUX_ASSET = "ptbrush-linux-amd64.tar.gz"
UPDATE_DIR_NAME = "updates"

# 节点表：key 同时用在 config.toml 的 [update] download_node 里
NODES: Dict[str, Dict[str, str]] = {
    "cf": {
        "label": "Cloudflare",
        "base": "https://zta.cc.cd/dl",
        "manifest": "https://zta.cc.cd/latest.json",
    },
    "ov": {
        "label": "海外节点",
        "base": "http://43.128.135.196:8080",
        "manifest": "http://43.128.135.196:8080/latest.json",
    },
}
NODE_ORDER = ("cf", "ov")


def deploy_mode() -> str:
    """部署形态：windows 便携版 / linux 原生安装 / docker 容器 / source 源码。

    容器里跑的也是同一个 PyInstaller 二进制，光看 frozen 分不出来，所以先认容器标记。
    """
    try:
        if Path("/.dockerenv").exists() or os.environ.get("PTBRUSH_DOCKER"):
            return "docker"
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            return "windows"
        if sys.platform.startswith("linux"):
            return "linux"
        return "other"
    return "source"


def platform_key() -> str:
    return LINUX_PLATFORM_KEY if deploy_mode() == "linux" else PLATFORM_KEY


def upgradeable() -> bool:
    """能不能自己下包、换文件、重启：只有 Windows 便携版和 Linux 原生安装版。"""
    return deploy_mode() in ("windows", "linux")


_APPLY_SCRIPT = r'''
param(
    [Parameter(Mandatory=$true)][int]$AppPid,
    [Parameter(Mandatory=$true)][string]$Zip,
    [Parameter(Mandatory=$true)][string]$Target
)
$ErrorActionPreference = "Stop"
$Parent = Split-Path -Parent $Target
$log = Join-Path $Parent "ptbrush-update.log"
# 自己的 CWD 不能落在安装目录里，否则后续改名/搬移会被自己占住
Set-Location -LiteralPath $Parent
function Write-Log([string]$msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -LiteralPath $log -Append -Encoding utf8
}
function Move-WithRetry([string]$From, [string]$To) {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Move-Item -LiteralPath $From -Destination $To -Force
            return $true
        }
        catch {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}
function Restore-Data {
    if (-not $DataTmp -or -not (Test-Path -LiteralPath $DataTmp)) { return }
    $Dest = Join-Path $Target "data"
    if (Test-Path -LiteralPath $Dest) {
        Move-WithRetry $DataTmp $Dest | Out-Null
    }
    else {
        Copy-Item -LiteralPath $DataTmp -Destination $Dest -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $DataTmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Log "等待进程 $AppPid 退出"
for ($i = 0; $i -lt 240; $i++) {
    if (-not (Get-Process -Id $AppPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}
# 顺手把同一目录下残留的实例也关掉，文件占用才释放得干净
try {
    Get-Process -Name PTBrush -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Target, 'OrdinalIgnoreCase') } |
        Stop-Process -Force -ErrorAction SilentlyContinue
} catch { }
Start-Sleep -Seconds 3

$Stage = Join-Path $Parent ("_ptbrush_stage_" + [Guid]::NewGuid().ToString("N").Substring(0, 6))
$Old = "$Target.old"
$DataTmp = $null
try {
    Write-Log "解压到 $Stage"
    Expand-Archive -LiteralPath $Zip -DestinationPath $Stage -Force
    $NewDir = Join-Path $Stage "PTBrush"
    if (-not (Test-Path -LiteralPath $NewDir)) {
        if (Test-Path -LiteralPath (Join-Path $Stage "PTBrush.exe")) { $NewDir = $Stage }
        else { throw "压缩包里没有 PTBrush.exe" }
    }

    # 升级包只有程序本体，用户数据（配置/数据库/日志）在安装目录的 data 里。
    # 先把 data 挪到旁边，替换完再放回去，任何一条替换路径都不会把它覆盖或丢掉。
    $OldData = Join-Path $Target "data"
    if (Test-Path -LiteralPath $OldData) {
        $DataTmp = Join-Path $Parent ("_ptbrush_data_" + [Guid]::NewGuid().ToString("N").Substring(0, 6))
        if (-not (Move-WithRetry $OldData $DataTmp)) { throw "用户数据目录被占用，暂时无法升级" }
        Write-Log "用户数据已暂存 -> $DataTmp"
    }

    if (Test-Path -LiteralPath $Old) { Remove-Item -LiteralPath $Old -Recurse -Force -ErrorAction SilentlyContinue }

    if (Move-WithRetry $Target $Old) {
        Write-Log "备份旧版本 -> $Old"
        Write-Log "装上新版本"
        if (-not (Move-WithRetry $NewDir $Target)) {
            Write-Log "新版本落位失败，回滚"
            if (Move-WithRetry $Old $Target) { Write-Log "已回滚到旧版本" }
            throw "新版本落位失败"
        }
    }
    else {
        # 整目录改名被占用（杀软扫描、资源管理器停在目录里等）时，改为原地换程序本体
        Write-Log "整目录改名失败，改用原地替换"
        New-Item -ItemType Directory -Path $Old -Force | Out-Null
        foreach ($n in @("PTBrush.exe", "_internal", "使用说明.txt")) {
            $src = Join-Path $Target $n
            if (Test-Path -LiteralPath $src) {
                if (-not (Move-WithRetry $src (Join-Path $Old $n))) { throw "旧文件 $n 无法移走" }
            }
        }
        foreach ($n in @("PTBrush.exe", "_internal", "使用说明.txt")) {
            $src = Join-Path $NewDir $n
            if (Test-Path -LiteralPath $src) {
                if (-not (Move-WithRetry $src (Join-Path $Target $n))) { throw "新文件 $n 落位失败" }
            }
        }
        Write-Log "原地替换完成"
    }

    Restore-Data
    Write-Log "用户数据已保留"

    # 旧升级包占地方，装完就清掉
    $Upd = Join-Path $Target "data\updates"
    if (Test-Path -LiteralPath $Upd) {
        Get-ChildItem -LiteralPath $Upd -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    }

    Write-Log "启动新版本"
    Start-Process -FilePath (Join-Path $Target "PTBrush.exe")
    Start-Sleep -Seconds 2
    Remove-Item -LiteralPath $Old -Recurse -Force -ErrorAction SilentlyContinue
    Write-Log "更新完成"
}
catch {
    Write-Log "更新失败: $_"
    if ((Test-Path -LiteralPath $Old) -and (Test-Path -LiteralPath (Join-Path $Old "PTBrush.exe"))) {
        if (-not (Test-Path -LiteralPath (Join-Path $Target "PTBrush.exe"))) {
            $flat = Move-WithRetry $Old $Target
            if ($flat) { Write-Log "已回滚到旧版本" }
            else {
                foreach ($n in @("PTBrush.exe", "_internal", "使用说明.txt")) {
                    $src = Join-Path $Old $n
                    if (Test-Path -LiteralPath $src) { Move-WithRetry $src (Join-Path $Target $n) | Out-Null }
                }
                Write-Log "已回滚到旧版本（原地）"
            }
        }
        else {
            foreach ($n in @("PTBrush.exe", "_internal", "使用说明.txt")) {
                $src = Join-Path $Old $n
                if (Test-Path -LiteralPath $src) { Move-WithRetry $src (Join-Path $Target $n) | Out-Null }
            }
        }
    }
    Restore-Data
    if (Test-Path -LiteralPath (Join-Path $Target "PTBrush.exe")) {
        Start-Process -FilePath (Join-Path $Target "PTBrush.exe")
    }
}
finally {
    if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue }
    if ((Test-Path -LiteralPath $Old) -and -not (Test-Path -LiteralPath (Join-Path $Old "PTBrush.exe")) -and (Test-Path -LiteralPath (Join-Path $Target "PTBrush.exe"))) {
        Remove-Item -LiteralPath $Old -Recurse -Force -ErrorAction SilentlyContinue
    }
}
'''


_lock = threading.Lock()
_state: Dict[str, Any] = {
    "current": get_version(),
    "latest": None,
    "has_update": False,
    "notes": "",
    "url": None,
    "size": None,
    "sha256": None,
    "downloaded": None,
    "ready": False,
    "checked_at": None,
    "error": None,
    # 多节点：这次清单是从哪个节点拿到的、安装包默认走哪个节点
    "manifest_source": None,
    "node": None,
}


def manifest_url() -> str:
    return os.environ.get("PTBRUSH_UPDATE_URL") or DEFAULT_MANIFEST_URL


def selected_node() -> str:
    """用户在设置里选的下载节点；配置读不出来时退回 CF。"""
    try:
        from config.config import PTBrushConfig

        node = str(getattr(PTBrushConfig().update, "download_node", "") or "cf").strip().lower()
    except Exception:  # noqa: BLE001
        node = "cf"
    return node if node in NODES else "cf"


def _manifest_candidates() -> list:
    """更新清单的候选地址：自建清单（env）优先，否则两个节点全试。"""
    custom = os.environ.get("PTBRUSH_UPDATE_URL")
    if custom:
        return [("custom", custom)]
    return [(key, NODES[key]["manifest"]) for key in NODE_ORDER]


def _version_tuple(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def state() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def _probe_manifest(node: str, url: str, timeout: int) -> Dict[str, Any]:
    """拉一个节点的清单，返回 {node, url, elapsed, data} 或 {node, url, error}。"""
    started = time.time()
    try:
        resp = requests.get(url, timeout=timeout, headers={"Cache-Control": "no-cache"})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("version"):
            raise ValueError("清单格式不对（缺少 version）")
        return {
            "node": node,
            "url": url,
            "elapsed": time.time() - started,
            "data": data,
        }
    except Exception as e:  # noqa: BLE001
        return {"node": node, "url": url, "elapsed": None, "error": str(e)}


def check(timeout: int = 10) -> Dict[str, Any]:
    """并发拉两个节点的清单，取最快返回的有效那份，刷新状态。

    网络失败不抛异常，状态里记 error；单个节点挂掉不影响另外两个。
    """
    candidates = _manifest_candidates()
    results = []
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        futures = [pool.submit(_probe_manifest, node, url, timeout) for node, url in candidates]
        for fut in futures:
            results.append(fut.result())

    ok_rows = [r for r in results if r.get("data")]
    for r in results:
        if r.get("error"):
            logger.warning(f"清单节点 {NODES.get(r['node'], {}).get('label', r['node'])} 不可用（{r['url']}）：{r['error']}")

    if not ok_rows:
        with _lock:
            _state["error"] = "两个节点都没拿到版本清单"
            _state["checked_at"] = time.time()
            _state["manifest_source"] = None
        return state()

    best = min(ok_rows, key=lambda r: r["elapsed"])
    data = best["data"]
    key = platform_key()
    platform = (data.get("platforms") or {}).get(key) or {}
    latest = str(data.get("version") or "").strip()
    current = get_version()

    if deploy_mode() == "linux" and not platform.get("url"):
        with _lock:
            _state["error"] = "版本清单里还没有 Linux 安装包，稍后再试"
            _state["checked_at"] = time.time()
        return state()

    with _lock:
        _state.update(
            {
                "current": current,
                "latest": latest,
                "has_update": bool(latest) and is_newer(latest, current),
                "notes": data.get("notes") or "",
                "release_date": data.get("release_date"),
                "url": platform.get("url"),
                "size": platform.get("size"),
                "sha256": (platform.get("sha256") or "").lower() or None,
                "fallback_url": platform.get("fallback"),
                "mirrors": data.get("mirrors") or {},
                "platform_key": key,
                "deploy": deploy_mode(),
                "downloaded": None,
                "ready": False,
                "checked_at": time.time(),
                "error": None,
                "manifest_source": best["node"],
                "manifest_latency": round(best["elapsed"] * 1000),
                "node": selected_node(),
            }
        )

    label = NODES.get(best["node"], {}).get("label", best["node"])
    if _state["has_update"]:
        logger.info(f"发现新版本 {latest}（当前 {current}，清单来自 {label}，{int(best['elapsed'] * 1000)}ms）")
    else:
        logger.info(f"已是最新版本 {current}（清单来自 {label}，{int(best['elapsed'] * 1000)}ms）")
    return state()



def _sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


_WINERROR_SHARING_VIOLATION = 32


def _is_sharing_violation(exc: BaseException) -> bool:
    """Windows 的 ERROR_SHARING_VIOLATION：文件被别的进程/句柄打开着。"""
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) == _WINERROR_SHARING_VIOLATION


def _retry_fs(fn, label: str = "", attempts: int = 6, delay: float = 0.4):
    """文件操作小幅退避重试。

    杀软扫描刚落盘的安装包、资源管理器预览、解压软件打开着 zip 时，Windows 会
    短暂返回 WinError 32；这类占用基本是瞬时（一般 1~3 秒），直接失败太脆。
    """
    last: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return fn()
        except OSError as e:
            if not _is_sharing_violation(e):
                raise
            last = e
            logger.info(f"{label or '文件操作'}被占用，第 {i + 1}/{attempts} 次重试：{e}")
            time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


def _describe_oserror(exc: BaseException) -> str:
    """把 WinError 32 翻译成能照着做的提示。"""
    if _is_sharing_violation(exc):
        name = getattr(exc, "filename", None) or ""
        return (
            f"安装包被其他程序占用（WinError 32）\n{name}\n"
            "原因通常是杀毒软件正在扫描刚下载的包、资源管理器/解压软件打开着它，"
            "或上一次升级脚本还没退出。\n"
            "等几秒再点一次「检查更新」即可，已下好的包会直接复用，不会重新下载。"
        )
    return str(exc)


def _package_ok(path: Path, size, sha256) -> bool:
    """已有的安装包是否可以直接复用（大小 + SHA256 都对得上）。"""
    try:
        if not path.is_file():
            return False
        if size and path.stat().st_size != int(size):
            return False
        if sha256 and _sha256_of(path) != sha256:
            return False
        return True
    except OSError:
        # 被占用时读不了，当作不可复用，走后面的重下/换名逻辑
        return False


def _mark_ready(path: Path, node: str = "") -> Dict[str, Any]:
    with _lock:
        _state["downloaded"] = str(path)
        _state["ready"] = True
        if node:
            _state["node"] = node
    return state()


def _package_name(url: str) -> str:
    """从清单地址里取安装包文件名。

    清单里的地址带 ?v=<sha8>（防缓存），直接 Path(url).name 会把 "?v=..." 一起带进来，
    存成 "xxx.zip_v_587c6980"，之后 Expand-Archive / tar 就认不出来。
    Windows 包是 zip，Linux 包是 tar.gz，后缀必须原样保留。
    """
    name = Path(urlsplit(url or "").path).name
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "PTBrush-update"
    lowered = name.lower()
    if lowered.endswith((".zip", ".tar.gz", ".tgz")):
        return name
    if deploy_mode() == "linux":
        return name + ".tar.gz"
    return name + ".zip"


def _linux_urls(snap: Dict[str, Any]) -> list:
    """Linux 安装包候选：清单地址（R2）→ 清单里的 fallback（GitHub Release）。"""
    rows = []
    if snap.get("url"):
        rows.append(("cf", snap["url"]))
    fallback = snap.get("fallback_url")
    if fallback and fallback != snap.get("url"):
        rows.append(("fallback", fallback))
    return rows


def _node_urls(snap: Dict[str, Any]) -> list:
    """安装包候选地址：用户选的节点排第一，其余节点兜底。

    清单里给了 mirrors.<node> 就用它，否则按「节点基址 + 文件名」拼出来。
    """
    name = _package_name(snap.get("url") or "")
    mirrors = snap.get("mirrors") or {}
    first = selected_node()
    order = [first] + [k for k in NODE_ORDER if k != first]
    rows = []
    for key in order:
        url = mirrors.get(key)
        if not url:
            base = NODES[key]["base"].rstrip("/")
            url = f"{base}/{name}"
        rows.append((key, url))
    return rows


def _download_one(url: str, part: Path, expected_size, expected_sha, progress, total_hint) -> None:
    """从一个地址下到 .part，并校验大小 / SHA256；失败时删掉 .part 再抛出。"""
    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or total_hint or 0)
            done = 0
            last_log = 0.0
            with part.open("wb") as fh:
                for block in resp.iter_content(1 << 20):
                    if not block:
                        continue
                    fh.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
                    if total and (time.time() - last_log > 2 or done >= total):
                        last_log = time.time()
                        logger.info(f"下载进度 {done * 100 // total}%（{done // 1048576} MB）")
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    size = part.stat().st_size
    if expected_size and size != int(expected_size):
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"下载大小不符：{size} != {expected_size}")

    if expected_sha:
        actual = _sha256_of(part)
        if actual != expected_sha:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"SHA256 校验失败：{actual} != {expected_sha}")


def download(progress=None) -> Dict[str, Any]:
    """下载更新包到 data/updates，校验大小与 SHA256。

    节点顺序：用户选的那个 → 其余两个兜底；单节点失败不影响升级。
    重试/复用策略（Windows 上 WinError 32 大多数是瞬时占用）：
      1. 目录里已有大小、SHA256 都对的包 → 直接复用，不重下；
      2. .part 用「包名.pid-时间戳.part」，多实例/上次残留不会互相抢；
      3. 落位阶段删旧包、改名都带退避重试，仍被占用就换个新名字，绝不失败退出。
    """
    snap = state()
    if not snap.get("has_update") or not snap.get("url"):
        raise RuntimeError("当前没有可下载的更新，请先检查更新")

    name = _package_name(snap["url"])
    target_dir = data_dir() / UPDATE_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / name

    expected_size = snap.get("size")
    expected_sha = snap.get("sha256")

    if _package_ok(final, expected_size, expected_sha):
        logger.info(f"复用已下载并校验通过的安装包: {final}")
        return _mark_ready(final)

    _cleanup_stale(target_dir, final)

    rows = _linux_urls(snap) if deploy_mode() == "linux" else _node_urls(snap)
    last_error: Optional[BaseException] = None
    for node_key, url in rows:
        label = NODES.get(node_key, {}).get("label", "GitHub" if node_key == "fallback" else node_key)
        part = target_dir / f"{final.stem}.{node_key}.{os.getpid()}-{int(time.time())}{final.suffix}.part"
        logger.info(f"开始从{label}下载 {url}")
        try:
            _download_one(url, part, expected_size, expected_sha, progress, expected_size)
            installed = _promote(part, final)
            logger.info(f"更新包已下载并校验通过（{label}）: {installed}")
            return _mark_ready(installed, node_key)
        except Exception as e:  # noqa: BLE001
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning(f"{label}下载失败，换下一个节点：{e}")
            last_error = e

    if isinstance(last_error, OSError):
        raise RuntimeError(_describe_oserror(last_error))
    raise RuntimeError(f"下载地址都没能成功，最后一次错误：{last_error}")



def _cleanup_stale(target_dir: Path, keep: Path) -> None:
    """清掉上次残留的 .part 和旧版本安装包；清不掉也不影响升级。"""
    for p in target_dir.iterdir():
        if p.name == keep.name or not p.is_file():
            continue
        if p.suffix == ".part" or (p.suffix in (".zip", ".gz", ".tgz") and p.stem != keep.stem):
            try:
                _retry_fs(lambda p=p: p.unlink(missing_ok=True), label=f"清理残留 {p.name}", attempts=2)
            except OSError as e:
                logger.info(f"残留文件暂时清不掉，先留着：{p}（{e}）")


def _promote(part: Path, final: Path) -> Path:
    """把 .part 变成正式安装包。

    旧包被占用时不去硬删（那正是 WinError 32 的来源），而是换一个带时间戳的
    新名字落位——升级脚本只认状态里记录的路径，名字叫什么无所谓。
    """
    try:
        _retry_fs(lambda: final.unlink(missing_ok=True), label="删除旧安装包")
        _retry_fs(lambda: part.replace(final), label="重命名安装包")
        return final
    except OSError as e:
        if not _is_sharing_violation(e):
            raise
        alt = final.with_name(f"{final.stem}-{time.strftime('%H%M%S')}{final.suffix}")
        _retry_fs(lambda: part.replace(alt), label="重命名安装包（换名）")
        logger.warning(f"原安装包被占用，改用新名字落位：{alt}")
        return alt


def _apply_windows(pkg: Path) -> None:
    """写升级脚本 → 启动独立进程 → 退出本程序，由脚本替换目录并重启。"""
    script = data_dir() / UPDATE_DIR_NAME / "apply_update.ps1"
    # powershell.exe（5.1）默认按 ANSI 读脚本，UTF-8 无 BOM 会把中文后面的引号、括号吃掉，
    # 所以这里必须写带 BOM 的 UTF-8，否则脚本直接语法错误。
    script.write_text(_APPLY_SCRIPT, encoding="utf-8-sig")

    # 窗口化打包后的 PATH 可能不全，别只写 powershell.exe
    import shutil

    ps = shutil.which("powershell.exe")
    if not ps:
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.exists():
            ps = str(candidate)
    if not ps or not Path(ps).exists():
        raise RuntimeError("找不到 powershell.exe，无法自动替换升级")

    target = str(app_root())
    cmd = [
        ps,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-AppPid",
        str(os.getpid()),
        "-Zip",
        str(pkg),
        "-Target",
        target,
    ]
    logger.info(f"启动升级脚本，准备替换 {target}")
    # 脚本进程的工作目录绝不能留在安装目录里（data 下也不行），否则它自己就占着要改名的目录
    import tempfile

    subprocess.Popen(
        cmd,
        creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        close_fds=True,
        cwd=tempfile.gettempdir(),
    )
    # 给脚本一点启动时间，然后干净退出，让出文件占用
    threading.Timer(1.2, lambda: os._exit(0)).start()


def _extract_tar(pkg: Path, dest: Path) -> None:
    import tarfile

    with tarfile.open(pkg, "r:gz") as tar:
        # Python 3.12+ 要求显式声明过滤策略；老版本没有这个参数
        try:
            tar.extractall(dest, filter="data")
        except TypeError:
            tar.extractall(dest)


def _move_into_prev(src: Path, prev: Path) -> None:
    dst = prev / src.name
    if dst.exists():
        import shutil

        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        else:
            dst.unlink(missing_ok=True)
    src.rename(dst)


def _apply_linux(pkg: Path) -> None:
    """在进程内换掉程序目录里的文件，然后退出让 systemd 拉起重启。

    安装包是 PyInstaller 的 onedir 结构（ptbrush + _internal），整个目录才是程序本体；
    数据目录默认在 /var/lib/ptbrush，和程序目录分开，换文件不会碰到配置和数据库。
    前提：/opt/ptbrush/app 对服务用户 ptbrush 可写（install.sh 里已经 chown 过）。
    """
    root = app_root()
    if not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeError(
            f"安装目录 {root} 对当前用户不可写，无法在网页里替换。"
            "请先执行一次 sudo bash /opt/ptbrush/install.sh --yes（数据不动），之后就能网页一键升级"
        )
    if not os.environ.get("INVOCATION_ID"):
        raise RuntimeError("当前不是 systemd 托管运行，替换后不会自动重启；请用 sudo bash /opt/ptbrush/install.sh --yes 升级")

    stage = root / ".upgrade"
    prev = root / ".prev"

    import shutil

    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(prev, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    logger.info(f"解包 {pkg} -> {stage}")
    _extract_tar(pkg, stage)

    new = stage / "ptbrush"
    if not (new / "ptbrush").is_file():
        # 兼容包里只有一层的结构
        if (stage / "ptbrush").is_file():
            new = stage
        else:
            raise RuntimeError("安装包结构不对：找不到 ptbrush 可执行文件")

    prev.mkdir(parents=True, exist_ok=True)
    data_path = data_dir().resolve()
    moved_old: list = []
    moved_new: list = []

    def _skip(name: str, dst: Path) -> bool:
        if name in ("data", ".upgrade", ".prev"):
            return True
        # 数据目录万一被放在程序目录下面，也绝不覆盖
        return str(data_path) == str(dst) or str(data_path).startswith(str(dst) + os.sep)

    try:
        for item in sorted(new.iterdir()):
            dst = root / item.name
            if _skip(item.name, dst):
                continue
            if dst.exists():
                _move_into_prev(dst, prev)
                moved_old.append(item.name)
            item.rename(dst)
            moved_new.append(item.name)
        os.chmod(root / "ptbrush", 0o755)
        logger.info(f"程序目录已替换为新版本：{', '.join(moved_new)}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"替换失败，回滚：{e}")
        for name in reversed(moved_new):
            try:
                target = root / name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                pass
        for name in moved_old:
            try:
                (prev / name).rename(root / name)
            except OSError as err:
                logger.error(f"回滚 {name} 失败：{err}")
        raise RuntimeError(f"替换程序文件失败，已回滚到旧版本：{e}") from e

    # 清掉升级包和暂存目录，旧文件留一份 .prev 以便下次启动失败时人工回滚
    try:
        shutil.rmtree(stage, ignore_errors=True)
    except OSError:
        pass
    upd = data_dir() / UPDATE_DIR_NAME
    if upd.is_dir():
        for f in upd.iterdir():
            if f.is_file() and f.suffix in (".gz", ".zip", ".part"):
                try:
                    f.unlink()
                except OSError:
                    pass

    logger.info("新版本已就位，退出进程交给 systemd 重启")
    threading.Timer(1.5, lambda: os._exit(1)).start()


def apply() -> None:
    snap = state()
    pkg = snap.get("downloaded")
    if not pkg or not Path(pkg).exists():
        raise RuntimeError("还没有下载好的更新包")

    mode = deploy_mode()
    if mode == "windows":
        _apply_windows(Path(pkg))
    elif mode == "linux":
        _apply_linux(Path(pkg))
    elif mode == "docker":
        raise RuntimeError("容器版升级走镜像：docker pull xinya585/ptbrush:latest 后重建容器")
    else:
        raise RuntimeError("当前部署方式不支持自动替换升级")


def auto_check_loop(stop_event: threading.Event, interval: int = 6 * 3600) -> None:
    """启动后立刻查一次，之后每 interval 秒查一次。容器版不查（升级走镜像）。"""
    time.sleep(6)
    while not stop_event.is_set():
        if upgradeable():
            check()
        stop_event.wait(interval)

