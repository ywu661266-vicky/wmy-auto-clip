#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autoclip.py — 一键 AI 自动剪辑（零积分 / 纯本地 / 免费模型）
==============================================================
两种模式，一个工具：

【模式一：长剪短】输入一个视频文件
  1) 本地转写（faster-whisper，不联网、不花积分），出 .srt 字幕稿
  2) 自动剪掉说话间的静音空白，成片更紧凑
  3) 【可选，默认关闭】把字幕烧进画面
     【注意】 防双字幕：原片【本来就有字幕】时千万别开 --burn-sub。

【模式二：散拼长】输入一个文件夹 或 一个 .zip 素材包
  1) ZIP 自动解压，读出里面所有视频素材
  2) 每段自动"统一化"：分辨率/帧率/横竖屏/音量拉到一致
     （竖屏进横屏画布默认用「该片段自己的放大模糊版」当背景，不裁不变形）
  3) 按文件名顺序拼接（01_xxx 02_xxx 这样命名就能控制顺序）
  4) 可选：片段间叠化转场(--transition fade)、加背景音乐(--bgm)
  5) 对合辑整体转写出字幕稿，可选烧录(同样遵守防双字幕规则)
  6) 输出一份「剪辑报告」：AI 对每段素材做了什么，讲得清、教得了

用法（学员只需一行）：
  长剪短：python autoclip.py 你的视频.mp4 [--burn-sub]
  散拼长：python autoclip.py 素材文件夹 或 素材包.zip [--burn-sub] [--transition fade] [--bgm 音乐.mp3]

模型：本机已缓存就离线用（秒开、零网络）；没缓存会自动走国内镜像免费下载一次
      （HF 模型免费、无需密钥、不耗积分），之后离线。
