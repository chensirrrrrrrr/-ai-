/**
 * 运行时配置占位文件。
 *
 * scripts/serve_frontend.py 收到 --api-base 时，会把这个路径动态替换成
 * 真实的后端地址（用别的静态服务器托管时用到的就是你眼前这份，值为空，
 * 前端会自动回落到 js/config.js 里的 DEFAULT_API_BASE）。
 */
window.__UAS_API_BASE__ = window.__UAS_API_BASE__ || '';
