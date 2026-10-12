# Codeg Lite：GitHub Actions 自动构建 → GHCR → 魔搭国际版

> 中文完整部署说明。此仓库借鉴 [herm_lite](https://github.com/kakaw-1/herm_lite) 的双入口方案，并复用 [Codeg 官方 Docker 镜像](https://docs.codeg.app/getting-started/deployment)，**不安装 Hermes Agent**，也不从源码重编译 Codeg。此包是可自行构建和部署的模板，尚未声称通过真实 ModelScope 国际版运行测试。

## 1. 最终结构

```text
GitHub 仓库（包含本部署包）
  └─ GitHub Actions (main 分支 push 或手动触发)
       ├─ Python 单元测试
       ├─ 构建 linux/amd64 Docker 镜像
       ├─ 推送 ghcr.io/<GitHub账号>/<仓库名>:latest
       │    同时推送 :sha-<12位Commit SHA>
       └─ 生成可下载的 modelscope-studio-* Artifact

魔搭 ModelScope International Docker Studio
  └─ FROM ghcr.io/<账号>/<仓库名>:sha-xxxxxxxxxxxx
       ├─ 0.0.0.0:7860：健康检查页面（由魔搭公开）
       ├─ 127.0.0.1:3080：Codeg Web API / WebSocket（仅本机）
       └─ Cloudflare cloudflared：连接 3080 与自己的域名

浏览器 ── https://codeg.example.com ── Cloudflare Access ── Codeg
```

## 2. 上传到 GitHub，一键触发 Actions

1. 新建一个 **GitHub 仓库**，例如 `codeg_lite`；默认分支用 **`main`**。
2. 把 **本 ZIP 解压后的所有文件和子目录** 上传到该仓库**根目录**，务必保留 `.github/workflows/docker-publish.yml`、`Dockerfile`、`supervisor.py` 等文件。GitHub 网页上传 ZIP 本身**不会**自动解压。
3. 提交到 `main` 后会自动运行：GitHub 仓库 → **Actions** → **Build and publish Codeg Lite**。也可以点 **Run workflow** 手动运行（选择 `main` 分支）。
4. 运行成功后，到 Actions 日志 / Summary 找到自动生成的两个 Docker 标签：

   ```text
   ghcr.io/你的GitHub账号/codeg_lite:latest
   ghcr.io/你的GitHub账号/codeg_lite:sha-xxxxxxxxxxxx
   ```

   镜像名自动来自 `${{ github.repository }}`（实际 workflow 在运行时取值）并转小写，不用提前改写 Dockerfile，也无需 Docker Hub Token 或 GitHub PAT。Action 用仓库自动提供的 `GITHUB_TOKEN`，具备 `packages: write` 权限。

5. **重要：新 GHCR Package 默认是 Private。** 第一次成功推送后进入 GitHub 个人主页 → **Packages** → 选择该镜像 → **Package settings** → **Change visibility → Public**。不公开时，魔搭无法匿名 `FROM ghcr.io/...` 拉取该镜像。公开镜像不代表 Codeg 登录公开，`CODEG_TOKEN` 仍需要保密。

如果仓库默认分支不是 `main`，先将其改为 `main`，或同时修改 `.github/workflows/docker-publish.yml` 的触发分支及 `publish.if` 分支条件。

## 3. 从 Actions 下载魔搭专用部署包

在 **Actions** → 成功运行的 **Build and publish Codeg Lite** → 页面底部 **Artifacts** 下载：

```text
modelscope-studio-<完整 GitHub Commit SHA>.zip
```

解压后只有必要的两份文件：

```text
Dockerfile   # 自动指向这次构建的 ghcr.io/...:sha-... 镜像
README.md    # 带 sdk: docker 的魔搭 Studio 描述/部署说明
```

**这份产物直接用于魔搭**，不需要你猜 GHCR 地址，也不需要在魔搭重新下载 Codeg/Cloudflare 二进制做安装构建。若想更新到新版镜像，再次运行 Action，下载新 Artifact，用新的 `Dockerfile` 重新部署魔搭即可。

**注意**：GitHub 构建成功不代表 ModelScope 国际版一定能拉取 GHCR；如果平台网络无法访问 GHCR，需要改用其允许的镜像仓库或中转方案。

## 4. 配置 Cloudflare 的独立域名

在 [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → **Networking / Tunnels** 创建一个 **remotely-managed Cloudflared Tunnel**，如 `codeg-lite`。

在 Tunnel 的 Published application route 中设置：

| Cloudflare 配置项 | 内容 |
| --- | --- |
| Public Hostname | `codeg.你的域名.com` |
| Type | HTTP |
| Service URL | `http://localhost:3080` |

从该 Tunnel 连接器的安装命令里复制 **Tunnel Token**（只复制长 token，不复制完整命令）。再给域名配置 Cloudflare Access 自托管应用，只允许你自己的邮箱访问；Codeg 本身还会用 `CODEG_TOKEN` 进行二次保护。

## 5. 在魔搭国际版部署

1. 在 [ModelScope International Studios](https://www.modelscope.ai/studios) 创建 Docker 类型的 Studio。
2. 使用第 3 步下载的 **`modelscope-studio-*` Artifact** 中的 `Dockerfile` 和 `README.md`，把它们放在 **Studio 仓库根目录**。若平台有“从 GitHub 导入”入口，也应导入这两份已生成文件，而不是把 GH 工作流当作运行入口。
3. 配置下列 **运行时 Secrets**（不写在 Dockerfile、GitHub 源码或构建参数中）：

   | 名称 | 值 |
   | --- | --- |
   | `CODEG_TOKEN` | 随机字符串至少 24 位，建议 48 位以上 |
   | `TUNNEL_TOKEN` | Cloudflare remotely-managed Tunnel 的连接令牌 |

4. 平台若要求指定服务端口：**`7860`**。这是健康页端口；**`3080` 不应通过魔搭公开**。
5. 等待 Studio 完成构建并启动。魔搭的 Studio URL 是健康检查页；Codeg 真正界面在你的 Cloudflare 域名下。

可在自己的电脑生成随机令牌：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

注意：魔搭必须能访问公开 GHCR 镜像，且要允许 `cloudflared` 出站连接 Cloudflare（常见为 TCP/UDP 7844）。平台资源休眠、实例重建、持久化能力等由魔搭国际版实际规则决定，**GitHub Actions 不能绕过这些限制**。

## 6. 健康检查、持久化和限制

- `/`：公开状态页，**不包含令牌或终端**。
- `/health`：200，表示健康检查 HTTP 服务活着；响应 JSON 带 `ready` 字段，表明是否同时探测到 Codeg 和 cloudflared 进程。
- `/ready`：200（依赖就绪）或 503（尚未就绪），便于自己排障。
- 状态页面的 `cloudflared_process: running` 仅说明进程存在，**无法证明 Cloudflare edge / DNS / Access 已连接**；要看 Studio 日志或 Cloudflare Dashboard。
- `CODEG_DATA_DIR`、`CODEG_HOME`、`HOME`、项目工作目录均位于 `/mnt/workspace/codeg/`。只有你的 Studio 资源本身保证 `/mnt/workspace` 持久化时，数据才真正跨重建保留；请另做备份。
- 默认关闭 Codeg 多端口预览桥（`CODEG_BRIDGE_PORTS=off`）和远程浏览器隧道；AI 编程 CLI（Claude Code、Codex、OpenCode 等）**没有随着 Codeg 镜像预装**，后续需要按各自方式安装、授权。
- Docker 构建只面向 `linux/amd64`（优先针对魔搭的 x86 资源）。若使用 ARM64 资源，可在 workflow 增加 `linux/arm64`，并相应配置多架构构建。
- `Dockerfile` 默认继承 `ghcr.io/spacering-net/codeg:latest`，所以每次 Action **重新构建**时才会带入当时最新的 Codeg 官方镜像；若希望固定 Codeg 版本，可把 `FROM` 改为官方具体版本标签或 digest。

## 7. 排障

| 现象 | 可能原因 / 处理 |
| --- | --- |
| Action `denied: permission_denied: write_package` | 检查仓库 Actions 开关、组织 Packages 策略、同名 GHCR package 是否授权仓库 Actions 写入 |
| Action `build` 卡在拉官方 Codeg 镜像 | GitHub / GHCR 网络临时错误、上游镜像可用性；重试 Action |
| 魔搭 Docker build `unauthorized` | GHCR Package 尚未设置为 **Public** |
| 魔搭 Docker build 网络超时 | Studio 构建环境无法访问 GHCR，改选可访问的仓库 |
| 健康页显示 `ready: false` | Codeg 未启动或 cloudflared 进程停止；查魔搭容器日志和 `/ready` |
| Cloudflare `1033` | Tunnel 不在线、Token 错误或平台阻止外连 7844 |
| Cloudflare `502` | Tunnel 连上了，但 Codeg 的 `127.0.0.1:3080` 服务不可用或目标地址不正确 |
| 重新部署后数据消失 | 实际 Studio 没有持久化 `/mnt/workspace` 或之前将项目建在临时目录 |

## 8. 本地测试（可选）

```bash
python3 -m unittest discover -s tests -v
cp .env.example .env   # 然后编辑 .env，填入真实令牌，勿提交
# 在装有 Docker Compose 的电脑上运行：
docker compose up --build -d
# 浏览器查看 http://localhost:7860
```

来源与参考：
- [Codeg 官方部署文档](https://docs.codeg.app/getting-started/deployment)
- [Codeg 官方配置说明](https://docs.codeg.app/getting-started/configuration)
- [GitHub Actions 发布 GHCR 镜像](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
- [GitHub Container Registry 镜像可见性](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
- [Cloudflare Tunnel Tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)