"""
import argparse, os, sys, subprocess, re, tempfile, zipfile, shutil

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

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".ts", ".flv", ".wmv", ".webm", ".mts"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}

# ── ffmpeg / ffprobe：按优先级找（系统PATH → 脚本同目录 → WorkBuddy自带 → imageio-ffmpeg 自带）──
FFMPEG = None
FFPROBE = None
_script_dir = os.path.dirname(os.path.abspath(__file__))
for base in ["", _script_dir, os.path.expanduser("~") + r"\.workbuddy\binaries\ffmpeg"]:
    cand_ff = "ffmpeg" if base == "" else os.path.join(base, "ffmpeg.exe")
    cand_fp = "ffprobe" if base == "" else os.path.join(base, "ffprobe.exe")
    try:
        subprocess.run([cand_ff, "-version"], capture_output=True, timeout=8)
        FFMPEG = cand_ff
        try:
            subprocess.run([cand_fp, "-version"], capture_output=True, timeout=8)
            FFPROBE = cand_fp
        except Exception:
            FFPROBE = None
        break
    except Exception:
        continue

# 兜底：pip 装的 imageio-ffmpeg 自带一份 ffmpeg（学员一条 pip 命令就能跑，不必手动配 PATH）
if FFMPEG is None:
    try:
        import imageio_ffmpeg
        _ff = imageio_ffmpeg.get_ffmpeg_exe()
        if _ff and os.path.exists(_ff):
            FFMPEG = _ff
            print("  （使用 imageio-ffmpeg 自带的 ffmpeg）")
    except Exception:
        pass

if FFMPEG is None:
    sys.exit("【失败】 没找到 ffmpeg。最快的办法：终端执行  pip install imageio-ffmpeg  ；"
             "或把 ffmpeg.exe 放到 autoclip.py 同一文件夹。")


def _brief(ex):
    """把异常压成一行人话（ffmpeg 的报错又长又全是英文栈）"""
    msg = str(ex)
    for key, human in (
        ("Invalid NAL unit size", "文件已损坏或下载不完整"),
        ("moov atom not found", "文件不完整（可能是下载中断）"),
        ("Cannot determine format", "文件无法解码，可能已损坏"),
        ("Invalid data found when processing input", "文件数据损坏"),
        ("No such file or directory", "文件找不到"),
        ("Permission denied", "没有访问权限"),
        ("Unknown encoder", "编码器不可用（ffmpeg 版本过旧）"),
        ("Invalid argument", "参数不被当前 ffmpeg 支持"),
        ("does not contain any stream", "文件里没有可用的音视频流"),
    ):
        if key in msg:
            return human
    first = msg.strip().splitlines()
    return (first[-1] if first else "未知错误")[:110]


def run(cmd, cwd=None):
    print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise RuntimeError("命令失败：" + " ".join(cmd[:3]))
    return r


# ════════════════════════════════════════════════════════════
#  公共：转写 / 时长 / 字幕烧录
# ════════════════════════════════════════════════════════════

def load_model(size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit(
            "\n【失败】 缺语音识别组件 faster-whisper（装一次，之后永久离线用，不花积分）。\n"
            "   在终端执行这一行：\n"
            "     pip install faster-whisper\n"
            "   国内网络慢就加镜像：\n"
            "     pip install faster-whisper -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
            "   【注意】 只想拼片、不要字幕？加参数 --no-sub 可以完全跳过转写，直接出片。\n"
        )
    try:
        return WhisperModel(size, device="cpu", compute_type="int8", local_files_only=True)
    except Exception:
        print("  （本机无模型缓存，走国内镜像免费下载一次，无需密钥/不耗积分）")
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        return WhisperModel(size, device="cpu", compute_type="int8")


# ════════════════════════════════════════════════════════════
#  安全编码红线（PowerPoint 视频页）
#  · profile 必须 Main（High 档在部分 PowerPoint 版本播不出/需修复）
#  · 宽高必须 mod 8（1194 这种非 8 倍数宽度是事故源）
#  · 必须 yuv420p + AAC + faststart
# ════════════════════════════════════════════════════════════
def pick_level(w=None, h=None):
    """按分辨率选 H.264 level。
    3.1 只覆盖到 720p；1080p 竖屏（1080x1920）若写 3.1，x264 会报 level 不兼容并降级，
    在部分 PowerPoint 上出现黑屏/需修复。所以按尺寸自适应。"""
    m = max(w or 0, h or 0)
    if m and m <= 1280:
        return "3.1"
    if m and m > 1920:
        return "4.1"
    return "4.0"


def safe_codec_args(crf="20", w=None, h=None):
    """统一的 PPT 安全编码参数（level 按分辨率自适应，1080p 竖屏走 4.0）"""
    return ["-c:v", "libx264", "-profile:v", "main", "-level", pick_level(w, h),
            "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", crf,
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]


def mod8_filter(w=None, h=None):
    """生成把宽高对齐到 8 倍数的 scale 滤镜（无参则按源分辨率自适应）。
    1194 -> 1192（取最近偶数倍 8），720 保持不变。"""
    if w and h:
        return f"scale={w}:{h}"
    return "scale=trunc(iw/8)*8:trunc(ih/8)*8"


def fmt(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def is_likely_song(seg, text):
    """判定这段转写是不是背景歌声/音乐而非人声。
    实测标定（faster-whisper small，中文）：
      真人讲话  logprob≈-0.25  nospeech 0.01~0.77
      背景歌声  logprob -0.34~-0.71，且多为 6~9 字短句、nospeech 常 >0.85
    规则：logprob < -0.60（置信度低）→ 歌声；
          或 nospeech>0.85 且短句(<10字) → 歌声。
    真人最低 -0.25，与 -0.60 之间有充分安全边界，不误杀。
    """
    lp = getattr(seg, "avg_logprob", 0)
    ns = getattr(seg, "no_speech_prob", 0)
    if lp < -0.60:
        return True
    if ns > 0.85 and len(text) < 10:
        return True
    return False


def has_audio_stream(path):
    """这个视频到底有没有音频流。

    没有 ffprobe 时退回「ffmpeg -i 读 stderr」判断（stderr 里有 Audio: 行就是有）。
    判断不出来就当有 —— 宁可多跑一次转写，也不要误杀有声音的素材。
    """
    if FFPROBE:
        try:
            r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0",
                                "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                               capture_output=True, timeout=25)
            out = (r.stdout or b"").decode("utf-8", "ignore").strip()
            if out:
                return out.startswith("audio")
        except Exception:
            pass
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, timeout=25)
        err = (r.stderr or b"").decode("utf-8", "ignore")
        if "Audio:" in err:
            return True
        if "Stream #" in err and "Video:" in err:
            return False
    except Exception:
        pass
    return True


def transcribe(video, model_size, outdir, name, skip_music=True):
    """转写视频出 SRT。
    skip_music=True 时过滤掉「高概率无人声」的段（歌声/BGM 会被 whisper 转出
    满篇错别字歌词，烧上去就是灾难）——宁可不烧，不瞎编。

    【无音轨不崩】录屏、会议录像、无人机空镜常常没有声音，
    faster-whisper 解音频会报 "Output file does not contain any stream"，
    以前这里直接把整个任务带崩，学员只看到一屏英文报错。
    现在：没有音轨就跳过转写、留一份空 SRT，成片照常产出。
    """
    srt_path = os.path.join(outdir, name + ".srt")
    if not has_audio_stream(video):
        print("   【注意】 这段素材没有音轨（录屏/空镜/纯画面），跳过转写。")
        print("      成片照常产出，只是不带字幕 —— 这不是故障。")
        open(srt_path, "w", encoding="utf-8").close()
        return srt_path
    model = load_model(model_size)
    print(f"① 本地转写（模型 {model_size}）…")
    # initial_prompt 纠偏：whisper 中文默认吐繁体，用简体样句拉回简体
    try:
        segments, info = model.transcribe(
            video, language="zh", beam_size=5, vad_filter=True,
            initial_prompt="以下是普通话的内容片段，请使用简体中文输出。")
    except Exception as ex:
        print(f"   【注意】 转写失败：{_brief(ex)}")
        print("      已跳过字幕，成片照常产出（可以改用人工校对字幕）。")
        open(srt_path, "w", encoding="utf-8").close()
        return srt_path
    srt_path = os.path.join(outdir, name + ".srt")
    idx = 0
    skipped = 0
    # 兜底：若 opencc 可用，再做一次 t2s（initial_prompt 偶尔不听话）
    _cc = None
    try:
        from opencc import OpenCC
        _cc = OpenCC("t2s")
    except Exception:
        pass
    with open(srt_path, "w", encoding="utf-8") as f:
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            if skip_music and is_likely_song(seg, text):
                skipped += 1
                continue
            idx += 1
            start = max(0, seg.start)
            f.write(f"{idx}\n{fmt(start)} --> {fmt(seg.end)}\n{text}\n\n")
    print(f"   字幕稿：{srt_path}（{idx} 段）")
    if skipped:
        print(f"    跳过 {skipped} 段疑似歌声/音乐（无人声）——歌词识别错别字多，宁可不烧。")
    if idx == 0:
        print("   【注意】 没转写出任何文字：这段素材可能没有人声（纯音乐/纯画面/环境音）。")
        print("      没人声就不烧字幕——AI 不瞎编，这是正常行为，不是故障。")
    return srt_path


def get_duration(video):
    if FFPROBE:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", video], capture_output=True, text=True)
        try:
            return float(r.stdout.strip().splitlines()[-1])
        except Exception:
            pass
    info = subprocess.run([FFMPEG, "-i", video], capture_output=True, text=True)
    m = re.search(r"Duration:\s*([\d:.]+)", info.stderr)
    if m:
        h, mm, ss = m.group(1).split(":")
        return int(h) * 3600 + int(mm) * 60 + float(ss)
    return 0.0


def display_size(video):
    """返回【显示尺寸】(自动旋转后的宽高)。

    手机竖拍素材码流里常记 1920x1080 + rotation=90，实际显示/成片应是 1080x1920。
    不校正会引发两类事故：① 竖视频被强行拼进横屏黑框 ② 字幕基准分辨率取错
    → libass 按 384x288 默认基准放大 ~5 倍，字幕糊满画面。
    """
    if not FFPROBE:
        return None
    try:
        import json
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,side_data=rotation",
             "-of", "json", video], capture_output=True, text=True)
        st = json.loads(r.stdout)["streams"][0]
        w, h = st.get("width", 0), st.get("height", 0)
        rot = 0.0
        for sd in st.get("side_data_list", []) or []:
            if "rotation" in sd:
                try:
                    rot = float(sd["rotation"])
                except Exception:
                    pass
        if not rot:
            r2 = subprocess.run(
                [FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream_tags=rotate", "-of", "csv=p=0", video],
                capture_output=True, text=True)
            try:
                rot = float(r2.stdout.strip().splitlines()[0])
            except Exception:
                pass
        if abs(rot) % 180 == 90:
            w, h = h, w
        return (int(w), int(h)) if w and h else None
    except Exception:
        return None


def make_tmp(prefix, workdir=None):
    """建临时目录。workdir 指定时放那下面（C 盘满时的逃生口）。
    4K 素材统一化的中间片段很大，几十段能吃掉几十 GB —— 别都堆在 C 盘临时目录。"""
    if workdir:
        os.makedirs(workdir, exist_ok=True)
        return tempfile.mkdtemp(prefix=prefix, dir=workdir)
    return tempfile.mkdtemp(prefix=prefix)


def pick_target_size(clips, explicit_vertical=False):
    """决定合辑的目标尺寸。

    以前只看用户有没有加 --vertical，忘了加就一律横屏 1920x1080
    → 学员给一堆手机竖拍素材时，会被塞进左右大黑边。
    现在：没显式指定就按【素材实际朝向】自动定（竖拍占多数就用竖屏）。
    """
    if explicit_vertical:
        return (1080, 1920), "用户指定竖屏"
    portrait = 0
    total = 0
    for c in clips:
        sz = display_size(c)
        if sz:
            total += 1
            if sz[1] > sz[0]:
                portrait += 1
    if total and portrait * 2 > total:
        print(f"    检测到 {portrait}/{total} 段素材是竖拍 → 自动用竖屏 1080x1920"
              f"（想强制横屏加 --vertical 关掉自动，或改 --fit pad）")
        return (1080, 1920), "自动（素材多数竖拍）"
    return (1920, 1080), "默认/横屏"


def write_ass(srt_path, ass_path, W, H, keywords=None, fontsize=None, card_y=0.775):
    """SRT → ASS，并【显式写死基准分辨率】=成片尺寸。

    为什么必须用 ASS 而不是 `subtitles=` 滤镜：
      `subtitles=` 走 libass 默认基准 384x288，成片被旋转/重定尺寸后字会被同比放大。
      实测竖屏 1080x1920 口播：FontSize=30 → 每字实际渲染 167~183px，17 字折 3 行、
      吃掉画面 34% 高度，完全不能看。显式 PlayResX/Y 与成片 1:1 后，
      每字 ~43px、17 字一行占宽 67%，正常。
    keywords: [(start_sec, end_sec, "词"], ...] 生成带缩放动画的关键词卡片样式。
    返回 (字幕行数, ass 路径)
    """
    def t_ass(sec):
        sec = max(0.0, float(sec))
        h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    W, H = int(W), int(H)
    base = min(W, H)
    fs = fontsize or int(max(24, min(80, round(base * 0.052))))
    mv = int(H * 0.075)          # 底部安全边距
    # 关键词卡：白字 + 橙色实心胶囊盒（ASS 颜色是 &HAABBGGRR，别写成 RGB 顺序）
    # 【注意】 BorderStyle=3 时"背景盒"用的是 OutlineColour 而不是 BackColour，
    #    之前写成白边白字 → 卡片变成一个没有字的白方块，等于事故。
    card_word_color = "&H00FFFFFF"
    card_box_color = "&H002D5AF0"    # 橙 F05A2D
    empty = True
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Microsoft YaHei,{fs},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H80000000,1,0,0,0,100,100,0,0,1,3,0,2,{int(W*0.04)},{int(W*0.04)},{mv},1\n"
        f"Style: Keyword,Microsoft YaHei,{int(fs*1.15)},{card_word_color},&H000000FF,"
        f"{card_box_color},&H00000000,1,0,0,0,100,100,0,0,3,16,0,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []

    if srt_path and os.path.exists(srt_path):
        try:
            raw = open(srt_path, encoding="utf-8").read().strip()
        except Exception:
            raw = ""
        for b in raw.split("\n\n"):
            ls = [l for l in b.splitlines() if l.strip()]
            if len(ls) < 3:
                continue

            def _sec(tok):
                try:
                    hh, mm, rest = tok.strip().replace(",", ".").split(":")
                    return int(hh) * 3600 + int(mm) * 60 + float(rest)
                except Exception:
                    return None
            pair = ls[1].split("-->") if "-->" in ls[1] else None
            if not pair:
                continue
            a, b2 = _sec(pair[0]), _sec(pair[1])
            if a is None or b2 is None:
                continue
            text = " \\N ".join(l.strip() for l in ls[2:])
            if not text:
                continue
            empty = False
            lines.append(f"Dialogue: 0,{t_ass(a)},{t_ass(b2)},Default,,0,0,0,,{text}\n")

    for kw in (keywords or []):
        a, b2, word = kw[0], kw[1], kw[2]
        if not word or b2 <= a:
            continue
        # 弹出：70%→115%→100% 缩放 + 淡入淡出；钉在 card_y 高度。
        # card_y 默认 0.775：人像类竖屏实测脸部占 0.32~0.72、字幕区从 0.86 起，
        # 0.775 落在下巴以下、字幕以上的胸口区，不挡脸也不压字幕。
        tag = (r"{\an5\pos(%.0f,%.0f)\fscx70\fscy70"
               r"\t(0,180,\fscx115\fscy115)\t(180,320,\fscx100\fscy100)"
               r"\fad(120,160)}" % (W * 0.5, H * card_y))
        lines.append(f"Dialogue: 1,{t_ass(a)},{t_ass(b2)},Keyword,,0,0,0,,{tag}{word}\n")

    open(ass_path, "w", encoding="utf-8").write(header + "".join(lines))
    return (len(lines) if not empty else 0), ass_path


def burn_sub(video, srt, out):
    """烧字幕：ASS 版（显式基准分辨率，竖屏/横屏都正常大小）。"""
    # 【注意】 run(cwd=输出目录) 会把【相对路径】重新基于 cwd 解析成
    #    out/x/out/x/文件名 → "No such file or directory"。先把路径全转成绝对。
    video = os.path.abspath(video)
    srt = os.path.abspath(srt)
    out = os.path.abspath(out)
    d = os.path.dirname(out)
    size = display_size(video) or None
    W, H = size if size else (1920, 1080)
    ass_name = os.path.splitext(os.path.basename(srt))[0] + ".ass"
    ass_path = os.path.join(d, ass_name)
    n, _ = write_ass(srt, ass_path, W, H)
    # Windows 下绝对路径的 D:\ 冒号会被 ffmpeg 当转义符 → cwd 切到输出目录用相对名
    run([FFMPEG, "-y", "-loglevel", "error", "-i", video,
         "-vf", f"ass='{ass_name}',{mod8_filter()}",
         ] + safe_codec_args("20") + [out], cwd=d)
    return n


_OCR = None
def has_burned_subtitle(video):
    """OCR 检测视频是否已烧录字幕（防双字幕的自动闸门）。

    2026-10-25 重修（修"单行字幕漏检 → 烧出双层字幕"事故）：
    旧规则要求"每帧检出 ≥2 个文字项"才计数，而绝大多数字幕是【单行】，
    每帧只有 1 个文字项 → 永远漏检 → 在自带字幕的原片上又烧一层。
    新规则只认"会和我们的字幕抢同一块位置"的文字，两类：
      规则1 底部字幕区（画面下方 28%）内有中文文字（框高≥画面高3%）→ 有字幕
      规则2 画面任何位置有"大号中文文字块"（框高≥画面高9% 且 ≥4 个汉字）→ 有字幕
    采样约每 1.5s 一帧、最多 14 帧；同一规则 ≥2 帧命中才判定（防单帧误杀）。
    PPT/背景屏的小字不拦——我们的字幕烧在底部，不会和它们重叠。
    水印类小字（框高<3%）也不拦，避免干净口播被误杀。
    返回 (判定, 文字样例列表)：True=有字幕别再烧 / False=干净 / None=OCR 不可用
    """
    global _OCR
    if _OCR is False:
        return None, []
    try:
        if _OCR is None:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        # 画面高度：底部字幕区/大号文字块判定都要用
        vh = 0
        if FFPROBE:
            r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=height", "-of",
                                "default=nw=1:nk=1", video], capture_output=True, text=True)
            try:
                vh = int(r.stdout.strip().splitlines()[0])
            except Exception:
                vh = 0
        tmp = tempfile.mkdtemp(prefix="ocr_")
        dur = get_duration(video)
        n = min(14, max(4, int(dur / 1.5) + 1))
        hits_zone = 0   # 规则1命中帧数（底部字幕区有文字）
        hits_big = 0    # 规则2命中帧数（有大号中文文字块）
        samples = []
        for k in range(n):
            t = dur * (k + 0.5) / n
            png = os.path.join(tmp, "f.png")
            run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.1f}", "-i", video,
                 "-frames:v", "1", png])
            result, _ = _OCR(png)
            if not result:
                continue
            zone_hit = big_hit = False
            for item in result:
                try:
                    box, txt, conf = item[0], str(item[1]), float(item[2])
                except (IndexError, ValueError, TypeError):
                    continue
                if conf < 0.5 or not re.search(r"[\u4e00-\u9fff]", txt):
                    continue
                if len(samples) < 8 and txt not in samples:
                    samples.append(txt)
                if vh:
                    y_c = (box[0][1] + box[2][1]) / 2.0      # 文字块中心纵坐标
                    box_h = abs(box[2][1] - box[0][1])      # 文字块高度
                    if y_c >= vh * 0.72 and box_h >= vh * 0.03:
                        zone_hit = True
                    if box_h >= vh * 0.09 and len(re.findall(r"[\u4e00-\u9fff]", txt)) >= 4:
                        big_hit = True
                else:
                    zone_hit = True   # 取不到画面高度时的兜底：有中文就当可能有字幕
            if zone_hit:
                hits_zone += 1
            if big_hit:
                hits_big += 1
        shutil.rmtree(tmp, ignore_errors=True)
        if hits_zone >= 2 or hits_big >= 2:
            return True, samples
        return False, samples
    except ImportError:
        _OCR = False
        return None, []
    except Exception:
        return None, []


def process_segment(raw, idx, tmp, args, W, H, want_sub=None, note="", want_mute=False):
    """单段完整处理（转写/OCR/字幕决策都基于【原始切片】，拉音量会破坏 vad 的歌声过滤）：
    1) 单独转写原始切片 —— 纯背景歌声被过滤（不烧，AI 不瞎编）
    2) OCR 检测原始切片是否带文字 —— 有就保守不烧（防双层，宁可漏烧）
    3) 有人声 且 画面干净 → 统一画面后烧字幕
    want_sub: None=按 args 默认；True=本段强制转写烧字幕；False=本段完全跳过（省时间）
    note: 选段单备注（用于查找人工校对字幕文件）
    want_mute: True=这一段丢掉原声只留画面（选段单标 [静音]），不转写不烧字
    返回 (处理后的路径, 状态说明)
    """
    dst = os.path.join(tmp, f"n{idx:02d}.mp4")
    normalize_segment(raw, dst, W, H, 30, args.fit,
                      denoise=not getattr(args, "no_denoise", False),
                      strong=getattr(args, "denoise_strong", False),
                      mute=want_mute)
    if want_mute:
        return dst, "已去原声（只留画面）"
    if want_sub is None:
        want_sub = not getattr(args, "no_sub", False)
    if not want_sub:
        return dst, "已跳过转写（--no-sub / 选段单未标[字幕]：只拼片不烧字幕）"

    # 优先用【人工校对字幕】：--sub-file 指定的目录里放 segNN.srt（段序号对应）
    manual = ""
    sub_dir = getattr(args, "sub_file", None)
    if sub_dir:
        cand = os.path.join(sub_dir, f"seg{idx:02d}.srt")
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            manual = cand

    if manual:
        srt = manual
        print(f"    用人工校对字幕：{os.path.basename(manual)}")
    else:
        srt = transcribe(raw, args.model, tmp, f"seg{idx:02d}")
    has_speech = os.path.getsize(srt) > 0
    if not has_speech:
        return dst, "无人声 → 不烧字幕"
    if manual:
        # 人工校对过的字幕直接烧，不再被 OCR 闸门拦（人已经确认过）
        out = os.path.join(tmp, f"s{idx:02d}.mp4")
        burn_sub(dst, srt, out)
        return out, "已烧人工校对字幕"
    has_sub, samples = has_burned_subtitle(raw)
    if has_sub is True and not getattr(args, "force_sub", False):
        hint = "、".join(samples[:4]) if samples else "文字"
        return dst, f"【禁止】 画面检出文字（{hint}…）→ 保守不烧（防双层；确认非字幕可 --force-sub）"
    if has_sub is True:
        out = os.path.join(tmp, f"s{idx:02d}.mp4")
        burn_sub(dst, srt, out)
        return out, "已按 --force-sub 强烧字幕"
    out = os.path.join(tmp, f"s{idx:02d}.mp4")
    burn_sub(dst, srt, out)
    return out, "有人声且画面干净 → 已烧字幕"


# ════════════════════════════════════════════════════════════
#  模式一：长剪短（剪静音）
# ════════════════════════════════════════════════════════════

def detect_silence(video, noise="-35dB", dur=0.4):
    r = subprocess.run([FFMPEG, "-i", video, "-af",
                        f"silencedetect=noise={noise}:d={dur}",
                        "-f", "null", "-"], capture_output=True, text=True)
    starts, ends = [], []
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m: starts.append(float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m: ends.append(float(m.group(1)))
    total = get_duration(video)
    keeps = []
    cursor = 0.0
    for s, e in zip(starts, ends):
        if s > cursor:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total:
        keeps.append((cursor, total))
    if not keeps:
        keeps = [(0, total)]
    return keeps, total


def cut_silence(video, keeps, out, pad=0.08):
    tmp = tempfile.mkdtemp()
    parts = []
    for i, (s, e) in enumerate(keeps):
        s0 = max(0, s - pad); e0 = e + pad
        if e0 <= s0:
            continue
        seg = os.path.join(tmp, f"p{i}.mp4")
        run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{s0}", "-to", f"{e0}",
             "-i", video, "-c", "copy", seg])
        parts.append(seg)
    # 【注意】 最终拼接必须【重新编码】，不能 -c copy：
    # stream-copy 拼多段时音频帧常延伸到比容器头更长的位置（实测本片音频307s/头258s），
    # 会导致烧字幕时画面在视频流结束后冻住、字幕轴对不上。重编码强制音视频时长一致。
    if len(parts) == 1:
        run([FFMPEG, "-y", "-loglevel", "error", "-i", parts[0],
             "-vf", mod8_filter()] + safe_codec_args("20") + [out])
    else:
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p in parts:
                f.write(f"file '{p.replace(chr(92), '/')}'\n")
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-vf", mod8_filter()] + safe_codec_args("20") + [out])
    for p in parts:
        try: os.remove(p)
        except Exception: pass


def mode_condense(args):
    """模式一：长视频 → 剪静音 → 成片"""
    video = args.video
    os.makedirs(args.outdir, exist_ok=True)
    name = os.path.splitext(os.path.basename(video))[0]
    srt = os.path.join(args.outdir, name + ".srt")
    cut = os.path.join(args.outdir, name + "_粗剪.mp4")
    sub = os.path.join(args.outdir, name + "_粗剪_字幕.mp4")

    print("② 自动剪静音（粗剪）…")
    keeps, total = detect_silence(video, noise=args.noise, dur=args.dur)
    kept = sum(e - s for s, e in keeps)
    print(f"   原片 {total:.1f}s → 保留 {kept:.1f}s，剪掉 {total-kept:.1f}s（{len(keeps)} 段）")
    cut_silence(video, keeps, cut)
    print(f"   粗剪成片：{cut}")

    if getattr(args, "no_sub", False):
        print("③ 已跳过转写与字幕（--no-sub：不生成字幕稿，直接出片）")
    else:
        # 【注意】 必须在【粗剪】上转写：原片剪掉静音后时间轴已变，若在原片转写，
        # 字幕会越往后越错位（原片351s→粗剪258s，结尾最大偏移~93s）。
        srt = transcribe(cut, args.model, args.outdir, name)

        if args.burn_sub:
            if os.path.getsize(srt) > 0:
                detected, samples = has_burned_subtitle(video)
                if detected is True and not args.force_sub:
                    hint = "、".join(samples[:4]) if samples else "文字"
                    print(f"③ 【禁止】 跳过烧字幕：OCR 检出画面文字（{hint}…），疑有字幕/屏显文字，防双层。")
                    print("      确认是背景屏文字要强烧，加 --force-sub 重跑。")
                else:
                    if detected is None:
                        print("   【说明】 字幕检测不确定，按你的 --burn-sub 执行")
                    print("③ 烧字幕…")
                    burn_sub(cut, srt, sub)
                    print(f"   带字幕成片：{sub}")
            else:
                print("③ 跳过烧字幕：没转写出文字（无人声），无可烧内容。")
        else:
            print("③ 字幕未烧录（默认关闭，防双字幕）。原片无字幕想加字幕就加 --burn-sub 重跑。")
    print("\n【OK】 完成。输出在：", args.outdir)


# ════════════════════════════════════════════════════════════
#  模式二：散拼长（多素材 → 合辑）
# ════════════════════════════════════════════════════════════

def natural_key(p):
    """自然排序：02_虹.mp4 排在 10_xxx.mp4 前面"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", os.path.basename(p))]


