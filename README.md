# uproxy · Unraid 出网加速编排工具

解决 Unraid 上 Docker 拉镜像慢/超时、CA 应用商店打不开、插件从 GitHub 更新失败的问题。

它**不是**又一个代理客户端，而是一层**配置编排**：把你选定的出口，
翻译成 Unraid 四个位置各自认识的配置，并负责备份、校验、持久化与还原。

## 为什么需要它

Unraid 上这四个位置互相独立、写法各异、且全在内存里（重启即丢）：

| 目标 | 位置 | 说明 |
|---|---|---|
| `docker pull` | `/usr/local/etc/rc.d/rc.docker` 的 `nohup $UNSHARE ...` 行 | 注入 `env http_proxy/https_proxy`，只影响 dockerd，不干扰其他容器网络 |
| 镜像加速 | `/etc/docker/daemon.json` 的 `registry-mirrors` | 走国内镜像站，不需要代理 |
| CA 应用商店 | `/boot/config/plugins/community.applications/proxy.cfg` | `port=` / `tunnel=` / `proxy=http://或socks5://`，改完需重启才生效 |
| 系统出网 | `/etc/profile.d/uproxy.sh` + `/boot/config/go` | 覆盖 wget / git / 插件下载 |

全部改动会同步写入 `/boot/config/go` 的托管块，开机自动重放。

## 快速开始

三种方式任选其一，**推荐方式一**（Unraid 默认不带 docker compose，方式二需要你先装 Compose 插件）。

### 方式一：一键脚本（推荐）

```bash
# 把整个项目目录传到 Unraid 上，然后：
chmod +x install.sh && ./install.sh
```

脚本会自动：建目录 → 拷源码 → 装 Unraid 容器模板 → `docker build` →
生成随机登录密码 → 启动容器。不依赖 docker compose。

卸载：`./uninstall.sh`（会先提示你去 WebUI 移除开机重放块并还原文件）。

### 方式二：docker compose

```bash
mkdir -p /mnt/user/appdata/uproxy && cd /mnt/user/appdata/uproxy
docker compose up -d --build
```

### 方式三：Unraid 容器模板

把 `unraid/my-uproxy.xml` 拷到 `/boot/config/plugins/dockerMan/templates-user/`，
再到 Unraid WebUI 的 Docker 页 → Add Container → 模板选 `uproxy`。
（需先把镜像 `docker build -t uproxy:latest .` 出来）

### 访问账号

**强烈建议设置密码**，否则同网段任何人都能改你的 Docker 配置。默认用户名为 `admin`，
密码可用环境变量预设，不设则 `install.sh` 自动生成并打印：

```bash
UPX_PASSWORD="换成你自己的长随机串" ./install.sh
```

打开 `http://<Unraid-IP>:8765`，用「用户名 + 密码」登录（默认 `admin` / 你设的密码）。

## 部署详解：手工部署与更新维护

「快速开始」给了三种首次部署方式（一键脚本 / docker compose / 容器模板）。下面补充**在 Unraid Web 界面手工填表创建容器**、**手工部署的完整手动步骤（命令行）**、**后续如何更新**，以及**验证与排错**，方便你在没有脚本或想完全掌控时操作。

### 〇、在 Unraid Web 界面手工创建容器（逐项对照）

如果你不想用脚本，可以在 Unraid「Docker → 添加容器 → 从零开始」手动填表。**前提**：镜像必须先在 Unraid 上构建好（`docker build -t uproxy:latest .`，或先跑一次 `install.sh` 的构建步骤），否则「存储库」填 `uproxy:latest` 会提示找不到镜像。「存储库(Repository)」就是 Docker 镜像名，本地构建的填 `uproxy:latest` 即可。

逐项对照下表填写：

