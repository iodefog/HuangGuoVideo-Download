# 黄果视频下载器（huangguo.me）

一键扫描 huangguo.me 全站短剧/系列视频，并批量下载为 mp4（HLS/m3u8 + AES-128 解密 + ffmpeg 封装）。

- 仅依赖 Python 3 标准库 + 系统 `openssl`（解密）+ 可选 `ffmpeg`（封装）
- 支持断点续跑：已下载的不会重复下
- 支持把视频挪到外置磁盘，继续下载时不从头开始

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `run.command` | **一键入口**（双击即可）：检查依赖 → 检测出口 → 扫描 → 验证 → 全量下载 |
| `scan_videos.py` | 扫描全站，产出视频清单 JSON/CSV |
| `download_videos.py` | 按清单批量下载（m3u8 → 解密 → 合并 → mp4） |
| `all.json` / `all.csv` | 扫描产物（全站清单，2702 条） |

## 快速开始

**最简单**：双击 `run.command`，全自动跑完。

> 若双击提示"无法打开/已损坏"，是 macOS 隔离策略，终端执行一次：
> ```bash
> xattr -dr com.apple.quarantine run.command
> ```

### 前置条件
- `python3` / `ffmpeg` / `openssl`（脚本第 1 步会自动检查）
- **视频 CDN 仅对中国大陆 IP 开放**，运行时请保持国内 VPN / TUN 代理开启
  （脚本下载前会先验证 1 条，被拒绝会明确提示）

## 手动用法

### 1. 扫描（生成清单）
```bash
python3 scan_videos.py --workers 24 --out all.json --csv all.csv
```
常用：`--kinds drama,series` 只扫某类 / `--max-dramas 10` 限量试跑 / `--cookie "..."` 解锁付费内容。

### 2. 下载
```bash
python3 download_videos.py --input all.json --jobs 1 --workers 8
```
常用：`--limit 5` 只下 5 条 / `--title-match "双生道侣"` 按标题正则筛 / `--refresh` 重取新鲜 m3u8。

## 续跑与挪盘（磁盘满/换盘场景）

进度记录在 `downloads/_progress.json`，每次运行结束自动写回。

| 需求 | 做法 |
| --- | --- |
| 继续下载，不从头开始 | 加 `--resume`（`run.command` 已默认带上） |
| 只刷新进度快照、不下载 | `python3 download_videos.py --input all.json --snapshot` |
| 新下载直接存外置盘 | `HG_OUTDIR=/Volumes/你的盘/downloads bash run.command` |
| 旧 `downloads` 已挪到外置盘 | `HG_DONE=/Volumes/你的盘/downloads bash run.command`（该目录视为已完成并跳过） |
| 只先下 2 条验证 | `HG_LIMIT=2 bash run.command` |
| 重新扫描全站 | `bash run.command --rescan` |

三者可叠加，例如：
```bash
HG_OUTDIR=/Volumes/Disk/downloads HG_DONE=/Volumes/Disk/downloads bash run.command
```

## 输出结构

```
downloads/
├── drama/<剧名>/EP1.mp4        # 短剧按剧名分目录
├── series/<标题>_<id>.mp4      # 系列视频
├── _report.csv                 # 下载报告（含失败原因）
└── _progress.json              # 续跑进度
```

## 常见问题

**`cdn_blocked`（分片 403）**
视频 CDN 仅对中国大陆 IP 开放。挂国内 VPN / 开 TUN 代理后重跑；或在代理客户端开本地端口后：
```bash
python3 download_videos.py --input all.json --jobs 1 --workers 8 --proxy http://127.0.0.1:7890
```

**`m3u8 无法获取` / `TLS/SSL connection has been closed (EOF)`**
出口到 huangguo.me 被对端重置，属网络侧问题（可用 `curl -sI https://huangguo.me/` 自测，返回 200 即恢复）。
`run.command` 自带连通性预检，不通会直接提示换 VPN 节点并退出。脚本已内置 6 次重试 + 退避。

**进度显示"已完成 0 条"但文件还在**
进度文件显示的是累计值；只要 `downloads/` 里的 mp4 存在就不会重复下载（按文件是否存在判断）。
可用 `--snapshot` 重新生成准确快照。

## 免责说明

仅供个人学习研究使用，请遵守目标网站的条款与相关法律法规，勿用于商业用途或大规模分发。