def unzip_if_needed(path):
    """输入是 zip 就解压到临时目录，返回素材目录"""
    if path.lower().endswith(".zip"):
        dst = make_tmp("clipzip_", getattr(args, "workdir", None))
        print(f"② 解压素材包 {os.path.basename(path)} …")
        with zipfile.ZipFile(path) as z:
            z.extractall(dst)
        # 有些 zip 里套一层同名文件夹，无所谓，递归找视频即可
        return dst
    return path


def collect_videos(folder):
    found = []
    for root, dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                found.append(os.path.join(root, f))
    return sorted(found, key=natural_key)


def probe(video):
    """返回 dict: w,h,fps,dur,has_audio,codec"""
    info = {"w": 0, "h": 0, "fps": 0.0, "dur": 0.0, "has_audio": False, "codec": "?"}
    if FFPROBE:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,codec_name",
             "-show_entries", "format=duration",
             "-of", "json", video], capture_output=True, text=True)
        try:
            import json
            j = json.loads(r.stdout)
            st = j["streams"][0]
            info["w"], info["h"] = st.get("width", 0), st.get("height", 0)
            num, den = st.get("r_frame_rate", "0/1").split("/")
            info["fps"] = float(num) / float(den) if float(den) else 0.0
            info["codec"] = st.get("codec_name", "?")
            info["dur"] = float(j["format"]["duration"])
        except Exception:
            pass
    ra = subprocess.run(
        [FFMPEG, "-i", video], capture_output=True, text=True)
    info["has_audio"] = "Audio:" in ra.stderr
    return info


