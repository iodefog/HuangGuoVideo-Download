#!/bin/bash
# 黄果视频 · 一键扫描 + 下载
# 双击本文件即可在终端中运行（首次会扫描全站，之后会复用 all.json 直接下载）
# 如需强制重新扫描：在终端执行  bash run.command --rescan
# 可选环境变量：
#   HG_LIMIT=2  bash run.command            只先下 2 条做验证
#   HG_OUTDIR=/Volumes/Disk/downloads  bash run.command   直接下载到外置磁盘（避免本地盘满）
#   HG_DONE=/Volumes/Disk/downloads    bash run.command   已挪到外置盘的 downloads 视为已完成并跳过
#   （两者可同时用：HG_OUTDIR 指定新下载位置，HG_DONE 指定旧存档位置）

set -u

# 进入脚本所在目录（双击时 cwd 默认是用户家目录）
cd "$(dirname "$0")" || exit 1

echo "=================================================="
echo " 黄果视频  一键扫描 + 下载"
echo " 工作目录: $(pwd)"
echo "=================================================="

# ---------- 1. 依赖检查 ----------
echo ">>> [1/4] 检查依赖..."
for bin in python3 ffmpeg openssl; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "    ✗ 未找到 '$bin'，请先安装后重试。"
        echo "      python3 / ffmpeg: brew install python ffmpeg"
        echo "      openssl:        通常系统自带"
        read -p "按回车关闭窗口..." ; exit 1
    fi
done
echo "    ✓ python3 / ffmpeg / openssl 就绪"

# ---------- 2. 出口检测提示 ----------
echo ">>> [2/4] 检测网络出口..."
IP=$(curl -s --max-time 8 ifconfig.me 2>/dev/null || true)
echo "    当前出口 IP: ${IP:-获取失败}"
if [ -n "$IP" ] && [[ ! "$IP" =~ ^(1[0-9]|2[0-3]|3[0-3]|4[0-2]|4[4-9]|5[0-9]|6[0-2]|1[0-1][0-9]|12[0-7]|2[0-2][0-3])\. ]]; then
    # 粗略判断：非常见中国大陆 CIDR 前缀段
    echo "    ⚠ 当前出口可能不是中国大陆 IP，视频分片大概率 403。"
    echo "      若下载失败(cdn_blocked)，请先挂国内 VPN / 开启 TUN 代理后重跑本文件。"
fi

# ---------- 2.5 连通性预检（huangguo.me TLS 握手）----------
echo ">>> 连通性预检（huangguo.me）..."
if ! curl -sI --max-time 12 https://huangguo.me/ >/dev/null 2>&1; then
    echo "    ✗ 当前到 huangguo.me 的 TLS 握手失败（对端重置）——下载必失败。"
    echo "      这是网络/VPN 出口问题，不是脚本问题（curl 作为参考客户端同样失败）。"
    echo "      请：重连 VPN / 切换到其他中国节点，然后自测："
    echo "        curl -sI https://huangguo.me/"
    echo "      出现 HTTP 响应头（如 HTTP/2 200）即代表恢复，再重新双击本文件。"
    read -p "按回车关闭窗口..." ; exit 1
fi
echo "    ✓ huangguo.me 可达，开始后续流程"

# ---------- 3. 扫描 ----------
if [ ! -f all.json ] || [ "${1:-}" = "--rescan" ]; then
    echo ">>> [3/4] 扫描全站视频（首次较慢，请耐心等待）..."
    python3 scan_videos.py --workers 24 --out all.json --csv all.csv
else
    echo ">>> [3/4] 检测到 all.json 已存在，跳过重扫。"
    echo "        （如需重新扫描请删除 all.json，或运行: bash run.command --rescan）"
fi

if [ ! -f all.json ]; then
    echo "    ✗ 扫描未生成 all.json，终止。"
    read -p "按回车关闭窗口..." ; exit 1
fi

# ---------- 4. 先验证一条，再全量 ----------
DL_EXTRA=""
if [ -n "${HG_LIMIT:-}" ]; then
    DL_EXTRA="--limit $HG_LIMIT"
fi

# 输出目录与"已下载存档目录"（可指向外置磁盘，解决本地盘满）
OUTDIR="${HG_OUTDIR:-downloads}"
done_args=()
if [ -n "${HG_DONE:-}" ]; then
    IFS=':' read -ra _d <<< "$HG_DONE"
    for d in "${_d[@]}"; do
        [ -n "$d" ] && done_args+=(--done-dir "$d")
    done
fi
echo "    输出目录: $OUTDIR"
if [ ${#done_args[@]} -gt 0 ]; then
    echo "    已下载存档目录(视为已完成): ${done_args[*]}"
fi

echo ">>> [4/4] 先验证 1 条是否可下载（检验出口/IP）..."
python3 download_videos.py --input all.json --out-dir "$OUTDIR" "${done_args[@]+"${done_args[@]}"}" --limit 1 --force $DL_EXTRA 2>&1 | tail -4
if grep -q "cdn_blocked" "$OUTDIR/_report.csv" 2>/dev/null; then
    echo
    echo "    ⚠ 检测到分片被 CDN 拒绝（cdn_blocked）。"
    echo "      原因：视频 CDN 仅对中国大陆 IP 开放，当前出口被拦截。"
    echo "      解决：先挂国内 VPN / 开启代理 TUN(IPv4 接管)，再重新双击本文件。"
    echo "            或在代理客户端开启本地端口(如 7890)后改用:"
    echo "              python3 download_videos.py --input all.json --out-dir \"$OUTDIR\" --jobs 1 --workers 8 --proxy http://127.0.0.1:7890"
    read -p "仍要继续全量下载(多半仍失败)？(y/N) " ans
    case "$ans" in
        y|Y) echo "    继续全量下载..." ;;
        *)   echo "    已取消。请处理出口后重跑。"; read -p "按回车关闭窗口..."; exit 0 ;;
    esac
fi

echo ">>> 开始全量下载（已存在的/已记录的会自动跳过，--resume 续跑）..."
python3 download_videos.py --input all.json --out-dir "$OUTDIR" "${done_args[@]+"${done_args[@]}"}" --resume --jobs 1 --workers 8 $DL_EXTRA

echo
echo "=================================================="
echo " 完成！"
echo " 视频目录: $OUTDIR/"
echo " 下载报告: $OUTDIR/_report.csv"
echo " 进度记录: $OUTDIR/_progress.json （下次 --resume 续跑，不从头开始）"
echo "=================================================="
read -p "按回车关闭窗口..."
