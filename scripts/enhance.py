# -*- coding: utf-8 -*-
"""
enhance.py —— 粗剪之后的「动效增强」层（本地 ffmpeg，零积分、离线可跑）

粗剪出来的片子能看，但像"监控录像"：机位死、画面静、只有人声。
这一层做四件规矩的事，让成片像正经短视频：

  1) 呼吸感运镜   —— 极缓慢的全片推拉（幅度 1% 上下，看不出但"活"）
  2) 关键点推镜头 —— 讲到关键词那 2~5 秒，镜头缓缓推近 ~6%（不用手动打点，
                     从字幕稿里自动找关键词和时间）
  3) 关键词动效   —— 关键词以「缩放弹出 + 淡入淡出」的卡片形式炸出来
  4) 背景音乐     —— 支持自动闪避（你一说话音乐自动退到后面，停下来才起来），
                     并且**优先挑纯伴奏**：带人声的歌会跟你的口播抢注意力

所有效果、幅度、开关都在命令行，默认值是"不打扰内容"的保守值。

用法：
  python enhance.py 粗剪.mp4 --srt 字幕.srt --bgm 纯音乐.wav
  python enhance.py 粗剪.mp4 --keywords "自贸她创,DeepSeek,Kimi" --no-card
  python enhance.py 粗剪.mp4 --no-zoom            # 只要字幕和音乐

输出：同名 _增强版.mp4（可用 --out 指定）
"""
import argparse
import os
import re
import subprocess
import sys

# ── 输出安全层 ────────────────────────────────────────────────────────
# Windows 中文控制台默认编码是 GBK(cp936)。脚本输出里只要带 emoji 之类符号，
# 就会直接抛 UnicodeEncodeError —— 学员敲第一条命令就崩，最劝退。
# 这里统一把标准输出切到 UTF-8；终端真不支持时降级 replace，
# 保证「最多显示成方块，绝不崩溃」。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from autoclip import (FFMPEG, FFPROBE, display_size, write_ass, get_duration,  # noqa: E402
                          probe, has_burned_subtitle, pick_level)
except (ImportError, SystemExit) as _e:
    # 注意：autoclip.py 顶层在「找不到 ffmpeg」时是 sys.exit(...)，
    # 那抛的是 SystemExit 不是 ImportError —— 只 catch ImportError 会漏掉，
    # 学员跑 enhance.py 却看到一段讲 autoclip.py 的报错，一头雾水。
    _msg = str(_e) if isinstance(_e, SystemExit) else ""
    sys.stderr.write(
        ("\n【autoclip.py 不可用】" + (_msg.strip() or "同目录缺少 autoclip.py") + "\n")
        + "   enhance.py 需要同目录的 autoclip.py（共用工具函数）。\n"
        + "   如果上面说的是找不到 ffmpeg：装一个即可  pip install imageio-ffmpeg\n"
        + "   如果说缺文件：技能包没装完整，请重新安装  python install.py --zip <技能包.zip>\n")
    sys.exit(2)

# ── 抽关键词用：这些是口语连接词/虚词，不能当"关键词"上屏 ──────────────
STOPWORDS = set("""
这个 那个 就是 然后 后来 因为 所以 而且 反而 但是 不过 如果 虽然 接着 再则 首先 其次 最后 当然 也许 大概 好像
我们 他们 她们 你们 大家 自己 人家 什么 怎么 为什么 哪些 那个样子 一样 一直 一会 一点 一下 一种
已经 现在 正在 时候 之后 之前 里面 外面 上面 下面 出来 出去 进去 回来 起来 下来 上来 过去 过来
东西 事情 问题 方式 部分 方面 结果 开始 结束 这些 那些 还有 没有 不是 不能 不会 不用 需要 应该
可以 觉得 认为 知道 发现 看到 听到 说的 说的 进行 一定 非常 比较 特别 真的 其实 反正 只是 而是
第一 第二 第三 一个 两个 三个 一次 一直 有些 有些东西 很多 太多 更加 这样 那样 样子 别人 大家
海南 咱们 各位 本期 本期
""".split())

LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\.\-]{1,}")