def measure_mean_volume(video):
    """测平均音量(dB)，用于把各段拉到一致；测不到返回 None"""
    r = subprocess.run([FFMPEG, "-i", video, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume: (-\d+\.?\d*) dB", r.stderr)
    return float(m.group(1)) if m else None


def normalize_segment(src, dst, W, H, fps, fit, target_lufs=-16.0,
                      denoise=True, strong=False, mute=False):
    """把一段素材统一成：WxH / fps / yuv420p / 48k立体声 / 音量拉齐
    fit: blur=竖屏内容放横屏时用自身放大模糊背景(不裁不变形)
         pad =黑边  crop=裁切填满(会丢画面)
    denoise=True 时启用降噪链（高通去风噪低频 + 频谱降噪 + 低通去嘶声）
    strong=True 时用重档降噪（压得掉中频噪声云，但人声也会薄一点）
    mute=True 时【丢掉原声只留画面】：音频换成数字静音（现场环境音全不要），
             整片只留 BGM——这是「纯画面段」的标准做法。"""
    info = probe(src)

    # ── 音频链：降噪 → 音量拉齐 → 统一 48k 立体声 ──
    if mute:
        # 【只留画面】整段原声直接静音（不是降噪，是彻底不要现场环境音）
        af = "volume=0,aresample=48000,aformat=channel_layouts=stereo"
    elif info["has_audio"]:
        af = "aresample=48000"
        if denoise:
            if strong:
                # 重档：afftdn 开 nr（降噪量）才压得住中频噪声云
                # 顺序：highpass 砍低频隆隆 → afftdn 强降噪 → lowpass 收嘶声
                #      → agate 噪声门 → afftdn 第二道收尾
                af = ("highpass=f=180,afftdn=nf=-38:tn=1:nr=30,"
                      "lowpass=f=11000,agate=threshold=0.01:ratio=5:attack=25:release=200,"
                      "afftdn=nf=-30:tn=1:nr=18," + af)
            else:
                # 默认轻档：温和，不把人声削薄
                af = ("highpass=f=120,afftdn=nf=-28:tn=1,lowpass=f=14000,"
                      "agate=threshold=0.003:ratio=2:attack=50:release=400," + af)
        mv = measure_mean_volume(src)
        if mv is not None and mv < 0:
            gain = max(-12.0, min(12.0, target_lufs - mv))
            if abs(gain) >= 0.5:
                af = f"volume={gain:.1f}dB," + af
                info["gain_db"] = gain
        af += ",aformat=channel_layouts=stereo"

    # ── 视频链 ──
    if fit == "blur":
        # 背景层：放大铺满+高斯模糊；前景层：等比缩小完整保留；叠加居中
        vfc = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{H},gblur=sigma=24[bg];"
            f"[fgsrc]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[vout]"
        )
        use_complex = True
    else:
        if fit == "crop":
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                  f"crop={W}:{H}")
        else:  # pad
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
        vf += f",setsar=1,fps={fps},format=yuv420p"
        use_complex = False

    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", src]
    if not info["has_audio"]:
        # 无声素材（纯B-roll）补一条静音轨，保证拼接不炸
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]

    if use_complex:
        fc = vfc
        if info["has_audio"]:
            fc += f";[0:a]{af}[aout]"
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]"]
        else:
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "1:a"]
    else:
        if info["has_audio"]:
            cmd += ["-af", af]
        cmd += ["-vf", vf]

    cmd += safe_codec_args("23") + [dst]
    run(cmd)
    return info


