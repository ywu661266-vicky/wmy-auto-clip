# -*- coding: utf-8 -*-
"""
effects.py —— 代码卡点动效生成器（配置驱动，学员可自己改）

把 s7/s9 那套「用代码逐帧画特效层、再叠到视频上」的能力，做成人人能改的版本。
生成的特效层包含 7 类元素，全部按你给的时间码自动卡点：

  1) 开场标题卡      —— 视频开头弹入的标题（副标题 + 强调色下划线）
  2) 章节卡 ×N       —— 话题一转就弹出来的章节提示（带序号）
  3) 关键词胶囊 ×N   —— 说到关键词时左右轮换弹出的小胶囊
  4) 进度条+章节名   —— 顶部全程可见的进度条，并显示当前章节
  5) 结尾 CTA 卡     —— 半透明行动号召卡（人还在说话，脸能透出来）
  6) 数字卡 ×N       —— 大数字滚动到位（支持 184万 / 百分比 / 千分位）
  7) 步骤卡 ×N       —— 横向时间线，节点随讲稿逐个弹出

再叠加：呼吸感运镜（极缓推拉）+ 可选硬字幕 + 可选 BGM 自动闪避。
全程本地 ffmpeg + PIL，零积分、离线。

动效手感来自开源项目 adunext/adu-motion-video（MIT）的 fx_engine.py：
缓动曲线族（expo/out5/spring/back）+ 入场预设（滑入/弹跳/冲击）+ 出场回缩。
想退回朴素的匀速淡入淡出，在配置里写 "fx": {"motion": 0}。

──────────────────────────────
学员怎么用（复制给 WorkBuddy 即可）：
  1) 把 effects.json 里的标题 / 章节 / 关键词 / CTA / 数字卡 / 步骤卡改成你自己的
     （时间码＝视频里的秒数）
  2) 发给 WorkBuddy：
     「运行 effects.py，视频用【你的片.mp4】，配置用【effects.json】，输出【成片.mp4】」
  3) 想要背景音乐就说：上面那句后面加「再加背景音乐【bgm.wav】，说话时自动压低」

配置字段说明见 references/动效库配置说明.md
"""
import os
import re
import sys
import json
import subprocess
import argparse

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
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.stderr.write(
        "\n【缺少 Pillow】动效卡点功能要用 Pillow 画卡片。\n"
        "   安装一条命令搞定：  \"<你的python>\" -m pip install pillow\n"
        "   装完再跑本脚本即可。\n")
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import fx_engine as FX
except ImportError:
    sys.stderr.write(
        "\n【缺少 fx_engine.py】effects.py 依赖同目录的 fx_engine.py（动效引擎）。\n"
        "   说明技能包没装完整，请重新安装：\n"
        "     python install.py --zip <技能包路径.zip>\n")
    sys.exit(2)


