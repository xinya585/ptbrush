"""规则引擎接口（1.3.0 预览版）。

路由：
  GET    /api/rules?kind=            规则列表（含一句话摘要、命中统计）
  POST   /api/rules                 新建
  PUT    /api/rules/<id>            更新
  DELETE /api/rules/<id>            删除
  POST   /api/rules/<id>/dryrun     单条干跑
  POST   /api/rules/dryrun-all      全部启用的规则干跑
  GET    /api/rules/fields          字段字典
  GET    /api/rules/actions         动作审计
  GET    /api/tasks / POST / PUT    任务
  GET    /api/presets               预设
  POST   /api/presets/<id>/preview  导入预览
  POST   /api/presets/<id>/import   执行导入
  GET    /api/quickstart/summary    快速开始状态
"""
import json
import time
from datetime import datetime, timedelta

import peewee
from flask import Blueprint, jsonify, request
from loguru import logger

from config.config import PTBrushConfig
from db import ActionLog, Rule, Task, Torrent, database
from rules_engine import (
    delete_context,
    fields_for,
    get_preset,
    item_from_db,
    item_from_qb,
    OPERATORS,
    preset_list,
    rule_dict,
    summarize,
    evaluate,
)
from web.auth import login_required

api_rules_bp = Blueprint("api_rules", __name__)


def _json_load(text, default):
    try:
        return json.loads(text) if text else default
    except Exception:
        return default


def _body():
    return request.get_json(silent=True) or {}


def _dump(value, default):
    return json.dumps(value if value is not None else default, ensure_ascii=False)


def _norm_conditions(conditions):
    result = []
    for c in conditions or []:
        field = str(c.get("field") or "").strip()
        op = str(c.get("op") or "eq").strip()
        if not field:
            continue
        item = {"field": field, "op": op, "value": c.get("value")}
        if op == "between":
            item["value2"] = c.get("value2")
        result.append(item)
    return result


def _hit_counts(days=7):
    since = datetime.now() - timedelta(days=days)
    rows = (ActionLog
            .select(ActionLog.rule_id, peewee.fn.COUNT(ActionLog.id).alias("n"))
            .where(ActionLog.created_time >= since)
            .group_by(ActionLog.rule_id))
    return {int(r.rule_id or 0): int(r.n) for r in rows}


def _rule_payload(row, hits=None):
    data = rule_dict(row)
    if hits is not None:
        data["hits"] = hits.get(row.id, 0)
    return data


@api_rules_bp.route("/api/rules/fields")
@login_required
def api_rules_fields():
    return jsonify({
        "select": fields_for("select"),
        "delete": fields_for("delete"),
        "operators": OPERATORS,
    })


@api_rules_bp.route("/api/rules", methods=["GET"])
@login_required
def api_rules_list():
    kind = request.args.get("kind") or ""
    query = Rule.select().order_by(Rule.kind, Rule.priority, Rule.id)
    if kind:
        query = query.where(Rule.kind == kind)
    hits = _hit_counts()
    return jsonify({"rules": [_rule_payload(r, hits) for r in query], "counts": _hit_counts()})


@api_rules_bp.route("/api/rules", methods=["POST"])
@login_required
def api_rules_create():
    body = _body()
    kind = str(body.get("kind") or "delete")
    if kind not in ("select", "delete"):
        return jsonify({"error": "kind 只能是 select / delete"}), 400
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "规则名不能为空"}), 400
    conditions = _norm_conditions(body.get("conditions"))
    if not conditions:
        return jsonify({"error": "至少需要一条条件（空条件可能删光种子）"}), 400
    row = Rule.create(
        kind=kind,
        name=name,
        enabled=bool(body.get("enabled", False)),
        priority=int(body.get("priority", 10) or 10),
        task_ids=_dump(body.get("task_ids"), []),
        match=str(body.get("match") or "all"),
        conditions=_dump(conditions, []),
        sort=_dump(body.get("sort"), {}),
        limits=_dump(body.get("limits"), {}),
        protect=_dump(body.get("protect"), {}),
        preset_id=str(body.get("preset_id") or ""),
        description=str(body.get("description") or ""),
    )
    return jsonify(_rule_payload(row, {})), 201