def concat_segments(segs, out, transition, tmp):
    """拼接。transition=fade 时段间 0.5s 叠化；none 直接 concat(快)"""
    if transition != "fade" or len(segs) == 1:
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p in segs:
                f.write(f"file '{p.replace(chr(92), '/')}'\n")
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-c", "copy", out])
        return
    # xfade 链式叠化：每接一段，偏移 = 已积累时长 - 叠化时长
    t = "0.5"
    inputs = []
    for p in segs:
        inputs += ["-i", p]
    durs = [get_duration(p) for p in segs]
    parts = []
    acc = durs[0]
    for i in range(1, len(segs)):
        prev_v = "0:v" if i == 1 else f"v{i-1}"
        prev_a = "0:a" if i == 1 else f"a{i-1}"
        off = acc - 0.5
        parts.append(f"[{prev_v}][{i}:v]xfade=transition=fade:duration={t}:offset={off:.3f}[v{i}]")
        parts.append(f"[{prev_a}][{i}:a]acrossfade=d={t}[a{i}]")
        acc = acc + durs[i] - 0.5
    parts.append(f"[v{len(segs)-1}]format=yuv420p[vout]")
    parts.append(f"[a{len(segs)-1}]aformat=channel_layouts=stereo[aout]")
    fc = ";".join(parts)
    run([FFMPEG, "-y", "-loglevel", "error"] + inputs +
        ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]"]
        + safe_codec_args("23") + [out])


def mix_bgm(video, bgm, out, bgm_db=-11.0, duck=True):
    """加背景音乐：原声保留，BGM 压低并淡入淡出
    duck=True 时用 sidechaincompress 做「人声闪避」——
    有人说话时 BGM 自动再降约 8dB，说话间隙回升，听感更干净。
    """
    dur = get_duration(video)
    # BGM 太短就循环；淡入淡出仍按整片时长算
    bg_pre = (f"[1:a]aloop=loop=-1:size=2e9,atrim=0:{dur:.2f},"
              if get_duration(bgm) < dur else f"[1:a]atrim=0:{dur:.2f},")
    # 淡入淡出时长不能超过片长，否则两段会叠在一起、甚至把整片都在淡出里
    # （片长 < 淡出时长时 st 被钳到 0，等于从头就在淡出，BGM 听不见）。
    fade_in = min(2.5, dur / 2)
    fade_out = min(3.0, dur / 2)
    bg_chain = (f"{bg_pre}asetpts=N/SR/TB,highpass=f=120,volume={bgm_db}dB,"
                f"afade=t=in:st=0:d={fade_in:.2f},"
                f"afade=t=out:st={max(0,dur-fade_out):.2f}:d={fade_out:.2f},"
                f"aresample=48000,aformat=channel_layouts=stereo[bgraw]")
    if duck:
        # 【关键】voice 必须先 asplit 拆成两路：
        #   [sc] 只喂给 sidechaincompress 当触发源（人声一出现就压 BGM）
        #   [vm] 送进 amix 参与最终混音
        # ⚠️ 同一个输出标签被引用两次时，ffmpeg 会隐式插入 split，
        #    sidechaincompress 的行为会变得不可预测——实测 BGM 后半段电平
        #    从 -38dB 被顶到 -20dB，整片失去段落层次。显式 asplit 才稳。
        #
        # 【闪避强度】ratio=6/release=700 实测平顺；
        #   ratio 太大或 release 太短 → 人声一停 BGM 猛升，来回跳成"咚-咚"泵动，听着像杂音。
        #   threshold 要够低才能让"轻声说话"也触发，但别低到环境音一直触发。
        #
        # 【混音链顺序】amix → alimiter → 结束。
        #   绝不能加 loudnorm：它的动态模式会把"人声段"和"纯画面段"的响度差压平，
        #   听完就分不出人在说话还是只有音乐（实测三段 mean 全被拉成一样）。
        #   段级 normalize_segment 已经把每段拉到 -16 LUFS 了，不需要再全局压一次。
        #   alimiter 必须用 level=disabled 关掉自动电平补偿，否则限掉的峰值又被顶回去。
        fc = (bg_chain +
              ";[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[sc][vm];"
              "[bgraw][sc]sidechaincompress=threshold=0.015:ratio=8:"
              "attack=8:release=600:makeup=1[bg];"
              "[vm][bg]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[amixed];"
              "[amixed]alimiter=limit=0.708:level=disabled:attack=5:release=50,"
              "aresample=48000[aout]")
    else:
        fc = (bg_chain +
              ";[0:a]aresample=48000,aformat=channel_layouts=stereo[vm];"
              "[vm][bgraw]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[amixed];"
              "[amixed]alimiter=limit=0.708:level=disabled:attack=5:release=50,"
              "aresample=48000[aout]")
    run([FFMPEG, "-y", "-loglevel", "error", "-i", video, "-i", bgm,
         "-filter_complex", fc,
         "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
         "-b:a", "160k", out])


