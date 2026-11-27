#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""搜片接口：关键字 → TMDB 候选（海报/年份/评分），对应 MP / MS 的「先识别再搜资源」第一步。

路由：
  GET  /api/search/settings          搜片自己的配置（TMDB Key 掩码、站点开关）
  PUT  /api/search/settings          改 TMDB Key / 地址 / 语言，立即生效
  GET  /api/search/sites             站点清单（搜片页专用，秒回）
  POST /api/search/sites             单个站点的搜片开关（只影响搜片，不影响刷流）
  GET  /api/search/tmdb?keyword=     关键字搜候选；kind=trending 时给热门
  GET  /api/search/detail?type=&id=  单部作品详情（简介/演职员/预告片/评分/状态）
  GET  /api/search/poster?path=      海报图代理（带本地缓存，内网/墙内都能出图）
  GET  /api/search/ytthumb?key=      预告片缩略图代理（同上）

设计要点：
- 站点开关存 data/search.json，与刷流 target_sites 完全独立
- TMDB 只做「识别」，返回 tmdb_id / media_type / 年份 / 评分 / 海报路径，
  后续跨站搜资源拿 tmdb_id 当锚点（和 MS 一样按 ID 锁定，避免片名歧义）
"""
import hashlib
import re
import threading
import time

import requests
from flask import Blueprint, Response, jsonify, request
from loguru import logger

from paths import data_dir
from web.auth import login_required
from web.search_store import (
    get_settings,
    public_view,
    set_site_flag,
    site_flags,
    update_settings,
)

api_search_bp = Blueprint("api_search", __name__)

# TMDB 图片尺寸白名单：只放行这几种，防止被拿去当任意图片代理
_POSTER_SIZES = {"w45", "w92", "w154", "w185", "w300", "w342", "w500", "w780", "h632", "original"}
_POSTER_PATH_RE = re.compile(r"^/[A-Za-z0-9._\-/]{1,200}$")

_IMAGE_LOCK = threading.Lock()
_HTTP_TIMEOUT = 15


def _http_session() -> requests.Session:
    """TMDB 和海报都直连；容器里就算配了代理环境变量也不自动接手，行为可预期。"""
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"Accept": "application/json"})
    return session


def _tmdb_base() -> str:
    return str(get_settings().get("tmdb_api_url") or "https://api.themoviedb.org/3").rstrip("/")


def _no_key_response():
    return jsonify({
        "error": "还没有配置 TMDB API Key，先在上面填一个再搜",
        "code": "no_key",
    }), 400


# ----------------------------------------------------------------- 配置
@api_search_bp.route("/api/search/settings", methods=["GET"])
@login_required
def search_settings():
    return jsonify(public_view())


@api_search_bp.route("/api/search/settings", methods=["PUT"])
@login_required
def search_settings_update():
    payload = request.get_json(silent=True) or {}
    data = update_settings(payload)
    return jsonify(public_view(data))


@api_search_bp.route("/api/search/sites", methods=["GET"])
@login_required
def search_site_list():
    """搜片页专用的站点清单：只读配置，不查账号快照和 qB 做种数。

    /api/sites 为了站点配置页要聚合 qB 做种数，没配下载器时会卡十几秒，
    搜片页只需要「名字 + 域名 + 类型」，单开一个快接口。
    """
    from config.config import site_domain_of
    from web.site_store import list_sites_raw

    sites = []
    for raw in list_sites_raw():
        domain = str(raw.get("domain") or "").strip().lower()
        if not domain:
            domain = site_domain_of(raw.get("base_url") or (raw.get("options") or {}).get("rss_url") or "") or ""
        sites.append({
            "name": str(raw.get("name") or ""),
            "key": domain or str(raw.get("name") or ""),
            "domain": domain,
            "type": str(raw.get("type") or ""),
        })
    sites.sort(key=lambda x: str(x["name"]))
    return jsonify({"sites": sites, "total": len(sites), "flags": site_flags()})


@api_search_bp.route("/api/search/sites", methods=["POST"])
@login_required
def search_site_toggle():
    payload = request.get_json(silent=True) or {}
    domain = str(payload.get("domain") or "").strip().lower()
    if not domain:
        return jsonify({"error": "缺少站点域名"}), 400
    enabled = bool(payload.get("enabled"))
    set_site_flag(domain, enabled)
    return jsonify({"ok": True, "domain": domain, "enabled": enabled, "sites": site_flags()})


# ----------------------------------------------------------------- TMDB
def _norm_item(item: dict) -> dict:
    media_type = item.get("media_type") or ("tv" if item.get("first_air_date") else "movie")
    title = item.get("title") or item.get("name") or ""
    date = item.get("release_date") or item.get("first_air_date") or ""
    return {
        "tmdb_id": item.get("id"),
        "media_type": media_type,
        "title": title,
        "original_title": item.get("original_title") or item.get("original_name") or "",
        "year": (date or "")[:4],
        "vote": round(float(item.get("vote_average") or 0), 1),
        "vote_count": int(item.get("vote_count") or 0),
        "poster": item.get("poster_path") or "",
        "overview": item.get("overview") or "",
        "popularity": float(item.get("popularity") or 0),
    }


@api_search_bp.route("/api/search/tmdb", methods=["GET"])
@login_required
def search_tmdb():
    settings = get_settings()
    api_key = str(settings.get("tmdb_api_key") or "")
    if not api_key:
        return _no_key_response()

    keyword = str(request.args.get("keyword") or "").strip()
    kind = str(request.args.get("kind") or "search").strip()
    try:
        page = max(1, min(int(request.args.get("page") or 1), 20))
    except (TypeError, ValueError):
        page = 1

    base = _tmdb_base()
    if kind == "trending":
        # 没输关键字时的默认内容：本周热门电影
        path = f"{base}/trending/movie/week"
        params = {"api_key": api_key, "language": settings.get("language") or "zh-CN", "page": page}
    else:
        if not keyword:
            return jsonify({"error": "关键字不能为空"}), 400
        path = f"{base}/search/multi"
        params = {
            "api_key": api_key,
            "language": settings.get("language") or "zh-CN",
            "query": keyword,
            "page": page,
            "include_adult": "false",
        }

    try:
        session = _http_session()
        resp = session.get(path, params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"TMDB 请求失败：{e}")
        return jsonify({"error": f"连不上 TMDB：{e}；检查容器网络，或把「TMDB 接口地址」换成能用的反代"}), 502

    if resp.status_code == 401:
        return jsonify({"error": "TMDB 说这个 API Key 不对（401），检查一下", "code": "bad_key"}), 401
    if resp.status_code != 200:
        return jsonify({"error": f"TMDB 返回 {resp.status_code}：{resp.text[:120]}"}), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({"error": "TMDB 返回的不是 JSON"}), 502

    items = []
    for item in data.get("results") or []:
        # 人物结果（演员/导演）跟搜片无关，直接丢掉
        if (item.get("media_type") or "") == "person":
            continue
        normalized = _norm_item(item)
        if not normalized["title"]:
            continue
        items.append(normalized)

    # TMDB 自己按相关度排过序（片名精确匹配的在最前），这里不再重排，
    # 只把「没有评分、还没有投票」的条目沉到后面（多为未上映的续集/条目）
    items.sort(key=lambda x: x["vote_count"] < 3)

    return jsonify({
        "keyword": keyword,
        "kind": kind,
        "page": page,
        "total_pages": int(data.get("total_pages") or 1),
        "items": items,
    })


# ----------------------------------------------------------------- 详情
_STATUS_ZH = {
    "Released": "已上映",
    "In Production": "制作中",
    "Post Production": "后期制作",
    "Planned": "已立项",
    "Canceled": "已取消",
    "Rumored": "传闻中",
    "Returning Series": "连载中",
    "Ended": "已完结",
    "Upcoming": "即将上映",
}

_DETAIL_CACHE: dict = {}
_DETAIL_TTL = 3600


_LANG_ZH = {
    "en": "英语", "zh": "汉语", "cn": "汉语", "ja": "日语", "ko": "韩语", "fr": "法语",
    "de": "德语", "es": "西班牙语", "ru": "俄语", "hi": "印地语", "it": "意大利语",
    "pt": "葡萄牙语", "th": "泰语", "vi": "越南语", "id": "印尼语", "ms": "马来语",
    "ar": "阿拉伯语", "tr": "土耳其语", "pl": "波兰语", "nl": "荷兰语", "sv": "瑞典语",
    "da": "丹麦语", "no": "挪威语", "fi": "芬兰语", "cs": "捷克语", "el": "希腊语",
    "he": "希伯来语", "hu": "匈牙利语", "uk": "乌克兰语", "fa": "波斯语", "tl": "他加禄语",
    "xx": "无对白",
}

_COUNTRY_ZH = {
    "US": "美国", "CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾", "MO": "中国澳门",
    "JP": "日本", "KR": "韩国", "KP": "朝鲜", "GB": "英国", "FR": "法国", "DE": "德国",
    "IT": "意大利", "ES": "西班牙", "PT": "葡萄牙", "RU": "俄罗斯", "SU": "苏联",
    "IN": "印度", "CA": "加拿大", "AU": "澳大利亚", "NZ": "新西兰", "BR": "巴西",
    "MX": "墨西哥", "AR": "阿根廷", "SE": "瑞典", "NO": "挪威", "DK": "丹麦",
    "FI": "芬兰", "NL": "荷兰", "BE": "比利时", "CH": "瑞士", "AT": "奥地利",
    "PL": "波兰", "CZ": "捷克", "HU": "匈牙利", "IE": "爱尔兰", "TR": "土耳其",
    "TH": "泰国", "VN": "越南", "SG": "新加坡", "MY": "马来西亚", "ID": "印度尼西亚",
    "PH": "菲律宾", "IL": "以色列", "IR": "伊朗", "ZA": "南非", "EG": "埃及",
    "CL": "智利", "CO": "哥伦比亚", "IS": "冰岛", "UA": "乌克兰",
}


_GENRE_ZH = {
    "Sci-Fi & Fantasy": "科幻奇幻",
    "Action & Adventure": "动作冒险",
    "War & Politics": "战争政治",
    "Sci-Fi": "科幻",
    "Fantasy": "奇幻",
    "TV Movie": "电视电影",
    "Kids": "儿童",
    "News": "新闻",
    "Reality": "真人秀",
    "Talk": "脱口秀",
    "Soap": "肥皂剧",
    "Foreign": "外语",
}


def _dedup(values) -> list:
    seen, out = set(), []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _runtime_text(minutes: int, media_type: str) -> str:
    if not minutes:
        return ""
    if media_type == "tv":
        return f"{minutes} 分钟/集"
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} 小时 {mins} 分"
    if hours:
        return f"{hours} 小时"
    return f"{mins} 分钟"


def _detail_payload(raw: dict, media_type: str) -> dict:
    title = raw.get("title") or raw.get("name") or ""
    date = raw.get("release_date") or raw.get("first_air_date") or ""
    if media_type == "tv":
        runtimes = raw.get("episode_run_time") or []
        runtime = int(runtimes[0]) if runtimes else 0
    else:
        runtime = int(raw.get("runtime") or 0)

    credits = raw.get("credits") or {}
    crew = credits.get("crew") or []
    directors = []
    if media_type == "tv":
        for person in raw.get("created_by") or []:
            directors.append({
                "id": person.get("id"),
                "name": person.get("name") or "",
                "job": "主创",
                "profile": person.get("profile_path") or "",
            })
    for person in crew:
        if (person.get("job") or "") == "Director" and len(directors) < 3:
            directors.append({
                "id": person.get("id"),
                "name": person.get("name") or "",
                "job": "导演",
                "profile": person.get("profile_path") or "",
            })
    cast = [
        {
            "id": p.get("id"),
            "name": p.get("name") or "",
            "character": p.get("character") or "",
            "profile": p.get("profile_path") or "",
        }
        for p in (credits.get("cast") or [])[:10]
    ]

    videos = []
    for v in (raw.get("videos") or {}).get("results") or []:
        if (v.get("site") or "") != "YouTube":
            continue
        if (v.get("type") or "") not in ("Trailer", "Teaser"):
            continue
        key = v.get("key") or ""
        if not key:
            continue
        videos.append({"key": key, "name": v.get("name") or "", "type": v.get("type") or ""})
    videos.sort(key=lambda x: 0 if x["type"] == "Trailer" else 1)
    videos = videos[:4]

    return {
        "tmdb_id": raw.get("id"),
        "media_type": media_type,
        "title": title,
        "original_title": raw.get("original_title") or raw.get("original_name") or "",
        "year": (date or "")[:4],
        "release_date": date,
        "overview": raw.get("overview") or "",
        "tagline": raw.get("tagline") or "",
        "poster": raw.get("poster_path") or "",
        "backdrop": raw.get("backdrop_path") or "",
        "vote": round(float(raw.get("vote_average") or 0), 1),
        "vote_count": int(raw.get("vote_count") or 0),
        "runtime": runtime,
        "runtime_text": _runtime_text(runtime, media_type),
        "genres": [_GENRE_ZH.get(g.get("name") or "", g.get("name") or "") for g in (raw.get("genres") or [])],
        "status": _STATUS_ZH.get(raw.get("status") or "", raw.get("status") or ""),
        "imdb_id": (raw.get("external_ids") or {}).get("imdb_id") or raw.get("imdb_id") or "",
        "seasons": int(raw.get("number_of_seasons") or 0),
        "episodes": int(raw.get("number_of_episodes") or 0),
        "languages": _dedup([
            _LANG_ZH.get(str(x.get("iso_639_1") or "").lower(), x.get("name") or "")
            for x in (raw.get("spoken_languages") or [])
        ]),
        "countries": _dedup([
            _COUNTRY_ZH.get(code, code) for code in (raw.get("origin_country") or [])
        ]),
        "directors": directors,
        "cast": cast,
        "videos": videos,
    }


@api_search_bp.route("/api/search/detail", methods=["GET"])
@login_required
def search_detail():
    settings = get_settings()
    api_key = str(settings.get("tmdb_api_key") or "")
    if not api_key:
        return _no_key_response()

    media_type = "tv" if str(request.args.get("type") or "") == "tv" else "movie"
    try:
        tmdb_id = int(request.args.get("id") or 0)
    except (TypeError, ValueError):
        tmdb_id = 0
    if tmdb_id <= 0:
        return jsonify({"error": "缺少 TMDB ID"}), 400

    cache_key = (media_type, tmdb_id, str(settings.get("language") or "zh-CN"))
    now = time.time()
    hit = _DETAIL_CACHE.get(cache_key)
    if hit and now - hit[0] < _DETAIL_TTL:
        return jsonify(hit[1])

    path = f"{_tmdb_base()}/{media_type}/{tmdb_id}"
    params = {
        "api_key": api_key,
        "language": settings.get("language") or "zh-CN",
        "append_to_response": "credits,videos,external_ids",
    }
    try:
        resp = _http_session().get(path, params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"TMDB 详情请求失败：{e}")
        return jsonify({"error": f"连不上 TMDB：{e}"}), 502
    if resp.status_code == 404:
        return jsonify({"error": "TMDB 上没有这部"}), 404
    if resp.status_code != 200:
        return jsonify({"error": f"TMDB 返回 {resp.status_code}：{resp.text[:120]}"}), 502
    try:
        raw = resp.json()
    except ValueError:
        return jsonify({"error": "TMDB 返回的不是 JSON"}), 502

    payload = _detail_payload(raw, media_type)
    _DETAIL_CACHE[cache_key] = (now, payload)
    if len(_DETAIL_CACHE) > 500:
        _DETAIL_CACHE.clear()
    return jsonify(payload)


_PERSON_CACHE: dict = {}
_PERSON_TTL = 3600

_DEPARTMENT_ZH = {
    "Acting": "演员",
    "Directing": "导演",
    "Writing": "编剧",
    "Production": "制片",
    "Camera": "摄影",
    "Art": "美术",
    "Sound": "音效",
    "Editing": "剪辑",
    "Costume & Make-Up": "服装化妆",
    "Visual Effects": "视效",
    "Lighting": "灯光",
    "Crew": "幕后",
}


def _person_works(raw: dict) -> list:
    """把 combined_credits 里的出演/参与作品压成一维列表，按热度排序。

    前台「演员作品里搜索」是纯前端过滤，所以这里多给一点（上限 160 条）。
    """
    credits = raw.get("combined_credits") or {}
    seen = set()
    works = []
    for kind in ("cast", "crew"):
        for w in (credits.get(kind) or []):
            media_type = w.get("media_type")
            if media_type not in ("movie", "tv"):
                continue
            tmdb_id = w.get("id")
            if not tmdb_id:
                continue
            key = (media_type, tmdb_id)
            if key in seen:
                continue
            # 本人出镜（脱口秀、综艺、颁奖礼的 Self/Self - Guest）对「推荐影片」是噪音，直接剔掉
            character = str(w.get("character") or "").strip()
            job = str(w.get("job") or "").strip()
            if character.lower().startswith("self") or (not character and job.lower().startswith("self")):
                continue
            if not character and not job:
                continue
            seen.add(key)
            works.append({
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": w.get("title") or w.get("name") or "",
                "original_title": w.get("original_title") or w.get("original_name") or "",
                "year": str(w.get("release_date") or w.get("first_air_date") or "")[:4],
                "character": w.get("character") or "",
                "job": w.get("job") or "",
                "poster": w.get("poster_path") or "",
                "vote": round(float(w.get("vote_average") or 0), 1),
                "vote_count": w.get("vote_count") or 0,
                "popularity": float(w.get("popularity") or 0),
                "episode_count": w.get("episode_count") or 0,
            })
    works.sort(key=lambda x: (x["popularity"], x["vote_count"]), reverse=True)
    return works[:160]


def _person_payload(raw: dict) -> dict:
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or "",
        "profile": raw.get("profile_path") or "",
        "department": _DEPARTMENT_ZH.get(
            raw.get("known_for_department") or "", raw.get("known_for_department") or ""
        ),
        "birthday": raw.get("birthday") or "",
        "deathday": raw.get("deathday") or "",
        "place_of_birth": raw.get("place_of_birth") or "",
        "biography": raw.get("biography") or "",
        "imdb_id": (raw.get("external_ids") or {}).get("imdb_id") or "",
        "works": _person_works(raw),
    }


@api_search_bp.route("/api/search/person", methods=["GET"])
@login_required
def search_person():
    settings = get_settings()
    api_key = str(settings.get("tmdb_api_key") or "")
    if not api_key:
        return _no_key_response()

    try:
        person_id = int(request.args.get("id") or 0)
    except (TypeError, ValueError):
        person_id = 0
    if person_id <= 0:
        return jsonify({"error": "缺少演员 ID"}), 400

    cache_key = (person_id, str(settings.get("language") or "zh-CN"))
    now = time.time()
    hit = _PERSON_CACHE.get(cache_key)
    if hit and now - hit[0] < _PERSON_TTL:
        return jsonify(hit[1])

    path = f"{_tmdb_base()}/person/{person_id}"
    params = {
        "api_key": api_key,
        "language": settings.get("language") or "zh-CN",
        "append_to_response": "combined_credits,external_ids",
    }
    try:
        resp = _http_session().get(path, params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"TMDB 演员详情请求失败：{e}")
        return jsonify({"error": f"连不上 TMDB：{e}"}), 502
    if resp.status_code == 404:
        return jsonify({"error": "TMDB 上没有这位"}), 404
    if resp.status_code != 200:
        return jsonify({"error": f"TMDB 返回 {resp.status_code}：{resp.text[:120]}"}), 502
    try:
        raw = resp.json()
    except ValueError:
        return jsonify({"error": "TMDB 返回的不是 JSON"}), 502

    payload = _person_payload(raw)
    _PERSON_CACHE[cache_key] = (now, payload)
    if len(_PERSON_CACHE) > 300:
        _PERSON_CACHE.clear()
    return jsonify(payload)


# ----------------------------------------------------------------- 海报代理
def _poster_cache_dir():
    path = data_dir() / "cache" / "poster"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_file(size: str, path: str):
    digest = hashlib.sha1(f"{size}:{path}".encode("utf-8")).hexdigest()
    return _poster_cache_dir() / f"{digest}.img"


_POSTER_MAGIC = (b"\xff\xd8", b"\x89PNG", b"RIFF", b"GIF8", b"BM")


@api_search_bp.route("/api/search/poster", methods=["GET"])
@login_required
def search_poster():
    path = str(request.args.get("path") or "")
    size = str(request.args.get("size") or "w300")
    if size not in _POSTER_SIZES:
        size = "w300"
    if not _POSTER_PATH_RE.match(path):
        return jsonify({"error": "海报路径不合法"}), 400

    cache = _cache_file(size, path)
    if cache.exists() and cache.stat().st_size > 1024:
        body = cache.read_bytes()
        return Response(body, mimetype=_guess_mime(body), headers={"Cache-Control": "public, max-age=604800"})

    url = f"https://image.tmdb.org/t/p/{size}{path}"
    try:
        session = _http_session()
        resp = session.get(url, timeout=_HTTP_TIMEOUT, stream=True)
        if resp.status_code != 200:
            return jsonify({"error": f"海报拉取失败：{resp.status_code}"}), 404
        body = resp.content
    except requests.RequestException as e:
        logger.debug(f"海报拉取失败 {path}：{e}")
        return jsonify({"error": "海报拉取失败"}), 404

    if len(body) < 1024:
        return jsonify({"error": "海报内容为空"}), 404
    with _IMAGE_LOCK:
        try:
            cache.write_bytes(body)
        except OSError:
            pass
    return Response(body, mimetype=_guess_mime(body), headers={"Cache-Control": "public, max-age=604800"})


_YT_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
# 预告片被下架/设私有时部分尺寸是 404，按住址顺序挨个试
_YT_THUMBS = (
    "https://i.ytimg.com/vi/{key}/hqdefault.jpg",
    "https://i.ytimg.com/vi/{key}/mqdefault.jpg",
    "https://img.youtube.com/vi/{key}/default.jpg",
)


@api_search_bp.route("/api/search/ytthumb", methods=["GET"])
@login_required
def search_ytthumb():
    """预告片缩略图：img.youtube.com 在部分网络下不稳，走同一条带缓存的代理。"""
    key = str(request.args.get("key") or "")
    if not _YT_KEY_RE.match(key):
        return jsonify({"error": "视频 key 不合法"}), 400

    cache = _poster_cache_dir() / f"yt-{key}.img"
    if cache.exists() and cache.stat().st_size > 512:
        return Response(cache.read_bytes(), mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})

    body = b""
    for tpl in _YT_THUMBS:
        try:
            resp = _http_session().get(tpl.format(key=key), timeout=_HTTP_TIMEOUT, stream=True)
        except requests.RequestException as e:
            logger.debug(f"缩略图拉取失败 {key}：{e}")
            continue
        if resp.status_code == 200 and len(resp.content) > 512:
            body = resp.content
            break
    if not body:
        return jsonify({"error": "缩略图拉取失败"}), 404

    with _IMAGE_LOCK:
        try:
            cache.write_bytes(body)
        except OSError:
            pass
    return Response(body, mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


def _guess_mime(body: bytes) -> str:
    if body[:4] == b"RIFF":
        return "image/webp"
    if body[:3] == b"GIF":
        return "image/gif"
    if body[:2] == b"\xff\xd8":
        return "image/jpeg"
    if body[:3] == b"\x89PN":
        return "image/png"
    return "application/octet-stream"
# ----------------------------------------------------------------- 跨站搜资源
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import quote

_RESOURCE_CACHE: dict = {}
_RESOURCE_TTL = 300
_RESOURCE_LOCK = threading.Lock()
_TID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")


def _site_key_of(site) -> str:
    from config.config import site_domain_of

    return (
        str(getattr(site, "domain", "") or "")
        or site_domain_of(getattr(site, "base_url", "") or (getattr(site, "options", None) or {}).get("rss_url") or "")
        or str(getattr(site, "name", "") or "")
    )


def _resource_sites() -> list:
    """参与搜片的站点：搜片页开关打开 + 站点配置里没被停用。"""
    from config.config import PTBrushConfig

    flags = site_flags()
    picked = []
    for site in (PTBrushConfig().sites or []):
        key = _site_key_of(site)
        if not flags.get(key, True):
            continue
        if not bool(getattr(site, "enabled", True)):
            continue
        picked.append((key, site))
    return picked


def _search_keywords() -> list:
    raw = []
    for name in ("title", "original_title", "keyword"):
        value = str(request.args.get(name) or "").strip()
        if value:
            raw.append(value)
    out = []
    for kw in raw:
        kw = re.sub(r"\s+", " ", kw)
        if kw and kw.lower() not in [x.lower() for x in out]:
            out.append(kw)
    return out[:3]


def _relevance_tokens(keywords: list) -> list:
    tokens = set()
    for kw in keywords:
        low = kw.lower()
        for word in re.findall(r"[a-z0-9]{3,}", low):
            tokens.add(word)
        cjk = re.findall(r"[\u4e00-\u9fff]", kw)
        for i in range(len(cjk) - 1):
            tokens.add("".join(cjk[i:i + 2]))
    return list(tokens)


def _score_name(name: str, tokens: list) -> int:
    low = (name or "").lower()
    return sum(1 for t in tokens if t in low)


# 站内种子的字幕标记：标题里的写法五花八门，这里按常见缩写/中英写法兜一层
_ZH_SUB_RE = re.compile(
    r"中字|中文字幕|中文字|中英|简中|繁中|简繁|中繁|繁简|简英|繁英|简体中文|繁体中文|简体|繁体"
    r"|粤语中字|国语中字|(?<![a-z])chs(?![a-z])|(?<![a-z])cht(?![a-z])|chs&cht|chs-cht",
    re.IGNORECASE,
)
_SFX_SUB_RE = re.compile(r"特效字幕|特效|动态字幕", re.IGNORECASE)


def _media_tags(name: str, subtitle: str = "") -> dict:
    """从发布名（+副标题）里认出分辨率、介质、编码，给前端筛选分组用。

    只做粗分桶，认不出的一律归「其他」，宁可让用户点「其他」看全量，
    也不要为了凑数把 480p 算进 720p。
    """
    text = f"{name or ''} {subtitle or ''}".lower()
    if re.search(r"2160p|4k|\buhd\b", text):
        resolution = "2160"
    elif "1440p" in text:
        resolution = "1440"
    elif re.search(r"1080[pi]", text):
        resolution = "1080"
    elif "720p" in text:
        resolution = "720"
    else:
        resolution = "other"

    if "remux" in text:
        medium = "remux"
    elif re.search(r"blu-?ray|bdrip|bd25|bd50|bdmv", text):
        medium = "bluray"
    elif re.search(r"web-?dl|webdl|webrip|\bweb\b", text):
        medium = "webdl"
    else:
        medium = "other"

    if re.search(r"x265|h\.?265|hevc", text):
        codec = "265"
    elif re.search(r"x264|h\.?264|avc", text):
        codec = "264"
    else:
        codec = "other"
    return {"res": resolution, "medium": medium, "codec": codec}


def _promo_bucket(promotion: str) -> str:
    """把站点促销类型归成三档：free（免费）/ half（半价）/ other。"""
    if promotion in ("free", "free_2x"):
        return "free"
    if promotion == "half_down":
        return "half"
    return "other"


def _sub_flags(name: str, subtitle: str = "", tags: str = "") -> dict:
    """从主标题 + 副标题 + 名后标签区判断是否含中文字幕 / 特效字幕（只做提示，不参与过滤）。

    主标题是英文发布名时，中字/特效信息往往只写在副标题（small_descr）或名字后面的
    标签区里，所以三段都要看：像我堡那种「英文主标题 + 中文副标题」的行，只看标题会漏标。
    """
    text = f"{name or ''} {subtitle or ''} {tags or ''}"
    return {"zh_sub": bool(_ZH_SUB_RE.search(text)),
            "sfx_sub": bool(_SFX_SUB_RE.search(text))}


def _search_one_site(key: str, site, keywords: list) -> dict:
    from ptsite import TorrentFetch

    name = str(getattr(site, "name", "") or key)
    payload = {"key": key, "name": name, "type": str(getattr(site, "type", "") or ""), "results": [], "error": ""}
    fetch = None
    try:
        fetch = TorrentFetch(site)
        seen = set()
        # 中文名和原始片名都搜一遍：中文名命中的多是转载发布，原始英文名覆盖更全，
        # 两个都留着结果最完整（每站最多两次请求，站点之间并发承担这点开销）
        for kw in keywords:
            for item in fetch.search(kw, limit=40) or []:
                tid = str(item.get("id") or "")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                item["id"] = tid
                item["site"] = name
                item["site_key"] = key
                payload["results"].append(item)
                if len(payload["results"]) >= 60:
                    break
            if len(payload["results"]) >= 60:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[{name}] 站内搜索失败：{e}")
        payload["error"] = str(e)[:160]
    finally:
        if fetch is not None:
            try:
                fetch.close()
            except Exception:  # noqa: BLE001
                pass
    return payload


@api_search_bp.route("/api/search/resources", methods=["GET"])
@login_required
def search_resources():
    """在启用的 PT 站并发搜同一部作品的资源，按站返回候选种子。"""
    keywords = _search_keywords()
    if not keywords:
        return jsonify({"error": "缺少作品名，无法搜资源"}), 400

    sites = _resource_sites()
    if not sites:
        return jsonify({"keywords": keywords, "sites": [], "total": 0,
                        "error": "没有开启任何参与搜片的站点"})

    tokens = _relevance_tokens(keywords)
    cache_key = "|".join(sorted(k.lower() for k in keywords)) + "||" + ",".join(sorted(k for k, _ in sites))
    now = time.time()
    with _RESOURCE_LOCK:
        hit = _RESOURCE_CACHE.get(cache_key)
        if hit and now - hit[0] < _RESOURCE_TTL:
            return jsonify(hit[1])

    pool = ThreadPoolExecutor(max_workers=min(8, len(sites)))
    try:
        futures = {pool.submit(_search_one_site, key, site, keywords): key for key, site in sites}
        wait(futures, timeout=55)
        site_payloads = []
        for future, key in futures.items():
            if future.done():
                try:
                    site_payloads.append(future.result())
                except Exception as e:  # noqa: BLE001
                    site_payloads.append({"key": key, "name": key, "results": [], "error": str(e)[:160]})
            else:
                site_payloads.append({"key": key, "name": key, "results": [], "error": "搜索超时（站点无响应）"})
    finally:
        pool.shutdown(wait=False)

    total = 0
    for sp in site_payloads:
        raw_items = sp.get("results") or []
        sp["raw_count"] = len(raw_items)
        kept = []
        for item in raw_items:
            score = _score_name(str(item.get("name") or ""), tokens)
            subtitle = str(item.get("subtitle") or "")
            # 副标题里常写中文片名/别名，纳入匹配能把「英文主标题 + 中文副标题」这类行救回来
            score = _score_name(f"{item.get('name') or ''} {subtitle}", tokens)
            item["match"] = score
            item.update(_sub_flags(str(item.get("name") or ""), subtitle,
                                   str(item.get("tags") or "")))
            item.update(_media_tags(str(item.get("name") or ""), subtitle))
            item["promo"] = _promo_bucket(str(item.get("promotion") or ""))
            if score > 0:
                kept.append(item)
        kept.sort(key=lambda x: (-x["match"], -int(x.get("seeders") or 0)))
        sp["results"] = kept
        sp["count"] = len(kept)
        total += len(kept)
    site_payloads.sort(key=lambda x: (-x.get("count", 0), str(x.get("name"))))

    data = {"keywords": keywords, "sites": site_payloads, "total": total}
    with _RESOURCE_LOCK:
        if len(_RESOURCE_CACHE) > 40:
            _RESOURCE_CACHE.clear()
        _RESOURCE_CACHE[cache_key] = (now, data)
    return jsonify(data)


@api_search_bp.route("/api/search/dl", methods=["GET"])
@login_required
def search_resource_download():
    """按站点 + 种子 id 下载 .torrent（馒头走 genDlToken 换链接）。"""
    from config.config import PTBrushConfig
    from ptsite import TorrentFetch

    key = str(request.args.get("site") or "").strip()
    tid = str(request.args.get("torrent_id") or request.args.get("id") or "").strip()
    if not key or not _TID_RE.match(tid):
        return jsonify({"error": "站点或种子 id 不合法"}), 400

    site = None
    for s in (PTBrushConfig().sites or []):
        if _site_key_of(s) == key or str(getattr(s, "name", "") or "") == key:
            site = s
            break
    if site is None:
        return jsonify({"error": f"找不到站点 {key}"}), 404

    fetch = None
    try:
        fetch = TorrentFetch(site)
        link = fetch.parse_torrent_link(tid)
        if not link:
            return jsonify({"error": "站点没有返回可用的下载链接（api_key 或 cookie 可能失效）"}), 502
        content = fetch.download_torrent_content(link)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[{key}] 下载种子 {tid} 失败：{e}")
        return jsonify({"error": f"下载失败：{str(e)[:120]}"}), 502
    finally:
        if fetch is not None:
            try:
                fetch.close()
            except Exception:  # noqa: BLE001
                pass

    if not content:
        return jsonify({"error": "下载到的内容是空的"}), 502

    title = re.sub(r"[\r\n]+", " ", str(request.args.get("name") or tid))
    title = re.sub(r'[\\/:*?"<>|]', "_", title).strip()[:120] or tid
    disposition = f"attachment; filename=\"{tid}.torrent\"; filename*=UTF-8''{quote(title)}.torrent"
    return Response(content, mimetype="application/x-bittorrent",
                    headers={"Content-Disposition": disposition})
