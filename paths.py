"""运行路径集中管理。

容器里所有数据都在 /app/data；Windows 便携版里数据放在 exe 同级的 data 目录，
换机器时把整个文件夹拷走就行。开发态（直接 python main.py）仍然用源码下的 data。

优先级：
1. 环境变量 PTBRUSH_DATA（显式指定）
2. 打包态：exe 所在目录 / data
3. 开发态：源码根目录 / data
"""

import os
import sys
from pathlib import Path

# 改路径必须发生在 db/config 这些模块 import 之前：它们在模块级就把 data 路径算好了，
# 所以 main.py 里是先 set_data_dir() 再 import 业务模块。
_DATA_DIR: Path | None = None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """打包态是 exe 所在目录，开发态是源码根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """只读资源目录：onedir/onefile 下 PyInstaller 把 datas 放在这里。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return Path(__file__).resolve().parent


def set_data_dir(path) -> Path:
    """显式指定数据目录（命令行 --data）。"""
    global _DATA_DIR
    _DATA_DIR = Path(path).expanduser().resolve()
    return _DATA_DIR


def data_dir() -> Path:
    """可写数据目录，不存在时自动创建。"""
    global _DATA_DIR
    if _DATA_DIR is None:
        env = os.environ.get("PTBRUSH_DATA")
        _DATA_DIR = Path(env).expanduser().resolve() if env else (app_root() / "data")
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR


def hide_portable_internal() -> bool:
    """把便携版的 _internal 目录设为隐藏，返回是否做了改动。

    打包目录里 _internal 下是一堆 pyd/dll，用户自己翻文件夹时很容易误删，
    所以首次解压、以及升级脚本换完文件拉起新版本时都统一隐藏掉。
    开发态和容器里没有这个目录，直接跳过。
    """
    if os.name != "nt" or not is_frozen():
        return False
    target = app_root() / "_internal"
    if not target.is_dir():
        return False
    try:
        import ctypes

        hidden = 0x2
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(target))
        if attrs == -1 or attrs & hidden:
            return False
        return bool(ctypes.windll.kernel32.SetFileAttributesW(str(target), attrs | hidden))
    except Exception:
        return False

