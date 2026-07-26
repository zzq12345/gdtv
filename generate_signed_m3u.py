#!/usr/bin/env python3
"""
生成广东卫视直播 m3u 播放列表。
优先使用 WASM 签名，若签名失败或 wasm 缺失，则自动降级到备选公开源。
"""
import sys
from gdtv_proxy import get_play_url

def main():
    try:
        # force=True 强制刷新缓存，custom_source=None 表示自动选择
        url = get_play_url(force=True, custom_source=None)
        with open("gdws.m3u", "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            f.write(url + "\n")
        print(f"✅ 成功生成 gdws.m3u，地址：{url}")
    except Exception as e:
        print(f"❌ 生成失败：{e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()    resp = requests.get(api_url, headers=headers, timeout=15)
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