def load_srt(path):
    """读 SRT → [(start, end, text)]，秒为单位的浮点"""
    out = []
    if not path or not os.path.exists(path):
        return out
    raw = open(path, encoding="utf-8").read().strip()
    for block in raw.split("\n\n"):
        ls = [l for l in block.splitlines() if l.strip()]
        if len(ls) < 3 or "-->" not in ls[1]:
            continue

        def _sec(tok):
            try:
                hh, mm, rest = tok.strip().replace(",", ".").split(":")
                return int(hh) * 3600 + int(mm) * 60 + float(rest)
            except Exception:
                return None
        try:
            a_raw, b_raw = [x.strip() for x in ls[1].split("-->")]
        except ValueError:
            continue
        a, b = _sec(a_raw), _sec(b_raw)
        if a is None or b is None:
            continue
        txt = " ".join(l.strip() for l in ls[2:]).strip()
        if txt:
            out.append((a, b, txt))
    return out


CN_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_STOP = set("""the and for you our with that this from have will can not are was were
its it's they them then than what when where which who how why yes no but get got one two
more most very just also into out up down over under new use used using make made""".split())


# 【注意】 历史教训：曾用「n-gram + 稀有度」自动抽中文关键词，结果抽出
#    「学习圈照」「我担任助」「举了一个」这种跨越词边界的碎片 —— 中文抽词
#    必须分词，而给学员加 jieba 依赖得不偿失。现改为：脚本只抽英文专名/数字，
#    中文关键词由大模型（免费模型即可）读转写稿给出，填进 --keywords。
def auto_terms(subs):
    """只抽【不依赖中文分词也稳】的两类词作为兜底：
      - 英文/数字专名：AI、DeepSeek、Kimi、github、PPT、ChatGPT…
      - 带单位的数量短语：3个人 / 五分钟 / 60%
    中文关键词抽取（要分词）交给大模型大脑做：把转写稿丢给免费的模型，
    让它给 --keywords；脚本只负责渲染。这样既准、又不给学员加 jieba 依赖。
    """
    num_unit = re.compile(r"[0-9零一二两三四五六七八九十百千万]+"
                          r"(个人|个人次|分钟|小时|天|年|次|元|块|岁|倍|层|%|％)")
    stats = {}
    for a, b, txt in subs:
        for tk in LATIN_RE.findall(txt):
            tk = tk.strip(".-")
            if len(tk) >= 2 and tk.lower() not in LATIN_STOP:
                stats.setdefault(tk, []).append(a)
        for tk in num_unit.findall(txt):
            if len(tk) >= 2:
                stats.setdefault(tk, []).append(a)
    out = []
    for t, times in stats.items():
        c = len(times)
        score = 8.0 + min(len(t), 9) * 0.4 + (6 - min(c, 6)) * 0.9   # 越稀有越像重点
        out.append((round(score, 2), t, c, c))
    out.sort(key=lambda x: -x[0])
    return out


def pick_keywords(subs, n=6, min_gap=14.0, user_words=None):
    """返回 [(start, end, 词)]：按时间排布，彼此至少间隔 min_gap 秒。"""
    if user_words:
        # 每个词只取【第一次出现】的那句，避免同一个词反复弹卡片
        res = []
        used_words = set()
        for w in user_words:
            if not w:
                continue
            hit = None
            for (a, b, txt) in subs:
                if w in txt:
                    hit = (a, b, w)
                    break
            if hit and w not in used_words:
                res.append(hit)
                used_words.add(w)
        res.sort(key=lambda x: x[0])
        sparse = []
        for r in res:
            if sparse and r[0] - sparse[-1][1] < min_gap:
                continue
            sparse.append(r)
        if not sparse:
            print("   【注意】 你给的关键词在字幕稿里一个都没匹配到，检查一下是不是说法不同")
        return sparse[:n]

    chosen = []
    for score, term, cnt, iso in auto_terms(subs):
        for (a, b, txt) in subs:
            if term not in txt:
                continue
            if any(abs((a + b) / 2 - (c[0] + c[1]) / 2) < min_gap for c in chosen):
                continue
            if any(term.lower() in w.lower() or w.lower() in term.lower()
                   for _, _, w in chosen):
                continue
            chosen.append((a, b, term))
            break
        if len(chosen) >= n:
            break
    chosen.sort(key=lambda x: x[0])
    if not chosen:
        print("   【说明】 没自动抽到英文专名/数字要点。想上中文关键词，请给 --keywords，")
        print("      或把字幕稿丢给免费模型让它提炼关键词再填进来。")
    return chosen


