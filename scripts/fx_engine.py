# -*- coding: utf-8 -*-
"""
fx_engine.py —— 弹性动效引擎（缓动曲线 + 入场预设 + 变换贴图）

移植自开源项目 adunext/adu-motion-video（MIT License，阿杜实验室）的
template/lib.js。原作是 HTML/CSS + Playwright 逐帧渲染，本模块把同一条
运动曲线翻译成 Python + Pillow，让「代码卡点动效」也有弹簧感而不是匀速淡入。

移植对照表（原作 → 本模块）：
    lib.js: EZ.expo / out5 / spring / back / inout / in / out   →  Ease.expo …
    lib.js: show(e,t,t0,{k:'up'|'down'|'left'|'right'|'pop'|'zoom'|'slam'})
                                                             →  entrance()
    lib.js: place(e,{x,y,s,o,blur}) + mk()                    →  Sprite.draw()

为什么不全盘照搬他的渲染管线：
    他的管线要 Node + Playwright + Chromium，且是「整屏替换场景」（人物被盖住）。
    本技能的口播片定位是「叠在人脸上方的半透明卡片」（人还在说话、脸能透出来），
    所以只取运动语言，不取管线。

──────────────────────────────
学员怎么用（一般不用直接调）：
    现有 effects.json 的字段一个都不用改，效果自动变弹性。
    想手动控制强度，在配置里加 "fx": {"motion": 1.0} 缩放全部动效幅度，
    或 {"fx": {"motion": 0}} 关掉动效退回纯淡入淡出。
"""
import os
import sys

from PIL import Image, ImageFilter

# 让同目录脚本能互相 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════
#  缓动曲线（对照 lib.js 的 EZ 对象）
# ══════════════════════════════════════════════════════════════
def clamp(x, a=0.0, b=1.0):
    return a if x < a else (b if x > b else x)


class Ease:
    """缓动曲线族。x ∈ [0,1] → y ∈ [0,1]（spring/back 可能短暂越界，属正常）。"""

    @staticmethod
    def linear(x):
        return clamp(x)

    @staticmethod
    def out(x):
        """三次缓出，快出慢停"""
        x = clamp(x)
        return 1 - (1 - x) ** 3

    @staticmethod
    def out5(x):
        """五次缓出，收尾更狠，阿杜用它做 slam 冲击"""
        x = clamp(x)
        return 1 - (1 - x) ** 5

    @staticmethod
    def expo(x):
        """指数缓出，快到像急刹"""
        x = clamp(x)
        return 1 if x >= 1 else 1 - 2 ** (-10 * x)

    @staticmethod
    def in_(x):
        """缓入，慢启动"""
        return clamp(x) ** 3

    @staticmethod
    def inout(x):
        """缓入缓出，转场用"""
        x = clamp(x)
        return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2

    @staticmethod
    def back(x):
        """回弹过头再回落，c1=1.6 与原作一致"""
        c1, c3 = 1.6, 2.6
        return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2

    @staticmethod
    def spring(x):
        """临界阻尼弹簧感（Apple 味），原作的招牌"""
        if x >= 1:
            return 1
        import math
        return 1 - math.exp(-7 * x) * math.cos(8.5 * x) * (1 - x * 0.2)


def lerp(a, b, x):
    return a + (b - a) * x


def pr(t, a, b):
    """进度：t 在 [a,b] 区间内映射到 [0,1]，区间外钳制。对应 lib.js 的 pr()"""
    if b == a:
        return 1.0 if t >= b else 0.0
    return clamp((t - a) / (b - a))