| 表单字段 | 填什么 |
| --- | --- |
| 名称 | `uproxy` |
| 存储库 | `uproxy:latest` |
| 网络 | `bridge` |
| **WebUI** | **`http://[IP]:[PORT:8765]/`** |
| 额外参数 | `--restart=unless-stopped` |
| 端口 | 主机 `8765` : 容器 `8765`（TCP） |
| 路径 /host/boot | 宿主 `/boot`（读写） |
| 路径 /host/etc/docker | 宿主 `/etc/docker`（读写） |
| 路径 /host/usr/local/etc/rc.d | 宿主 `/usr/local/etc/rc.d`（读写） |
| 路径 /host/etc/profile.d | 宿主 `/etc/profile.d`（读写） |
| 路径 /config | 宿主 `/mnt/user/appdata/uproxy/state`（读写，**务必挂此卷持久化**） |
| 设备 /var/run/docker.sock | 宿主 `/var/run/docker.sock`（**选「设备」类型，不是「路径」**） |
| 变量 UPX_HOST_ROOT | `/host` |
| 变量 UPX_STATE_DIR | `/config` |
| 变量 TZ | `Asia/Shanghai` |
| 变量 UPX_USER | `admin` |
| 变量 UPX_PASSWORD | 你的登录密码（强烈建议设，留空=不校验） |
| 变量 UPX_TOKEN | 同上 |

> 关键点：**WebUI 字段一定要填 `http://[IP]:[PORT:8765]/`**（`[IP]`/`[PORT:8765]` 是 Unraid 占位符，自动替换为本机 IP 与端口）。填了之后，Docker 页面 uproxy 卡片上才会出现可点击的「WebUI」按钮，无需手动输入地址。
> `/config` 必须挂到阵列/缓存上的 `state` 目录，否则重启后配置与密码全丢；Docker 套接字务必选「设备」类型挂载。

### 一、手工部署（完全手动，不依赖 install.sh）

本质就是 `install.sh` 做的事情，拆开手动执行。适合想看清每一步、或在受限环境里分步排错：

```bash
# 1. 准备目录（BASE 可改，但必须放在阵列/缓存上以持久化）
BASE=/mnt/user/appdata/uproxy
SRC=$BASE/src
STATE=$BASE/state
mkdir -p "$SRC" "$STATE" /boot/config/plugins/dockerMan/templates-user

# 2. 取得代码：有 git 就 clone，没有就把项目目录 scp/rsync 到 Unraid 后直接用
git clone https://github.com/bcfxclub/Unraid-uproxy.git "$SRC/repo"
# 或：把本地项目拷过来（排除 .git）
#   rsync -avz --exclude='.git' /本地/uproxy/ "$SRC/repo/"

# 3. 把运行所需文件拷进构建目录（先删再拷，保证干净重建）
rm -rf "$SRC/build" && mkdir -p "$SRC/build"
cp -r "$SRC/repo/app" "$SRC/repo/web" "$SRC/repo/Dockerfile" "$SRC/repo/requirements.txt" "$SRC/build"/

# 4. 安装 Unraid 容器模板（仅当你要用 WebUI 模板建容器时需要；用下面第 6 步手动 docker run 可跳过）
cp "$SRC/repo/unraid/my-uproxy.xml" /boot/config/plugins/dockerMan/templates-user/

# 5. 构建镜像（镜像源拉不动见末尾「排错」）
docker build -t uproxy:latest "$SRC/build"

# 6. 启动容器（等价于 install.sh 的 docker run）
PW="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"   # 自己设个固定密码更省心
docker rm -f uproxy >/dev/null 2>&1 || true
docker run -d --name uproxy --restart unless-stopped \
  -p 8765:8765 \
  -e "UPX_HOST_ROOT=/host" -e "UPX_STATE_DIR=/config" -e "TZ=Asia/Shanghai" \
  -e "UPX_USER=admin" -e "UPX_PASSWORD=$PW" -e "UPX_TOKEN=$PW" \
  -v /boot:/host/boot \
  -v /etc/docker:/host/etc/docker \
  -v /usr/local/etc/rc.d:/host/usr/local/etc/rc.d \
  -v /etc/profile.d:/host/etc/profile.d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$STATE:/config" \
  uproxy:latest
echo "登录密码：$PW   访问 http://<Unraid-IP>:8765/"
```

> 关键点：`/config` 必须挂到阵列/缓存（即 `/mnt/user/appdata/uproxy/state`），否则重启后配置与备份全丢。挂载宿主机的四个目录（`/boot`、`/etc/docker`、`rc.d`、`profile.d`）是工具改写代理配置的前提，缺一个功能就会受限。

