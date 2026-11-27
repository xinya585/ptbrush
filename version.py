from pathlib import Path

from paths import app_root, bundle_dir

_FALLBACK_VERSION = "0.0.0"


def _candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    return [bundle_dir() / "VERSION", app_root() / "VERSION", here / "VERSION"]


def get_version() -> str:
    """从 VERSION 文件读取版本号：打包态读随包资源，开发态读源码目录。"""
    for path in _candidates():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        if text:
            return text
    return _FALLBACK_VERSION
