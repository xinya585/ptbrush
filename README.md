# PTBrush

一个自用的 PT 站点刷流工具：Flask 后端 + Vue 3 / Element Plus 前端，提供 Linux 原生二进制（不依赖 Docker）和 Docker 镜像两种运行方式。按 MoviePilot 的站点管理与刷流思路重写，支持多站点、多下载器、免费种筛选、自动删种和可视化仪表盘。

- Linux 安装包：`ptbrush-linux-amd64.tar.gz`（GitHub Releases，原生二进制）
- 镜像：`xinya585/ptbrush:latest`（统一用 latest，不指定版本号）
- 平台：`linux/amd64` / `linux/arm64`（安装包当前提供 amd64）
- 端口：默认 `44445`

## 安装方式

### Linux 原生安装（推荐，无需 Docker）

要求 x86_64、systemd、glibc ≥ 2.31（Debian 11 / Ubuntu 20.04 及以上）。

```bash
curl -fsSL https://raw.githubusercontent.com/xinya585/ptbrush/main/deploy/linux/install.sh | sudo bash
```

自定义端口和数据目录：

```bash
curl -fsSL https://raw.githubusercontent.com/xinya585/ptbrush/main/deploy/linux/install.sh | sudo bash -s -- --port 8080 --data /srv/ptbrush
```

装完打开 `http://<机器IP>:44445`。程序在 `/opt/ptbrush`，数据在 `/var/lib/ptbrush`，systemd 服务名 `ptbrush`。

管理菜单：

```bash
sudo ptbrush
```

菜单包含启动 / 停止 / 重启、升级到最新版、修改端口、查看日志、备份数据、从备份恢复、卸载；也支持命令行 `sudo ptbrush status`、`sudo ptbrush upgrade`、`sudo ptbrush backup`。

离线安装：从 Releases 下载 `ptbrush-linux-amd64.tar.gz`，解压后执行：

```bash
tar -xzf ptbrush-linux-amd64.tar.gz
sudo bash ptbrush/install.sh --asset ./ptbrush-linux-amd64.tar.gz
```

### Docker

见下方「Docker 快速开始」。

## 功能

**站点管理**
- 两种解析器：NexusPHP（填地址 + 浏览器 Cookie，覆盖绝大多数国内 PT 站）、M-Team（x-api-key 鉴权），另支持 RSS 源
- 站点卡片展示上传量 / 下载量 / 分享率 / 做种数，可刷新、编辑、新增、删除；排序只保留「中文名称」和「上传量」
- 批量清理：站点页工具栏有「删除无效站点」（只删最近一次刷新失败为异常的站，待抓取/已停用的保留）和「删除全部站点」（清空站点配置，连带相关的 API Key 与刷流引用），两个按钮都有确认框并列明要删的站
- 站点库（pre-dessert-sites）：195 个站点的域名→中文名映射与 128px 图标，每周一 04:10 自动同步，也可手动「立即更新」

**刷流**
- 选种条件：促销类型（free / free_2x / 2x / half_down / all）、体积区间、做种人数、发布时间、排除 H&R、免费剩余时间门槛
- 运行控制：刷流间隔、检查周期、下载位上限、最小剩余磁盘空间、工作时间段
- 失败站点按退避时间跳过，不会每轮重试
- 下载器可配多个（别名区分），站点按别名绑定；支持账号密码或 qBittorrent v5.2+ 的 API Key

**删种**（每条规则可单独开关）
- 做种时长达到上限
- H&R 种子做种时长
- 分享率达到目标
- 低速下载持续 N 分钟（只看下载速度，不占下载位的那部分种子不会被误删）
- 低速上传持续 N 分钟（盒子专用）：只监测**已经下载完成**的种子，上传速度一直低于阈值（单位 MB/s）满 N 分钟就删掉腾保种位；独立开关、默认关闭，关着时两个数字只是草稿；H&R 种子不参与
- 无上传超时
- 免费结束前 N 分钟
- 促销失效（不再是免费/2X）
- 长期处于等待状态
- 手动删除

删种都会写入记录表，仪表盘按规则分类展示最近 60 条；同一 hash 在 180 秒内重复写入会去重。

**搜片**
- TMDB 搜影视 → 一次用多个 PT 站搜资源，海报墙按相关度排序，点海报直接看简介、评分、演职员
- 资源列表带分辨率 / 促销 / 中字 / 特效字幕、按体积或做种排序，可一键下载种子到下载器
- 搜片站点开关与刷流站点名单完全独立，单独存在 `data/search.json`；TMDB Key、接口地址、语言也在同一页配置，改完立即生效

**其他**
- 仪表盘：总上传/下载、实时速度、剩余空间、活跃种子、刷流站点账号数据（每 10 分钟更新）、删种记录
- CookieCloud 同步：把浏览器 Cookie 按域名写进站点（同一站点分散在多个 cookie 域时自动合并，不会只写一半），公共服务器默认 `https://cookie.scy.cc.cd`
- 登录密码保护、日志页自动刷新（8 秒）、系统设置里可改密码
- 备份与恢复：系统设置里「导出备份」把配置、库、账号数据、缓存打成一个 zip；换设备时在新机器「导入备份」，导入后点「重启生效」即恢复（导入前自动再备份一份，中途失败会回滚）
- 升级：Docker 版换镜像即可（`docker compose pull && docker compose up -d`，`data` 目录不动）；Windows 便携版托盘右键「检查更新」自动下载替换

## Docker 快速开始

```yaml
services:
  ptbrush:
    image: xinya585/ptbrush:latest
    container_name: ptbrush
    restart: always
    network_mode: host
    environment:
      - PUID=1000
      - PGID=1000
      - WEB_PORT=44445
    volumes:
      - /path/to/ptbrush/data:/app/data
```

