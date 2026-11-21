# 📕 考研英语红宝书 · 网页版 (Web Red Book)

**暖纸书排版 · 真题例句高亮 · 按单元异步加载 · SQLite 多端云同步**

![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?style=flat-square&logo=sqlite&logoColor=white)
![Zero Dependency](https://img.shields.io/badge/Dependencies-Zero-2EA44F?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-B91C1C?style=flat-square)

</div>

---

## ✨ 界面预览


| 暖纸书主界面 & 真题例句高亮） | 速刷模式  |
| :---: | :---: |
| <img width="2549" height="1254" alt="image" src="https://github.com/user-attachments/assets/ff630b18-29de-43b2-ae97-1d16ba4a037f" /> | <img width="2549" height="1254" alt="image" src="https://github.com/user-attachments/assets/fbae15cf-d4ff-4c66-8d34-39068fcd712b" />|



全屏沉浸式闪卡模式

<img width="2549" height="1254" alt="image" src="https://github.com/user-attachments/assets/c79cb2d5-334e-4f03-a680-1aa1f8a5ae76" />


---

## 🚀 核心特性

- **极简零依赖架构**：单文件前端（`index.html`）+ Python 原生后端（`server.py`）+ SQLite 持久化，无需 `pip install`，无需配置复杂数据库。
- **按单元异步加载 + 智能预取**：启动仅加载约 26 KB 的目录元数据，点选单元按需拉取（内存 Gzip 压缩 + ETag 强缓存），空闲时自动静默预加载下一单元，1M 小水管服务器也能秒开。
- **真题语境深度复习**：内置 6500+ 考研词汇与历年真题例句，自动按词形变化正则高亮目标词，支持阅读/完形/翻译/选项分类筛选及英美双语发音。
- **三种高效刷词模式**：支持**卡片精读**、**双列速刷**、**3D 翻转闪卡**，并提供「遮挡释义 / 遮挡单词」自测遮罩。
- **多端云端同步**：掌握状态、生词本、深色模式与上次背诵单元实时落盘到服务端 `redbook.db`，手机/电脑无缝接力，清缓存不丢记录，同时支持 JSON 导入导出备份。

---

## 📂 目录结构

```text
.
├── index.html               # 单文件前端（响应式适配手机与桌面）
├── server.py                # 零依赖 Python 后端（按需加载 + Gzip + SQLite）
├── vocab_shuffled.json      # 乱序版词库（默认加载）
└── vocab_alphabetical.json  # 字母顺序版词库（可按需切换）
```

> **如何切换为字母顺序版？**  
> 将 `server.py` 中的 `DATA_FILE` 改为 `vocab_alphabetical.json` 并重启服务即可。

---

## 🛠️ 快速开始

### 1. 本地运行

```bash
git clone [https://github.com/shuang154/kaoyan-redbook.git](https://github.com/shuang154/kaoyan-redbook.git)
cd 仓库名
python3 server.py
```

浏览器打开 `http://127.0.0.1:8888` 即可开始背词。

### 2. 云服务器后台常驻（Systemd + Nginx）

1. 创建系统服务 `/etc/systemd/system/redbook.service`：

```ini
[Unit]
Description=Web Red Book Vocabulary Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/your/repo
ExecStart=/usr/bin/python3 /path/to/your/repo/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
systemctl daemon-reload && systemctl enable --now redbook
```

2. 在 Nginx 站点配置中添加反向代理：

```nginx
location /redbook/ {
    proxy_pass [http://127.0.0.1:8888/](http://127.0.0.1:8888/);
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

重载生效：

```bash
nginx -t && systemctl reload nginx
```

---

## ⌨️ 闪卡快捷键

| 按键 | 功能 |
| :---: | :--- |
| `Space` | 翻转卡片查看释义与例句 |
| `←` / `→` | 上一个 / 下一个单词 |
| `1` | 标记「已掌握」并跳下一词 |
| `2` | 加入 / 移出「生词本」 |
| `S` | 朗读当前单词发音 |
| `Esc` | 退出闪卡模式 |

---

## 📄 开源协议

本项目基于 [MIT License](https://opensource.org/licenses/MIT) 开源，欢迎 Star 和 PR。
