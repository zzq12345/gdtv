#!/usr/bin/env python3
"""
强制使用 Node.js + WASM 签名获取广东卫视直播 m3u8，
不采用任何备选源，若签名失败直接退出。
"""
import sys
import os
import subprocess
import json
import requests
from gdtv_proxy import TV_CHANNEL_ID, API_URL_TPL, _upstream_headers

# 从 gdtv_proxy 导入常量
WASM_JS_PATH = os.path.join(os.path.dirname(__file__), "lizhi_sign.js")
NODE_BIN = "node"

def fetch_signed_url():
    """只走签名路径，不降级"""
    if not os.path.exists(WASM_JS_PATH):
        raise FileNotFoundError(f"签名脚本 {WASM_JS_PATH} 不存在")

    api_url = API_URL_TPL.format(TV_CHANNEL_ID, TV_CHANNEL_ID)
    # 调用 Node.js
    result = subprocess.run(
        [NODE_BIN, WASM_JS_PATH, "GET", api_url],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node.js 签名失败: {result.stderr[:300]}")

    try:
        headers = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"签名输出无法解析: {result.stdout[:200]}")

    headers.update(_upstream_headers())
    resp = requests.get(api_url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    play_url_obj = json.loads(data.get("playUrl", "{}"))
    url = play_url_obj.get("hd") or play_url_obj.get("sd") or play_url_obj.get("url")
    if not url:
        raise RuntimeError(f"API 未返回 playUrl: {data}")
    return url

def main():
    try:
        url = fetch_signed_url()
        with open("gdws.m3u", "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            f.write(url + "\n")
        print(f"✅ 成功生成 gdws.m3u，地址：{url}")
    except Exception as e:
        print(f"❌ 生成失败：{e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
