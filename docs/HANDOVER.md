# 交接说明（换机器 / 换会话时先看这个）

> 本文件是**进度与结论的载体**。代码在 Git 里，但"哪些源实测能用""哪些坑已踩过"
> 这类结论只存在于这里和开发者的记忆里 —— 换机器时它们比代码更容易丢。

- 仓库：https://github.com/bcfxclub/Unraid-uproxy
- 作者账号：bcfxclub
- 最后更新：2026-10-31

---

## 一、现在做到哪了

| 阶段 | 状态 |
|---|---|
| 代码（29 文件 / ~3900 行） | ✅ 已推送 GitHub，3 个 commit |
| 本地功能验证（冒烟 + 解析 + 全路由） | ✅ 通过 |
| **真实 Unraid 部署** | ⏸️ **进行中，卡在 docker build 拉基础镜像** |
| 用户真机跑通 | ❌ 还没 |

用户已在自己的 Unraid 上执行过 `install.sh`，失败在：
`docker build` 取 `python:3.12-slim` 时报 `lookup mirror.ccs.tencentyun.com: no such host`
——他 daemon.json 里配的腾讯云镜像源域名已失效。

## 二、下次继续，按这个顺序

```bash
cd /mnt/user/appdata/uproxy
git pull                        # 拿到最新修复
bash fix-mirror.sh              # 自动体检镜像源并修复（见第三节）
docker pull python:3.12-slim    # 验证基础镜像能拉
./install.sh                    # 装起来，记下打印的 ?token= URL
```

装起来后**第一件事**：WebUI「下发」页点「生成变更预览」，把 `dockerd` 那条 diff
人工核对一遍（`rc.docker` 的启动行格式随 Unraid 版本变，没在真机验证过）。
确认匹配再点「应用变更」。

## 三、2026-10-30 实测结论（本机网络，各地差异大，务必重测不要照抄）

### Docker 镜像源

| 源 | /v2/ | python:3.12-slim | 结论 |
|---|---|---|---|
| `docker.1ms.run` | 200 / 0.10s | 401 ✅ | **首选** |
| `docker.m.daocloud.io` | 200 / 0.13s | 401 ✅ | **首选** |
| `docker.xuanyuan.me` | 200 / 2.9s | 403 | 备用 |
| `mirror.ccs.tencentyun.com` | DNS 失败 | — | ❌ 已死（用户中招的就是它） |
| `hub-mirror.c.163.com`、`docker.mirrors.ustc.edu.cn` | DNS 失败 | — | ❌ |
| `dockerhub.icu`、`docker.1panel.live`、`dockerpull.org` 等 | 超时 | — | ❌ |

判定方法：`curl -o /dev/null -w '%{http_code}' https://<源>/v2/`，**200 或 401 都算活**。

### Cloudflare WARP（免费出口）

- 注册接口**可用**：`POST https://api.cloudflareclient.com/v0a2223/reg`，
  body 需含 base64 的 X25519 公钥 → 200，拿 free 账户 + 内网 IP。
- **但下发的 endpoint 是域名 `engage.cloudflareclient.com`，在国内被解析到
  `198.18.224.65`（RFC2544 黑洞段）**。换 DNS 没用（114 和 8.8.8.8 返回同一个污染值）。
- 绕法：`docker run --add-host engage.cloudflareclient.com:<可达IP>` 把解析钉死。
- 实测可达接入点：`162.159.199.1`（0% 丢包 / 249ms 最优）。到 CF 边缘吞吐仅 ~350 KB/s。
- 结论：**只能做小流量补位**（CA 模板 / GitHub raw），docker pull 必须走镜像源。

### GitHub 加速镜像（用于把本项目搬到 Unraid）

| 通道 | 结果 |
|---|---|
| `codeload.github.com` 直连 | 200 / 1.9s ✅ |
| `ghfast.top/https://...` | 200 / 2.1s ✅ 首选 |
| `gh-proxy.com/https://...` | 200 / 5.7s ✅ 备选 |
| `gh.llkk.cc`、`mirror.ghproxy.com` | 超时 ❌ 已死 |

**鸡生蛋问题**：本工具为"Unraid 访问 GitHub 困难"而生，自己却托管在 GitHub 上。
所以**必须始终保留离线 zip 兜底**。

## 四、已经踩过并修掉的坑（别再踩第二次）

1. **Docker 默认 bridge 没有容器名 DNS** —— 出口容器挂到 `uproxy-net` 后，
   必须把 uproxy 自己也 `docker network connect` 进去，否则 `http://uproxy-warp:9091`
   永远解析不了。`outlets._ensure_net()` 已实现自检自连。
2. **`host.run()` 按 UTF-8 硬解码子进程输出** —— 碰到非 UTF-8 字节会抛异常并
   **把输出整个丢掉**，导致"命令成功"被误判成失败。已改逐层降级解码。
3. **ping 解析依赖本地化文案** —— 编码一乱就把可达的全报成"不可达"（假阴性比不测更糟）。
   改为数回包行（`bytes from|来自|Reply from`）。
4. **前端忘记带访问口令** —— 后端所有 `/api/*` 校验 `UPX_TOKEN`，前端一处没带，
   结果页面能开、点什么都是 401「未授权」。已修（页头口令框 + `?token=` 自动记忆）。
   **教训：加鉴权必须同步改前端。**
5. **CRLF / 可执行位** —— Windows 提交会把 `install.sh` 变 CRLF（Unraid 上
   `bad interpreter`）且权限位变 644。已加 `.gitattributes` 强制 LF，权限位改 755。
6. **WorkBuddy 的 GitHub 连接器是只读的** —— 建库 403、推文件 403/unauthorized。
   别在它身上耗轮次，**更不要 reconnect**（重连后 GitHub 工具会从索引里整个消失）。
   正确做法：本机 git + Git Credential Manager（`git config --global credential.helper manager`
   → `git-credential-manager.exe github login` → `git push`），不用手工建 PAT。

## 五、还没做 / 已知限制（诚实清单）

- ❌ **没在真实 Unraid 上验证过**。`rc.docker` 启动行格式随版本变，这是最大未知
- ❌ 重启 Docker 走的 `nsenter` 特权容器未实测
- ❌ WARP / sing-box 容器真启动未实测（开发环境没有 docker）
- ❌ CA 商店里的 Docker **图标不显示** —— 那是浏览器直连 GitHub，不经过 Unraid，无解
- ❌ Cloudflare 共享出口段被 GitHub 限流 —— IP 信誉问题，换什么配置都绕不开

## 六、安全提醒

用户曾把一条含明文 UUID 的 vless 节点贴进对话（CF Workers 上的 vless，
`?ed=2560` 是 edtunnel 特征），**已公开，建议更换**。