### 二、更新 / 重部署

代码推到 GitHub 后，Unraid 上拉新代码并重建即可。**更新不等于重新配置**：配置都在 `state`，不在代码里，重部署不会丢配置。

**最简（推荐）：用 update.sh**

```bash
cd /mnt/user/appdata/uproxy/repo   # 当初 clone 的目录
./update.sh                        # = git pull --ff-only + install.sh
```

`update.sh` 内部已包含 `git pull` 与重部署；`install.sh` 会**自动沿用已有密码**（界面改过的 `auth.json`，或首次生成的 `state/.deploy_pw` 备份），不会把登录密码重置成新的。

**没有 git 仓库目录时**（例如之前用纯容器模板部署）：先 clone 一次，再 `./update.sh`：

```bash
mkdir -p /mnt/user/appdata/uproxy && cd /mnt/user/appdata/uproxy
git clone https://github.com/bcfxclub/Unraid-uproxy.git repo
cd repo && ./update.sh
```

**纯手工更新**：把上面「手工部署」的第 2–6 步再做一遍（`git pull` 代替 clone、复用同一个 `$PW` 密码即可）。

### 三、验证部署成功

- 浏览器开 `http://<Unraid-IP>:8765` 能登录；
- 「出口」页启动 `sing-box`（或 WARP）后，节点网格里**正在使用的节点会自动标「使用中」**；
- 异常看日志：`docker logs uproxy`，或 WebUI 的「日志」页。

### 四、排错

- **`docker build` 拉不动基础镜像**（报错 `lookup xxx: no such host`）：daemon.json 里的镜像源域名失效。先 `./fix-mirror.sh` 再重试；都不通就给 dockerd 配 `http_proxy` 走出口。
- **WebUI 打不开**：确认 Unraid 防火墙/端口映射，`docker ps` 看 uproxy 是否在跑、`8765` 是否映射。
- **密码不对 / 登不进**：若从没在界面改过密码，重部署后密码以 `install.sh` 打印的为准（已被备份到 `state/.deploy_pw`）；若改过界面密码则永远以 `auth.json` 为准。忘记可删 `state/auth.json` 并重启容器回退到环境变量密码。

## 界面

仿 v2rayA 的深色侧边栏风格（深底 + 金色 accent），左侧分组导航，右侧卡片式内容。

![概览页](docs/screenshot-overview.png)

![出口/订阅页](docs/screenshot-subs.png)

![下发页](docs/screenshot-push.png)

![文档页](docs/screenshot-docs.png)

侧边栏分组：**总览**（仪表板）、**出口**（出口 / 订阅）、**系统**（下发 / 备份与还原 / 日志 / 账号 / 文档）。
顶部状态徽章实时显示是否已接入宿主机，右上角显示当前登录用户与「退出」。

### 节点管理（仿 v2rayA）

「订阅」页的节点区支持两种视图，右上角可切换 **网格 / 列表**：

- **网格卡片**：每个节点一张卡，显示名称、协议、地址、来源订阅，以及**当前是「启用」还是「停用」**。
  **点卡片 = 把出口切到这个节点**（原本停用的会顺手启用），卡片变金色边框 + 右上角「默认」角标；
  左侧勾选框只负责**批量选择**，两者互不干扰。卡片底部显示测速结果（`HTTP 321ms` / `失败`）。
  卡片**自适应平铺**：宽屏一行能放几张就放几张，不会变成一列竖着排下去。
- **进页面就有延迟，默认走 TCP 握手**：打开页面会自动对全部节点做一轮 TCP 握手，
  胶囊上直接出数字，不用先点测速；「测速」按钮默认也是 TCP 握手（3 秒超时，快、不起容器）。
  想看节点**能不能真正出网**，再点 **「目标 ▾」** 换成 Google / GitHub / Cloudflare / 自定义地址——
  这个菜单**只选不跑**，选完再点旁边的 **「测速」** 才开始。HTTP 实测会临时起一个一次性 sing-box 容器，
  让每个节点各自真实访问目标站点，量到的是 HTTP 状态码 + 往返耗时；测完容器立即删除，
  不影响正在运行的出口。拿不到 Docker 时自动降级为 TCP 握手，原因只写在提示条和胶囊的悬停说明里。