def mode_stitch(args):
    """模式二：零散素材（文件夹/zip）→ 合辑成片"""
    src_path = args.video
    if not os.path.exists(src_path):
        sys.exit(f"【失败】 找不到：{src_path}")

    folder = unzip_if_needed(src_path)
    clips = collect_videos(folder)
    if not clips:
        sys.exit("【失败】 没在素材里找到视频文件（支持 mp4/mov/mkv/avi/m4v/ts/flv/wmv/webm）。")
    print(f"② 读到 {len(clips)} 段素材（按文件名顺序）：")
    for c in clips:
        print("   -", os.path.basename(c))

    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    (W, H), why = pick_target_size(clips, args.vertical)
    print(f"   目标画面：{W}x{H}（{why}）")
    fps = 30

    tmp = make_tmp("stitch_", getattr(args, "workdir", None))
    normed, report, failed = [], [], []
    for i, c in enumerate(clips):
        # 段级处理：字幕决策基于原始素材（防歌声乱入/防双字幕），再统一画面
        # 【单段失败不许炸掉整批】素材可能下载不完整/已损坏，
        #   ffmpeg 会报 Invalid NAL unit size / moov atom not found 之类，
        #   以前这里直接抛 RuntimeError，前面剪好的段全白干、学员一脸懵。
        #   现在：记下来、跳过、继续处理其余素材，最后统一汇总。
        try:
            dst, sub_state = process_segment(c, i, tmp, args, W, H)
            info = probe(c)
            normed.append(dst)
            note = (f"{os.path.basename(c)}｜原 {info['w']}x{info['h']}@{info['fps']:.0f}fps "
                    f"{info['codec']}｜{'有声' if info['has_audio'] else '无声(补静音轨)'}"
                    + f"｜{sub_state}")
            report.append(note)
            print(f"   处理 {i+1}/{len(clips)}：{note}")
        except Exception as ex:
            failed.append((os.path.basename(c), _brief(ex)))
            print(f"   【注意】 处理 {i+1}/{len(clips)} 失败：{os.path.basename(c)}")
            print(f"      原因：{_brief(ex)}")
            print("      该文件可能下载不完整或已损坏，已跳过，继续处理其余素材。")

    if failed:
        print(f"【注意】 共 {len(failed)} 段处理失败（已跳过，不影响其余片段）：")
        for nm, err in failed:
            print(f"   - {nm}：{err}")

    joined = os.path.join(args.outdir, base + "_合辑.mp4")
    print(f"③ 拼接（{'段间叠化0.5s' if args.transition == 'fade' else '直连'}）…")
    concat_segments(normed, joined, args.transition, tmp)

    if args.bgm:
        print("④ 混入背景音乐（压低并淡入淡出）…")
        mixed = os.path.join(args.outdir, base + "_合辑_带BGM.mp4")
        mix_bgm(joined, args.bgm, mixed, bgm_db=args.bgm_db,
                duck=not getattr(args, "no_duck", False))
        joined = mixed

    # 剪辑报告（教学用：让学员看见 AI 到底做了什么）
    rp = os.path.join(args.outdir, base + "_剪辑报告.md")
    total = get_duration(joined)
    with open(rp, "w", encoding="utf-8") as f:
        f.write(f"# 合辑剪辑报告 · {base}\n\n")
        f.write(f"- 素材段数：{len(clips)}（顺序按文件名）\n")
        f.write(f"- 统一规格：{W}x{H} @ {fps}fps，yuv420p，48kHz 立体声，平均音量拉到 ≈-16dB\n")
        f.write(f"- 拼接方式：{'段间 0.5s 叠化' if args.transition == 'fade' else '直连'}\n")
        f.write(f"- 成片时长：{total:.1f} 秒\n\n")
        f.write("## 每段素材的处理\n\n")
        for i, n in enumerate(report, 1):
            f.write(f"{i}. {n}\n")
        if failed:
            f.write(f"\n## 处理失败的素材（{len(failed)} 段，已跳过）\n\n")
            for nm, err in failed:
                f.write(f"- {nm}：{err}\n")
            f.write("\n> 常见原因：文件下载不完整 / 素材本身已损坏。"
                    "重新下载或用别的文件替换这一段即可。\n")
        f.write("\n## 仍需你把关的事\n\n")
        f.write("- 顺序：文件名 01_ 02_ 03_ 决定顺序，重命名就能换序\n")
        f.write("- 竖屏素材：默认模糊背景不裁画面；要黑边 --fit pad，要裁满 --fit crop\n")
        f.write("- 字幕：逐段智能决定（有人声+原片无字幕才烧）；想自己挑段用 --draft/--picks 选段模式\n")
    print(f"   剪辑报告：{rp}")
    print("\n【OK】 完成。输出在：", args.outdir)


# ════════════════════════════════════════════════════════════
#  模式三：选段拼片（先转写出文字稿 → 人/AI 挑时间段 → 按挑中的拼）
#  "在一堆素材里挑有用的"就靠这个模式：AI 不猜，人当导演
# ════════════════════════════════════════════════════════════

def parse_time(s):
    """'00:03' '1:02:03' '12.5' → 秒"""
    s = s.strip()
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return parts[0] * 60 + parts[1]
    return float(s)


_TIME_RE = re.compile(
    r"((?:\d{1,3}:)?\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*[-~]\s*"
    r"((?:\d{1,3}:)?\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?)")


def parse_picks(path):
    """读选段单。每行：文件名  起始-结束  备注（# 开头为注释）。
    时间段用正则从行中识别，所以文件名带空格也行。
    备注里写 [字幕] 表示「这一段要转写并烧字幕」——采访/发言段用，
    其余段默认不转写（快，且不会给空镜硬配字）。"""
    picks = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _TIME_RE.search(line)
            if not m:
                print(f"   【注意】 第{ln}行没找到时间段（格式如 00:03-00:12），已跳过：{line}")
                continue
            fname = line[:m.start()].strip()
            note = line[m.end():].strip()
            if not fname:
                print(f"   【注意】 第{ln}行没找到文件名，已跳过：{line}")
                continue
            # 段级标记：[字幕]（这段要烧中文字幕）/ [静音]（这段丢掉原声只留画面）
            # 两者互斥：静音段没有声音可转写，[字幕] 自动让位给 [静音]
            want_mute = False
            for tag in ("[静音]", "【静音】", "[无声]", "[只留画面]", "[无原声]"):
                if tag in note:
                    want_mute = True
                    note = note.replace(tag, "").strip()
                    break
            want_sub = False
            for tag in ("[字幕]", "【字幕】", "[字幕+]", "[有字幕]"):
                if tag in note:
                    want_sub = not want_mute
                    note = note.replace(tag, "").strip()
                    break
            picks.append({"file": fname, "start": parse_time(m.group(1)),
                          "end": parse_time(m.group(2)), "note": note,
                          "want_sub": want_sub, "want_mute": want_mute,
                          "line": ln})
    return picks