@api_rules_bp.route("/api/rules/<int:rule_id>", methods=["PUT"])
@login_required
def api_rules_update(rule_id):
    row = Rule.get_or_none(Rule.id == rule_id)
    if not row:
        return jsonify({"error": "规则不存在"}), 404
    body = _body()
    if "name" in body:
        row.name = str(body.get("name") or "").strip() or row.name
    if "enabled" in body:
        row.enabled = bool(body.get("enabled"))
    if "priority" in body:
        row.priority = int(body.get("priority") or 10)
    if "task_ids" in body:
        row.task_ids = _dump(body.get("task_ids"), [])
    if "match" in body:
        row.match = str(body.get("match") or "all")
    if "conditions" in body:
        conditions = _norm_conditions(body.get("conditions"))
        if not conditions:
            return jsonify({"error": "至少需要一条条件"}), 400
        row.conditions = _dump(conditions, [])
    if "sort" in body:
        row.sort = _dump(body.get("sort"), {})
    if "limits" in body:
        row.limits = _dump(body.get("limits"), {})
    if "protect" in body:
        row.protect = _dump(body.get("protect"), {})
    if "description" in body:
        row.description = str(body.get("description") or "")
    row.updated_time = datetime.now()
    row.save()
    return jsonify(_rule_payload(row, _hit_counts()))


@api_rules_bp.route("/api/rules/<int:rule_id>", methods=["DELETE"])
@login_required
def api_rules_delete(rule_id):
    deleted = Rule.delete().where(Rule.id == rule_id).execute()
    return jsonify({"deleted": deleted})


# ---------------------------------------------------------------- 干跑
def _sample_select(limit=200):
    cfg = PTBrushConfig()
    names = {s.name for s in (cfg.sites or []) if s.enabled}
    query = Torrent.select().where(Torrent.brushed == False)  # noqa: E712
    if names:
        query = query.where(Torrent.site.in_(list(names)))
    return list(query.order_by(Torrent.score.desc()).limit(limit))


def _run_rule(row, qb_service=None):
    hits, checked = [], 0
    if row.kind == "select":
        for t in _sample_select():
            checked += 1
            item = item_from_db(t)
            ok, failed = evaluate(rule_dict(row), item)
            if ok:
                hits.append({
                    "name": t.name, "site": t.site,
                    "size_gb": round((t.size or 0) / 1024 ** 3, 2),
                    "promo": t.promotion,
                })
            if len(hits) >= 20:
                break
    else:
        if qb_service is None:
            from tasks.services import QBTorrentService
            qb_service = QBTorrentService()
        ctx = delete_context(qb_service)
        torrents = []
        for qb in qb_service._qbs:
            try:
                torrents.extend(qb.torrents)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"干跑读取种子失败: {e}")
        for t in torrents:
            checked += 1
            item = item_from_qb(t, ctx)
            ok, failed = evaluate(rule_dict(row), item)
            if ok:
                hits.append({
                    "name": item["name"], "site": item["tracker"],
                    "size": item["total_size"], "ratio": round(item["ratio"], 2),
                    "seed_hours": round(item["seed_time"] / 3600, 1),
                    "up_speed": item["upload_speed"],
                })
            if len(hits) >= 20:
                break
    return {"checked": checked, "hits": hits, "hit_count": len(hits)}


@api_rules_bp.route("/api/rules/<int:rule_id>/dryrun", methods=["POST"])
@login_required
def api_rules_dryrun(rule_id):
    row = Rule.get_or_none(Rule.id == rule_id)
    if not row:
        return jsonify({"error": "规则不存在"}), 404
    result = _run_rule(row)
    result.update({"rule_id": row.id, "rule_name": row.name, "kind": row.kind,
                   "summary": summarize(rule_dict(row))})
    return jsonify(result)