def real_fps(video):
    """视频的真实平均帧率 = 实际帧数 / 视频流时长。

    ffprobe 报的 r_frame_rate 对手机 VFR 录屏常常是假的 30/1，
    直接用会让 zoompan 把片子整体压短，音画对不上。
    """
    if FFPROBE:
        try:
            dv = float(subprocess.check_output(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1",
                 video], stderr=subprocess.DEVNULL).decode().strip())
            nb = subprocess.run(
                [FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1",
                 video], capture_output=True, text=True)
            n = int((nb.stdout.strip() or "0"))
            if n > 0 and dv > 0:
                return max(1.0, n / dv)
        except Exception:
            pass
    return probe(video).get("fps") or 30.0


def build_zoom_expr(base, amp, period, pushes, fps):
    """生成 zoompan 的 z 表达式：呼吸 + 关键词处的钟形推近。

    【注意】 踩过的坑（别再退回旧写法）：
      - crop 的 w/h 表达式只在【初始化】时算一次，那时 t 还没意义 → 直接报错
        "Undefined constant ... t"，所以动态缩放不能用 crop+gscale。
      - zoompan 不认 t，只认 **on**（输出帧序号）→ 所有"秒"都要乘 fps 转成帧。
    """
    z = f"{base:.4f}"
    if amp > 0:
        z += f"+{amp:.4f}*sin(2*PI*on/{fps * period:.2f})"
    for (a, b, k) in pushes:
        A, B = a * fps, b * fps
        d = max(1.0, B - A)
        bell = (f"if(lt(on,{A:.2f}),0,if(gt(on,{B:.2f}),0,"
                f"{k:.4f}*(0.5-0.5*cos(2*PI*(on-{A:.2f})/{d:.2f}))))")
        z += "+" + bell
    return z


def find_srt(video, explicit=None):
    """找字幕稿：显式 > 同名 > 去掉'_粗剪'后缀的同名 > 目录里唯一的 srt。"""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    d = os.path.dirname(os.path.abspath(video))
    base = os.path.splitext(os.path.basename(video))[0]
    for cand in [f"{base}.srt", f"{base[:-3]}.srt" if base.endswith("_粗剪") else None]:
        if cand and os.path.exists(os.path.join(d, cand)):
            return os.path.join(d, cand)
    srt_list = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".srt")]
    if len(srt_list) == 1:
        return srt_list[0]
    return None