- **测速后按延迟排序**：延迟最低的排最前，**超时 / 失败的垫底**，没测过的排在超时之后并保持原有相对顺序。
  排序结果存进 `outlet.sb_order`，重启、重新同步订阅、刷新页面都不会乱；重新测一次就按新结果重排一次。
  目标菜单最底下一项可以关掉这个行为（关闭后测速只更新胶囊、不动顺序）。
- **结果只落在胶囊上**：`HTTP 321ms` / `TCP 279ms` / `TIMEOUT` / `失败`，页面里不再堆一行行文字；
  汇总（可用 x/y、最快 xx ms、已按延迟重排）走一条角落提示，几秒后自己消失。
- **每个节点可单独操作**：卡片上的「启用 / 停用」点一下立刻切换（列表视图的「状态」列同样可点）；
  右上角常显 **垃圾桶按钮**（一键删除，需确认），`⋮` 菜单另外提供
  **HTTP 实测 / TCP 握手测速 / 修改名称 / 分享链接 / 启用或停用 / 设为默认出口 / 删除**。列表视图同一位置也有这两个按钮。
- **批量操作**：勾选多个节点后可「删除所选 / 分享所选 / 启用 / 停用」，顶部有全选。
- **按订阅来源筛选**：节点会打上其来源订阅的标记，「来源」下拉可按订阅分组过滤（如 `uuu-eyi.pages.dev` / `cf.xmrth.one`）。
- **搜索**：按名称 / 协议 / 地址关键字过滤。
- **删除是可恢复的**：删掉的节点会进黑名单（不会因为刷新订阅又冒出来），顶部会出现
  「恢复已删除 (N)」按钮，一键找回。
- **分享链接**：把节点还原成 `vless://` / `vmess://` / `trojan://` / `ss://` / `hysteria2://`
  链接，可直接导入其它客户端（其它协议会提示暂不支持）。

### 订阅卡片与批量管理

「订阅」页每个订阅是一张卡片，显示**名称、节点数、更新时间**和状态，右上角 `⋮` 菜单提供
**更新 / 修改 / 分享全部节点 / 启用停用 / 删除**。顶部工具条支持**全选 / 删除所选 / 更新所选**，
一次删掉多个订阅。

> 删除订阅是**即时**的：直接移除该订阅带来的节点，不会重新联网拉取其它订阅
> （否则别的订阅一超时，删除就会跟着卡住）。停用同理。

「新增订阅 / 粘贴导入」表单默认收起，点按钮展开，页面不会因为表单常驻而被拉长。
每个订阅卡片右上角同样是「垃圾桶 + `⋮`」两个入口。

> 订阅更新时如果某个源这次拉取失败，会**沿用该源上次的节点**，不会因为网络抖一下就把节点清空。

## 布局

内容区宽度自适应并**居中**（最大 1560px）：宽屏下卡片横向铺满，窗口再宽则两侧留白对称，
不会固定挤在左边一条、也不会把页面拉成一长列。

### 日志页（仿 v2rayA「日志」）

「日志」页记录 uproxy 的关键操作（出口启停、订阅拉取/同步、WARP 预检、下发还原），
每条带时间戳、级别、来源模块。顶部标签可切换 **全部 / 错误 / 警告 / 信息 / 调试 / 跟踪**，
支持关键字搜索、按来源模块筛选、可配置的自动刷新间隔，以及一键**导出日志**为文本文件。
日志保存在进程内存（最多 2000 条），重启即清空，不做持久化。

### 多订阅与分流规则