```bash
docker compose up -d
```

浏览器打开 `http://<NAS-IP>:44445`。

### 环境变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `PUID` | 运行容器的用户 UID，挂载目录的属主要对上 | 镜像内 `app` 用户 |
| `PGID` | 对应 GID | 同上 |
| `WEB_PORT` | Web 端口，示例统一 `44445` | `44445` |

### 为什么用 host 网络

PTBrush 要直接访问 qBittorrent，宿主机上的 qB 填 `http://127.0.0.1:<端口>` 最省事；而且站点抓取、CookieCloud 都按容器的真实网络走。用 host 网络时容器和宿主共用端口空间，`WEB_PORT` 不能和已有服务冲突。若要改成桥接，把 `network_mode` 换成 `ports: ["44445:44445"]`，qB 地址改填宿主 IP。

## 首次使用

1. 打开页面，用 `data/config.toml` 里 `[web].password` 登录；该项默认为空，建议进「系统设置」先设一个密码。
2. 「下载器配置」里加 qBittorrent：别名、地址、账号密码（或 API Key）、默认保存路径。第一个是主下载器，剩余空间/任务数/限速按它判断。
3. 「站点配置」里加站点：NexusPHP 站填地址 + Cookie；M-Team 填地址 + x-api-key。站点上的「下载器」按别名选。
4. 「刷流配置」里选参与刷流的站点（开关改动立即保存），再按需调选种条件与删种规则。页面上有「默认值」和「抢上传推荐值」两个按钮可一键回填。
5. 「仪表盘」看实时状态与删种记录。

## 刷流机制要点

- 刷流任务按「刷流间隔」跑一轮；「检查周期」负责检查已下载种子的状态（做种、删种）。
- 只有促销类型命中、体积/做种数/发布时间满足、且不在 H&R 排除范围的种子才会被加入。
- 剩余磁盘低于阈值、或下载位已满时不会再加种。
- 活动种子数超过下载位上限不会立刻删，交给删种规则去处理。
- 站点连续失败会按「加种失败退避」跳过，避免撞 429；站点 429 时也会进入冷却。

## 数据目录

容器内 `/app/data`，建议挂到宿主固定目录：

```
data/
├── config.toml        # 全部配置（刷流、下载器、站点、Web 密码）
├── ptbrush.db         # SQLite：站点数据、种子状态、刷流失败记录、删种记录
├── ptbrush.log        # 运行日志（10MB 轮转，保留 10 天）
├── brush_report.json  # 每轮刷流的诊断快照
├── backups/           # 导出的备份包、导入前自动备份
└── predessert/        # 站点库（图标、适配映射、同步报告）
```

备份只需要拷 `data/` 目录；换机器时整目录搬过去即可。

## 常用接口

页面之外也可以用 HTTP 直接查：

- `GET /api/version` — 版本（免登录）
- `GET /api/config` — 全部配置
- `GET /api/sites` — 站点列表与账号数据
- `GET /api/stats` — 下载器实时状态与种子列表
- `GET /api/brush/report` — 最近一轮刷流诊断
- `GET /api/brush/deletions?limit=60` — 删种记录
- `POST /api/brush/run` — 手动跑一轮刷流
- `GET /api/backup/info` — 备份状态与已有备份包
- `GET /api/backup/export` — 导出备份 zip
- `POST /api/backup/import` — 导入备份（multipart 上传）
- `POST /api/backup/restart` — 导入后重启生效

## 常见问题

**Q：qB 登录失败？**
先确认容器能访问该地址（host 网络下 qB 在同一台机器就用 `http://127.0.0.1:<端口>`）、账号密码正确、qB 的 WebUI 没有被 IP 白名单限制。「下载器配置」里有「测试连接」，失败原因会直接提示；qB v5.2.0 以上也可以改用 API Key 登录，绕过密码。

**Q：站点报「读取站点失败」**
多数是 Cookie 过期或被站点反爬拦了，先在浏览器重新登录该站、更新 Cookie，再看日志页的具体错误。

**Q：改了刷流间隔没生效？**
刷流配置保存后会重排调度，但当前正在跑的那一轮不会中断；等这一轮结束才按新间隔走。

**Q：删种记录能留多久？**
没有过期清理，只按条数展示最近 60 条，历史都在 `ptbrush.db` 的 `brushdelrecord` 表里。

**Q：容器版怎么升级？**
`docker compose pull && docker compose up -d`。容器里的代码在镜像内，程序没法替换自己，所以设置页只给这条命令，不提供「下载安装包」按钮；`data` 目录不动，配置和记录都在。

**Q：换机器怎么迁移？**
旧机器「系统设置 → 备份与恢复 → 导出备份」，新机器装好后「导入备份」再点「重启生效」。站点配置、下载器、CookieCloud 凭据、账号数据、缓存都会一起搬过去。

## 版本

统一用 `latest`，部署时不用写版本号，`latest` 始终指向最新构建。实际版本看页面左上角或 `GET /api/version`，`VERSION` 文件用于构建与 Windows 升级清单比对。

## 说明

本项目基于 [huihuidehui/ptbrush](https://hub.docker.com/r/huihuidehui/ptbrush) 的运行时与依赖，在其之上重写了站点管理、选种/删种逻辑与前端界面；容器镜像以该镜像为基础层，只替换 `/app` 下的源码。
## 从源码运行与构建

源码直接跑：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py --port 44445 --data ./data
```

构建 Linux 原生包（需要 Docker，产物在 `dist/`）：

```bash
bash deploy/linux/build.sh
```

脚本会打出 `dist/ptbrush-linux-amd64.tar.gz` 和对应的 `.sha256`。安装器与菜单源码在 `deploy/linux/`。