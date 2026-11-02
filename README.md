# 黄果视频下载器

下载 huangguo.me 的短剧/系列视频为 mp4。

## 一、直接下载

```bash
python3 download_videos.py --input all.json --jobs 1 --workers 8
```

已下过的自动跳过，中断后再跑同一条命令即可继续，不会从头开始。

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--resume` | 续跑，跳过已完成的条目（不从头开始） |
| `--limit 5` | 只下 5 条（先试跑） |
| `--title-match "双生道侣"` | 按标题关键词筛选 |
| `--out-dir /Volumes/盘/downloads` | 存到外置磁盘 |
| `--done-dir /Volumes/盘/downloads` | 已挪走的旧存档视为已完成，不重复下 |
| `--snapshot` | 只刷新进度快照，不下载 |

也可双击 `run.command` 一键跑（依赖检查 → 扫描 → 下载），支持：

```bash
HG_OUTDIR=/Volumes/盘/downloads bash run.command   # 存到外置盘
HG_DONE=/Volumes/盘/downloads  bash run.command    # 旧存档在外置盘
HG_LIMIT=5 bash run.command                        # 只下 5 条
bash run.command --rescan                          # 重新扫描全站
```

视频存到 `downloads/drama/<剧名>/EPn.mp4`、`downloads/series/<标题>_<id>.mp4`，
报告 `_report.csv`，续跑进度 `_progress.json`。

## 二、必须挂 VPN（全局模式 / TUN 模式）

视频 CDN 仅对中国大陆 IP 开放，不挂代理必然失败（`cdn_blocked`）。

**要开「全局模式」或「TUN 模式」**——即由代理接管全部流量。
**系统代理 / 规则模式无效**：脚本是直连请求，不走系统代理，只有全局/TUN 才能覆盖到它。

自测是否生效（返回 200 即正常）：

```bash
curl -sI https://huangguo.me/
```

若代理客户端只提供本地端口（如 7890）而不支持全局/TUN，则显式指定：

```bash
python3 download_videos.py --input all.json --jobs 1 --workers 8 --proxy http://127.0.0.1:7890
```

> 双击 `run.command` 若提示"无法打开"，终端执行一次 `xattr -dr com.apple.quarantine run.command`。

---

仅供个人学习研究使用，请遵守目标网站条款与相关法律法规。