- **多订阅源**：可添加多个订阅并存（含本地粘贴内容），一键「更新全部」或按间隔**自动更新**。
- **分流规则可自己改**：订阅页底部是一张**有序规则表**，从上往下匹配、命中即停，顺序就是优先级。
  每条规则 = 启用开关 + 匹配类型 + 内容 + 动作，可上移 / 下移 / 收起 / 删除 / 加新条：

  | 匹配类型 | 说明 |
  |---|---|
  | 域名后缀 | 匹配该域名及所有子域名，最常用（填 `cn` 等于所有 `.cn` 站点） |
  | 精确域名 | 只匹配这个域名本身 |
  | 域名关键词 | 域名里含这个词就命中 |
  | 域名正则 | RE2 正则，写错会当场报错，不会写进配置 |
  | IP 段 | 按目标 IP 匹配，裸 IP 自动补 `/32` |
  | 目标端口 / 网络类型 / 识别协议 / 进程名 | 更细的条件，进程名仅 TUN 模式可识别 |

  动作有 **走代理 / 直连 / 拦截**，还能**直接指定某一个节点**（例如「Netflix 走日本节点」）。
  底部再选「规则都没命中时」→ 直连，就是黑名单式（只有列出的走代理）；
  → 走代理，就是白名单式（只有列出的走直连，其余全代理）。

- **改完能立刻验证**：填个域名点「测命中」，会告诉你命中第几条、动作是什么，并高亮那条规则；
  点「预览配置」能看到最终下发给 sing-box 的 JSON。都在本地算，不用起容器。
- **一键模板**：国内站点直连 / 开发源加速 / AI 服务走代理 / 流媒体走代理 / 拦截常见广告域名 /
  NAS 局域网直连，套用后照样逐条改。
- **升级不会打乱老配置**：以前用「智能分流 / 全部代理」的老设置，会自动铺成等价的规则表，
  第一次打开就看得见、改得动；保存之后这张表就是唯一依据。
- **启动前先自检**：`sing-box check` 会先验证配置，被拒时直接把 sing-box 的原话显示出来，
  不会出现「容器起来了但秒退、只能看到连不上」。
- **版本自适应**：会探测镜像里 sing-box 的版本，按 1.10 / 1.11 / 1.12+ 分别生成旧式 DNS、
  旧式 block 出站或新的 `action: reject`，`geoip`/`geosite`（1.12 已移除）一概不用。
- **TUN 透明代理**（可选，默认关）：接管整台主机出网，需 `NET_ADMIN`，谨慎开启。

### 登录方式：用户名 + 密码

不再用裸 token。首次打开会弹出登录页（仿 v2rayA 的登录框），输入用户名与密码即可；
登录后浏览器自动记住会话，无需再次输入。默认用户名 `admin`，密码由 `install.sh` 打印
（也可通过环境变量 `UPX_PASSWORD` 预设）。老的 `?token=` 直连链接仍可兼容使用。

**首次登录**：用户名 `admin`、密码 `admin`（初始默认密码，登录后请立刻改掉）。

**在界面里改账号密码**：侧边栏「系统 → 账号」页可以直接改用户名和密码，改完立即生效，
不需要重启容器。凭据存在状态目录的 `auth.json`（PBKDF2-SHA256 + 随机盐，只存哈希不存明文），
优先级为：

```
界面设置的 auth.json  >  环境变量 UPX_PASSWORD  >  默认密码 admin
```

改密码后其它设备上的会话立即失效，当前页面会自动续期，不用重新登录。
用默认密码登录时，账号页会显示醒目提示，提醒尽快修改。

> 安全提示：这是面向家庭局域网的工具，账户不存在「找回密码」流程。忘了密码就删掉状态目录下的
> `auth.json` 并重启容器，即回退到环境变量里的密码，都没有则回退默认密码 `admin`；也可以在 Unraid 终端重跑
> `install.sh` 或给容器重设 `UPX_PASSWORD`。

### 仪表板（首页）

首页是一屏式的卡片仪表板（12 列网格，窄屏自动降成 6 列 / 单列），常用的开关都摆在这里，
不用再翻页找。每张卡片都直接对接真实接口，没有「装饰性」数据：

