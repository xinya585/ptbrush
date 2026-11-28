import mimetypes
import os
import sys
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from version import get_version

# 固定前端静态资源的 MIME：Linux 容器读 /etc/mime.types、Windows 读注册表，
# 两边都可能把 .mjs 认成 text/plain，浏览器会直接拒绝加载导致页面白屏。
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"


def create_app():
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
        template_folder=None,
    )
    CORS(app, supports_credentials=True)

    # 优先 env，其次从 toml 读取并自动生成
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        from web.config_io import ensure_secret_key
        secret = ensure_secret_key()
    app.config["SECRET_KEY"] = secret
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
    # 静态资源走协商缓存（带 ETag/Last-Modified）：改了前端文件浏览器立刻拿到新版本，
    # 没改的时候仍然是 304，不会每次都全量下载
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    from web.blueprints.api_auth import api_auth_bp
    from web.blueprints.api_backup import api_backup_bp
    from web.blueprints.api_config import api_config_bp
    from web.blueprints.api_cookiecloud import api_cookiecloud_bp
    from web.blueprints.api_search import api_search_bp
    from web.blueprints.api_logs import api_logs_bp
    from web.blueprints.api_security import api_security_bp
    from web.blueprints.api_sites import api_sites_bp
    from web.blueprints.api_stats import api_stats_bp
    from web.blueprints.api_update import api_update_bp
    from web.blueprints.api_quickstart import api_quickstart_bp

    app.register_blueprint(api_auth_bp)
    app.register_blueprint(api_stats_bp)
    app.register_blueprint(api_config_bp)
    app.register_blueprint(api_cookiecloud_bp)
    app.register_blueprint(api_sites_bp)
    app.register_blueprint(api_search_bp)
    app.register_blueprint(api_logs_bp)
    app.register_blueprint(api_security_bp)
    app.register_blueprint(api_update_bp)
    app.register_blueprint(api_backup_bp)
    app.register_blueprint(api_quickstart_bp)

    @app.route("/api/version")
    def api_version():
        from update import deploy_mode

        return jsonify({
            "version": get_version(),
            "name": "ptbrush",
            # 只有 PyInstaller 打出来的 Windows 便携版才能自己下载 zip、替换文件、重启；
            # Linux 原生安装版换 tar.gz 后退出让 systemd 重启；容器版升级走换镜像。
            "portable": bool(getattr(sys, "frozen", False)),
            "deploy": deploy_mode(),
            "platform": sys.platform,
        })

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def spa(path):
        if path.startswith("api/"):
            return jsonify({"error": "Not Found"}), 404
        return send_from_directory(STATIC_DIR, "index.html")

    return app