# ══════════════════════════════════════════════════════════════
#  入场/出场预设（对应 lib.js 的 show()）
# ══════════════════════════════════════════════════════════════
def entrance(t, t0, dur=0.55, kind="up", dist=50.0,
             out=None, out_dur=0.35, out_dy=0.0, out_dx=0.0,
             out_scale=0.0, out_blur=8.0, opacity=1.0):
    """算出一个元素在时刻 t 的变换参数。

    返回 dict:
        o     不透明度 0..1
        dx    水平位移（px）
        dy    垂直位移（px）
        s     缩放倍率（1 = 原大小）
        blur  高斯模糊半径（px）

    kind 取值对应阿杜的入场预设：
        up     从下方滑入（带轻微去模糊）
        down   从上方滑入
        left   从左侧滑入
        right  从右侧滑入
        pop    弹跳入场（spring），缩放 0.6→1
        zoom   放大淡入（1.25→1）
        slam   冲击入场（out5），1→2.2 砸下来再回落
    """
    x = pr(t, t0, t0 + dur)
    o = clamp(x * 3)
    E = Ease.expo(x)
    tx = ty = 0.0
    s = 1.0
    blur = 0.0

    if kind == "up":
        ty = (1 - E) * dist
        blur = (1 - E) * 6
    elif kind == "down":
        ty = -(1 - E) * dist
    elif kind == "left":
        tx = -(1 - E) * dist
    elif kind == "right":
        tx = (1 - E) * dist
    elif kind == "pop":
        s = 0.6 + 0.4 * Ease.spring(x)
        o = clamp(x * 5)
    elif kind == "zoom":
        s = 1 + (1 - E) * 0.25
        blur = (1 - E) * 14
    elif kind == "slam":
        s = 1 + (1 - Ease.out5(x)) * 1.2
        o = clamp(x * 5)
        blur = (1 - x) * 10

    # 出场：淡出 + 可选位移/缩放/去模糊
    if out is not None:
        y = pr(t, out, out + out_dur)
        Y = Ease.in_(y)
        o *= 1 - y
        ty -= Y * out_dy
        tx += Y * out_dx
        s *= 1 - out_scale * Y
        blur += out_blur * Y

    return {"o": clamp(o) * opacity, "dx": tx, "dy": ty, "s": s, "blur": blur}


# ══════════════════════════════════════════════════════════════
#  变换贴图
# ══════════════════════════════════════════════════════════════
def blit(dst, src, x, y):
    """把 RGBA 图 src 贴到 dst 的 (x, y)，正确处理越界裁剪（source-over 合成）。"""
    w, h = dst.size
    sw, sh = src.size
    sx = max(0, -x)
    sy = max(0, -y)
    dx = max(0, x)
    dy = max(0, y)
    cw = min(sw - sx, w - dx)
    ch = min(sh - sy, h - dy)
    if cw <= 0 or ch <= 0:
        return
    dst.alpha_composite(src.crop((sx, sy, sx + cw, sy + ch)), dest=(dx, dy))


class Sprite:
    """一张预先渲染好的 RGBA 元素，可按 entrance() 的参数做位移/缩放/模糊贴图。

    构造时按 alpha 通道自动裁到内容边界，只保留有效像素——
    这样缩放动画只处理小图，不会每帧重采样整张 1080p 画布。
    """

    def __init__(self, layer, anchor=(0.5, 0.5)):
        self.src = layer
        bb = layer.getchannel("A").getbbox()
        if not bb:
            bb = (0, 0, layer.width, layer.height)
        self.crop = layer.crop(bb)
        self.anchor = anchor
        # 锚点在整幅画布上的绝对坐标
        self.ax = bb[0] + anchor[0] * (bb[2] - bb[0])
        self.ay = bb[1] + anchor[1] * (bb[3] - bb[1])

    def draw(self, canvas, o=1.0, dx=0.0, dy=0.0, s=1.0, blur=0.0):
        img = self.crop
        if abs(s - 1.0) > 1e-3:
            w = max(1, int(round(img.width * s)))
            h = max(1, int(round(img.height * s)))
            img = img.resize((w, h), Image.LANCZOS)
        if blur > 0.05:
            img = img.filter(ImageFilter.GaussianBlur(blur))
        if o < 0.999:
            img = img.copy()
            a = img.getchannel("A").point(lambda p: int(p * o))
            img.putalpha(a)
        x = int(round(self.ax - self.anchor[0] * img.width + dx))
        y = int(round(self.ay - self.anchor[1] * img.height + dy))
        blit(canvas, img, x, y)


# ══════════════════════════════════════════════════════════════
#  设计令牌（与 effects.py 的 style 配套，保证新旧元素同一套语言）
# ══════════════════════════════════════════════════════════════
def radius_of(H):
    """统一圆角。同一稿内所有卡片共用一个半径（审美加强原则）。"""
    return max(8, int(H * 0.022))


def motion_scale(cfg):
    """从配置读动效强度系数，0 = 退回纯淡入淡出。"""
    fx = cfg.get("fx", {}) or {}
    try:
        v = float(fx.get("motion", 1.0))
    except (TypeError, ValueError):
        v = 1.0
    return clamp(v, 0.0, 3.0)
