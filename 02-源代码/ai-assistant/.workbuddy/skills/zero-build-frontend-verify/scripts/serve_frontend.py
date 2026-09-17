"""前端静态服务（前后端分离部署里的「前端侧」）。

前端零构建、零依赖，用任意静态服务器托管即可。这个脚本只是为了把
Python 自带的 http.server 包一层：补 .js / .mjs 的 MIME、关掉缓存，
方便本地联调时改完刷新就生效。

用法：
    python scripts/serve_frontend.py                      # http://127.0.0.1:8020
    python scripts/serve_frontend.py --port 5173
    python scripts/serve_frontend.py --host 0.0.0.0 --port 8080

注意：页面用了 ES Module，必须通过 HTTP 访问；
直接双击 index.html（file://）会被浏览器的模块同源策略拦住。
"""
from __future__ import annotations

import argparse
import functools
import http.server
import mimetypes
import os
import socketserver
import sys
from pathlib import Path

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/json", ".json")

ROOT = Path(__file__).resolve().parent.parent / "frontend"


class Handler(http.server.SimpleHTTPRequestHandler):
    """关缓存 + 静默日志（只打一行简化信息）。"""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        sys.stderr.write("  %s\n" % (fmt % args))


class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="前端静态服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--dir", default=str(ROOT))
    args = parser.parse_args()

    directory = Path(args.dir).resolve()
    if not (directory / "index.html").exists():
        print(f"[x] 目录里没有 index.html：{directory}")
        return 1

    handler = functools.partial(Handler, directory=str(directory))
    with ReuseServer((args.host, args.port), handler) as httpd:
        print("=" * 62)
        print("  留学机构 AI 智能助手 · 前端")
        print("=" * 62)
        print(f"  目录      : {directory}")
        print(f"  访问地址  : http://{args.host}:{args.port}/")
        print(f"  后端 API  : 在登录页「接口地址」里填（默认 http://127.0.0.1:8010）")
        print("  停止      : Ctrl+C")
        print("=" * 62)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