def mode_draft(args):
    """第一步：把素材文件夹/zip 里的每段素材转写成带时间戳的文字稿，
    产出 文字稿.md（人/AI 看它挑段）+ picks模板.txt（照着填）。"""
    src_path = args.video
    folder = unzip_if_needed(src_path)
    clips = collect_videos(folder)
    if not clips:
        sys.exit("【失败】 没在素材里找到视频文件。")
    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]

    md = os.path.join(args.outdir, base + "_文字稿.md")
    tpl = os.path.join(args.outdir, base + "_picks模板.txt")
    print(f"② 转写 {len(clips)} 段素材出文字稿（供人/WorkBuddy/豆包挑选）…")
    model = load_model(args.model)
    with open(md, "w", encoding="utf-8") as fm, open(tpl, "w", encoding="utf-8") as ft:
        fm.write(f"# {base} · 素材文字稿\n\n")
        fm.write("> 每段素材的转写（带时间戳）。人看这份稿挑段；"
                 "也可以整份发给 WorkBuddy/豆包，让它按你的主题挑。\n\n")
        ft.write("# 选段单：每行「文件名  起始-结束  备注」，顺序=成片顺序，# 开头为注释\n")
        ft.write("# 例：01_活动全景.mp4   00:03-00:12   开场全员大场景\n\n")
        for c in clips:
            name = os.path.basename(c)
            dur = get_duration(c)
            fm.write(f"\n## {name}（时长 {dur:.0f} 秒）\n\n")
            ft.write(f"# {name}（时长 {dur:.0f} 秒）\n")
            try:
                segments, _ = model.transcribe(
                    c, language="zh", beam_size=5, vad_filter=True,
                    initial_prompt="以下是普通话的内容片段，请使用简体中文输出。")
                n = 0
                for seg in segments:
                    text = seg.text.strip()
                    if not text:
                        continue
                    if is_likely_song(seg, text):
                        continue
                    n += 1
                    fm.write(f"- `{fmt(max(0, seg.start))}` {text}\n")
                if n == 0:
                    fm.write("- （没人声：纯音乐/空镜，按画面挑）\n")
            except Exception as e:
                fm.write(f"- （转写失败：{e}）\n")
            print(f"   {name} → 已写入文字稿")
    print(f"\n【OK】 文字稿：{md}")
    print(f"【OK】 选段单模板：{tpl}")
    print("下一步：打开文字稿挑段填进选段单（或发给 WorkBuddy/豆包 让它挑），")
    print("       然后加 --picks 选段单.txt 重跑，就按你挑的拼成片。")


def mode_select(args):
    """第三步：按选段单剪时段 → 统一化 → 拼接 → 可选字幕。"""
    src_path = args.video
    folder = unzip_if_needed(src_path)
    clips = {os.path.basename(c): c for c in collect_videos(folder)}
    picks = parse_picks(args.picks)
    if not picks:
        sys.exit("【失败】 选段单是空的或格式不对。参考 picks模板.txt。")
    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    (W, H), why = pick_target_size(list(clips.values()), args.vertical)
    print(f"   目标画面：{W}x{H}（{why}）")

    print(f"② 按选段单剪 {len(picks)} 段：")
    tmp = make_tmp("select_", getattr(args, "workdir", None))
    normed, report, failed = [], [], []
    # 【注意】 先全量校验文件名，避免「某段被静默跳过」导致成片缺段且事后才发现。
    #    （实战踩坑：选段单里把一段素材的时间戳抄错了（真实名 ...181626_0867_D.MP4 抄成 ...172039_0867_D.MP4），
    #      脚本只跳过那一段，成片少了 20 秒，人在成片里根本看不出来是哪段丢了。）
    missing = []
    for p in picks:
        if p["file"] in clips:
            continue
        if not any(p["file"] in k for k in clips):
            missing.append(p)
    if missing:
        print(f"   【失败】 选段单里有 {len(missing)} 段在素材里找不到对应文件（会造成成片缺段）：")
        for p in missing:
            near = [k for k in clips if k.split("_")[-2:] == p["file"].split("_")[-2:]]
            hint = f"　→ 素材里相近的是：{near[0]}" if near else ""
            print(f"      第{p['line']}行：{p['file']}{hint}")
        print("   已中止。请核对文件名后重跑（文件名必须和素材完全一致）。")
        sys.exit("【失败】 选段单文件名与素材不匹配，已中止，未产出成片。")
    for i, p in enumerate(picks):
        src = clips.get(p["file"])
        if not src:
            # 允许只写文件名的一部分
            cand = [v for k, v in clips.items() if p["file"] in k]
            if not cand:
                print(f"   【注意】 第{p['line']}行：素材里没有「{p['file']}」，已跳过")
                continue
            src = cand[0]
        dur = get_duration(src)
        s, e = max(0, p["start"]), min(dur, p["end"])
        if e - s < 0.3:
            print(f"   【注意】 第{p['line']}行：时段太短或超出时长，已跳过")
            continue
        raw = os.path.join(tmp, f"r{i:02d}.mp4")
        # 【单段失败不许炸掉整批】切片失败通常是这段素材已损坏/下载不完整。
        try:
            run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}",
                 "-i", src] + safe_codec_args("20") + [raw])
        except Exception as ex:
            failed.append((p["file"], _brief(ex)))
            print(f"   【注意】 第{p['line']}行「{p['file']}」切片失败：{_brief(ex)}")
            print("      该段素材可能已损坏或下载不完整，已跳过，继续处理其余段。")
            continue
        # 段级处理：字幕决策基于原始切片（防歌声乱入/防双字幕），再统一画面
        # 字幕策略由 --pick-sub 决定：
        #   marked（默认）只有选段单标了 [字幕] 的段才转写烧字
        #   all   每段都按智能规则烧
        #   none  全不烧
        # 段级「只留画面」标记：[静音] → 丢掉原声，不转写不烧字
        want_mute = bool(p.get("want_mute"))
        if args.pick_sub == "marked":
            want_sub = bool(p.get("want_sub"))
        elif args.pick_sub == "all":
            want_sub = None
        else:
            want_sub = False
        if want_mute:
            want_sub = False
        try:
            dst, sub_state = process_segment(raw, i, tmp, args, W, H,
                                            want_sub=want_sub, want_mute=want_mute)
        except Exception as ex:
            failed.append((p["file"], _brief(ex)))
            print(f"   【注意】 第{p['line']}行「{p['file']}」处理失败：{_brief(ex)}")
            print("      该段素材可能已损坏或格式不支持，已跳过，继续处理其余段。")
            continue
        normed.append(dst)
        note = f"{p['file']} {fmt(s)}-{fmt(e)}（{e - s:.1f}s）"
        if p["note"]:
            note += f"  {p['note']}"
        note += f"｜{sub_state}"
        report.append(note)
        print(f"   {i+1}. {note}")

    if not normed:
        sys.exit("【失败】 没有任何有效片段，检查选段单。")

    joined = os.path.join(args.outdir, base + "_选段合辑.mp4")
    print(f"③ 拼接（{'段间叠化0.5s' if args.transition == 'fade' else '直连'}）…")
    concat_segments(normed, joined, args.transition, tmp)

    if args.bgm:
        print("④ 混入背景音乐…")
        mixed = os.path.join(args.outdir, base + "_选段合辑_带BGM.mp4")
        mix_bgm(joined, args.bgm, mixed, bgm_db=args.bgm_db,
                duck=not getattr(args, "no_duck", False))
        joined = mixed

    rp = os.path.join(args.outdir, base + "_选段报告.md")
    total = get_duration(joined)
    with open(rp, "w", encoding="utf-8") as f:
        f.write(f"# 选段合辑报告 · {base}\n\n")
        f.write(f"- 成片时长：{total:.1f} 秒；片段数：{len(normed)}\n")
        f.write(f"- 统一规格：{W}x{H} @ 30fps；拼接："
                f"{'0.5s叠化' if args.transition == 'fade' else '直连'}\n\n")
        f.write("## 成片结构（顺序来自你的选段单）\n\n")
        for i, n in enumerate(report, 1):
            f.write(f"{i}. {n}\n")
        if failed:
            f.write(f"\n## 处理失败的片段（{len(failed)} 段，已跳过）\n\n")
            for nm, err in failed:
                f.write(f"- {nm}：{err}\n")
            f.write("\n> 常见原因：文件下载不完整 / 素材本身已损坏。"
                    "重新下载或从选段单里去掉这一段即可。\n")
        f.write("\n## 字幕策略\n\n")
        if args.pick_sub == "marked":
            f.write("- 只给选段单里标了 [字幕] 的段烧字幕（采访/发言段）\n")
            f.write("- 其余段完全不转写 → 快，也不会给空镜硬配字\n")
        elif args.pick_sub == "all":
            f.write("- 每段都按智能规则烧：有人声、原片无字幕 → 烧\n")
        else:
            f.write("- 全片不烧字幕\n")
        f.write("- 纯背景歌声 → vad 滤掉，不烧（AI 不瞎编歌词）\n")
        f.write("- 原片自带字幕 → OCR 检出，不烧（防双层）\n")
        _muted = sum(1 for p in picks if p.get("want_mute"))
        if _muted:
            f.write(f"- 标了 [静音] 的 { _muted } 段：原声已丢弃，只留画面（整片只剩 BGM + 人声）\n")
        f.write("\n## 音频处理\n\n")
        if getattr(args, "no_denoise", False):
            f.write("- 降噪链：已关闭（--no-denoise）\n")
        elif getattr(args, "denoise_strong", False):
            f.write("- 降噪链（重档）：高通180Hz → 强谱降噪 → 低通11kHz → 噪声门 → 收尾降噪\n")
        else:
            f.write("- 降噪链（默认轻档）：高通120Hz（去空调/风噪低频）→ afftdn 谱降噪 → 低通14kHz → 噪声门\n")
        f.write(f"- BGM：{os.path.basename(args.bgm) if args.bgm else '未加'}，"
                f"基准 {args.bgm_db}dB"
                f"{'，带人声闪避（说话时 BGM 自动让位）' if not getattr(args, 'no_duck', False) else ''}\n")
        f.write("\n## 挑段的人是你/AI，不是机器\n\n")
        f.write("这份片子的叙事来自选段单——AI 只负责剪、拼、拉齐、降噪、烧字。\n")
    print(f"   选段报告：{rp}")
    print("\n【OK】 完成。输出在：", args.outdir)


