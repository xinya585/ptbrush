"""CookieCloud 数据拉取与解密。

CookieCloud（easychen/CookieCloud）的浏览器插件把 cookie 加密上传到服务器，
客户端凭 UUID + 密码取回：

    GET {server}/get/{uuid}
    -> {"encrypted": "<base64>", "crypto_type": "legacy"}

crypto_type 可能是数字也可能是字符串，这里统一归一成三种：

* legacy（=1，默认）：CryptoJS AES-256-CBC，OpenSSL 兼容格式
  密文 = base64("Salted__" + 8 字节 salt + AES-256-CBC)
  口令 = MD5(uuid + 分隔 + password).hexdigest()[:16]
  官方插件用 `uuid + password` 直接拼接；实测的这套自建服务端（路由器上的
  easychen/cookiecloud + 改过的 app.js）用的是 `uuid + "-" + password`，
  两种都试一遍，谁解得开就用谁。
* aes-128-cbc-fixed：AES-128-CBC，固定全 0 IV，密文是纯 base64（无 Salted__ 头），
  口令同样是 MD5(uuid + "-" + password)[:16] 的 16 字节 UTF-8 字符串直接当 key。
* 0 / none：服务端直接返回明文 JSON。

AES 解密走内置的纯 Python 实现（ptsite/aes_cbc.py），不再依赖 openssl 命令行：
Windows 便携版没有 openssl，以前那边会直接报「找不到 openssl 命令」。
EVP_BytesToKey 用 hashlib.md5 手算，和 openssl `-pass` 的派生方式一致。
"""
import base64
import hashlib
import json
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
import urllib3

from ptsite.aes_cbc import aes_cbc_decrypt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_SERVER = "https://ccc.youcl.com"

KIND_PLAIN = "plain"
KIND_LEGACY = "legacy"
KIND_AES128_FIXED = "aes-128-cbc-fixed"


def _passphrases(uuid: str, password: str) -> List[bytes]:
    """两种约定的口令候选：带横杠（自建改版）优先，不带横杠（官方插件）兜底。"""
    return [
        hashlib.md5(f"{uuid}-{password}".encode("utf-8")).hexdigest()[:16].encode("utf-8"),
        hashlib.md5(f"{uuid}{password}".encode("utf-8")).hexdigest()[:16].encode("utf-8"),
    ]


