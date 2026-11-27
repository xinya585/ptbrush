"""纯 Python 的 AES-CBC 解密（只做解密，够用就行）。

存在的理由：CookieCloud 数据要用 AES-256/128-CBC 解，最早是调 `openssl enc -d`，
而 Windows 便携版没有 openssl 命令，功能在那边直接不可用。改成内置实现后，
Docker 容器和 Windows 都是同一套代码，也不再往镜像/打包里加 crypto 依赖。

实现覆盖 128/192/256 bit 密钥，解密完按 openssl 的习惯去掉 PKCS#7 填充。
性能靠预乘表兜底：CookieCloud 的 JSON 通常几十到几百 KB，纯查表几十毫秒。
"""

_BLOCK = 16


def _gf_mul(a: int, b: int) -> int:
    """GF(2^8) 上的乘法（AES 用的 x^8+x^4+x^3+x+1 约简多项式）。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _build_tables():
    # 先暴力求乘法逆元（256*256 次，import 时一次性的开销可忽略）
    inv = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if _gf_mul(a, b) == 1:
                inv[a] = b
                break
    sbox = []
    for a in range(256):
        x = inv[a]
        rot = lambda n: ((x << n) | (x >> (8 - n))) & 0xFF
        sbox.append(x ^ rot(1) ^ rot(2) ^ rot(3) ^ rot(4) ^ 0x63)
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_tables()

# InvMixColumns 的常数乘法预表，避免每个分组都去算 GF 乘法
_M9 = [_gf_mul(a, 9) for a in range(256)]
_M11 = [_gf_mul(a, 11) for a in range(256)]
_M13 = [_gf_mul(a, 13) for a in range(256)]
_M14 = [_gf_mul(a, 14) for a in range(256)]


def _expand_key(key: bytes):
    """密钥扩展，返回 (每轮的 16/24/32 字节轮密钥列表, 轮数)。"""
    if len(key) not in (16, 24, 32):
        raise ValueError("AES 密钥长度必须是 16 / 24 / 32 字节")
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(words[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _gf_mul(rcon, 2)
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        words.append([words[i - nk][j] ^ t[j] for j in range(4)])
    round_keys = [
        [b for word in words[4 * r:4 * r + 4] for b in word] for r in range(nr + 1)
    ]
    return round_keys, nr


def _inv_shift_rows(s):
    out = [0] * _BLOCK
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = s[r + 4 * ((c - r) % 4)]
    return out


def _inv_mix_columns(s):
    out = [0] * _BLOCK
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c] = _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3]
        out[4 * c + 1] = _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3]
        out[4 * c + 2] = _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3]
        out[4 * c + 3] = _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]
    return out


def _decrypt_block(block, round_keys, nr):
    s = [b ^ k for b, k in zip(block, round_keys[nr])]
    for r in range(nr - 1, 0, -1):
        s = _inv_shift_rows(s)
        s = [_INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, round_keys[r])]
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = [_INV_SBOX[b] for b in s]
    return [b ^ k for b, k in zip(s, round_keys[0])]


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes, unpad: bool = True) -> bytes:
    """AES-CBC 解密。数据长度必须是 16 的倍数；unpad=True 时去掉 PKCS#7 填充。"""
    if len(iv) != _BLOCK:
        raise ValueError("AES-CBC 的 IV 必须是 16 字节")
    if not data or len(data) % _BLOCK:
        raise ValueError("AES 密文长度不是 16 的倍数")
    round_keys, nr = _expand_key(key)

    out = bytearray()
    prev = iv
    for i in range(0, len(data), _BLOCK):
        block = list(data[i:i + _BLOCK])
        plain = _decrypt_block(block, round_keys, nr)
        out.extend(bytes(a ^ b for a, b in zip(plain, prev)))
        prev = block

    if unpad:
        pad = out[-1]
        if not 1 <= pad <= _BLOCK or out[-pad:] != bytes([pad]) * pad:
            raise ValueError("bad decrypt：填充不正确（密钥或 IV 不对）")
        del out[-pad:]
    return bytes(out)