| 卡片 | 内容 | 点了会怎样 |
|---|---|---|
| **网络速度** | canvas 画的上行/下行实时面积图（1.5s 刷新）、当前速率、累计收发、活跃连接数、各节点连接分布 | 只看，不操作 |
| **核心状态** | 出口活着没（绿点/红点）、出口类型、端口、可用节点数 | 「启动 / 停止」直接拉起或停掉 WARP / sing-box；「日志」跳日志页 |
| **代理组** | 右上分组胶囊（PROXY …）+ 组内节点胶囊 + 该节点延迟 | 分组菜单：勾选当前分组、齿轮改名/选节点、垃圾桶删组、`+ 创建` 新组；节点菜单：`自动（延迟最低）` 或指定某个节点 |
| **出口 / 系统代理** | 出口方式下拉、TUN 透明代理开关、系统环境变量开关 | 改完立即存盘；TUN 需重启 sing-box 生效，系统变量需去「下发」页应用 |
| **规则端口的分流模式** | 大陆白名单 / 全局代理（= 规则表的兜底动作） | 切换后自动保存并重启 sing-box；「分流规则」跳规则表编辑 |
| **节点延迟** | **当前代理组**里最快的几个节点 + 条形图（卡片右上角标着组名） | 「测速」只测这一组并按延迟重排；点某一行把该组固定到它 |
| **订阅** | 订阅数量、每个订阅的节点数与更新时间 | 「全部更新」拉取所有订阅 |
| **宿主机状态 / 连通性体检** | 原来的两张卡原样保留 | 体检功能不变 |

> 流量数据取自出口容器（WARP / sing-box）的真实网络吞吐，连接数仅 sing-box 提供；
> 没启动出口时曲线区会显示「暂无流量」。

#### 代理组（分组）怎么用

节点一多（几十上百个）就没法在一个下拉里挑，所以出口按「分组」组织，和 v2rayA 的代理组一致：

- 右上角的胶囊是**当前分组**，只有这个分组里的节点参与出口选路；点它打开分组菜单——
  `✓` 表示当前分组，齿轮配**测速参数**（探测 URL / 探测间隔 / 类型），垃圾桶删组，
  `+ 创建` 新建。改名和勾选成员在卡片左下角的「编辑分组」里。
- 下面那颗胶囊是**组内节点**：选 `自动（延迟最低）` 时，生成的 sing-box 配置用 `urltest`
  出站，由 sing-box 自己定期测速并在本组里挑最快的那台；选了某个节点则用 `selector`
  固定到它。两种状态都会写进配置，不是界面上的装饰。
- **自动时也显示现在到底走的是哪台**：sing-box 跑着就问它的 clash API（urltest 的 `now`），
  胶囊上直接写节点名，旁边标「自动（N 个成员）」「TCP 95ms」「HTTP 260ms」；
  固定之后标「已固定」。容器没起时退到本组测速最快的那台，只是给你看一眼「自动」会选谁。
- **两种延迟并排**：`TCP` 是握手耗时（只连节点端口），`HTTP` 是真经节点访问「目标 ▾」里
  那个网址的耗时，同一台节点差一个量级很正常。代理组卡片、组内节点菜单、编辑成员列表、
  节点延迟卡片（左列 TCP、右列 HTTP）都同时显示，点「测速 TCP / 测速 HTTP」按钮按当前目标测。
- 齿轮里的探测参数直接写进 `urltest` 出站：`探测 URL` 默认
  `https://www.gstatic.com/generate_204`，`探测间隔` 10 ~ 3600 秒，`类型` 选
  「最小时延优先」才会自动挑；选「手动选择」就用组内第一台（不自动测速换节点）。
- 编辑分组弹窗支持批量操作：`筛选` + `全选 / 反选 / 清空 / 仅留`（仅留 = 只保留筛选结果，
  其余移出本组），65 个节点也不用一个个点；每行右侧同时标着 TCP / HTTP 延迟，方便「只留快的」。
- 只剩一个分组时也照常保存：新拉到的节点会自动进第一个分组，但**你手工移出去的不会被塞回来**
  （`outlet.sb_group_seen` 记录「认领过」的节点名，不再拿「没归属」当条件）。
- 老配置升级上来会自动建一个 `PROXY` 分组，把所有可用节点放进去，原来的默认节点保持不变；
  订阅新增的节点会补进第一个分组，删掉/停用的节点会从各组里自动剔除。
- 分组、组内选中节点都会落盘（`outlet.sb_groups` / `outlet.sb_group`），改完需要重启
  sing-box 才生效（界面会提示）。


## 出口方案怎么选

