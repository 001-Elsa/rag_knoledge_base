"""下载前端依赖到 app/static/vendor（只用标准库，无需 pip 安装任何东西）。

- 优先走国内 npmmirror 镜像（registry.npmmirror.com），失败自动回退 unpkg；
- Docker 构建时自动执行；本地开发手动跑一次即可：python scripts/download_vendor.py
- 下载后前端完全离线可用，不依赖任何外网 CDN。
"""
import sys
import urllib.request
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "vendor"

# (包名, 版本, 包内路径, 保存文件名)
ASSETS = [
    ("vue", "3.4.38", "dist/vue.global.prod.js", "vue.global.prod.js"),
    ("element-plus", "2.8.4", "dist/index.full.min.js", "element-plus.full.min.js"),
    ("element-plus", "2.8.4", "dist/index.css", "element-plus.css"),
    ("element-plus", "2.8.4", "theme-chalk/dark/css-vars.css", "element-plus-dark.css"),
    ("@element-plus/icons-vue", "2.3.1", "dist/index.iife.min.js", "element-plus-icons.iife.min.js"),
    ("marked", "12.0.2", "marked.min.js", "marked.min.js"),
    ("dompurify", "3.1.6", "dist/purify.min.js", "purify.min.js"),
    ("echarts", "5.5.1", "dist/echarts.min.js", "echarts.min.js"),
    ("@highlightjs/cdn-assets", "11.10.0", "highlight.min.js", "highlight.min.js"),
    ("@highlightjs/cdn-assets", "11.10.0", "styles/github.min.css", "hljs-github.min.css"),
    ("@highlightjs/cdn-assets", "11.10.0", "styles/github-dark.min.css", "hljs-github-dark.min.css"),
]

MIRRORS = [
    "https://registry.npmmirror.com/{pkg}/{ver}/files/{path}",  # 国内镜像（阿里）
    "https://unpkg.com/{pkg}@{ver}/{path}",                     # 回退
]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "vendor-downloader"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def main() -> int:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for pkg, ver, path, filename in ASSETS:
        target = VENDOR_DIR / filename
        if target.exists() and target.stat().st_size > 0:
            print(f"[跳过] {filename}（已存在）")
            continue
        ok = False
        for mirror in MIRRORS:
            url = mirror.format(pkg=pkg, ver=ver, path=path)
            try:
                data = fetch(url)
                target.write_bytes(data)
                print(f"[完成] {filename}（{len(data) // 1024}KB ← {url.split('/')[2]}）")
                ok = True
                break
            except Exception as exc:
                print(f"[重试] {filename}: {url} 失败（{exc}）")
        if not ok:
            failed.append(filename)
    if failed:
        print(f"\n下载失败: {failed}\n请检查网络后重跑本脚本。")
        return 1
    print("\n前端依赖全部就绪 → app/static/vendor/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
