# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [
    ("web/static", "web/static"),
    ("config/config.example.toml", "config"),
    ("VERSION", "."),
    ("assets", "assets"),
]
datas += collect_data_files("tzdata")

hiddenimports = [
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "peewee",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.date",
    "apscheduler.triggers.interval",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    "qbittorrentapi",
    "tomli_w",
    "tomli",
    "loguru",
    "flask_cors",
    "jsonpath",
    "tzlocal",
    "jaraco",
    "jaraco.text",
    "jaraco.context",
    "jaraco.functools",
    "more_itertools",
    "autocommand",
    "backports.tarfile",
    "importlib_resources",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "pystray", "win_desktop", "win_ui", "pkg_resources", "setuptools"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ptbrush",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ptbrush",
)