| 模式 | 适用 | 说明 |
|---|---|---|
| 不用代理（仅镜像源） | 只想修 `docker pull` | 零成本、零依赖，靠 registry-mirrors |
| 手动填代理地址 | 已有旁路由 / Clash / 机场 | 支持 `http://` 与 `socks5://` |
| Cloudflare WARP | 想要免费出口 | 容器内自动拉起，免费无限流量；出口 IP 是共享段，GitHub 偶有限流 |
| sing-box + 订阅 | 有订阅链接 | 解析订阅 → 生成配置 → 拉起容器，提供 HTTP/SOCKS 混合端口 |

### ⚠️ Cloudflare Workers「前缀反代」不能用

形如 `https://xxx.workers.dev/https://raw.githubusercontent.com/...` 的是**路径前缀式网关**，
它只接受 `GET`、不实现 `CONNECT` 方法，**无法当作正向代理**使用，填进 `proxy.cfg` 或 `http_proxy` 必然失败。

WebUI 的「探测可用性」会自动判定地址属于 `标准正向代理` / `前缀反代` / `不可达`，填错会当场告诉你。

同理，跑在 Cloudflare Workers 上的 vless 节点（路径常带 `?ed=2560`）虽然协议上是正向代理，
但**出口 IP 是 Cloudflare 数据中心共享段**，GitHub / Docker Hub 会限流或风控，
表现为「商店时好时坏、付费代理却正常」。这是 IP 信誉问题，配置层面无解。

### 推荐组合

**docker pull 走镜像源（不碰代理） + CA 商店/GitHub 走代理** —— 这样大流量不走代理，
既省代理流量，也绕开共享出口对大文件的限流。这正是本工具「按目标分别下发」的设计意图。

## 功能

- **体检**：docker.io / ghcr.io / raw.githubusercontent / GitHub API / CA 商店，支持「直连 vs 走代理」对比
- **镜像源池**：内置多个公共源，探测 `/v2/` 后按响应速度排序、剔除失效源（公共源半年一换血，写死必然踩坑）
- **变更预览**：应用前先出 diff，确认后才写入；每次写入自动备份（保留 40 份）
- **幂等**：重复应用不会叠加 `env`；关闭出口会干净地清除注入
- **一键还原**：按目标还原最近一次备份
- **开机重放**：写入 `/boot/config/go` 托管块，重启后自动重建

## 安全设计

- 任何写入前先 `cp` 备份原文件，`rc.docker` 会**保留原有权限位**（否则 Docker 起不来）
- `daemon.json` 解析失败时**拒绝写入**，不会覆盖你的现有配置
- `rc.docker` 找不到 `nohup` 启动行时**跳过**，不冒险改启动脚本
- 重启 Docker 需显式勾选「允许重启 Docker」，且会二次确认（该操作会按自动启动设置重启所有容器）
- 无硬编码密钥；默认账号 `admin` / `admin`，可用界面或 `UPX_PASSWORD` 改掉

## WARP 在国内网络的实测（2026-10）

在一台国内家庭宽带上实测，结论**与常见教程的说法不同**，请先看这段再决定要不要用 WARP：

| 检测项 | 实测结果 |
|---|---|
| 当前出口 | 国内，`colo=LAX` |
| 注册接口 | ✅ 通，拿到 free 账户、分配到 172.16.0.2 |
| 接入点域名解析 | ❌ `engage.cloudflareclient.com` → `198.18.224.65`（黑洞段） |
| 换 DNS 能否解决 | ❌ **不能**。114 与 8.8.8.8 返回同一个污染结果，是链路层被劫持 |
| 直连接入点 IP | ✅ 5/7 可达，最快的 `162.159.199.1` 丢包 0%、249ms |
| 到 CF 边缘吞吐 | ⚠️ 约 350 KB/s（≈2.8 Mbps） |

**要点：**

1. WARP 注册下发的 endpoint 是**域名**，而该域名被解析到黑洞 IP —— 按默认配置启动**必然握手失败**。
2. 唯一的绕法是**把接入点钉成 IP**：在「出口」页把预检给出的 IP 填进「接入点 IP」，工具会以
   `--add-host engage.cloudflareclient.com:<IP>` 启动容器，绕开被污染的 DNS（不依赖镜像的环境变量名）。