def _evp_bytes_to_key(passphrase: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple:
    """OpenSSL EVP_BytesToKey（MD5、单次迭代），CryptoJS 的默认派生方式。"""
    derived = b""
    prev = b""
    while len(derived) < key_len + iv_len:
        prev = hashlib.md5(prev + passphrase + salt).digest()
        derived += prev
    return derived[:key_len], derived[key_len:key_len + iv_len]


def _aes_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC 解密并去掉 PKCS#7 填充（密钥长度自动识别 128/192/256）。

    以前这里是调 openssl 命令行，Windows 便携版没有 openssl 直接报错，
    改成纯 Python 实现，任何平台都不再依赖外部命令。
    """
    return aes_cbc_decrypt(key, iv, ciphertext)


def _crypto_kind(crypto_type) -> str:
    """把 crypto_type 归一成 plain / legacy / aes-128-cbc-fixed。

    CookieCloud 新版返回的不是数字而是字符串（实测服务器返回 "legacy"），
    legacy 指的就是老的 CryptoJS AES 格式。
    """
    if isinstance(crypto_type, bool):
        return KIND_LEGACY if crypto_type else KIND_PLAIN
    if isinstance(crypto_type, (int, float)):
        return KIND_PLAIN if int(crypto_type) == 0 else KIND_LEGACY
    s = str(crypto_type if crypto_type is not None else "").strip().lower()
    if s in ("0", "none", "plain", "plaintext", "raw", "false"):
        return KIND_PLAIN
    if s.replace("_", "-") in ("aes-128-cbc-fixed", "aes128-cbc-fixed", "aes-128-fixed", "fixed"):
        return KIND_AES128_FIXED
    if s in ("", "1", "legacy", "aes", "cryptojs", "true"):
        return KIND_LEGACY
    raise ValueError(f"不支持的 CookieCloud crypto_type: {crypto_type!r}")


def _loads(plain: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("CookieCloud 解密结果不是合法 JSON（密码或 UUID 不对？）")
    if not isinstance(data, dict):
        raise ValueError("CookieCloud 数据格式异常")
    return data


def decrypt_payload(uuid: str, password: str, encrypted: str, crypto_type=1) -> Dict[str, Any]:
    """把 /get/{uuid} 返回的 encrypted 解成 cookie 数据 dict。"""
    if not encrypted:
        raise ValueError("CookieCloud 返回的 encrypted 为空")

    kind = _crypto_kind(crypto_type)

    # 明文模式
    if kind == KIND_PLAIN:
        try:
            return _loads(encrypted.encode("utf-8"))
        except ValueError:
            pass

    padded = encrypted.strip()
    padded += "=" * (-len(padded) % 4)
    try:
        raw = base64.b64decode(padded)
    except Exception:
        raise ValueError("CookieCloud 密文不是合法 base64")

    # 新版固定 IV 的 AES-128-CBC（密文没有 Salted__ 头）
    if kind == KIND_AES128_FIXED:
        key = _passphrases(uuid, password)[0]
        plain = _aes_decrypt(key, b"\x00" * 16, raw)
        return _loads(plain)

    # legacy：CryptoJS 的 OpenSSL 兼容格式
    if not raw.startswith(b"Salted__") or len(raw) < 16:
        raise ValueError("CookieCloud 密文格式不对（缺少 Salted__ 头）")
    salt = raw[8:16]
    ciphertext = raw[16:]

    last_error: Optional[Exception] = None
    for passphrase in _passphrases(uuid, password):
        key, iv = _evp_bytes_to_key(passphrase, salt)
        try:
            return _loads(_aes_decrypt(key, iv, ciphertext))
        except ValueError as e:
            last_error = e
            continue
    raise ValueError(f"CookieCloud 数据解密失败（密码或 UUID 不对？）：{last_error}")


def fetch_cookie_cloud(server: str, uuid: str, password: str, timeout: int = 20) -> Dict[str, Any]:
    """拉取并解密，返回插件上传的整份数据（含 cookie_data / local_storage_data）。"""
    server = (server or DEFAULT_SERVER).strip().rstrip("/")
    uuid = (uuid or "").strip()
    if not uuid:
        raise ValueError("请填写 CookieCloud 的 UUID")
    if not server.startswith(("http://", "https://")):
        server = "http://" + server

    url = f"{server}/get/{uuid}"
    try:
        resp = requests.get(url, timeout=timeout, verify=False)
    except requests.RequestException as e:
        raise RuntimeError(f"连不上 CookieCloud 服务器 {server}：{e}")
    if resp.status_code != 200:
        raise RuntimeError(f"CookieCloud 返回 HTTP {resp.status_code}（UUID 是否正确？）")

    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError("CookieCloud 返回的不是 JSON（服务器地址是否填错？）")
    encrypted = body.get("encrypted")
    crypto_type = body.get("crypto_type", 1)
    if not encrypted:
        raise RuntimeError("CookieCloud 没有返回 encrypted 字段（这个 UUID 可能还没上传过数据）")

    data = decrypt_payload(uuid, password, encrypted, crypto_type)
    if not isinstance(data, dict):
        raise ValueError("CookieCloud 数据格式异常")
    return data


def normalize_domain(domain: str) -> str:
    return (domain or "").strip().lstrip(".").lower()


def match_domain(host: str, cookie_domain: str) -> bool:
    """站点 host 与 cookie 归属域是否匹配（允许子域/父域）。"""
    h = normalize_domain(host)
    d = normalize_domain(cookie_domain)
    if not h or not d:
        return False
    return h == d or h.endswith("." + d) or d.endswith("." + h)


def cookie_domain_related(host: str, cookie_domain: str) -> bool:
    """这个 cookie 域名算不算该站点自己的（站点本身或它的父域，子域不算）。

    pterclub.net     ← .pterclub.net         是本站
    pterclub.net     ← .wiki.pterclub.net    不是，那是 wiki 子系统
    pt.keepfrds.com  ← .keepfrds.com         是父域，登录态就存在这里
    """
    h = normalize_domain(host)
    d = normalize_domain(cookie_domain)
    if not h or not d:
        return False
    return h == d or h.endswith("." + d)


def cookie_hosts(host: str, cookie_data: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    """站点相关、且 CookieCloud 里确实有数据的 cookie 域名，从泛到具体排序。"""
    h = normalize_domain(host)
    if not h:
        return []
    hits = [d for d in (cookie_data or {}) if cookie_domain_related(h, d)]
    hits.sort(key=lambda d: len(normalize_domain(d)))
    return hits


def _cookie_usable(cookie: Dict[str, Any], now: float) -> bool:
    if (cookie.get("name") or "").strip() == "":
        return False
    if cookie.get("value") is None:
        return False
    exp = cookie.get("expirationDate") or cookie.get("expires")
    if exp:
        try:
            if float(exp) <= now:
                return False
        except (TypeError, ValueError):
            pass
    return True


def cookie_string_for_site(
    host: str, cookie_data: Dict[str, List[Dict[str, Any]]], now: Optional[float] = None
) -> tuple:
    """把一个站点的**所有**相关 cookie 域合并成一条 cookie 串。

    浏览器会把同一个站点拆到多个 cookie 域里，典型例子：
    我堡的 ourbits_jwt 在 `ourbits.club`，cf_clearance 在 `.ourbits.club`；
    以前按"最长域名"只取一个域，另一半直接丢掉，同步完站点就登录不了。

    合并顺序从泛域到具体域，同名的 cookie 由更具体的域覆盖。
    返回 (cookie 串, 用到的域名列表, cookie 条目数)。
    """
    now = time.time() if now is None else now
    hosts = cookie_hosts(host, cookie_data)
    merged: Dict[str, str] = {}
    used: List[str] = []
    for domain in hosts:
        usable = [c for c in (cookie_data.get(domain) or []) if _cookie_usable(c, now)]
        if not usable:
            continue
        used.append(domain)
        for cookie in usable:
            merged[(cookie.get("name") or "").strip()] = str(cookie.get("value") or "")
    return "; ".join(f"{k}={v}" for k, v in merged.items()), used, len(merged)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def cookie_string(cookies: List[Dict[str, Any]], now: Optional[float] = None) -> str:
    """把 CookieCloud 的 cookie 数组拼成 requests 用的 `k=v; k=v` 字符串。

    过期的、已删除的（value 为空且 session 已过）跳过。
    """
    now = time.time() if now is None else now
    parts = []
    for c in cookies or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        value = c.get("value")
        if value is None:
            continue
        exp = c.get("expirationDate") or c.get("expires")
        if exp:
            try:
                if float(exp) <= now:
                    continue
            except (TypeError, ValueError):
                pass
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def cookie_domains(data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """取 cookie_data，顺手兼容个别版本把 cookie 直接放顶层的情况。"""
    cookie_data = data.get("cookie_data")
    if isinstance(cookie_data, dict):
        return {k: v for k, v in cookie_data.items() if isinstance(v, list)}
    return {}
