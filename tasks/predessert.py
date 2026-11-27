"""pre-dessert-sites 站点配置库的定期更新。

这个库（GitHub `mantou568/pre-dessert-sites`）里对我们真正有用的只有两样：
① `site_favicon/*.png` 的 128×128 站点图标；② 各站 `sites/*.json` 适配定义，
可以用来发现「某站的账号信息解析规则变了」。其余（订阅、签到、公告）我们不使用。

所以这个任务只做两件事：
- 图标有变化就写进 siteicon（不覆盖更清晰的？没有更清晰的，仓库图标就是最清晰的）；
- 把在用的适配文件存一份快照，下次下载时对比，变了就在日志里列出来，
  提示「该站解析可能失效，需要人工看一眼」，但绝不自动改我们自己的解析代码。

省钱约定（2026-11-16）：解压完即删 `main.zip`，不再保留上一版 `repo_prev` 副本
（差异比对用的是 `snapshots/`，repo_prev 从来没被读过）。同步频率改为每周一次。
"""
import json
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests
from loguru import logger

from config.config import PTBrushConfig, site_key
from paths import data_dir
from ptsite import store as site_store

LIB_URL = "https://codeload.github.com/mantou568/pre-dessert-sites/zip/refs/heads/main"
DATA_DIR = data_dir() / "predessert"
REPO_DIR = DATA_DIR / "repo"
REPORT_FILE = DATA_DIR / "last_update.json"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
MAX_ICON_BYTES = 256 * 1024

# 仓库用的是站点真实入口域名，这里对到我们的域名键（跟导入脚本保持同一张表）
ALIAS = {
    "www.open.cd": "open.cd",
    "pt.keepfrds.com": "keepfrds.com",
    "kp.m-team.cc": "m-team.io",
    "api.m-team.cc": "m-team.io",
    "mtapi.m-team.cc": "m-team.io",
    "www.uhddolby.com": "uhddolby.com",
}


def _norm_domain(value) -> str:
    d = str(value or "").strip().split("://", 1)[-1].split("/", 1)[0].lower()
    if d.startswith("www."):
        d = d[4:]
    return ALIAS.get(d, d)


