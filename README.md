# uproxy · 基于 v2rayA 的 Unraid 出网加速

> ✅ **当前状态：可用。** 底层引擎已切换为 **v2rayA v2.5.10**（自带 xray 内核），WebUI、订阅、节点、透明代理、DNS 防污染全部由 v2rayA 提供。
>
> ⚠️ **关键前提**：节点「可用数 0」的根因是**节点服务器域名在大陆网络下解析不出**，必须在 v2rayA 里开启「DNS 防污染（DoH / 经代理转发）」才能解决，否则仍会连不上。详见下方「排障」。

## 为什么从 sing-box 转 v2rayA

早期版本（v1.x）用自研 sing-box 引擎。在用户实际环境（Unraid 容器 + 大陆网络）下始终「可用数 0」，根因不在代码而在数据链路：

- 机场下发的节点服务器域名（如 `xmrhmode.com` / `xmrthnode.com`）在容器使用的大陆 DNS（223.5.5.5 / 119.29.11.11）下**解析不出**（`lookup ...: empty result (cached)`）；
- 部分节点 TLS 证书域名与 SNI 完全不匹配（`certificate is valid for ws-aicdn.com, not ...xmrhmode.com`）。

sing-box 在本地解析节点域名失败 → 所有节点连不上，无论怎么调配置都救不了。而 v2rayA（Xray 系）的 **DNS 防污染**（DoH / 经代理转发 DNS 查询）能把节点域名通过加密通道或代理解析出真实 IP，绕开这个死结；且对 Trojan 默认就带宽松证书校验，节点兼容性更好。v2rayN / Clash 在你自己电脑能连，正是因为它们用这套机制。

因此项目在 **v2.0.0** 正式从自研 sing-box 引擎切换为 v2rayA 底座：uproxy 不再维护独立代理引擎，只做 Unraid 场景化部署与辅助。

## 这是什么

uproxy 是一个把 **v2rayA** 封装成 Unraid 一键方案的部署工具：

- **代理引擎与 WebUI**：v2rayA（自带 xray 内核），开箱即用的订阅 / 节点 / 透明代理 / DNS 防污染
- **透明代理（iptables redirect）**：接管 Unraid 宿主机 + Docker 容器出网，覆盖 Docker 拉镜像 / CA 应用商店 / GitHub 更新 / 插件下载等全部场景
- **系统代理端口**：HTTP `20171` / SOCKS `20170`，可单独给某个客户端或 dockerd 指向

## 部署

### 方式一：一键脚本（推荐，在 Unraid 终端执行）

```bash
git clone https://github.com/bcfxclub/Unraid-uproxy /mnt/user/appdata/uproxy/src
cd /mnt/user/appdata/uproxy/src
bash install.sh
```

脚本会：安装 Unraid 容器模板 → 拉取/构建镜像 → 启动容器（host 网络 + privileged）。完成后浏览器打开 `http://<Unraid-IP>:2017/`。

### 方式二：docker-compose

```bash
docker compose up -d
```

### 方式三：Unraid 模板

把 `unraid/my-uproxy.xml` 放到 `/boot/config/plugins/dockerMan/templates-user/`，在 Unraid Docker 页面「从模板添加」即可（Network=host、Privileged=true 已配好）。

## 首次配置（在 v2rayA WebUI，http://<IP>:2017）

1. **设置管理员密码**（第一个注册的人即为管理员）。
2. **导入订阅**：订阅 → 添加 → 粘贴你的机场订阅 URL → 更新。
3. **透明代理**：设置 → 透明代理 → 开启；实现方式选 **redirect**；分流模式选 **绕过大陆（Bypass CN）**（只代理境外流量，不影响局域网 / PT）。
4. **防止 DNS 污染（关键！）**：设置 → 防止 DNS 污染 → 选 **DoH** 或 **经代理转发**。这是解决「节点域名解析不出、可用数 0」的核心开关。
5. **选节点并启动**：在节点列表选一个延迟低且可用的节点，点启动。宿主机与 Docker 容器出网即走代理。

> 不想用透明代理？关掉它，改为把 Unraid 的 Docker / 商店 / GitHub 指向 v2rayA 系统代理端口 `http://<IP>:20171`（HTTP）或 `socks5://<IP>:20170`。

## 端口

| 端口 | 用途 |
|------|------|
| 2017 | v2rayA WebUI / API |
| 20170 | SOCKS5 入站（给单个客户端） |
| 20171 | HTTP 代理入站（给单个客户端 / dockerd） |
| 20172 | 应用路由规则的 HTTP 代理入站 |

## 排障

- **节点可用数 0 / 连不上**：几乎都是 DNS 问题。先确认上面的步骤 4「防止 DNS 污染」已开启（DoH 或经代理转发）。仍不行则检查节点域名能否通过 DoH 解析（见下方进阶）。
- **开启透明代理后断网 / 部分站点打不开**：分流模式务必选「绕过大陆」，不要把局域网 / 国内流量也送进代理。若 Unraid 是 nftables 环境，把环境变量 `V2RAYA_NFTABLES_SUPPORT` 改为 `on`。
- **镜像拉不下来**：先 `bash fix-mirror.sh` 修 Docker 镜像源；若 ghcr.io 也拉不动，需先有基础网络（或手动从能联网的机器 pull 后导出导入）。

### 进阶：节点域名解析不出时手动指定

若某节点服务器域名在 DoH 下仍解析不出，可在 v2rayA 的「高级设置 → DNS 设置」里为该域名指定解析方式，或联系机场修正其 CDN 域名 / 证书数据。这属于上游数据源问题，非本工具缺陷。

## 项目结构

```
Dockerfile               基于 ghcr.io/v2raya/v2raya:v2.5.10，复制辅助脚本
docker-compose.yml       host 网络 + privileged 部署
unraid/my-uproxy.xml     Unraid 容器模板（host / privileged / 挂载）
install.sh / update.sh / uninstall.sh   Unraid 一键部署 / 更新 / 卸载
fix-mirror.sh            修复失效的 Docker 镜像源（与代理引擎无关，独立可用）
app/  web/               ⚠️ 已废弃：v1.x 自研 sing-box 引擎与 WebUI，v2.0.0 起不再使用
```

## License

AGPL-3.0（遵循 v2rayA 上游许可）