@api_rules_bp.route("/api/rules/dryrun-all", methods=["POST"])
@login_required
def api_rules_dryrun_all():
    qb_service = None
    rows = list(Rule.select().where(Rule.enabled == True))  # noqa: E712
    results = []
    for row in rows:
        if row.kind == "delete" and qb_service is None:
            from tasks.services import QBTorrentService
            qb_service = QBTorrentService()
        results.append(_run_rule(row, qb_service))
        results[-1]["rule_id"] = row.id
        results[-1]["rule_name"] = row.name
        results[-1]["kind"] = row.kind
    return jsonify({"rules": results, "total_hits": sum(r["hit_count"] for r in results)})


# ---------------------------------------------------------------- 任务
def _task_dict(row):
    return {
        "id": row.id, "name": row.name, "enabled": bool(row.enabled),
        "sites": _json_load(row.sites, []), "downloader": row.downloader,
        "category": row.category, "save_path": row.save_path,
        "per_hour": row.per_hour, "interval": row.interval,
    }


@api_rules_bp.route("/api/tasks", methods=["GET"])
@login_required
def api_tasks_list():
    return jsonify({"tasks": [_task_dict(t) for t in Task.select().order_by(Task.id)]})


@api_rules_bp.route("/api/tasks", methods=["POST"])
@login_required
def api_tasks_create():
    body = _body()
    name = str(body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "任务名不能为空"}), 400
    row = Task.create(
        name=name, enabled=bool(body.get("enabled", True)),
        sites=_dump(body.get("sites"), []), downloader=str(body.get("downloader") or ""),
        category=str(body.get("category") or "ptbrush"),
        save_path=str(body.get("save_path") or ""),
        per_hour=int(body.get("per_hour") or 20),
        interval=int(body.get("interval") or 10),
    )
    return jsonify(_task_dict(row)), 201


@api_rules_bp.route("/api/tasks/<int:task_id>", methods=["PUT"])
@login_required
def api_tasks_update(task_id):
    row = Task.get_or_none(Task.id == task_id)
    if not row:
        return jsonify({"error": "任务不存在"}), 404
    body = _body()
    for key in ("name", "downloader", "category", "save_path"):
        if key in body:
            setattr(row, key, str(body.get(key) or ""))
    for key in ("per_hour", "interval"):
        if key in body:
            setattr(row, key, int(body.get(key) or 0))
    if "enabled" in body:
        row.enabled = bool(body.get("enabled"))
    if "sites" in body:
        row.sites = _dump(body.get("sites"), [])
    row.updated_time = datetime.now()
    row.save()
    return jsonify(_task_dict(row))


# ---------------------------------------------------------------- 预设
@api_rules_bp.route("/api/presets", methods=["GET"])
@login_required
def api_presets():
    cfg = PTBrushConfig()
    site_count = len(cfg.sites or [])
    return jsonify({"presets": preset_list(), "sites": site_count})


@api_rules_bp.route("/api/presets/<preset_id>/preview", methods=["POST"])
@login_required
def api_preset_preview(preset_id):
    pack = get_preset(preset_id)
    if not pack:
        return jsonify({"error": "套餐不存在"}), 404
    cfg = PTBrushConfig()
    sites = _body().get("sites") or [s.domain for s in (cfg.sites or [])]
    existing = {r.name for r in Rule.select()}
    items = []
    for r in pack["rules"]:
        items.append({"kind": r["kind"], "name": r["name"], "conflict": r["name"] in existing,
                      "summary": summarize(r)})
    return jsonify({
        "preset": {"id": pack["id"], "name": pack["name"]},
        "sites": sites, "site_count": len(sites),
        "items": items, "conflicts": sum(1 for i in items if i["conflict"]),
        "to_add": sum(1 for i in items if not i["conflict"]),
    })


