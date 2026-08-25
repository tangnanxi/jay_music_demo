# 曲风大闯关 · 部署说明

## 目录内容
- `server.py` —— Python 薄后端（BFF）：持 app_key、算 `X-QYOPI-Sign` 签名、代调 QQ 音乐搜索接口、托管前端页面。
- `index.html` —— 前端 H5（已接 `/api/song`，拿不到时回退内置视觉）。

前端和后端由同一个 `server.py` 同源提供，**不需要处理 CORS**。

---

## 一、本地先跑通（可选）
```bash
cd qfdgg_realdata
python3 server.py          # 未配 key -> MOCK 模式，浏览器开 http://127.0.0.1:8000
```
首页「主页」底部会出现一张「稻香 · 周杰伦」曲目卡（MOCK 模式标注"内置数据"）。

配上 key 后即调真实接口：
```bash
export QQ_APP_ID=你的appid
export QQ_APP_KEY=你的appkey
python3 server.py
```
> app_key 只放环境变量，**不要写进代码或提交到仓库**。

---

## 二、部署到云服务器（Linux）

### 1. 上传文件
```bash
scp -r qfdgg_realdata user@你的服务器IP:/opt/qfdgg
```

### 2. 确认 Python3（一般自带）
```bash
python3 --version   # >=3.7 即可，server.py 只用标准库，无需 pip 安装
```

### 3. ⚠️ 最关键：把服务器出口 IP 加白名单
QQ 音乐开放平台通常要求把**调用方服务器的公网出口 IP** 加入白名单，否则签名再对也会被拒。
- 查本机出口 IP：`curl ifconfig.me`
- 到 QQ 音乐开放平台后台，把这个 IP 填进应用的 IP 白名单（找不到入口就联系对接同学）。
- 若先用测试环境，网关可切到 `test.y.qq.com`（通过环境变量 `QQ_GATEWAY` 覆盖）。

### 4. 用 systemd 常驻（推荐）
新建 `/etc/systemd/system/qfdgg.service`：
```ini
[Unit]
Description=Qufeng Escape Backend
After=network.target

[Service]
WorkingDirectory=/opt/qfdgg
Environment=PORT=8000
Environment=QQ_APP_ID=你的appid
Environment=QQ_APP_KEY=你的appkey
# 可选：拿播放 url 需用户登录态
# Environment=QQ_OPEN_ID=...
# Environment=QQ_ACCESS_TOKEN=...
ExecStart=/usr/bin/python3 /opt/qfdgg/server.py
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```
> `server.py` 默认只监听 `127.0.0.1`。若用下面的 nginx 反代（推荐），保持不变即可；若想直接对公网暴露，把 `server.py` 里 `("127.0.0.1", PORT)` 改成 `("0.0.0.0", PORT)`。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qfdgg
sudo systemctl status qfdgg
```

### 5. nginx 反代 + HTTPS（强烈推荐）
手机端 H5 基本要求 HTTPS。用 nginx 在前面挡一层，并配证书（Let's Encrypt / 你云厂商的证书）：
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
之后手机浏览器访问 `https://your-domain.com` 即是完整 demo。

### 6. 安全组 / 防火墙
在云控制台放行 **443**（和 80 用于证书签发）。若直接暴露 8000 则放行 8000。

---

## 三、生产注意事项
- `server.py` 用的是 Python 标准库 `http.server`，适合 demo / 低并发路演；**高并发或正式上线**建议换成 gunicorn+Flask/FastAPI，或至少放在 nginx 后面并开多进程。
- 前端 `index.html` 约 6MB（内嵌了音频和场景图）。正式环境建议把媒体拆成外部文件走 CDN，页面会更轻、加载更快（这也是我们之前聊的"数据驱动/资源外部化"的下一步）。
- 播放 url：应用级签名拿到的是"只浏览权限"，`song_play_url` 为空是**预期**；要真播放需接 QPlay Auth 用户登录态且用户为 VIP（二期）。
- 日志：`server.py` 默认静默访问日志，排查问题时可在 `Handler.log_message` 里打开。

---

## 四、接口约定
`GET /api/song?name=稻香` → 返回：
```json
{
  "source": "qqmusic",       // 或 "mock"
  "song_name": "稻香",
  "singer_name": "周杰伦",
  "album_name": "魔杰座",
  "album_pic": "https://.../300x300.jpg",
  "song_mid": "...",
  "song_h5_url": "https://...",   // 可用于"去QQ音乐听"跳转
  "playable": 1,
  "song_play_url": ""             // 未登录/非VIP 为空，属预期
}
```
> 首次接真实接口时，请核对一下真实返回的 JSON 结构（`server.py` 里的 `_find_songs` 已对"是否包一层"做了兼容，但字段以实际为准）。

`GET /api/lyric?name=青花瓷`（或 `?song_mid=xxx` / `?song_id=xxx`，透传 `fcg_music_custom_get_lyric.fcg`）→ 返回：
```json
{
  "source": "qqmusic",       // 或 "mock"（未配 key / 调用失败时回退内置歌词）
  "song_name": "青花瓷",
  "song_mid": "...",
  "lines": ["素胚勾勒出青花笔锋浓转淡", "..."]   // 已剥掉 LRC 时间标签与词曲署名行
}
```
> 只传 `name` 时后端先走搜索拿 `song_mid` 再取歌词；`qinghuaci_p1.html` 的"歌词同步"场景即用此接口实时取《青花瓷》歌词。
# jay_music_demo