# ═══════════════ ffmpeg 定位（系统PATH → WorkBuddy自带 → 脚本同目录 → imageio-ffmpeg） ═══════════════
def find_ffmpeg():
    from shutil import which
    p = which("ffmpeg")
    if p:
        return p
    for cand in [
        os.path.expanduser("~/.workbuddy/binaries/ffmpeg/ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
    ]:
        if os.path.exists(cand):
            return cand
    try:  # 学员只 pip install imageio-ffmpeg 也能跑
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    raise RuntimeError("找不到 ffmpeg：执行  pip install imageio-ffmpeg  即可，或把 ffmpeg 加进 PATH")


def find_ffprobe():
    """没有 ffprobe 不影响出片——下面会自动退回用 `ffmpeg -i` 解析元数据。"""
    from shutil import which
    p = which("ffprobe")
    if p:
        return p
    for cand in [
        os.path.expanduser("~/.workbuddy/binaries/ffmpeg/ffprobe.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffprobe.exe"),
    ]:
        if os.path.exists(cand):
            return cand
    return None


def _ffmpeg_meta(path, ff):
    """无 ffprobe 时，从 `ffmpeg -i` 的 stderr 里抠出 宽高 和 时长"""
    r = subprocess.run([ff, "-i", path], capture_output=True, text=True, errors="ignore")
    txt = r.stderr or ""
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    w, h = 1080, 1920
    mv = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", txt)
    if mv:
        w, h = int(mv.group(1)), int(mv.group(2))
    return w, h, dur


def get_duration(path, fp=None, ff=None):
    if fp:
        try:
            return float(subprocess.check_output(
                [fp, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path]).decode().strip())
        except Exception:
            pass
    return _ffmpeg_meta(path, ff or find_ffmpeg())[2]


def probe_size(path, fp=None, ff=None):
    if fp:
        try:
            out = subprocess.check_output(
                [fp, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", path]).decode().strip()
            parts = out.replace("\n", ",").split(",")
            return int(parts[0]), int(parts[1])
        except Exception:
            pass
    w, h, _ = _ffmpeg_meta(path, ff or find_ffmpeg())
    return w, h


# ═══════════════ 字体（Windows 微软雅黑/黑体 → 其他系统回退；找不到会明确警告） ═══════════════
_FONT_CACHE = {}
_FONT_WARNED = [False]
_FONT_INST = {}


def find_font(bold=False):
    if bold in _FONT_CACHE:
        return _FONT_CACHE[bold]
    here = os.path.dirname(os.path.abspath(__file__))
    self_font = os.path.join(here, "..", "assets", "font.ttf")   # 允许自带字体覆盖
    if bold:
        win = [r"C:/Windows/Fonts/msyhbd.ttc", r"C:/Windows/Fonts/msyhbd.ttf"]
    else:
        win = [r"C:/Windows/Fonts/msyh.ttc", r"C:/Windows/Fonts/msyh.ttf"]
    cands = [self_font] + win + [
        r"C:/Windows/Fonts/simhei.ttf",        # 黑体（精简版 Windows 常见）
        r"C:/Windows/Fonts/Deng.ttf",          # 等线
        r"C:/Windows/Fonts/simsun.ttc",        # 宋体
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in cands:
        if c and os.path.exists(c):
            # 这个字体真的有中文字形吗？DejaVu 这类西文字体会把汉字渲染成豆腐块，
            # 满屏方块却没人知道原因 —— 这种情况直接换下一个候选。
            if not font_has_cjk(c):
                continue
            _FONT_CACHE[bold] = c
            return c
    _FONT_CACHE[bold] = None
    return None


# ═══════════════ 画布与基础绘制 ═══════════════
def new_canvas(w, h):
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def font_has_cjk(path):
    """这个字体真的有中文字形吗？DejaVu 之类西文字体会把汉字渲染成豆腐块，
    学员看到满屏方块却不知道原因。这里用「汉字宽度 == 缺字符宽度」来判定。"""
    try:
        f = ImageFont.truetype(path, 20)
        w_cjk = f.getlength("测")
        w_missing = f.getlength("\uffff")
        return not (w_cjk > 0 and abs(w_cjk - w_missing) < 0.5)
    except Exception:
        return True


def load_font(sz, bold=False):
    key = (int(sz), bool(bold))
    if key in _FONT_INST:
        return _FONT_INST[key]
    path = find_font(bold)
    fnt = None
    if path:
        try:
            fnt = ImageFont.truetype(path, int(sz))
        except Exception:
            fnt = None
    if fnt is None:
        if not _FONT_WARNED[0]:
            _FONT_WARNED[0] = True
            print("【注意】 没找到「带中文字形」的字体（微软雅黑/黑体最好），"
                  "标题文字会变成方块。把任意中文 .ttf 放到 assets/font.ttf 即可解决。")
        fnt = ImageFont.load_default()
    _FONT_INST[key] = fnt
    return fnt


def tlen(text, fnt):
    return int(ImageDraw.Draw(new_canvas(10, 10)).textlength(text, font=fnt))


def rrect(d, box, fill, radius=28, outline=None, width=0):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def ctext(d, xy, text, fnt, fill, anchor="mm", stroke=0, scolor=None):
    d.text(xy, text, font=fnt, fill=fill, anchor=anchor,
           stroke_width=stroke, stroke_fill=scolor or fill)


def paste(canvas, layer, env):
    """按透明度包络 env(0..1) 把图层叠上去"""
    if env <= 0.003:
        return
    a = layer.split()[3].point(lambda p: int(p * env))
    layer = layer.copy()
    layer.putalpha(a)
    canvas.alpha_composite(layer)


def env(t, s, e, fi, fo):
    """时间包络：s..e 之间为 1，开头 fi 秒淡入、结尾 fo 秒淡出"""
    if t < s or t > e:
        return 0.0
    if t < s + fi:
        return (t - s) / fi
    if t > e - fo:
        return (e - t) / fo
    return 1.0


def wrap_cjk(text, fnt, max_w):
    """按像素宽度折行。

    中文优先在标点/连接处断，不硬切单字——"写好故事旁白"宁可断成
    "写好故事"+"旁白"，也不要断成"写好故事旁"+"白"。
    """
    # 常见可断点（在这些字符后面断）
    BREAK_AFTER = "、，。！？：；）】》」』,.;:!?)]}…—"
    tokens, buf = [], ""
    for ch in text:
        if ch == "\n":
            if buf:
                tokens.append(buf)
            buf = ""
            tokens.append("\n")
            continue
        buf += ch
        if ch in BREAK_AFTER:
            tokens.append(buf)
            buf = ""
    if buf:
        tokens.append(buf)

    lines, cur = [], ""
    for tok in tokens:
        if tok == "\n":
            lines.append(cur)
            cur = ""
            continue
        if tlen(cur + tok, fnt) <= max_w:
            cur += tok
            continue
        # 这个 token 本身就超宽 → 内部再硬切
        if cur:
            lines.append(cur)
            cur = ""
        while tlen(tok, fnt) > max_w and len(tok) > 1:
            # 找到最后一个能放下的字符位置
            cut = 0
            while cut < len(tok) and tlen(tok[:cut + 1], fnt) <= max_w:
                cut += 1
            if cut == 0:
                # 单个字符都放不下（max_w 太小），至少放一个避免死循环
                cut = 1
            lines.append(tok[:cut])
            tok = tok[cut:]
        cur = tok
    if cur:
        lines.append(cur)
    return [l for l in lines if l]


# ═══════════════ 各元素预渲染（整画布 RGBA） ═══════════════
def _split_title_words(text):
    """把主标题切成用于逐词弹入的词组。

    英文按空格切；中文没有空格，按标点和语义边界切成 2-4 字的短语，
    避免整句挤成一个词（那样弹入效果等于没有）。
    """
    out = []
    for seg in re.split(r"([\s，、。！？：；｜|·\-—/]+)", text):
        if not seg:
            continue
        if re.match(r"^[\s，、。！？：；｜|·\-—/]+$", seg):
            out.append(seg)          # 分隔符原样保留，撑开间距
            continue
        # 英文单词（连续拉丁字母/数字）单独成组
        for piece in re.split(r"([A-Za-z0-9@._+#]+)", seg):
            if not piece:
                continue
            if re.match(r"^[A-Za-z0-9@._+#]+$", piece):
                out.append(piece)
            else:
                # 中文：切成 2-4 字短语
                i = 0
                while i < len(piece):
                    n = 3 if len(piece) - i > 4 else len(piece) - i
                    out.append(piece[i:i + n])
                    i += n
    return out


def make_title(W, H, cfg, st):
    """开场标题卡。返回 (底板层, 逐词列表)。

    底板只画卡片和下划线，主标题文字拆成逐词层单独返回——
    这样每个词能按自己的时间点弹出来，而不是整块淡入。
    逐词手法移植自阿杜 lib.js 的 wordsAt()。
    """
    c = new_canvas(W, H)
    d = ImageDraw.Draw(c)
    x0, x1 = int(W * 0.08), int(W * 0.92)
    y0, y1 = int(H * 0.12), int(H * 0.12 + H * 0.20)
    rrect(d, (x0, y0, x1, y1), (*st["ink"], 150), radius=int(W * 0.033))
    f1 = load_font(int(H * 0.045), True)
    # 【标题太长要缩字号】一行放不下会直接冲出画面右边。
    # 学员标题写得长是常事，这里按可用宽度自动降档，直到能放下。
    _avail = int(W * 0.84) - int(W * 0.06)
    _sz = int(H * 0.045)
    while _sz > int(H * 0.018) and tlen(cfg["line1"], f1) > _avail:
        _sz = int(_sz * 0.92)
        f1 = load_font(_sz, True)
    if tlen(cfg["line1"], f1) > _avail:
        print("  [warn] 标题太长，画面放不下，建议改短：%s" % cfg["line1"][:30])
    f2 = load_font(int(H * 0.022), False)
    _line2 = cfg.get("line2", "")
    if _line2:
        ctext(d, (W // 2, int(H * 0.265)), _line2, f2, st["grey"], anchor="mm")

    # ── 逐词层：主标题切成若干「词组」，每组单独渲染一层 ──
    # 中文按标点/间隔切，英文按空格切。逐词弹入是阿杜 wordsAt() 的手法。
    words = []
    parts = _split_title_words(cfg["line1"])
    widths = [tlen(p, f1) for p in parts]
    total = sum(widths)
    cursor = (W - total) // 2
    for p, pw in zip(parts, widths):
        if p.strip():
            wc = new_canvas(W, H)
            wd = ImageDraw.Draw(wc)
            ctext(wd, (cursor + pw // 2, int(H * 0.165)), p, f1, st["white"], anchor="mm")
            words.append(FX.Sprite(wc))
        cursor += pw

    # 下划线单独成层：跟着主标题一起出现，不单独悬在空卡片里
    uc = new_canvas(W, H)
    ud = ImageDraw.Draw(uc)
    uw = tlen(cfg["line1"], f1)
    rrect(ud, (W // 2 - uw // 2, int(H * 0.205), W // 2 + uw // 2, int(H * 0.212)),
          st["accent"], radius=6)
    underline = FX.Sprite(uc)
    return c, words, underline


def make_chapter(W, H, st, num, text):
    c = new_canvas(W, H)
    d = ImageDraw.Draw(c)
    ft = load_font(int(H * 0.024), True)
    fnum = load_font(int(H * 0.039), True)
    tw = tlen(text, ft)
    nw = int(W * 0.089)
    pad = int(W * 0.065)
    total = nw + int(W * 0.028) + tw + pad * 2
    x0 = (W - total) // 2
    y0 = int(H * 0.14)
    hh = int(H * 0.075)
    rrect(d, (x0, y0, x0 + total, y0 + hh), (*st["ink"], 185), radius=int(H * 0.031),
          outline=st["accent"], width=max(3, int(W * 0.0037)))
    ctext(d, (x0 + pad + nw // 2, y0 + hh // 2), num, fnum, st["accent_light"], anchor="mm")
    ctext(d, (x0 + pad + nw + int(W * 0.014) + tw // 2, y0 + hh // 2), text, ft,
          st["white"], anchor="mm")
    return c


def make_keyword(W, H, st, text, side):
    c = new_canvas(W, H)
    d = ImageDraw.Draw(c)
    f = load_font(int(H * 0.024), True)
    tw = tlen(text, f)
    pad = int(W * 0.039)
    w = tw + pad * 2
    h = int(H * 0.044)
    y = int(H * 0.060)
    x = int(W * 0.06) if side == "left" else (W - int(W * 0.06) - w)
    rrect(d, (x, y, x + w, y + h), st["accent"], radius=h // 2)
    d.ellipse([x + int(W * 0.024), y + h // 2 - int(H * 0.0094),
               x + int(W * 0.041), y + h // 2 + int(H * 0.0094)], fill=st["white"])
    ctext(d, (x + int(W * 0.024) + int(W * 0.031) + tw // 2, y + h // 2), text,
          f, st["white"], anchor="mm")
    return c


def make_cta(W, H, st, cfg):
    c = new_canvas(W, H)
    d = ImageDraw.Draw(c)
    cw = int(W * 0.87)
    ch = int(H * 0.21)
    cx0 = (W - cw) // 2
    cy0 = int(H * 0.33)
    rrect(d, (cx0, cy0, cx0 + cw, cy0 + ch), (*st["ink"], 175), radius=int(H * 0.021),
          outline=st["accent"], width=max(4, int(W * 0.0046)))
    lines = cfg.get("lines", ["感谢观看", "欢迎关注"])
    tag = cfg.get("tag", "关注美瑶老师")
    f1 = load_font(int(H * 0.030), True)
    f3 = load_font(int(H * 0.021), False)
    n = len(lines)
    top = cy0 + ch * 0.21
    gap = ch * 0.27
    for i, ln in enumerate(lines):
        fill = st["accent_light"] if (i == n - 1 and len(lines) > 1) else st["white"]
        ctext(d, (W // 2, int(top + i * gap)), ln, f1, fill, anchor="mm",
              stroke=2, scolor=st["ink"])
    # 底部小标签
    tw = tlen(tag, load_font(int(H * 0.019), True))
    bw = max(tw + int(W * 0.08), int(W * 0.20))
    bh = int(H * 0.033)
    bx = (W - bw) // 2
    by = cy0 + ch - bh - int(H * 0.03)
    rrect(d, (bx, by, bx + bw, by + bh), st["accent"], radius=bh // 2)
    ctext(d, (W // 2, by + bh // 2), tag, load_font(int(H * 0.019), True),
          st["white"], anchor="mm")
    return c


def draw_progress(W, H, canvas, t, total, st, chapters):
    d = ImageDraw.Draw(canvas)
    x0, x1 = int(W * 0.06), int(W * 0.94)
    y = max(10, int(H * 0.016))
    hh = max(6, int(H * 0.0042))
    rrect(d, (x0, y, x1, y + hh), (255, 255, 255, 55), radius=hh // 2)
    frac = max(0.0, min(1.0, t / total)) if total > 0 else 0.0
    fx = x0 + (x1 - x0) * frac
    rrect(d, (x0, y, fx, y + hh), st["accent"], radius=hh // 2)
    for s, num, _tx in chapters:
        if total > 0:
            px = x0 + (x1 - x0) * (s / total)
            d.line([(px, y - 5), (px, y + hh + 5)], fill=st["white"], width=3)
    label = "开篇"
    for s, num, tx in chapters:
        if t >= s:
            label = f"{num} {tx}"
    f = load_font(int(H * 0.016), False)
    d.text((x0, y + hh + int(H * 0.012)), label, font=f,
           fill=(235, 235, 245, 220))


# ═══════════════ 动态卡：数字卡 + 步骤卡（逐帧绘制） ═══════════════
def _fmt_number(v, mode):
    """按 fmt 模式格式化数值。"""
    if mode == "wan":                       # 1840000 → 184万
        return f"{v / 10000:.0f}万"
    if mode == "wan1":                      # 1840000 → 184.0万
        return f"{v / 10000:.1f}万"
    if mode == "pct":                       # 78 → 78%
        return f"{v:.0f}%"
    if mode == "one":                       # 3.5
        return f"{v:.1f}"
    return f"{int(round(v)):,}"             # 默认千分位


def draw_number_card(W, H, canvas, t, cfg, st, m):
    """大数字滚动卡。数字从 from 滚到 value，进度条同步填充。

    移植自阿杜 TPL.number（数字滚动 + 进度条），配色沿用本技能 style 令牌。
    """
    s = cfg.get("start", 0.0)
    e = cfg.get("end", s + 3.0)
    if t < s or t > e:
        return
    at = cfg.get("at", s + 0.25)
    dur = float(cfg.get("dur", 1.6))
    frm = float(cfg.get("from", 0))
    to = float(cfg.get("value", 0))
    fmt = cfg.get("fmt", "int")
    unit = cfg.get("unit", "")
    label = cfg.get("label", "")
    sub = cfg.get("sub", "")

    # 卡片本体：进场用 up，出场整体缩小淡出
    a = FX.entrance(t, s, dur=0.6, kind="up", dist=H * 0.022 * m,
                    out=e - 0.45, out_dur=0.45, out_dy=-H * 0.012 * m,
                    out_scale=0.05 * m, out_blur=6 * m)
    if a["o"] <= 0.004:
        return

    d = ImageDraw.Draw(canvas)
    x0, x1 = int(W * 0.08), int(W * 0.92)
    y0, y1 = int(H * 0.49), int(H * 0.755)
    rrect(d, (x0, y0, x1, y1), (*st["ink"], 165), radius=FX.radius_of(H),
          outline=st["accent"], width=max(3, int(W * 0.0037)))

    # 顶部小标签
    if label:
        fl = load_font(int(H * 0.021), False)
        ctext(d, (x0 + int(W * 0.045), y0 + int(H * 0.032)), label, fl,
              st["grey"], anchor="lm")

    # 数字本体：大号 + 单位（单位跟数字底部对齐，不吊在中间）
    fn = load_font(int(H * 0.092), True)
    val = frm + (to - frm) * FX.Ease.out(FX.pr(t, at, at + dur))
    num_txt = _fmt_number(val, fmt)
    nw = tlen(num_txt, fn)
    fw = load_font(int(H * 0.038), True)
    uw = tlen(unit, fw) if unit else 0
    gap = int(W * 0.030) if unit else 0
    total_w = nw + gap + uw
    nx = (W - total_w) // 2
    ny = int(H * 0.600)
    ctext(d, (nx + nw // 2, ny), num_txt, fn, st["accent_light"], anchor="mm")
    if unit:
        # 单位基线贴近数字底部，视觉上像同一个数字的一部分
        ctext(d, (nx + nw + gap + uw // 2, ny + int(H * 0.030)), unit, fw,
              st["white"], anchor="mm")

    # 底部进度条：跟着数字同步填
    bx0, bx1 = x0 + int(W * 0.045), x1 - int(W * 0.045)
    by = y1 - int(H * 0.042)
    bh = max(6, int(H * 0.0072))
    # 补充说明：紧贴进度条上方，与顶部 label 分处卡片上下两端，不重叠
    if sub:
        fs = load_font(int(H * 0.019), False)
        ctext(d, (W // 2, by - int(H * 0.035)), sub, fs, st["grey"], anchor="mm")
    rrect(d, (bx0, by, bx1, by + bh), (255, 255, 255, 48), radius=bh // 2)
    fill = FX.Ease.out(FX.pr(t, at, at + dur))
    if fill > 0.002:
        rrect(d, (bx0, by, bx0 + int((bx1 - bx0) * fill), by + bh),
              st["accent"], radius=bh // 2)


def draw_steps_card(W, H, canvas, t, cfg, st, m):
    """步骤时间线：节点随讲稿逐个弹出，连接线随之延伸。

    移植自阿杜 TPL.steps（时间线逐段点亮）。
    竖屏走纵向时间线（从上往下），横屏走横向时间线——
    竖屏硬塞横排会导致标签挤成一团，这是竖屏版式的硬约束。
    """
    s = cfg.get("start", 0.0)
    e = cfg.get("end", s + 4.0)
    if t < s or t > e:
        return
    steps = cfg.get("steps") or []
    if not steps:
        return
    n = len(steps)

    a = FX.entrance(t, s, dur=0.6, kind="up", dist=H * 0.020 * m,
                    out=e - 0.45, out_dur=0.45, out_dy=-H * 0.010 * m,
                    out_scale=0.04 * m, out_blur=6 * m)
    if a["o"] <= 0.004:
        return

    d = ImageDraw.Draw(canvas)
    portrait = H >= W
    base_sz = int(H * 0.021) if portrait else int(H * 0.024)

    if portrait:
        # 竖屏底板：让时间线有个稳定的"底"，不散落在画面上
        x_line = int(W * 0.235)
        r = max(13, int(W * 0.028))
        top = int(H * 0.315)
        slot_h = int(H * 0.088)
        bottom = top + slot_h * (n - 1)
        bx0, bx1 = int(W * 0.10), int(W * 0.94)
        by0 = top - int(H * 0.050)
        by1 = bottom + int(H * 0.050)
        rrect(d, (bx0, by0, bx1, by1), (*st["ink"], 160), radius=FX.radius_of(H),
              outline=st["accent"], width=max(3, int(W * 0.0037)))
        _steps_vertical(W, H, d, t, steps, n, base_sz, st, m,
                        x_line=x_line, r=r, top=top, slot_h=slot_h)
    else:
        _steps_horizontal(W, H, d, t, steps, n, base_sz, st, m)


def _steps_vertical(W, H, d, t, steps, n, base_sz, st, m,
                    x_line=None, r=None, top=None, slot_h=None):
    """竖屏：纵向时间线，节点从上往下，标签在右侧。"""
    if x_line is None:
        x_line = int(W * 0.235)
        r = max(13, int(W * 0.028))
        top = int(H * 0.30)
        slot_h = int(H * 0.088)

    d.line([(x_line, top), (x_line, top + slot_h * (n - 1))],
           fill=(255, 255, 255, 70), width=max(3, int(W * 0.0035)))

    popped = []
    for i, item in enumerate(steps):
        at = item[1] if len(item) > 1 else 0
        p = FX.pr(t, at, at + 0.45)
        popped.append(p)
        if p <= 0.002:
            continue
        cy = top + slot_h * i
        sc = 0.4 + 0.6 * FX.Ease.spring(p)
        rr = int(r * sc)
        d.ellipse([x_line - rr, cy - rr, x_line + rr, cy + rr], fill=st["accent"])
        ctext(d, (x_line, cy), str(i + 1), load_font(int(base_sz * 1.15 * sc), True),
              st["white"], anchor="mm")

    # 点亮到最远已弹出的节点
    lit = 0
    for i, p in enumerate(popped):
        if p > 0.002:
            lit = i
    if lit > 0:
        d.line([(x_line, top), (x_line, top + slot_h * lit)], fill=st["accent"],
               width=max(3, int(W * 0.0035)))

    # 标签在节点右侧
    tx = x_line + r + int(W * 0.055)
    avail = W - tx - int(W * 0.06)
    for i, item in enumerate(steps):
        p = popped[i]
        if p <= 0.002:
            continue
        fnt, lines = None, None
        for shrink in (1.0, 0.92, 0.84):
            fnt = load_font(int(base_sz * shrink), False)
            cand = wrap_cjk(item[0], fnt, avail)
            if len(cand) <= 2:
                lines = cand
                break
        if lines is None:
            lines = wrap_cjk(item[0], fnt, avail)[:2]
        cy = top + slot_h * i
        # 文字整体在节点高度上居中
        lh = int(base_sz * 1.35)
        y0 = cy - (len(lines) - 1) * lh // 2
        for j, ln in enumerate(lines):
            ctext(d, (tx, y0 + j * lh), ln, fnt,
                  st["white"] if j == 0 else st["grey"], anchor="lm")


def _steps_horizontal(W, H, d, t, steps, n, base_sz, st, m):
    """横屏：横向时间线，节点从左往右，标签在节点下方。"""
    x0, x1 = int(W * 0.115), int(W * 0.885)
    y_line = int(H * 0.575)
    r = max(14, int(W * 0.026))
    step_x = [x0] if n == 1 else [x0 + int((x1 - x0) * i / (n - 1)) for i in range(n)]

    d.line([(step_x[0], y_line), (step_x[-1], y_line)], fill=(255, 255, 255, 70),
           width=max(3, int(W * 0.0035)))

    popped = []
    for i, item in enumerate(steps):
        at = item[1] if len(item) > 1 else 0
        p = FX.pr(t, at, at + 0.45)
        popped.append(p)
        if p <= 0.002:
            continue
        sc = 0.4 + 0.6 * FX.Ease.spring(p)
        cx = step_x[i]
        rr = int(r * sc)
        d.ellipse([cx - rr, y_line - rr, cx + rr, y_line + rr], fill=st["accent"])
        ctext(d, (cx, y_line), str(i + 1), load_font(int(base_sz * 1.15 * sc), True),
              st["white"], anchor="mm")

    lit = 0
    for i, p in enumerate(popped):
        if p > 0.002:
            lit = i
    if lit > 0:
        d.line([(step_x[0], y_line), (step_x[lit], y_line)], fill=st["accent"],
               width=max(3, int(W * 0.0035)))

    pad = int(W * 0.035)
    label_gap = int(H * 0.042)
    slot = int((x1 - x0) / max(1, n - 1)) if n > 1 else int(x1 - x0)
    for i, item in enumerate(steps):
        p = popped[i]
        if p <= 0.002:
            continue
        avail = max(slot - int(W * 0.030), int(W * 0.10))
        fnt, lines = None, None
        for shrink in (1.0, 0.92, 0.84):
            fnt = load_font(int(base_sz * shrink), False)
            cand = wrap_cjk(item[0], fnt, avail)
            if all(tlen(l, fnt) <= avail for l in cand) and len(cand) <= 2:
                lines = cand
                break
        if lines is None:
            lines = wrap_cjk(item[0], fnt, avail)[:2]
        for j, ln in enumerate(lines):
            col = st["white"] if j == 0 else st["grey"]
            lw = tlen(ln, fnt)
            cx = step_x[i]
            half = lw // 2
            if cx - half < pad:
                cx = pad + half
            elif cx + half > W - pad:
                cx = W - pad - half
            ctext(d, (cx, y_line + r + label_gap + j * int(H * 0.028)),
                  ln, fnt, col, anchor="mm")


# ═══════════════ 主流程：生成特效层 =═════════════
def _anim(t, t0, dur, kind, dist, out=None, out_dur=0.35, m=1.0, **kw):
    """动效参数。m=0 时退回纯淡入淡出（不用曲线、不位移）。"""
    if m <= 0.001:
        o = FX.pr(t, t0, t0 + dur)
        if out is not None:
            o *= 1 - FX.pr(t, out, out + out_dur)
        return {"o": o, "dx": 0.0, "dy": 0.0, "s": 1.0, "blur": 0.0}
    r = FX.entrance(t, t0, dur, kind, dist, out=out, out_dur=out_dur, **kw)
    r["s"] = 1 + (r["s"] - 1) * m
    r["dx"] *= m
    r["dy"] *= m
    r["blur"] *= m
    return r


def _build_scene(W, H, cfg, st):
    """把配置里所有元素准备好，返回可直接逐帧绘制的结构。"""
    if cfg.get("title"):
        t_layer, t_words, t_ul = make_title(W, H, cfg["title"], st)
        title = FX.Sprite(t_layer)
    else:
        t_layer, t_words, t_ul, title = None, [], None, None
    chapters = [FX.Sprite(make_chapter(W, H, st, c["num"], c["text"]))
                for c in cfg.get("chapters", [])]
    keys = [FX.Sprite(make_keyword(W, H, st, k["text"], k.get("side", "left")))
            for k in cfg.get("keywords", [])]
    cta = FX.Sprite(make_cta(W, H, st, cfg["cta"])) if cfg.get("cta") else None
    return {
        "title": title,
        "title_words": t_words,
        "title_ul": t_ul,
        "chapters": chapters,
        "keys": keys,
        "cta": cta,
        "chaps_time": [(c["start"], c["num"], c["text"]) for c in cfg.get("chapters", [])],
        "kw_time": [(k["start"], k.get("side", "left")) for k in cfg.get("keywords", [])],
    }


def _paint_frame(W, H, canvas, t, total, cfg, st, m, scene):
    """在 canvas 上画第 t 秒的完整特效层。"""
    draw_progress(W, H, canvas, t, total, st, scene["chaps_time"])

    if scene["title"]:
        ts = cfg["title"].get("start", 0.0)
        te = cfg["title"].get("end", 3.8)
        # 卡片本体：滑入 + 出场缩小
        a = _anim(t, ts, 0.7, "up", H * 0.030 * m,
                  out=te - 0.55, out_dur=0.55, m=m,
                  out_dy=-H * 0.014 * m, out_scale=0.06 * m)
        scene["title"].draw(canvas, **a)
        # 主标题逐词弹入：第 i 个词在 ts+0.25+i*0.12 时弹出
        # （移植自阿杜 lib.js 的 wordsAt()，逐词 stagger）
        words = scene["title_words"]
        if words:
            for i, wd in enumerate(words):
                wat = ts + 0.25 + i * 0.12
                if t < wat:
                    continue
                a = _anim(t, wat, 0.5, "pop", 0, out=te - 0.55, out_dur=0.55, m=m,
                          out_dy=-H * 0.010 * m, out_scale=0.05 * m)
                wd.draw(canvas, **a)
            # 下划线跟着最后一个词一起出现
            if scene["title_ul"]:
                last_at = ts + 0.25 + (len(words) - 1) * 0.12
                a = _anim(t, last_at, 0.4, "right", 0, out=te - 0.55, out_dur=0.55,
                          m=m, out_scale=0.04 * m)
                scene["title_ul"].draw(canvas, **a)
    for (s, _n, _x), ch in zip(scene["chaps_time"], scene["chapters"]):
        a = _anim(t, s, 0.5, "pop", 0, out=s + 3.2 - 0.45, out_dur=0.45, m=m,
                  out_dy=-H * 0.010 * m, out_scale=0.05 * m)
        ch.draw(canvas, **a)
    for (s, side), k in zip(scene["kw_time"], scene["keys"]):
        kind = "left" if side == "left" else "right"
        a = _anim(t, s, 0.45, kind, W * 0.05 * m,
                  out=s + 2.6 - 0.35, out_dur=0.35, m=m,
                  out_dy=-H * 0.008 * m, out_scale=0.04 * m)
        k.draw(canvas, **a)

    for nc in cfg.get("numbers", []) or []:
        draw_number_card(W, H, canvas, t, nc, st, m)
    for sc in cfg.get("steps", []) or []:
        draw_steps_card(W, H, canvas, t, sc, st, m)

    if scene["cta"]:
        _cs = cfg["cta"].get("start", 0.0)
        _ce = cfg["cta"].get("end", _cs + 3.0)
        a = _anim(t, _cs, 0.65, "zoom", 0,
                  out=_ce - 0.55, out_dur=0.55, m=m, out_scale=0.06 * m)
        if a["o"] > 0.004:
            d = ImageDraw.Draw(canvas)
            d.rectangle([0, 0, W, H], fill=(*st["ink"], int(95 * a["o"])))
            scene["cta"].draw(canvas, **a)


def build_overlay(W, H, fps, total, cfg, st, out_mov, ff):
    m = FX.motion_scale(cfg)
    scene = _build_scene(W, H, cfg, st)

    n = int(total * fps) + 1
    cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-c:v", "qtrle", "-pix_fmt", "argb", out_mov]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE)
    for i in range(n):
        canvas = new_canvas(W, H)
        _paint_frame(W, H, canvas, i / fps, total, cfg, st, m, scene)
        p.stdin.write(canvas.tobytes())
    p.stdin.close()
    err = p.stderr.read().decode(errors="ignore")
    rc = p.wait()
    if rc != 0:
        raise RuntimeError("ffmpeg 生成特效层失败：\n" + err[-2000:])


# ═══════════════ 最终合成 =═════════════
def _srt_time(raw):
    """把 SRT 的 00:00:01,500 规整成 ASS 的 0:00:01.50。格式怪就原样返回。"""
    s = raw.strip().replace(",", ".")
    if "." not in s:
        return s
    head, tail = s[:s.rfind(".")], s[s.rfind(".") + 1:]
    tail = (tail + "00")[:2]
    return head + "." + tail


def srt_to_ass(srt_path, ass_path, W, H, font_size):
    blocks = []
    # 编码不固定：学员用记事本另存的 SRT 常是 GBK 或带 BOM
    try:
        raw = read_text_auto(srt_path)
    except Exception as ex:
        print("   【注意】 读字幕稿失败：%s" % ex)
        raw = ""
    for blk in raw.replace("\r\n", "\n").strip().split("\n\n"):
        ls = [l for l in blk.splitlines() if l.strip()]
        if len(ls) < 3:
            continue
        if "-->" not in ls[1]:
            continue
        tm = ls[1].split("-->")
        if len(tm) < 2:
            continue
        st = _srt_time(tm[0])
        en = _srt_time(tm[1])
        if not st or not en:
            continue
        blocks.append((st, en, "\\N".join(ls[2:])))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,{font_size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,{int(H*0.18)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    open(ass_path, "w", encoding="utf-8").write(
        head + "".join(f"Dialogue: 0,{st},{en},Default,,0,0,0,,{txt}\n" for st, en, txt in blocks))


def has_audio_stream(path, fp=None):
    """源视频有没有音频流。没有就别挂音频滤镜（[0:a] 会匹配失败）。

    【没 ffprobe 也要判对】以前这里在拿不到 ffprobe 时直接 return True，
    于是无音轨的素材会被挂上 [0:a] 滤镜 → 合成直接失败；
    而且 BGM 会被无声地丢掉，学员以为加了音乐其实没有。
    现在退回「ffmpeg -i 读 stderr」判断。
    """
    if fp:
        try:
            out = subprocess.check_output(
                [fp, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", path]
            ).decode().strip()
            if out:
                return out.startswith("audio")
        except Exception:
            pass
    # 退回 ffmpeg -i 解析
    try:
        ff = find_ffmpeg()
        r = subprocess.run([ff, "-i", path], capture_output=True, timeout=30)
        err = (r.stderr or b"").decode("utf-8", "ignore")
        if "Audio:" in err:
            return True
        if "Video:" in err:
            return False
    except Exception:
        pass
    return True
    return True


def composite(src, overlay, out, ff, fp, bgm=None, srt=None):
    total = get_duration(src, fp)
    W, H = probe_size(src, fp)
    mod8 = f"scale=trunc(iw/8)*8:trunc(ih/8)*8"
    level = "3.1" if max(W, H) <= 1280 else "4.0" if max(W, H) <= 1920 else "4.1"
    fps = 30
    breath = fps * 9
    zoom = (f"fps={fps},"
            f"zoompan=z='1.012+0.012*sin(on/{breath}*2*PI)'"
            f":d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={W}x{H}:fps={fps}")
    inputs = [src]
    fc_parts = []
    # 视频：呼吸 + 可选字幕 + 特效层
    vchain = f"[0:v]{zoom},setsar=1"
    if srt:
        ass_name = os.path.basename(srt)[:-4] + ".ass"
        srt_to_ass(srt, os.path.join(os.path.dirname(os.path.abspath(src)), ass_name), W, H,
                   max(28, int(H * 0.03)))
        vchain += f",ass='{ass_name}'"
    fade = 0.6
    # 片尾淡出起点必须钳制在有效区间内：短视频上 total-fade-0.3 会算成
    # 负数或过小，导致整片被压暗（实测 1 秒片几乎全黑）。
    fade_out_start = max(0.0, total - fade - 0.3)
    fade_in_d = min(fade, max(0.0, total * 0.5))
    fade_out_d = min(fade, max(0.0, total - fade_out_start))
    vchain += (f",fade=t=in:st=0:d={fade_in_d:.3f}"
               f",fade=t=out:st={fade_out_start:.3f}:d={fade_out_d:.3f}[vsub]")
    fc_parts.append(vchain)
    fc_parts.append(
        f"[vsub][1:v]overlay=format=auto:eof_action=pass,scale=trunc(iw/8)*8:trunc(ih/8)*8[v]")
    # 音频（源视频没有音频流时整段跳过）
    src_has_audio = has_audio_stream(src, fp)
    if not src_has_audio:
        bgm = None
    if bgm:
        fc_parts.append(
            f"[2:a]volume=-14dB,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[bgm];"
            f"[0:a]asplit=2[voice][sc];"
            f"[bgm][sc]sidechaincompress=threshold=0.03:ratio=6:attack=200:release=1200[bgmduck];"
            f"[voice][bgmduck]amix=inputs=2:duration=first:normalize=0[a1];"
            f"[a1]loudnorm=I=-14:TP=-1.0:LRA=11[a]")
        inputs += [bgm]
        amap = "[a]"
    elif src_has_audio:
        # 纯静音片送进 loudnorm 会产生 NaN，aac 编码直接报错。
        # 先把音量压到安全区间再用 dynaudnorm（不做增益计算，不会出 NaN）。
        fc_parts.append(
            f"[0:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"dynaudnorm=f=150:g=15:p=0.9[a]")
        amap = "[a]"
    fc = ";".join(fc_parts)
    src_has_audio = has_audio_stream(src, fp)
    cmd = [ff, "-y", "-loglevel", "error", "-i", src, "-i", overlay] + \
          (["-stream_loop", "-1", "-i", bgm] if bgm else []) + \
          ["-filter_complex", fc, "-map", "[v]"]
    if src_has_audio:
        cmd += ["-map", amap, "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-profile:v", "main", "-level", level,
            "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(src)))
    if r.returncode != 0:
        raise RuntimeError("合成失败：\n" + r.stderr[-2500:])
    print(f"【OK】 动效版 {get_duration(out, fp):.3f}s -> {out}")


# ═══════════════ 入口 =═════════════

# ═══════════════ 配置校验（学员改配置最容易翻车的地方）═══════════════
KNOWN_TOP = {"title", "chapters", "keywords", "numbers", "steps", "cta", "style"}
BLOCK_FIELDS = {
    "title": ({"start", "end", "line1", "line2"}, {"start", "line1"}),
    "chapters": ({"start", "num", "text"}, {"start", "text"}),
    "keywords": ({"start", "text", "side"}, {"start", "text"}),
    "numbers": ({"start", "end", "at", "dur", "from", "value", "fmt", "unit",
                 "label", "sub"}, {"start", "value"}),
    "steps": ({"start", "end", "steps"}, {"start", "steps"}),
    "cta": ({"start", "end", "lines", "tag"}, {"start", "lines"}),
    "style": ({"accent", "accent_light", "ink", "white", "grey"}, set()),
}


def read_text_auto(path):
    """按 utf-8-sig → utf-8 → gbk → gb18030 顺序读文本。
    学员用记事本另存的 SRT 常是 GBK/带 BOM，直接 utf-8 读会 UnicodeDecodeError。"""
    data = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _safe_name(s, limit=24):
    r"""把任意文字变成安全的文件名片段（Windows 不允许 \ / : * ? " < > |）"""
    bad = '<>:"/\\|?*'
    out = "".join(("_" if ch in bad else ch) for ch in str(s))
    out = "".join(ch for ch in out if ord(ch) >= 32).strip(" .")
    return (out[:limit] or "x")


def normalize_cfg(cfg):
    """把配置归一化，让「写成对象/写成单项」也能跑。返回 (cfg, 提示列表)"""
    notes = []
    if not isinstance(cfg, dict):
        raise ValueError("配置文件最外层必须是一个 JSON 对象 { }")
    if cfg.get("style") is None:
        cfg["style"] = {}
    if not isinstance(cfg.get("style"), dict):
        raise ValueError("style 必须写成 { } 对象，例如 \"style\": {\"accent\": [255,92,138]}")
    # numbers / steps 写成单个对象时，包成只有一项的列表
    for key in ("numbers", "steps"):
        v = cfg.get(key)
        if isinstance(v, dict):
            cfg[key] = [v]
            notes.append("%s 写成了单个对象，已自动当成只有一项的列表" % key)
    # numbers 里 from 写成 for 是常见笔误
    for nc in (cfg.get("numbers") or []):
        if isinstance(nc, dict) and "for" in nc and "from" not in nc:
            nc["from"] = nc.pop("for")
            notes.append("numbers 里的 \"for\" 已自动改成 \"from\"")
    # steps 内层项统一成 [文字, 时间]
    for sc in (cfg.get("steps") or []):
        if isinstance(sc, dict) and isinstance(sc.get("steps"), list):
            fixed = []
            for it in sc["steps"]:
                if isinstance(it, dict):
                    txt = it.get("text", "")
                    tm = it.get("at", it.get("time", 0))
                elif isinstance(it, (list, tuple)) and len(it) >= 2:
                    txt, tm = it[0], it[1]
                else:
                    txt, tm = str(it), 0
                fixed.append([txt, tm])
            sc["steps"] = fixed
    return cfg, notes


OPTIONAL = {
    "title": ["end", "line2"],
    "cta": ["end", "tag"],
    "chapters": ["num"],
    "keywords": ["side"],
    "numbers": ["end", "at", "dur", "from", "fmt", "unit", "label", "sub"],
    "steps": ["end"],
}


def validate_cfg(cfg):
    """校验配置。返回 (错误列表, 警告列表)。错误非空就别开跑。"""
    errs, warns = [], []
    for k in cfg:
        if k not in KNOWN_TOP:
            warns.append("顶层多了字段 \"%s\"（代码不认，多半是拼错了）" % k)
    # title / cta 是单块
    for blk in ("title", "cta"):
        v = cfg.get(blk)
        if v is None:
            continue
        if not isinstance(v, dict):
            errs.append("%s 必须写成 { } 对象" % blk)
            continue
        allowed, need = BLOCK_FIELDS[blk]
        for f in need:
            if f not in v:
                errs.append("%s 少了必填字段 \"%s\"" % (blk, f))
        for f in v:
            if f not in allowed:
                warns.append("%s 里的 \"%s\" 不是这个卡片认识的字段" % (blk, f))
        for f in OPTIONAL.get(blk, []):
            if f not in v:
                warns.append("%s 建议写上 \"%s\"（不写会用默认值）" % (blk, f))
    # 列表块
    for blk in ("chapters", "keywords", "numbers", "steps"):
        v = cfg.get(blk)
        if v is None:
            continue
        if not isinstance(v, list):
            errs.append("%s 必须写成 [ ... ] 数组" % blk)
            continue
        allowed, need = BLOCK_FIELDS[blk]
        for i, item in enumerate(v, 1):
            if not isinstance(item, dict):
                errs.append("%s 第 %d 项必须写成 { } 对象" % (blk, i))
                continue
            for f in need:
                if f not in item:
                    errs.append("%s 第 %d 项少了必填字段 \"%s\"" % (blk, i, f))
            for f in item:
                if f not in allowed:
                    warns.append("%s 第 %d 项的 \"%s\" 不是这个卡片认识的字段" % (blk, i, f))
    # 数字卡专项：value / num 极易混淆
    for i, nc in enumerate(cfg.get("numbers") or [], 1):
        if not isinstance(nc, dict):
            continue
        if "value" not in nc and "num" in nc:
            errs.append("numbers 第 %d 项：写成了 \"num\"，数字卡要用 \"value\""
                        "（\"num\" 是章节卡的序号字段）" % i)
        if "value" in nc and not isinstance(nc["value"], (int, float)):
            errs.append("numbers 第 %d 项：\"value\" 必须是数字" % i)
        for f in ("start", "end", "at", "dur"):
            if f in nc and not isinstance(nc[f], (int, float)):
                errs.append("numbers 第 %d 项：\"%s\" 必须是数字（秒）" % (i, f))
    # 步骤卡内层
    for i, sc in enumerate(cfg.get("steps") or [], 1):
        if not isinstance(sc, dict):
            continue
        for j, it in enumerate(sc.get("steps") or [], 1):
            if isinstance(it, (list, tuple)):
                if len(it) < 2:
                    errs.append("steps 第 %d 项的第 %d 个步骤要写成 [文字, 出现秒数] 两项" % (i, j))
                elif not isinstance(it[0], str):
                    errs.append("steps 第 %d 项的第 %d 个步骤，第一个元素要是文字" % (i, j))
            elif not isinstance(it, dict):
                errs.append("steps 第 %d 项的第 %d 个步骤写法不对" % (i, j))
    return errs, warns


def load_style(cfg):
    s = cfg.get("style", {})
    def c(key, default):
        v = s.get(key, default)
        return tuple(v) if isinstance(v, list) else v
    return {
        "accent": c("accent", [255, 92, 138]),
        "accent_light": c("accent_light", [255, 143, 179]),
        "ink": c("ink", [18, 18, 26]),
        "white": c("white", [255, 255, 255]),
        "grey": c("grey", [225, 225, 235]),
    }


def preview_frames(W, H, fps, total, cfg, st, outdir):
    """只画关键帧 PNG，用于目视检查排版/溢出/对比度，不做视频编码。"""
    m = FX.motion_scale(cfg)
    scene = _build_scene(W, H, cfg, st)
    marks = {}

    def add(t, tag):
        marks[round(t, 2)] = tag

    if cfg.get("title"):
        ts = cfg["title"].get("start", 0.0)
        add(ts + 0.15, "title_in")
        add(ts + 0.55, "title_w1")      # 逐词弹入中间态
        add(ts + 1.05, "title_w2")
        add(cfg["title"].get("end", 3.8) - 0.1, "title_out")
    for c in cfg.get("chapters", []):
        add(c["start"] + 0.12, "chapter_" + _safe_name(c.get("num", "x"), 8))
    for k in cfg.get("keywords", []):
        add(k["start"] + 0.12, "kw_" + _safe_name(k.get("text", "x"), 16))
    for nc in cfg.get("numbers", []) or []:
        add(nc.get("start", 0) + 0.15, "num_in")
        add(nc.get("start", 0) + float(nc.get("dur", 1.6)) + 0.6, "num_done")
    for sc in cfg.get("steps", []) or []:
        add(sc.get("start", 0) + 0.15, "steps_in")
        add(sc.get("start", 0) + 2.4, "steps_mid")
    if cfg.get("cta"):
        add(cfg["cta"]["start"] + 0.15, "cta_in")

    for t in sorted(marks):
        canvas = new_canvas(W, H)
        _paint_frame(W, H, canvas, t, total, cfg, st, m, scene)
        canvas.convert("RGB").save(os.path.join(outdir, f"{t:07.2f}_{marks[t]}.png"))
    print(f"      共 {len(marks)} 帧 -> {outdir}")


def _warn_overlap(cfg):
    """纯检查：若两块都占画面中央的大卡（标题/数字/步骤/CTA）时间窗重叠，
    给出可读提示，让学员自行错开时间码。零风险，不改任何绘制。"""
    blocks = []
    if cfg.get("title"):
        t = cfg["title"]
        blocks.append(("标题卡", t.get("start", 0.0), t.get("end", t.get("start", 0.0) + 3.0)))
    for n in cfg.get("numbers", []) or []:
        blocks.append(("数字卡", n.get("start", 0.0), n.get("end", n.get("start", 0.0) + 3.0)))
    for s in cfg.get("steps", []) or []:
        blocks.append(("步骤卡", s.get("start", 0.0), s.get("end", s.get("start", 0.0) + 3.0)))
    if cfg.get("cta"):
        c = cfg["cta"]
        blocks.append(("CTA卡", c.get("start", 0.0), c.get("end", c.get("start", 0.0) + 3.0)))
    warn = []
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            a0, a1 = blocks[i][1], blocks[i][2]
            b0, b1 = blocks[j][1], blocks[j][2]
            if a0 < b1 and b0 < a1:  # 时间窗相交
                warn.append((blocks[i][0], blocks[j][0], max(a0, b0)))
    if warn:
        print("[warn] 以下大卡时间窗重叠，成片里会互相遮挡，建议错开 start/end：")
        for x, y, at in warn:
            print(f"       · {x} 与 {y} 在约 {at:.1f}s 处重叠")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--bgm", default=None, help="背景音乐（纯伴奏/无人声）。留空则自动用技能自带轻量BGM")
    ap.add_argument("--no-bgm", action="store_true", help="不加背景音乐")
    ap.add_argument("--srt", default=None)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--tmp", default=None, help="特效层中间文件目录")
    ap.add_argument("--preview", default=None, help="只导出关键帧 PNG 到该目录（检查用，不出片）")
    a = ap.parse_args()

    # 【要点】 默认自带 BGM：未显式给 --bgm 且未 --no-bgm 时，自动用技能自带轻量音乐
    if not a.bgm and not a.no_bgm:
        _self_bgm = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "assets", "bgm.mp3"))
        if os.path.exists(_self_bgm):
            a.bgm = _self_bgm

    ff = find_ffmpeg()
    fp = find_ffprobe()
    # ── 配置先校验，再开跑 ──────────────────────────────────────────
    # 学员改配置最容易翻车：少个字段、类型写错、value 写成 num。
    # 以前这些会变成一屏 KeyError/AttributeError traceback，学员完全看不懂。
    # 现在：先用中文说清楚哪里错、改了就能跑。
    try:
        with open(a.config, encoding="utf-8-sig") as _cf:
            cfg = json.load(_cf)
    except Exception as ex:
        sys.exit("配置读不出来（%s）：%s" % (a.config, ex))
    try:
        cfg, _notes = normalize_cfg(cfg)
    except ValueError as ex:
        sys.exit("配置有问题：%s" % ex)
    _errs, _warns = validate_cfg(cfg)
    for _w in _warns:
        print("  [warn] %s" % _w)
    if _errs:
        print("配置里有 %d 处问题，改完再跑：" % len(_errs))
        for _e in _errs:
            print("  · %s" % _e)
        print("  字段说明见 references/动效库配置说明.md")
        sys.exit(1)
    for _n in _notes:
        print("  · %s" % _n)
    st = load_style(cfg)
    _warn_overlap(cfg)
    # 全部转绝对路径，避免 composite() 切换 cwd 后相对路径失效
    a.video = os.path.abspath(a.video)
    a.config = os.path.abspath(a.config)
    if a.out:
        a.out = os.path.abspath(a.out)
    if a.bgm:
        a.bgm = os.path.abspath(a.bgm)
    if a.srt:
        a.srt = os.path.abspath(a.srt)
    if a.tmp:
        a.tmp = os.path.abspath(a.tmp)

    W, H = probe_size(a.video, fp)
    total = get_duration(a.video, fp)
    tmp = a.tmp or os.path.dirname(os.path.abspath(a.video))
    print(f"[info] 视频 {W}x{H}，时长 {total:.2f}s，fps={a.fps}")

    if a.preview:
        os.makedirs(a.preview, exist_ok=True)
        preview_frames(W, H, a.fps, total, cfg, st, a.preview)
        print(f"[preview] 关键帧 -> {a.preview}")
        return

    overlay = os.path.join(tmp, "_effects_overlay.mov")
    print("[1/2] 生成特效层（标题/章节/关键词/进度条/CTA/数字卡/步骤卡）…")
    build_overlay(W, H, a.fps, total, cfg, st, overlay, ff)
    print(f"      特效层 -> {overlay}")

    out = a.out or (os.path.splitext(a.video)[0] + "_动效版.mp4")
    print("[2/2] 合成（呼吸运镜 + 特效层" +
          (" + 字幕" if a.srt else "") + (" + BGM闪避" if a.bgm else "") + "）…")
    composite(a.video, overlay, out, ff, fp, bgm=a.bgm, srt=a.srt)
    try:
        os.remove(overlay)
    except OSError:
        pass


if __name__ == "__main__":
    main()