@api_rules_bp.route("/api/presets/<preset_id>/import", methods=["POST"])
@login_required
def api_preset_import(preset_id):
    pack = get_preset(preset_id)
    if not pack:
        return jsonify({"error": "套餐不存在"}), 404
    body = _body()
    cfg = PTBrushConfig()
    domains = body.get("sites")
    if not domains:
        domains = [s.domain for s in (cfg.sites or [])]
    names = body.get("rules") or [r["name"] for r in pack["rules"]]
    created, skipped = 0, 0
    for r in pack["rules"]:
        if r["name"] not in names:
            continue
        if Rule.select().where(Rule.name == r["name"]).exists():
            skipped += 1
            continue
        Rule.create(
            kind=r["kind"], name=r["name"], enabled=False,
            priority=r.get("priority", 10), task_ids=_dump([], []),
            match=r.get("match", "all"),
            conditions=_dump(r.get("conditions", []), []),
            limits=_dump(r.get("limits", {}), {}),
            protect=_dump({"keep": True, "tags": ["保种"], "categories": ["KEEP"]}, {}),
            preset_id=pack["id"], description=r.get("description", ""),
        )
        created += 1
    task_name = pack["task"]["name"]
    task = Task.get_or_none(Task.name == task_name)
    if not task:
        task = Task.create(
            name=task_name, enabled=True,
            sites=_dump(domains, []),
            per_hour=int(pack["task"].get("per_hour", 20)),
            interval=int(pack["task"].get("interval", 10)),
        )
    else:
        task.sites = _dump(domains, [])
        task.save()
    return jsonify({"created": created, "skipped": skipped, "task": _task_dict(task),
                    "sites": domains, "enabled": False})


@api_rules_bp.route("/api/rules/actions", methods=["GET"])
@login_required
def api_rule_actions():
    limit = min(int(request.args.get("limit") or 50), 200)
    rows = ActionLog.select().order_by(ActionLog.id.desc()).limit(limit)
    return jsonify({"actions": [{
        "id": r.id, "kind": r.kind, "rule_name": r.rule_name, "hash": r.hash,
        "name": r.name, "site": r.site, "size": r.size, "reason": r.reason,
        "detail": r.detail,
        "time": r.created_time.strftime("%Y-%m-%d %H:%M:%S") if r.created_time else "",
    } for r in rows]})


# ---------------------------------------------------------------- 快速开始
@api_rules_bp.route("/api/quickstart/summary", methods=["GET"])
@login_required
def api_quickstart_summary():
    cfg = PTBrushConfig()
    sites = list(cfg.sites or [])
    total = len(sites)
    cookie_ok = sum(1 for s in sites if (s.cookie or s.api_key or s.token or s.public))
    downloaders = cfg.downloader_list()
    tasks = list(Task.select().order_by(Task.id))
    rules = list(Rule.select())
    return jsonify({
        "sites": {"total": total, "cookie_ok": cookie_ok},
        "downloaders": {"total": len(downloaders),
                        "alias": downloaders[0].alias if downloaders else ""},
        "tasks": {"total": len(tasks), "enabled": sum(1 for t in tasks if t.enabled)},
        "rules": {"total": len(rules), "enabled": sum(1 for r in rules if r.enabled),
                  "select": sum(1 for r in rules if r.kind == "select"),
                  "delete": sum(1 for r in rules if r.kind == "delete")},
        "steps": [
            {"key": "downloader", "label": "下载器地址与账号", "done": len(downloaders) > 0,
             "detail": downloaders[0].alias if downloaders else "未配置", "to": "/downloader"},
            {"key": "sites", "label": "添加你自己的站点", "done": total > 0,
             "detail": f"{total} 个", "to": "/sites"},
            {"key": "cookies", "label": "站点 Cookie", "done": total > 0 and cookie_ok >= total,
             "detail": f"{cookie_ok} / {total} 已填" if total else "加站时填", "to": "/sites"},
            {"key": "rules", "label": "导入一套规则", "done": len(rules) > 0,
             "detail": f"{len(rules)} 条" if rules else "未导入", "to": "/start"},
            {"key": "save_path", "label": "选择保存目录",
             "done": any(bool(t.save_path) for t in tasks) or any(bool(getattr(d, "save_path", "")) for d in downloaders),
             "detail": "已设置" if (any(bool(t.save_path) for t in tasks) or any(bool(getattr(d, "save_path", "")) for d in downloaders)) else "用下载器默认目录",
             "to": "/brush"},
        ],
    })
