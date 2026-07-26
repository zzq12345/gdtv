"""
广东卫视 (荔枝网 / gdtv.cn) — m3u8 提取 & 本地转发代理
=================================================================

本脚本提供 3 种工作模式：
  1) --extract   仅提取并打印 m3u8 地址（调试用）
  2) --proxy     启动本地 HTTP 代理（默认端口 8080）
  3) --play      直接调用系统播放器播放

═══════════════════════════════════════════════════════════
⚠️  重要说明
─────────────────────────────────────────────────────────
荔枝网 (gdtv.cn) 的直播 API 使用 WASM + HMAC 动态签名，
直接请求会被 CDN 策略拦截 (403)。

本脚本采用「多源降级」策略：
  ✅ 优先：通过官方 API（需 Node.js + WASM 签名）
  ✅ 备选：多个公开可用的广东卫视 m3u8 镜像源
  ✅ 兜底：允许用户手动指定 m3u8 地址

任何能返回合法 m3u8 的源都会被自动代理转发，
并加上正确的 Referer / Origin 头以绕过防盗链。
═══════════════════════════════════════════════════════════

依赖：
  pip install flask requests

用法示例：
  python gdtv_proxy.py --extract
  python gdtv_proxy.py --proxy
  python gdtv_proxy.py --proxy --port 9090
  python gdtv_proxy.py --proxy --source "http://your-custom-source/live.m3u8"
"""

import os
import sys
import time
import json
import base64
import hashlib
import hmac
import subprocess
import threading
from urllib.parse import urljoin, urlparse, quote

import requests
from flask import Flask, Response, request

# ═════════════════════════════════════════════════════════
#  配置区
# ═════════════════════════════════════════════════════════
TV_CHANNEL_ID   = 43
API_URL_TPL     = "https://gdtv-api.gdtv.cn/api/tv/v2/tvChannel/{}?tvChannelPk={}&node=web_pc"
REFERER         = "https://www.gdtv.cn"
ORIGIN          = "https://www.gdtv.cn"
UA              = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 备选公开源（自动检测可用性）
FALLBACK_SOURCES = [
    "http://tcdn.itouchtv.cn/live/gdws.m3u8",
    "http://tv.gdtv.ah.cn/live/01.m3u8",
    "http://baidu.live.cqccn.com/__cl/cg:live/__c/guangdongHD/__op/default/__f//index.m3u8",
    # 移动 IPTV 源（仅限对应运营商网络内可用）
    "http://rrs03.hw.gmcc.net:8088/PLTV/651/224/3221227161/1.m3u8",
]

# WASM 签名相关（可选，需要 Node.js 环境）
WASM_JS_PATH    = os.path.join(os.path.dirname(__file__), "lizhi_sign.js")
NODE_BIN        = "node"

# 代理默认配置
DEFAULT_PORT    = 8080
DEFAULT_HOST    = "0.0.0.0"
PROXY_PATH      = "/gdws.m3u8"

# 源地址缓存
CACHE_SECONDS   = 600
_cache = {"url": None, "expire": 0, "lock": threading.Lock()}

# ═════════════════════════════════════════════════════════
#  工具函数
# ═════════════════════════════════════════════════════════
def _upstream_headers(referer=REFERER, origin=ORIGIN):
    return {"User-Agent": UA, "Referer": referer, "Origin": origin}

def _is_m3u8(text):
    """判断响应是否为 m3u8 播放列表"""
    head = text.lstrip().lower()
    return head.startswith("#extm3u") or "application/vnd.apple.mpegurl" in head