def _download(dest: Path, timeout: int = 60) -> int:
    """下载库压缩包；返回字节数（失败抛异常，由调用方兜底）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with requests.get(LIB_URL, timeout=timeout, stream=True, allow_redirects=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if chunk:
                    fh.write(chunk)
    size = tmp.stat().st_size
    if size < 2 * 1024 * 1024:
        raise ValueError(f"下载内容过小（{size} 字节），可能不是有效的压缩包")
    tmp.replace(dest)
    return size


def _extract(zip_path: Path, dest: Path) -> None:
    """解压并去掉压缩包里的顶层目录（xxx-main/）。"""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / "_extract_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp)
    roots = list(tmp.iterdir())
    src = roots[0] if len(roots) == 1 and roots[0].is_dir() else tmp
    for item in src.iterdir():
        shutil.move(str(item), str(dest / item.name))
    shutil.rmtree(tmp, ignore_errors=True)


def _site_map(repo: Path) -> dict:
    """仓库 domain → (适配 id, 站点名)。"""
    by_domain = {}
    sites_dir = repo / "site_config" / "sites"
    if not sites_dir.exists():
        return by_domain
    for fn in sites_dir.glob("*.json"):
        try:
            cfg = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = str(cfg.get("id") or fn.stem)
        name = str(cfg.get("name") or sid)
        for key in ("domain", "additional_domain"):
            raw = cfg.get(key)
            for item in raw if isinstance(raw, list) else [raw]:
                host = _norm_domain(item)
                if host:
                    by_domain.setdefault(host, (sid, name))
    return by_domain


def _flatten_diff(old, new, path="", out=None) -> list:
    """找出两份 JSON 的差异路径，只记到两级，够定位是哪块规则变了。"""
    if out is None:
        out = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            _flatten_diff(old.get(key), new.get(key), f"{path}.{key}" if path else key, out)
        return out
    if isinstance(old, list) and isinstance(new, list):
        if old != new:
            out.append(path or "list")
        return out
    if old != new:
        out.append(path or "value")
    return out


def _diff_summary(paths, limit: int = 6) -> list:
    """把差异路径压到 section 级：requests.search.fields.x → requests.search。"""
    sections = []
    for path in paths:
        parts = str(path).split(".")
        key = ".".join(parts[:2]) if len(parts) > 1 else parts[0]
        if key not in sections:
            sections.append(key)
    return sections[:limit]


def _used_sites() -> dict:
    """我们配置里在用的站点：domain → SiteModel。"""
    cfg = PTBrushConfig()
    return {site_key(s): s for s in (cfg.sites or []) if site_key(s)}


def _sync_icons(repo: Path, site_map: dict, used: dict) -> dict:
    """图标同步：内容变了才写库，避免每次刷新都把 14 个站重写一遍。"""
    filled, updated, same, missing = [], [], 0, []
    icons_dir = repo / "site_favicon"
    for domain, site in used.items():
        hit = site_map.get(domain)
        if not hit:
            missing.append(domain)
            continue
        sid, name = hit
        png = icons_dir / f"{sid}.png"
        if not png.exists():
            missing.append(domain)
            continue
        content = png.read_bytes()
        if not content or len(content) > MAX_ICON_BYTES:
            continue
        record = site_store.icon_record(domain)
        cur = record.get("base64") or ""
        if cur:
            import base64

            try:
                cur_bytes = base64.b64decode(cur)
            except Exception:
                cur_bytes = b""
            if cur_bytes == content:
                same += 1
                continue
        changed = bool(cur)
        url = f"repo:pre-dessert-sites/{sid}.png"
        if site_store.save_icon(domain, name, url, content, "image/png"):
            (updated if changed else filled).append(f"{domain}←{sid}")
    return {
        "filled": filled,
        "updated": updated,
        "same": same,
        "missing": missing,
    }


def _sync_adapters(repo: Path, site_map: dict, used: dict) -> list:
    """把在用的适配文件存快照并对比上一版；返回变化摘要（不自动改代码）。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    changed = []
    for domain in used:
        hit = site_map.get(domain)
        if not hit:
            continue
        sid = hit[0]
        src = repo / "site_config" / "sites" / f"{sid}.json"
        if not src.exists():
            continue
        try:
            new_cfg = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            continue
        snap = SNAPSHOT_DIR / f"{domain}.json"
        old_cfg = None
        if snap.exists():
            try:
                old_cfg = json.loads(snap.read_text(encoding="utf-8"))
            except Exception:
                old_cfg = None
        if old_cfg is not None:
            paths = _flatten_diff(old_cfg, new_cfg)
            if paths:
                changed.append({
                    "domain": domain,
                    "adapter": sid,
                    "sections": _diff_summary(paths),
                    "paths": paths[:12],
                })
        snap.write_text(json.dumps(new_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def _write_report(payload: dict) -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_report() -> dict:
    try:
        return json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_library(reason: str = "计划任务") -> dict:
    """下载 → 解压 → 同步图标 → 对比适配 → 写报告。整个流程失败不影响刷流。"""
    started = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"[pre-dessert-sites] 开始更新（{reason}）")
    report = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "ok": False,
        "message": "",
        "seconds": 0,
    }
    try:
        zip_path = DATA_DIR / "main.zip"
        size = _download(zip_path)
        report["zip_bytes"] = size

        # 解压后立刻删包：不再让 26MB 的 main.zip 常驻，需要时会自动重下
        _extract(zip_path, REPO_DIR)
        try:
            zip_path.unlink()
        except OSError:
            pass

        site_map = _site_map(REPO_DIR)
        used = _used_sites()
        report["repo_sites"] = len(list((REPO_DIR / "site_config" / "sites").glob("*.json")))
        report["matched_sites"] = len([d for d in used if d in site_map])

        icons = _sync_icons(REPO_DIR, site_map, used)
        report["icons"] = icons
        adapters = _sync_adapters(REPO_DIR, site_map, used)
        report["adapters_changed"] = adapters

        report["ok"] = True
        msg = (
            f"库内站点 {report['repo_sites']} 个，命中我们用到的 {report['matched_sites']} 个；"
            f"图标新增 {len(icons['filled'])}、更新 {len(icons['updated'])}、未变 {icons['same']}；"
            f"适配变化 {len(adapters)} 个"
        )
        report["message"] = msg
        logger.info(f"[pre-dessert-sites] 更新完成：{msg}")
        for item in adapters:
            logger.warning(
                f"[pre-dessert-sites] {item['domain']}（{item['adapter']}）适配有变化："
                f"{', '.join(item['sections'])} —— 若该站账号数据抓取异常，检查这里的解析规则"
            )
        for domain in icons["missing"]:
            logger.debug(f"[pre-dessert-sites] 库里没有 {domain} 的适配，跳过图标")
    except Exception as e:  # 网络/解压/写库失败都不该影响刷流
        report["message"] = f"{type(e).__name__}: {e}"[:200]
        logger.warning(f"[pre-dessert-sites] 更新失败：{report['message']}")
    report["seconds"] = round(time.time() - started, 1)
    _write_report(report)
    return report