3. 即使通了：延迟 ~250ms、吞吐 ~350 KB/s。**只够 CA 模板、插件更新、GitHub raw 这类小流量**；
   拿它拉 Docker 镜像一个 500MB 的层要 20 分钟以上 —— `docker pull` 请务必走 registry-mirrors。

所以工具默认把 `mirror` 打开、`dockerd` 代理只作兜底，WARP 定位是**小流量补位**，不是主力出口。

## 已知限制

- **未在任何真实 Unraid 上验证过**。`rc.docker` 的启动行格式随版本变化，首次请务必先用
  「生成变更预览」人工核对 diff 再应用（找不到 `nohup` 启动行时工具会自动跳过，不会盲改）
- WARP 接入点 IP 会随 Cloudflare 调整而变化，预检给出的 IP 可能过一段时间失效，届时重跑预检
- WARP 预检的 `ping` 在部分网络会被 ICMP 限速，丢包率仅供参考，以「验证是否走 WARP」的结果为准
- 容器内无法直接操作宿主机服务，重启 Docker 通过临时特权容器 + `nsenter` 完成
- WARP / sing-box 出口容器**不映射端口到宿主**，只在 `uproxy-net` 网络内按容器名可达
  （避免把无认证的代理暴露到局域网）。其他容器要用，把它也加入 `uproxy-net` 即可
- CA 商店的 `proxy.cfg` 改完后**需要重启 Unraid 或重启 emhttp** 才生效，工具不会自动重启整机
- CA 商店里的 Docker 图标由**你的浏览器**直接访问 GitHub 获取，不经过 Unraid，配代理无效
- 节点 **HTTP 实测**依赖 Docker（复用 `sb_image` 那个 sing-box 镜像）：镜像没拉过时第一次会慢一些
  （要先把镜像拉下来）；容器内没有 docker 命令时（本地开发机）会自动降级成 TCP 握手测速，界面会提示
- HTTP 实测的耗时包含一次真实 TLS + HTTP 往返，比 TCP 握手大是正常的；数值只反映「能不能通 + 大概多快」，
  不代表带宽
- 不支持 ssr 等已停止维护的协议，解析时会自动跳过并提示
- 分流规则**按域名 / IP / 端口匹配，不做 geoip、geosite 分流**：这两个数据库在 sing-box 1.12
  已被官方移除，用了就是「镜像一升级规则全废」。国内直连请用内置的「国内站点直连」模板
  （`.cn` + 常见国内站点后缀），或自己往规则里加域名
- 「测命中」是**本地按同一套语义离线算**的，能判断域名/IP 是否命中哪条规则；端口、协议、
  进程名这类条件要等真实流量才能判断，页面上会跳过它们
- 订阅拉取内置**重试（3 次）、跟随重定向、连接超时**兜底；若宿主机直连订阅源不稳，可在订阅页填「通过代理拉取」走已有 WARP / 手动节点；拉取失败时错误信息会区分 DNS / 超时 / TLS / 代理拒绝等原因

## 卸载

WebUI「备份与还原」页点「移除开机重放块」，再用还原功能把各文件恢复即可。

## 开发

```bash
python -m pip install -r requirements.txt
python tests/smoke.py         # 在伪造的 Unraid 目录树上跑全流程
python tests/parse_check.py   # 验证订阅解析
python tests/nettest_check.py # 验证节点测速（TCP / HTTP / 降级），不需要 Docker
python tests/route_check.py   # 验证分流规则：清洗、生成、命中测试、接口，不需要 Docker
python tests/order_check.py   # 验证测速排序的落盘：重排、改名、删除、订阅重同步后顺序还原
node   tests/sort_check.js    # 验证前端排序算法（延迟升序、超时垫底、稳定排序）
python tests/groups_check.py  # 验证代理分组：自动铺默认组、改名/删组、成员维护、selector 与 urltest 生成
```

`tests/smoke.py` 不需要真实 Unraid：它会造一棵假目录树，验证注入幂等性、
权限保留、`daemon.json` 合并、go 托管块去重、还原与卸载。