def _extract_m3u8_urls(text):
    """从网页/JSON 中提取 m3u8 链接"""
    import re
    return list(set(re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", text)))

# ═════════════════════════════════════════════════════════
#  方式 A：通过 Node.js + WASM 获取签名后的播放地址
# ═════════════════════════════════════════════════════════
def fetch_via_nodejs(channel_id=TV_CHANNEL_ID):
    """
    调用 lizhi_sign.js 获取签名 headers，再请求 API 拿 playUrl。
    返回 m3u8 地址字符串，失败抛异常。
    """
    if not os.path.exists(WASM_JS_PATH):
        raise FileNotFoundError(f"未找到 WASM 签名脚本: {WASM_JS_PATH}")

    api_url = API_URL_TPL.format(channel_id, channel_id)
    # 调用 Node.js 获取签名 headers
    result = subprocess.run(
        [NODE_BIN, WASM_JS_PATH, "GET", api_url],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node.js 签名失败: {result.stderr[:300]}")

    try:
        headers = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"签名脚本输出无法解析: {result.stdout[:200]}")

    headers.update(_upstream_headers())
    resp = requests.get(api_url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    play_url_obj = json.loads(data.get("playUrl", "{}"))
    url = play_url_obj.get("hd") or play_url_obj.get("sd") or play_url_obj.get("url")
    if not url:
        raise RuntimeError(f"API 未返回 playUrl: {data}")
    return url

# ═════════════════════════════════════════════════════════
#  方式 B：备选公开源自动检测
# ═════════════════════════════════════════════════════════
def detect_fallback_source():
    """遍历备选源，返回第一个可用的 m3u8 地址"""
    for url in FALLBACK_SOURCES:
        try:
            r = requests.get(url, headers=_upstream_headers(), timeout=6, allow_redirects=True)
            if r.status_code == 200 and _is_m3u8(r.text):
                print(f"  ✓ 备选源可用: {url}")
                return url
            else:
                print(f"  ✗ {url} -> HTTP {r.status_code}")
        except Exception as e:
            print(f"  ✗ {url} -> {type(e).__name__}: {str(e)[:80]}")
    return None

# ═════════════════════════════════════════════════════════
#  统一获取入口（带缓存 + 多级降级）
# ═════════════════════════════════════════════════════════
def get_play_url(force=False, custom_source=None):
    """
    获取广东卫视 m3u8 播放地址。
    优先级：自定义源 > Node.js签名 > 备选源检测。
    结果缓存 10 分钟。
    """
    now = time.time()
    with _cache["lock"]:
        if not force and _cache["url"] and now < _cache["expire"]:
            return _cache["url"]

    # 1) 用户手动指定
    if custom_source:
        url = custom_source
        print(f"[1] 使用用户指定源: {url}")
    else:
        # 2) 尝试 Node.js + WASM 签名
        try:
            url = fetch_via_nodejs()
            print(f"[2] 通过 API 签名获取: {url}")
        except Exception as e:
            print(f"[2] API 签名方式不可用: {str(e)[:120]}")
            # 3) 备选源检测
            url = detect_fallback_source()
            if not url:
                raise RuntimeError(
                    "所有源均不可用。请检查网络，或使用 --source 手动指定 m3u8 地址。"
                )

    with _cache["lock"]:
        _cache["url"] = url
        _cache["expire"] = now + CACHE_SECONDS
    return url

# ═════════════════════════════════════════════════════════
#  m3u8 列表重写（将相对路径转为代理绝对路径）
# ═════════════════════════════════════════════════════════
def rewrite_m3u8(m3u8_text, base_url, proxy_base):
    """
    重写 m3u8 内容：
    - 将相对 URL 转为上游绝对 URL
    - 将分片 URL 包装为 /segment?u=<b64> 走代理
    """
    lines = []
    for line in m3u8_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if stripped.startswith("http://") or stripped.startswith("https://"):
            target = stripped
        else:
            target = urljoin(base_url, stripped)
        # 对分片/子列表做 base64 编码后交给 /segment 代理
        encoded = base64.urlsafe_b64encode(target.encode()).decode()
        lines.append(f"{proxy_base}/segment?u={encoded}")
    return "\n".join(lines) + "\n"

# ═════════════════════════════════════════════════════════
#  上游请求 + 流式转发
# ═════════════════════════════════════════════════════════
def proxy_request(target_url, is_m3u8_list=False):
    """向上游请求资源并流式返回，自动附加防盗链头"""
    resp = requests.get(target_url, headers=_upstream_headers(), stream=True, timeout=15)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if is_m3u8_list or "mpegurl" in ctype or target_url.endswith(".m3u8"):
        text = resp.text
        proxy_base = f"http://{request.host}{PROXY_PATH}"
        rewritten = rewrite_m3u8(text, target_url, proxy_base)
        return Response(rewritten, mimetype="application/vnd.apple.mpegurl",
                       headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
    return Response(resp.iter_content(chunk_size=64 * 1024), mimetype=ctype,
                   headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})

# ═════════════════════════════════════════════════════════
#  Flask 代理应用
# ═════════════════════════════════════════════════════════
app = Flask(__name__)

@app.route(PROXY_PATH)
def route_m3u8():
    """入口：/gdws.m3u8 → 获取并重写播放列表"""
    custom = request.args.get("src", "").strip() or None
    try:
        play_url = get_play_url(custom_source=custom)
    except Exception as e:
        return Response(f"获取直播地址失败: {e}", status=502, mimetype="text/plain")
    try:
        return proxy_request(play_url, is_m3u8_list=True)
    except Exception as e:
        return Response(f"上游请求失败: {e}", status=502, mimetype="text/plain")

@app.route("/segment")
def route_segment():
    """代理 ts 分片：/segment?u=<base64_url>"""
    u = request.args.get("u", "")
    if not u:
        return Response("missing u", status=400)
    try:
        target = base64.urlsafe_b64decode(u).decode("utf-8")
    except Exception:
        return Response("bad base64", status=400)
    try:
        return proxy_request(target, is_m3u8_list=False)
    except Exception as e:
        return Response(f"segment proxy error: {e}", status=502)

@app.route("/refresh")
def route_refresh():
    """强制刷新播放地址缓存"""
    try:
        url = get_play_url(force=True, custom_source=request.args.get("src", "").strip() or None)
        return Response(f"refreshed: {url}", mimetype="text/plain")
    except Exception as e:
        return Response(f"refresh failed: {e}", status=502, mimetype="text/plain")

@app.route("/")
def route_index():
    port = request.environ.get("SERVER_PORT", DEFAULT_PORT)
    host = request.host.split(":")[0]
    local = f"http://127.0.0.1:{port}{PROXY_PATH}"
    lan   = f"http://{host}:{port}{PROXY_PATH}"
    return Response(f"""
<!doctype html><meta charset=utf-8><title>广东卫视直播代理</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.7}}
code{{background:#f3f3f3;padding:2px 6px;border-radius:3px;word-break:break-all}}
.h{{color:#c00}}.ok{{color:#080}}</style>
<h2><span class="ok">●</span> 广东卫视 直播代理运行中</h2>
<h3>播放地址</h3>
<p>本机：<code>{local}</code></p>
<p>局域网：<code>{lan}</code></p>
<h3>使用方式</h3>
<ol>
<li>复制上方地址，粘贴到 <b>VLC / PotPlayer / IINA / FFmpeg</b> 等播放器</li>
<li>或浏览器安装 Native HLS 插件后直接打开</li>
<li>或在代码里用 <code>ffmpeg -i URL output.mp4</code> 录制</li>
</ol>
<h3>API</h3>
<ul>
<li><code>/refresh</code> — 强制刷新源地址</li>
<li><code>/gdws.m3u8?src=URL</code> — 手动指定 m3u8 源</li>
</ul>
<p class="h">提示：如无法播放，先访问 /refresh 刷新，或检查网络是否能访问上游 CDN。</p>
""", mimetype="text/html")

# ═════════════════════════════════════════════════════════
#  命令行入口
# ═════════════════════════════════════════════════════════
def cmd_extract():
    """仅提取并打印 m3u8 地址"""
    custom = None
    for i, a in enumerate(sys.argv):
        if a == "--source" and i + 1 < len(sys.argv):
            custom = sys.argv[i + 1]
    try:
        url = get_play_url(force=True, custom_source=custom)
    except Exception as e:
        print(f"[✗] 提取失败: {e}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  频道    : 广东卫视 (ID={TV_CHANNEL_ID})")
    print(f"  m3u8    : {url}")
    print(f"{'='*60}\n")

    # 尝试拉取并展示前几行
    try:
        r = requests.get(url, headers=_upstream_headers(), timeout=10)
        print(f"  HTTP {r.status_code}  Content-Type: {r.headers.get('Content-Type','?')}")
        print(f"  ── 前 15 行 ──")
        for line in r.text.splitlines()[:15]:
            print(f"  {line}")
    except Exception as e:
        print(f"  预览失败: {e}")

def cmd_proxy():
    """启动 Flask 代理"""
    port = DEFAULT_PORT
    host = DEFAULT_HOST
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        if a == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]

    print(f"[*] 广东卫视直播代理启动中...")
    print(f"[*] 播放地址: http://127.0.0.1:{port}{PROXY_PATH}")
    print(f"[*] 网页面板: http://127.0.0.1:{port}/")
    print(f"[*] 按 Ctrl+C 停止")
    app.run(host=host, port=port, threaded=True, debug=False)

def cmd_play():
    """调用系统默认播放器直接播放"""
    import subprocess, sys as _sys
    custom = None
    for i, a in enumerate(_sys.argv):
        if a == "--source" and i + 1 < len(_sys.argv):
            custom = _sys.argv[i + 1]
    url = get_play_url(force=True, custom_source=custom)
    print(f"[▶] 播放: {url}")
    # macOS
    if _sys.platform == "darwin":
        subprocess.Popen(["open", url])
    # Windows
    elif _sys.platform.startswith("win"):
        os.startfile(url)
    # Linux
    else:
        subprocess.Popen(["xdg-open", url])

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--extract" in args:
        cmd_extract()
    elif "--play" in args:
        cmd_play()
    else:
        # 默认启动代理
        cmd_proxy()import time
import json
import base64
import hashlib
import hmac
import subprocess
import threading
from urllib.parse import urljoin, urlparse, quote

import requests
from flask import Flask, Response, request

# ═════════════════════════════════════════════════════════
#  配置区
# ═════════════════════════════════════════════════════════
TV_CHANNEL_ID   = 43
API_URL_TPL     = "https://gdtv-api.gdtv.cn/api/tv/v2/tvChannel/{}?tvChannelPk={}&node=web_pc"
REFERER         = "https://www.gdtv.cn"
ORIGIN          = "https://www.gdtv.cn"
UA              = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 备选公开源（自动检测可用性）
FALLBACK_SOURCES = [
    "http://tcdn.itouchtv.cn/live/gdws.m3u8",
    "http://tv.gdtv.ah.cn/live/01.m3u8",
    "http://baidu.live.cqccn.com/__cl/cg:live/__c/guangdongHD/__op/default/__f//index.m3u8",
    # 移动 IPTV 源（仅限对应运营商网络内可用）
    "http://rrs03.hw.gmcc.net:8088/PLTV/651/224/3221227161/1.m3u8",
]

# WASM 签名相关（可选，需要 Node.js 环境）
WASM_JS_PATH    = os.path.join(os.path.dirname(__file__), "lizhi_sign.js")
NODE_BIN        = "node"

# 代理默认配置
DEFAULT_PORT    = 8080
DEFAULT_HOST    = "0.0.0.0"
PROXY_PATH      = "/gdws.m3u8"

# 源地址缓存
CACHE_SECONDS   = 600
_cache = {"url": None, "expire": 0, "lock": threading.Lock()}

# ═════════════════════════════════════════════════════════
#  工具函数
# ═════════════════════════════════════════════════════════
def _upstream_headers(referer=REFERER, origin=ORIGIN):
    return {"User-Agent": UA, "Referer": referer, "Origin": origin}

def _is_m3u8(text):
    """判断响应是否为 m3u8 播放列表"""
    head = text.lstrip().lower()
    return head.startswith("#extm3u") or "application/vnd.apple.mpegurl" in head

def _extract_m3u8_urls(text):
    """从网页/JSON 中提取 m3u8 链接"""
    import re
    return list(set(re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", text)))

# ═════════════════════════════════════════════════════════
#  方式 A：通过 Node.js + WASM 获取签名后的播放地址
# ═════════════════════════════════════════════════════════
def fetch_via_nodejs(channel_id=TV_CHANNEL_ID):
    """
    调用 lizhi_sign.js 获取签名 headers，再请求 API 拿 playUrl。
    返回 m3u8 地址字符串，失败抛异常。
    """
    if not os.path.exists(WASM_JS_PATH):
        raise FileNotFoundError(f"未找到 WASM 签名脚本: {WASM_JS_PATH}")

    api_url = API_URL_TPL.format(channel_id, channel_id)
    # 调用 Node.js 获取签名 headers
    result = subprocess.run(
        [NODE_BIN, WASM_JS_PATH, "GET", api_url],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node.js 签名失败: {result.stderr[:300]}")

    try:
        headers = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"签名脚本输出无法解析: {result.stdout[:200]}")

    headers.update(_upstream_headers())
    resp = requests.get(api_url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    play_url_obj = json.loads(data.get("playUrl", "{}"))
    url = play_url_obj.get("hd") or play_url_obj.get("sd") or play_url_obj.get("url")
    if not url:
        raise RuntimeError(f"API 未返回 playUrl: {data}")
    return url

# ═════════════════════════════════════════════════════════
#  方式 B：备选公开源自动检测
# ═════════════════════════════════════════════════════════
def detect_fallback_source():
    """遍历备选源，返回第一个可用的 m3u8 地址"""
    for url in FALLBACK_SOURCES:
        try:
            r = requests.get(url, headers=_upstream_headers(), timeout=6, allow_redirects=True)
            if r.status_code == 200 and _is_m3u8(r.text):
                print(f"  ✓ 备选源可用: {url}")
                return url
            else:
                print(f"  ✗ {url} -> HTTP {r.status_code}")
        except Exception as e:
            print(f"  ✗ {url} -> {type(e).__name__}: {str(e)[:80]}")
    return None

# ═════════════════════════════════════════════════════════
#  统一获取入口（带缓存 + 多级降级）
# ═════════════════════════════════════════════════════════
def get_play_url(force=False, custom_source=None):
    """
    获取广东卫视 m3u8 播放地址。
    优先级：自定义源 > Node.js签名 > 备选源检测。
    结果缓存 10 分钟。
    """
    now = time.time()
    with _cache["lock"]:
        if not force and _cache["url"] and now < _cache["expire"]:
            return _cache["url"]

    # 1) 用户手动指定
    if custom_source:
        url = custom_source
        print(f"[1] 使用用户指定源: {url}")
    else:
        # 2) 尝试 Node.js + WASM 签名
        try:
            url = fetch_via_nodejs()
            print(f"[2] 通过 API 签名获取: {url}")
        except Exception as e:
            print(f"[2] API 签名方式不可用: {str(e)[:120]}")
            # 3) 备选源检测
            url = detect_fallback_source()
            if not url:
                raise RuntimeError(
                    "所有源均不可用。请检查网络，或使用 --source 手动指定 m3u8 地址。"
                )

    with _cache["lock"]:
        _cache["url"] = url
        _cache["expire"] = now + CACHE_SECONDS
    return url

# ═════════════════════════════════════════════════════════
#  m3u8 列表重写（将相对路径转为代理绝对路径）
# ═════════════════════════════════════════════════════════
def rewrite_m3u8(m3u8_text, base_url, proxy_base):
    """
    重写 m3u8 内容：
    - 将相对 URL 转为上游绝对 URL
    - 将分片 URL 包装为 /segment?u=<b64> 走代理
    """
    lines = []
    for line in m3u8_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if stripped.startswith("http://") or stripped.startswith("https://"):
            target = stripped
        else:
            target = urljoin(base_url, stripped)
        # 对分片/子列表做 base64 编码后交给 /segment 代理
        encoded = base64.urlsafe_b64encode(target.encode()).decode()
        lines.append(f"{proxy_base}/segment?u={encoded}")
    return "\n".join(lines) + "\n"

# ═════════════════════════════════════════════════════════
#  上游请求 + 流式转发
# ═════════════════════════════════════════════════════════
def proxy_request(target_url, is_m3u8_list=False):
    """向上游请求资源并流式返回，自动附加防盗链头"""
    resp = requests.get(target_url, headers=_upstream_headers(), stream=True, timeout=15)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if is_m3u8_list or "mpegurl" in ctype or target_url.endswith(".m3u8"):
        text = resp.text
        proxy_base = f"http://{request.host}{PROXY_PATH}"
        rewritten = rewrite_m3u8(text, target_url, proxy_base)
        return Response(rewritten, mimetype="application/vnd.apple.mpegurl",
                       headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
    return Response(resp.iter_content(chunk_size=64 * 1024), mimetype=ctype,
                   headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})

# ═════════════════════════════════════════════════════════
#  Flask 代理应用
# ═════════════════════════════════════════════════════════
app = Flask(__name__)

@app.route(PROXY_PATH)
def route_m3u8():
    """入口：/gdws.m3u8 → 获取并重写播放列表"""
    custom = request.args.get("src", "").strip() or None
    try:
        play_url = get_play_url(custom_source=custom)
    except Exception as e:
        return Response(f"获取直播地址失败: {e}", status=502, mimetype="text/plain")
    try:
        return proxy_request(play_url, is_m3u8_list=True)
    except Exception as e:
        return Response(f"上游请求失败: {e}", status=502, mimetype="text/plain")

@app.route("/segment")
def route_segment():
    """代理 ts 分片：/segment?u=<base64_url>"""
    u = request.args.get("u", "")
    if not u:
        return Response("missing u", status=400)
    try:
        target = base64.urlsafe_b64decode(u).decode("utf-8")
    except Exception:
        return Response("bad base64", status=400)
    try:
        return proxy_request(target, is_m3u8_list=False)
    except Exception as e:
        return Response(f"segment proxy error: {e}", status=502)

@app.route("/refresh")
def route_refresh():
    """强制刷新播放地址缓存"""
    try:
        url = get_play_url(force=True, custom_source=request.args.get("src", "").strip() or None)
        return Response(f"refreshed: {url}", mimetype="text/plain")
    except Exception as e:
        return Response(f"refresh failed: {e}", status=502, mimetype="text/plain")

@app.route("/")
def route_index():
    port = request.environ.get("SERVER_PORT", DEFAULT_PORT)
    host = request.host.split(":")[0]
    local = f"http://127.0.0.1:{port}{PROXY_PATH}"
    lan   = f"http://{host}:{port}{PROXY_PATH}"
    return Response(f"""
<!doctype html><meta charset=utf-8><title>广东卫视直播代理</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.7}}
code{{background:#f3f3f3;padding:2px 6px;border-radius:3px;word-break:break-all}}
.h{{color:#c00}}.ok{{color:#080}}</style>
<h2><span class="ok">●</span> 广东卫视 直播代理运行中</h2>
<h3>播放地址</h3>
<p>本机：<code>{local}</code></p>
<p>局域网：<code>{lan}</code></p>
<h3>使用方式</h3>
<ol>
<li>复制上方地址，粘贴到 <b>VLC / PotPlayer / IINA / FFmpeg</b> 等播放器</li>
<li>或浏览器安装 Native HLS 插件后直接打开</li>
<li>或在代码里用 <code>ffmpeg -i URL output.mp4</code> 录制</li>
</ol>
<h3>API</h3>
<ul>
<li><code>/refresh</code> — 强制刷新源地址</li>
<li><code>/gdws.m3u8?src=URL</code> — 手动指定 m3u8 源</li>
</ul>
<p class="h">提示：如无法播放，先访问 /refresh 刷新，或检查网络是否能访问上游 CDN。</p>
""", mimetype="text/html")

# ═════════════════════════════════════════════════════════
#  命令行入口
# ═════════════════════════════════════════════════════════
def cmd_extract():
    """仅提取并打印 m3u8 地址"""
    custom = None
    for i, a in enumerate(sys.argv):
        if a == "--source" and i + 1 < len(sys.argv):
            custom = sys.argv[i + 1]
    try:
        url = get_play_url(force=True, custom_source=custom)
    except Exception as e:
        print(f"[✗] 提取失败: {e}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  频道    : 广东卫视 (ID={TV_CHANNEL_ID})")
    print(f"  m3u8    : {url}")
    print(f"{'='*60}\n")

    # 尝试拉取并展示前几行
    try:
        r = requests.get(url, headers=_upstream_headers(), timeout=10)
        print(f"  HTTP {r.status_code}  Content-Type: {r.headers.get('Content-Type','?')}")
        print(f"  ── 前 15 行 ──")
        for line in r.text.splitlines()[:15]:
            print(f"  {line}")
    except Exception as e:
        print(f"  预览失败: {e}")

def cmd_proxy():
    """启动 Flask 代理"""
    port = DEFAULT_PORT
    host = DEFAULT_HOST
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        if a == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]

    print(f"[*] 广东卫视直播代理启动中...")
    print(f"[*] 播放地址: http://127.0.0.1:{port}{PROXY_PATH}")
    print(f"[*] 网页面板: http://127.0.0.1:{port}/")
    print(f"[*] 按 Ctrl+C 停止")
    app.run(host=host, port=port, threaded=True, debug=False)

def cmd_play():
    """调用系统默认播放器直接播放"""
    import subprocess, sys as _sys
    custom = None
    for i, a in enumerate(_sys.argv):
        if a == "--source" and i + 1 < len(_sys.argv):
            custom = _sys.argv[i + 1]
    url = get_play_url(force=True, custom_source=custom)
    print(f"[▶] 播放: {url}")
    # macOS
    if _sys.platform == "darwin":
        subprocess.Popen(["open", url])
    # Windows
    elif _sys.platform.startswith("win"):
        os.startfile(url)
    # Linux
    else:
        subprocess.Popen(["xdg-open", url])

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--extract" in args:
        cmd_extract()
    elif "--play" in args:
        cmd_play()
    else:
        # 默认启动代理
        cmd_proxy()