# ════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="视频文件(长剪短) 或 素材文件夹/zip(散拼长)")
    ap.add_argument("--model", default="small")
    ap.add_argument("--burn-sub", action="store_true",
                    help="把字幕烧进画面（【注意】 仅当原片【没有】字幕时才用！）")
    ap.add_argument("--noise", default="-35dB", help="[长剪短]静音阈值，越负越灵敏")
    ap.add_argument("--dur", default="0.4", help="[长剪短]静音最短时长(秒)")
    # 散拼长专用
    ap.add_argument("--fit", default="blur", choices=["blur", "pad", "crop"],
                    help="[散拼长]横竖不一致时的处理：blur=模糊背景(默认) pad=黑边 crop=裁满")
    ap.add_argument("--vertical", action="store_true",
                    help="[散拼长]目标是竖屏 1080x1920（默认横屏 1920x1080）")
    ap.add_argument("--transition", default="none", choices=["none", "fade"],
                    help="[散拼长]段间转场：none=直连(默认) fade=0.5s叠化")
    ap.add_argument("--bgm", default=None, help="[散拼长/选段]背景音乐文件(mp3/wav/m4a)。留空则自动用技能自带的无版权轻量BGM")
    ap.add_argument("--no-bgm", action="store_true", help="不加背景音乐（默认会用技能自带的无版权轻量BGM）")
    ap.add_argument("--draft", action="store_true",
                    help="[选段第1步]只转写全部素材出文字稿+选段单模板，不出片")
    ap.add_argument("--picks", default=None,
                    help="[选段第3步]选段单txt（每行：文件名 起始-结束 备注），按它拼片")
    ap.add_argument("--force-sub", action="store_true",
                    help="OCR 检出画面文字时仍强烧字幕（确认那些字是背景屏/标语、不是字幕时才用）")
    ap.add_argument("--no-sub", action="store_true",
                    help="完全不转写、不烧字幕（只要拼片/BGM 时用：快很多，也不用装 whisper）")
    ap.add_argument("--pick-sub", choices=["marked", "all", "none"], default="marked",
                    help="[选段模式]字幕策略：marked=只给选段单里标了[字幕]的段烧（默认）"
                         " all=每段都按智能规则烧 none=全不烧")
    ap.add_argument("--no-denoise", action="store_true",
                    help="关掉降噪链（默认开启：高通+谱降噪+低通，去空调声/人群嘈杂）")
    ap.add_argument("--denoise-strong", action="store_true",
                    help="用重档降噪：默认是轻档（温和不削人声）；"
                         "重档压得更掉中频环境噪声，但人声会略薄，环境很吵时才加")
    ap.add_argument("--no-duck", action="store_true",
                    help="BGM 不做人声闪避（默认开启：有人说话时 BGM 自动让位）")
    ap.add_argument("--sub-file", default=None,
                    help="人工校对字幕目录：放 seg00.srt/seg01.srt…（按段序号），"
                         "有就用它替代机器转写（采访类必用：机器会写错别字）")
    ap.add_argument("--bgm-db", type=float, default=-11.0,
                    help="BGM 音量(dB)，默认 -19（比人声低 3dB 左右，人声段听得清）")
    ap.add_argument("--workdir", default=None,
                    help="中间文件（切片/统一化片段）放哪。默认系统临时目录；"
                         "C 盘空间不够时指到 D 盘，例如 --workdir D:/tmp/clip")
    ap.add_argument("--outdir", default=None,
                    help="成品输出目录（默认：跟原视频放同一个文件夹，方便找）")
    args = ap.parse_args()

    # 【要点】 输出目录默认落在原视频旁边。
    # 老版本默认写到「技能目录/scripts/out」，学员剪完在技能目录里翻不到成品，还会把技能目录搞脏。
    if not args.outdir:
        _v = os.path.abspath(args.video)
        if os.path.isdir(_v):
            args.outdir = _v
        else:
            args.outdir = os.path.dirname(_v) or os.getcwd()
    args.outdir = os.path.abspath(args.outdir)

    # 【要点】 默认自带 BGM：散拼长/选段模式若没显式给 --bgm，自动用技能自带的无版权轻量音乐
    # 优先用 bgm_calm.mp3（Mixkit《Classical vibes 3》钢琴+弦乐，Mixkit Free License，商用免署名），
    # 没有再退回 bgm.mp3（Mixkit《Relaxation 05》笛+竖琴）
    if not args.bgm and not args.no_bgm:
        if os.path.isdir(args.video) or str(args.video).lower().endswith(".zip"):
            _adir = os.path.abspath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "assets"))
            _self_bgm = ""
            for _cand in ("bgm_calm.mp3", "bgm.mp3"):
                _p = os.path.join(_adir, _cand)
                if os.path.exists(_p):
                    _self_bgm = _p
                    break
            if _self_bgm:
                args.bgm = _self_bgm
                print(f" 未指定 BGM，自动使用技能自带：{os.path.basename(_self_bgm)}")

    # 选段模式的字幕策略（详见 mode_select 里的分支）
    #   marked: 只有选段单标了 [字幕] 的段才转写烧字（默认，快且干净）
    #   all:    每段都按智能规则烧
    #   none:   全不烧
    if args.pick_sub == "none":
        args.no_sub = True

    if not os.path.exists(args.video):
        sys.exit(f"【失败】 找不到：{args.video}")

    if os.path.isdir(args.video) or args.video.lower().endswith(".zip"):
        if args.draft:
            mode_draft(args)
        elif args.picks:
            mode_select(args)
        else:
            mode_stitch(args)
    else:
        mode_condense(args)


if __name__ == "__main__":
    main()