def main():
    ap = argparse.ArgumentParser(
        description="粗剪之后的本地动效增强（呼吸运镜/关键词推镜头/关键词卡片/自动闪避BGM）")
    ap.add_argument("video")
    ap.add_argument("--srt", default=None, help="字幕稿（默认找同名 .srt）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--burn-sub", action="store_true", default=True,
                    help="烧录字幕（默认就是开的，不用写这个参数；要关掉用 --no-sub）")
    ap.add_argument("--no-sub", dest="burn_sub", action="store_false")
    # 运镜
    ap.add_argument("--no-zoom", action="store_true", help="完全不动镜头")
    ap.add_argument("--zoom-base", type=float, default=None,
                    help="基准放大倍率（默认竖屏1.035/横屏1.02）")
    ap.add_argument("--zoom-amp", type=float, default=0.012, help="呼吸幅度")
    ap.add_argument("--zoom-period", type=float, default=9.0, help="呼吸周期(秒)")
    ap.add_argument("--zoom-anchor-y", type=float, default=0.47,
                    help="画面垂直锚点 0~1，越小越往上取景（口播可少带一点手部动作）")
    # 关键词
    ap.add_argument("--keywords", default=None, help="关键词，逗号分隔；不给就自动从字幕稿抽")
    ap.add_argument("--auto-keywords", type=int, default=6, help="自动抽几个关键词")
    ap.add_argument("--min-gap", type=float, default=14.0, help="两张关键词卡最短间隔(秒)")
    ap.add_argument("--card-y", type=float, default=0.775,
                    help="关键词卡纵向位置(0~1)。默认 0.775=下巴以下胸口区，不挡脸")
    ap.add_argument("--push", type=float, default=0.055, help="关键词处推镜头幅度")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-card", action="store_true", help="不要关键词弹出卡片")
    ap.add_argument("--force-sub", action="store_true",
                    help="画面已检出文字仍强行烧字幕（确认那是背景屏不是字幕时用）")
    # 音乐
    ap.add_argument("--bgm", default=None, help="背景音乐（建议纯伴奏/无人声）。留空则自动用技能自带轻量BGM")
    ap.add_argument("--no-bgm", action="store_true", help="不加背景音乐")
    ap.add_argument("--bgm-db", type=float, default=-14.0,
                    help="音乐音量(dB)。 empirically: 音乐原声均值约 -22dB、"
                         "人声约 -26dB，-14 左右才听得见又不抢话；别再给到 -20")
    ap.add_argument("--duck", action="store_true", default=True,
                    help="说话时自动压低音乐（默认开着；要关掉用 --no-duck）")
    ap.add_argument("--no-duck", dest="duck", action="store_false")
    args = ap.parse_args()

    # 【要点】 默认自带 BGM：未显式给 --bgm 且未 --no-bgm 时，自动用技能自带的轻量音乐
    if not args.bgm and not args.no_bgm:
        _self_bgm = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "assets", "bgm.mp3"))
        if os.path.exists(_self_bgm):
            args.bgm = _self_bgm

    video = os.path.abspath(args.video)
    if not os.path.exists(video):
        sys.exit(f"【失败】 找不到视频：{video}")
    outdir = os.path.dirname(os.path.abspath(video))
    if args.bgm:
        args.bgm = os.path.abspath(args.bgm)
    name = os.path.splitext(os.path.basename(video))[0]
    found_srt = find_srt(video, args.srt)
    out = args.out or os.path.join(outdir, name + "_增强版.mp4")

    size = display_size(video) or (1920, 1080)
    W, H = size
    portrait = H > W
    dur = get_duration(video)
    print(f"① 输入：{os.path.basename(video)}  {W}x{H}（{'竖屏' if portrait else '横屏'}）  {dur:.1f}s")

    # ── 字幕：三层保险（显式 > 自动找 > OCR 闸门防双层）─────────────
    subs = load_srt(found_srt)
    if subs:
        print(f"② 字幕：{os.path.basename(found_srt)}  {len(subs)} 行")
    else:
        print("② 没找到字幕稿 → 只做画面/音乐增强")
    burn_sub = args.burn_sub
    if burn_sub and subs and not args.force_sub:
        gate, samples = has_burned_subtitle(video)
        if gate is True:
            burn_sub = False
            print(f"   【禁止】 检到画面里已有字幕/文字（{'、'.join(samples[:3])}…）→ 不重复烧，防双层。"
                  f"确认那是背景文字就加 --force-sub")
    srt = found_srt

    # ── 关键词（推镜头 + 弹卡片共用同一批时间点）───────────────────
    user_words = [w.strip() for w in args.keywords.split(",")] if args.keywords else None
    # 【注意】 --auto-keywords 的默认 6 只用于【自动抽词】；用户自己给的关键词不许被截断
    n_cap = max(len(user_words or []), 16) if user_words else args.auto_keywords
    kws = pick_keywords(subs, n=n_cap, min_gap=args.min_gap,
                        user_words=user_words) if subs else []
    if kws:
        print("③ 关键词（从这里自动挑的，可用 --keywords 改）：")
        for a, b, w in kws:
            print(f"   {a:6.1f}s–{b:6.1f}s  「{w}」")

    # ── 生成 ASS（字幕 + 关键词卡片都在同一个 ASS 里，一次渲染成型）──
    ass_name = name + "_enhanced.ass"
    ass_path = os.path.join(outdir, ass_name)
    card_events = [] if args.no_card else [(a, min(b + 0.4, a + 3.4), w) for a, b, w in kws]
    n_line, _ = write_ass(srt if burn_sub and subs else None, ass_path, W, H,
                          keywords=card_events, card_y=args.card_y)
    print(f"④ ASS：{ass_path}（字幕 {n_line} 行 + 关键词卡 {len(card_events)} 张）")

    # ── 视频滤镜链：动态裁切(=运镜) → 拉回原尺寸 → 字幕/卡片 → 淡入淡出 ──
    vf_parts = []
    if not args.no_zoom:
        # 【注意】 必须用【真实帧率】而不是容器标的 30fps：手机录屏多为 VFR（实测本片
        #    平均 29.77fps），zoompan 会按帧序号重新等间隔打时间戳，
        #    用错 fps 会把 264.8s 的片子压成 262.8s → 音画整体错开近 2 秒。
        fps = real_fps(video)
        if fps <= 0 or fps > 60:
            fps = 30.0
        base = args.zoom_base or (1.035 if portrait else 1.02)
        pushes = [] if args.no_push else [(max(0.0, a - 0.4), b + 0.5, args.push) for a, b, w in kws]
        z = build_zoom_expr(base, args.zoom_amp, args.zoom_period, pushes, fps)
        vf_parts.append(
            f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih*{args.zoom_anchor_y:.3f}-(ih/zoom/2)'"
            f":s={W}x{H}:fps={fps:g}")
        print(f"⑤ 运镜：基准 {base:.3f}× + 呼吸 ±{args.zoom_amp}/{args.zoom_period}s"
              f" + {len(pushes)} 处推近 {args.push:.3f}（锚点 y={args.zoom_anchor_y}，{fps:g}fps）")
    else:
        print("⑤ 运镜：关闭")
    vf_parts.append(f"ass='{ass_name}'")
    vf_parts.append("fade=t=in:st=0:d=0.6")
    vf_parts.append(f"fade=t=out:st={max(0.1, dur - 1.0):.3f}:d=1.0")
    vf_parts.append("format=yuv420p")
    vf = ",".join(vf_parts)

    # ── 音频：人声 + 自动闪避的 BGM ─────────────────────────────────
    has_audio = "Audio:" in subprocess.run([FFMPEG, "-i", video], capture_output=True,
                                           text=True).stderr
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", video]
    if has_audio and args.bgm and os.path.exists(args.bgm):
        cmd += ["-stream_loop", "-1", "-i", args.bgm]
        bgm_chain = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={args.bgm_db}dB,"
                     f"afade=t=in:st=0:d=1.0,afade=t=out:st={max(0.1,dur-1.6):.3f}:d=1.6[bg0];")
        if args.duck:
            bgm_chain += ("[bg0][sc]sidechaincompress=threshold=0.08:ratio=4:"
                          "attack=25:release=400:makeup=1[bg];")
        else:
            bgm_chain += "[bg0]anull[bg];"
        af = ("[0:a]aresample=48000,aformat=channel_layouts=stereo,"
              "asplit=2[sp][sc];" + bgm_chain +
              "[sp][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
              "alimiter=limit=0.95,"
              "afade=t=in:st=0:d=0.4,"
              f"afade=t=out:st={max(0.1,dur-1.0):.3f}:d=1.0[aout]")
        print(f"⑥ 音频：保留人声 + BGM {os.path.basename(args.bgm)} "
              f"({args.bgm_db}dB, {'自动闪避' if args.duck else '不闪避'})")
    else:
        if args.bgm:
            print(f"⑥ 【注意】 跳过 BGM：找不到 {args.bgm}")
        else:
            print("⑥ 音频：保持原声（未给 BGM）")
        af = ("[0:a]aresample=48000,aformat=channel_layouts=stereo,"
              "afade=t=in:st=0:d=0.4,"
              f"afade=t=out:st={max(0.1,dur-1.0):.3f}:d=1.0[aout]"
              if has_audio else None)

    cmd += ["-vf", vf]
    if af:
        cmd += ["-filter_complex", af]
    cmd += ["-map", "0:v"]
    cmd += ["-map", "[aout]"] if af else ["-map", "0:a?"]
    cmd += ["-c:v", "libx264", "-profile:v", "main", "-level", pick_level(W, H),
            "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]

    print("⑦ 渲染中（1080p 全片重编码 + 逐帧运镜，几分钟）…")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=outdir)
    if r.returncode != 0:
        print("【失败】 渲染失败：\n" + r.stderr[-2000:])
        sys.exit(1)
    print(f"【OK】 完成：{out}  ({get_duration(out):.1f}s)")


if __name__ == "__main__":
    main()
