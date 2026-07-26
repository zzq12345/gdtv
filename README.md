# gdtv
# 广东卫视 (荔枝网) — m3u8 提取 & 转发代理

## 功能

通过 Python 提取广东卫视直播 m3u8 地址，并通过本地 HTTP 代理转发，绕过防盗链，可直接在 VLC / PotPlayer / FFmpeg 等工具中播放。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 仅提取并打印 m3u8 地址
python gdtv_proxy.py --extract

# 3. 启动本地代理（默认端口 8080）
python gdtv_proxy.py --proxy

# 4. 在播放器中打开
#    http://127.0.0.1:8080/gdws.m3u8
