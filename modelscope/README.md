---
title: Codeg Lite
emoji: 🧩
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# Codeg Lite — ModelScope 国际版

这是从 GitHub Actions 生成的**轻量部署仓库**；镜像由 GitHub Actions 构建并推送至 GHCR，魔搭只需拉取镜像。

- Studio 健康检查网页：`http://0.0.0.0:7860`（由平台提供 HTTPS 地址）
- Codeg Web 服务：`127.0.0.1:3080`（不会直接向魔搭用户公开）
- Cloudflare Tunnel：Cloudflare Dashboard 的 Public Hostname → `http://localhost:3080`

## 必须设置的运行时 Secrets

| 名称 | 值 |
| --- | --- |
| `CODEG_TOKEN` | 自己生成的强随机密码，至少 24 位 |
| `TUNNEL_TOKEN` | Cloudflare 远程管理 Tunnel 的 Token（不是 API Token） |

**切勿写进源码、Dockerfile、README 或构建参数。**

## 部署方式

1. 先在 GitHub 的 Packages 页面，将 GHCR 镜像改为 **Public**；否则魔搭匿名拉取会失败。
2. 把本目录的 `Dockerfile`、`README.md` 提交到新建的 ModelScope International Docker Studio 仓库根目录。
3. 在创空间的设置中配置两个 Secrets；如平台需要设置端口，选 `7860`。
4. 确认 Cloudflare Tunnel 公网域名的 Service URL 是 `http://localhost:3080`，并建议开启 Cloudflare Access。
5. 通过魔搭地址看健康页，通过 Cloudflare 域名打开真正的 Codeg。

此镜像需要魔搭允许拉取公开 GHCR 镜像，并允许 Cloudflare 出站 Tunnel 连接（通常用 TCP/UDP 7844）。账号是否支持此类连接需以实测为准。`/mnt/workspace` 的持久化能力取决于 Studio 资源类型。

使用了 GitHub commit tag (`sha-xxxxxxxxxxxx`) 而非浮动 `latest`。升级请在 Action 重新构建后下载新部署包，替换魔搭中的 `Dockerfile`，再触发重新部署。
