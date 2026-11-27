#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
种子文件 infohash（BT v1）计算。

qB 里每个种子有个 40 位 hex 的 hash，加种后把它记进数据库，就能和站点、免费截止时间
建立关联 —— 不必再把这些信息拼进种子名（拼接会让转种到其他站点时文件名带上垃圾后缀）。

这里只做 bencode 的结构跳读，不完整反序列化：取 top-level `info` 字典的原始字节算 SHA1。
"""
from __future__ import annotations

import hashlib


class TorrentHashError(ValueError):
    pass


def _blob(data: bytes, i: int) -> bytes:
    """读 <len>:<bytes> 形式的字符串，i 指向长度数字。"""
    sep = data.index(b":", i)
    n = int(data[i:sep])
    return data[sep + 1: sep + 1 + n]


def _skip(data: bytes, i: int) -> int:
    """跳过一个 bencode 值，返回它后面的下标。"""
    c = data[i:i + 1]
    if c == b"i":
        return data.index(b"e", i) + 1
    if c == b"l":
        i += 1
        while data[i:i + 1] != b"e":
            i = _skip(data, i)
        return i + 1
    if c == b"d":
        i += 1
        while data[i:i + 1] != b"e":
            i = _skip(data, i)  # key
            i = _skip(data, i)  # value
        return i + 1
    if c.isdigit():
        sep = data.index(b":", i)
        n = int(data[i:sep])
        return sep + 1 + n
    raise TorrentHashError(f"无法识别的 bencode 片段: {c!r}")


def _info_range(data: bytes) -> tuple[int, int]:
    """返回 info 字典在原始字节里的 [start, end)。"""
    if data[0:1] != b"d":
        raise TorrentHashError("种子文件不是 bencode 字典")
    i = 1
    while i < len(data) and data[i:i + 1] != b"e":
        key_start = i
        i = _skip(data, i)
        key = _blob(data, key_start)
        val_start = i
        i = _skip(data, i)
        if key == b"info":
            return val_start, i
    raise TorrentHashError("种子文件里没有 info 字段")


def infohash_v1(data: bytes) -> str:
    """算 BT v1 infohash（40 位 hex）。算不出来返回空串，调用方按名字兜底。"""
    if not data:
        return ""
    try:
        start, end = _info_range(data)
    except Exception:
        return ""
    return hashlib.sha1(data[start:end]).hexdigest()
