#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install.py — 「AI自动剪辑」技能一键安装器（零依赖，只用标准库）

设计目标：让 AI 一条命令完成全部安装工作，学员不需要手动解压、不需要找目录、
不需要复制文件、不需要知道 .workbuddy 在哪。

用法（AI 执行，任意 Python 3.6+ 均可，只用标准库）：
    python install.py --zip <技能包路径.zip>      # 从 zip 安装
    python install.py --source <已解压目录>        # 从目录安装
    python install.py --zip <x.zip> --dest <目录>  # 指定技能目录
    python install.py --verify                    # 只做体检，不改文件

行为保证：
  · 幂等：装过了再装一次不会出错，旧的自动备份到 .backup/ 时间戳目录
  · 自检：装完自动校验 SKILL.md 五字段、脚本语法、硬路径、目录层级
  · 回滚：安装过程出错自动还原，不会留半成品
  · 干净：自动剔除 __pycache__ / *.pyc / out/ 等垃圾
  · 环境体检：装完实测 python 依赖 / ffmpeg / 字体，直接给出「该用哪条命令跑」
  · 冒烟测试：装完真跑一次各脚本的 --help，确认不是一装就残的壳
"""
import argparse
import ast
import datetime
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import json
import urllib.request

SKILL_NAME = "wmy-auto-clip"
REQUIRED_FIELDS = ["name", "display_name", "display_name_en", "description_zh", "description_en"]
EXCLUDE_NAMES = {"__pycache__", "out", ".backup", ".git", ".idea", ".vscode", ".DS_Store"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".tmp", ".zip", ".swp", ".DS_Store")
# 技能包里绝不能出现的个人痕迹
LEAK_PATTERNS = [
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"'<>|]+"), "绝对用户路径"),
    (re.compile(r"/Users/[A-Za-z0-9._-]+/"), "macOS 用户家目录"),
    (re.compile(r"/home/[A-Za-z0-9._-]+/"), "Linux 用户家目录"),
]
LEAK_EXT = (".py", ".md", ".txt", ".json", ".sh", ".bat", ".ps1", ".cfg", ".ini", ".yaml", ".yml", ".toml", ".html")

# 跑起来需要的第三方依赖：模块名 → 说明
NEED_MODULES = [
    ("PIL", "Pillow（画动效卡片）"),
    ("imageio_ffmpeg", "imageio-ffmpeg（兜底提供 ffmpeg）"),
    ("faster_whisper", "faster-whisper（语音转文字）"),
]
# 可选依赖：缺了不影响出片，只是少一道保险
OPT_MODULES = [
    ("opencc", "opencc（繁体转简体）"),
    ("rapidocr_onnxruntime", "rapidocr（画面文字识别，防双字幕）"),
]

# ── 输出层：Windows 中文控制台是 GBK，打勾/叉号/· 这些符号编不进去会直接崩 ──
_SYM = {
    "\u2713": "[OK]", "\u2717": "[X]", "\u26a0": "[!]",
    "\u2705": "[OK]", "\u274c": "[X]", "\u00b7": "-", "\u2192": "->",
    "\u26a1": "[!]",
}


def _reconfigure_std():
    """尽量把标准输出切到 UTF-8（Python 3.7+）。
    切不了也没关系 —— safe() 会把编不出来的符号降级成 ASCII。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _enc_ok(ch):
    """当前 stdout 编码能不能输出这个字符"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        ch.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def safe(msg):
    """把不能被当前终端编码的字符降级成 ASCII，保证任何环境都不崩"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    out = []
    for ch in str(msg):
        if _enc_ok(ch):
            out.append(ch)
        else:
            out.append(_SYM.get(ch, "?"))
    return "".join(out)


def log(msg):
    try:
        print(safe(msg), flush=True)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def run_capture(cmd, timeout=30):
    """跑个命令拿输出，失败不抛异常"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           text=True, encoding="utf-8", errors="ignore")
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as e:
        return -1, "", str(e)


def default_dest():
    """默认技能安装目录：~/.workbuddy/skills（WorkBuddy 各平台一致）"""
    home = os.path.expanduser("~")
    return os.path.join(home, ".workbuddy", "skills")


def find_skill_root(base):
    """在 base 里找到含 SKILL.md 的那层目录（容忍多套一层目录）"""
    if not base or not os.path.isdir(base):
        return None
    # 1) 优先同名目录
    cand = os.path.join(base, SKILL_NAME)
    if os.path.isfile(os.path.join(cand, "SKILL.md")):
        return cand
    # 2) base 本身就是
    if os.path.isfile(os.path.join(base, "SKILL.md")):
        return base
    # 3) 广度搜索一层，同名优先
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    hits = []
    for name in names:
        p = os.path.join(base, name)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "SKILL.md")):
            hits.append(p)
    if not hits:
        return None
    for h in hits:
        if os.path.basename(h) == SKILL_NAME:
            return h
    return hits[0]


def read_frontmatter(sk_md):
    """解析 SKILL.md 头部的 YAML frontmatter，返回 (字段dict, 正文)"""
    with open(sk_md, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}, text
    block = m.group(1)
    data = {}
    key = None
    for line in block.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if km:
            key = km.group(1)
            val = km.group(2).strip()
            if val.startswith("|") or val.startswith(">"):
                data[key] = ""
            else:
                data[key] = val.strip("\"'")
        elif key:
            data[key] = (data.get(key, "") + " " + line.strip()).strip()
    return data, text


def should_skip(name):
    if name in EXCLUDE_NAMES:
        return True
    if name.endswith(EXCLUDE_SUFFIX):
        return True
    return False


def copy_clean(src, dst):
    """只复制必要文件，跳过垃圾。保持原目录结构。"""
    src = os.path.abspath(src)
    n = 0
    for dp, dn, fn in os.walk(src):
        dn[:] = [d for d in dn if not should_skip(d)]
        rel = os.path.relpath(dp, src)
        rel = "" if rel == "." else rel
        for f in fn:
            if should_skip(f):
                continue
            s = os.path.join(dp, f)
            d = os.path.join(dst, rel, f) if rel else os.path.join(dst, f)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            n += 1
    return n


def safe_extract(zip_path, dest):
    """安全解压：拒绝 Zip Slip（防止恶意包把文件写到目标目录之外）"""
    dest = os.path.abspath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = os.path.abspath(os.path.join(dest, info.filename))
            if not (target == dest or target.startswith(dest + os.sep)):
                raise RuntimeError("压缩包内含非法路径，已拒绝安装：%s" % info.filename)
        z.extractall(dest)


def verify(skill_dir):
    """自检：返回 (是否通过, 报告列表)"""
    rep = []
    ok = True
    sk = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(sk):
        return False, ["[X] 缺少 SKILL.md"]
    rep.append("[OK] SKILL.md 存在")

    fm, body = read_frontmatter(sk)
    missing = [k for k in REQUIRED_FIELDS if not fm.get(k)]
    if missing:
        ok = False
        rep.append("[X] frontmatter 缺字段：" + ", ".join(missing))
    else:
        rep.append("[OK] frontmatter 五字段齐全（name=%s）" % fm.get("name"))
    if fm.get("name") and fm["name"] != SKILL_NAME:
        rep.append("[!] frontmatter name=%s 与预期 %s 不同" % (fm["name"], SKILL_NAME))

    # 目录层级 ≤ 2（根目录算第 0 层，scripts/references 下的文件算第 2 层）
    deepest = 0
    for dp, dn, fn in os.walk(skill_dir):
        r = os.path.relpath(dp, skill_dir)
        depth = 0 if r == "." else r.count(os.sep) + 1
        deepest = max(deepest, depth)
    if deepest > 2:
        ok = False
        rep.append("[X] 目录层级 %d 超过 2" % deepest)
    else:
        rep.append("[OK] 目录层级 %d（<=2）" % deepest)

    # 脚本语法
    scripts = []
    for dp, dn, fn in os.walk(skill_dir):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith(".py"):
                scripts.append(os.path.join(dp, f))
    bad = 0
    for sp in scripts:
        try:
            with open(sp, encoding="utf-8") as f:
                ast.parse(f.read())
        except SyntaxError as e:
            ok = False
            bad += 1
            rep.append("[X] 语法错误 %s：%s" % (os.path.relpath(sp, skill_dir), e))
    if scripts:
        rep.append("[OK] %d 个脚本语法校验通过" % len(scripts))

    # 硬路径泄漏 —— 检出即判定安装失败（带出去就是隐私事故，不能只提示）
    leaks = []
    for dp, dn, fn in os.walk(skill_dir):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if not f.endswith(LEAK_EXT):
                continue
            try:
                with open(os.path.join(dp, f), encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read()
            except OSError:
                continue
            for pat, desc in LEAK_PATTERNS:
                if pat.search(txt):
                    leaks.append("%s（%s）" % (f, desc))
    if leaks:
        ok = False
        rep.append("[X] 隐私泄漏 %d 处，安装已阻断：%s" % (len(leaks), "; ".join(leaks[:6])))
    else:
        rep.append("[OK] 无个人路径泄漏")

    # 关键文件（缺任何一个都算安装不完整——效果脚本是核心；BGM 改为首次安装时联网下载，
    # 不在这里强制要求，否则没网/没拉到就判安装失败，反而把能用的技能回滚掉）
    MUST_HAVE = [
        "SKILL.md",
        "scripts/autoclip.py",
        "scripts/enhance.py",
        "scripts/effects.py",
        "scripts/fx_engine.py",
        "references/动效库配置说明.md",
        "references/effects.example.json",
    ]
    for must in MUST_HAVE[1:]:
        p = os.path.join(skill_dir, must.replace("/", os.sep))
        if os.path.isfile(p):
            rep.append("[OK] %s 就位" % must)
        else:
            ok = False
            rep.append("[X] 缺少 %s" % must)

    # 垃圾文件
    junk = []
    for dp, dn, fn in os.walk(skill_dir):
        for f in fn:
            if f.endswith((".pyc", ".pyo")):
                junk.append(f)
    if junk:
        rep.append("[!] 残留 %d 个 .pyc（不影响使用）" % len(junk))

    return ok, rep


# ───────────────── BGM 按需下载 ─────────────────
def _url_get(url, timeout=60, as_json=False):
    """联网取数据，自动读环境代理（学员机多半无代理，直连）。返回 bytes 或 dict。"""
    proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    handlers = {}
    if proxy:
        handlers["https"] = proxy
        handlers["http"] = proxy
    op = (urllib.request.build_opener(urllib.request.ProxyHandler(handlers))
          if handlers else urllib.request.build_opener())
    req = urllib.request.Request(url, headers={
        "User-Agent": "wmy-autoclip-install",
        "Accept": "application/vnd.github+json",
    })
    with op.open(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data) if as_json else data


def download_bgm(skill_dir, files=("bgm_calm.mp3", "bgm.mp3", "bgm_synth.mp3")):
    """首次安装后自动从 GitHub Release 拉取 BGM 到 assets/。

    联网失败只返回 False 并提示，绝不阻断安装——没有 BGM 脚本照样能剪，
    只是不会自动配乐（用户可用 --bgm 自带音乐，或手动从 Release 下载放入 assets/）。
    """
    assets = os.path.join(skill_dir, "assets")
    os.makedirs(assets, exist_ok=True)
    if all(os.path.isfile(os.path.join(assets, f)) for f in files):
        return True
    log("  - 正在获取背景音乐（首次安装需要联网，约 12MB）...")
    try:
        rel = _url_get(
            "https://api.github.com/repos/ywu661266-vicky/wmy-auto-clip/releases/latest",
            as_json=True)
    except Exception as e:
        log("  [!] 无法访问 GitHub 获取 BGM 列表（不影响剪辑）：%s" % e)
        log("      需要配乐时，手动从 Release 下载 3 个 mp3 放进 assets/ 即可")
        return False
    urls = {a.get("name"): a.get("browser_download_url")
            for a in rel.get("assets", []) if a.get("name")}
    ok = True
    for f in files:
        if os.path.isfile(os.path.join(assets, f)):
            continue
        u = urls.get(f)
        if not u:
            ok = False
            continue
        try:
            data = _url_get(u)
            with open(os.path.join(assets, f), "wb") as fh:
                fh.write(data)
            log("  [OK] 已下载 %s" % f)
        except Exception as e:
            ok = False
            log("  [!] 下载 %s 失败（不影响剪辑）：%s" % (f, e))
    if ok:
        log("  - 背景音乐已就位，剪辑会自动配乐")
    else:
        log("  [!] 部分 BGM 未下载（不影响剪辑）；想配乐可手动从 Release 放入 assets/")
    return ok


# ───────────────── 环境体检 ─────────────────
def _candidate_pythons():
    """列出本机可能可用的 python 解释器（WorkBuddy 自带优先）"""
    cands = []
    home = os.path.expanduser("~")
    base = os.path.join(home, ".workbuddy", "binaries", "python", "envs")
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            exe = os.path.join(base, name, "Scripts", "python.exe")
            if os.path.isfile(exe):
                cands.append(exe)
    # PATH 上的 python
    for exe in ("python", "python3"):
        p = shutil.which(exe)
        if p:
            cands.append(p)
    # 去重保序
    seen, out = set(), []
    for c in cands:
        k = os.path.normcase(os.path.abspath(c))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _mods_of(python_exe, mods):
    """问某个 python 有哪些模块，返回 dict"""
    code = (
        "import json,sys\n"
        "r={'ver':sys.version.split()[0]}\n"
        "for m in %r:\n"
        "    try:\n"
        "        __import__(m); r[m]=1\n"
        "    except Exception:\n"
        "        r[m]=0\n"
        "print(json.dumps(r))\n" % (list(mods),)
    )
    rc, out, err = run_capture([python_exe, "-c", code], timeout=90)
    if rc != 0:
        return None
    lines = [l for l in (out or "").strip().splitlines() if l.strip().startswith("{")]
    if not lines:
        return None
    try:
        import json as _json
        return _json.loads(lines[-1])
    except Exception:
        return None


def probe_env(skill_dir):
    """环境体检：python 依赖 / ffmpeg / 字体。返回 (推荐python, 报告行)"""
    rep = []
    allmods = [m for m, _ in NEED_MODULES] + [m for m, _ in OPT_MODULES]

    # ffmpeg
    ff_ok = False
    ff_src = ""
    for cand in (shutil.which("ffmpeg"),
                 os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "ffmpeg", "ffmpeg.exe"),
                 os.path.join(skill_dir, "scripts", "ffmpeg.exe")):
        if cand and os.path.exists(cand):
            rc, out, err = run_capture([cand, "-version"], timeout=20)
            if rc == 0:
                ff_ok = True
                ff_src = cand
                break
    if ff_ok:
        rep.append("[OK] ffmpeg 可用：%s" % ff_src)
    else:
        rep.append("[!] ffmpeg 未找到（会自动用 imageio-ffmpeg 兜底；两者都没有才会失败）")

    # 中文字体（缺了字会变方块）
    font_ok = False
    for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
              "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simfang.ttf",
              "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc"):
        if os.path.exists(f):
            font_ok = True
            break
    if font_ok:
        rep.append("[OK] 中文字体可用")
    else:
        rep.append("[!] 没找到中文字体，动效卡片的汉字会变方块。"
                   "把任意中文 .ttf 放到 assets/font.ttf 可解决")

    # 自带 BGM（默认行为会自动配乐，缺了学员会以为"怎么没音乐"）
    assets = os.path.join(skill_dir, "assets")
    bgm_found = [f for f in ("bgm_calm.mp3", "bgm.mp3", "bgm_synth.mp3")
                 if os.path.isfile(os.path.join(assets, f))]
    if bgm_found:
        rep.append("[OK] 自带 BGM 就位（%s）" % "、".join(bgm_found))
    else:
        rep.append("[!] 当前没有自带 BGM：脚本会正常出片，但不会自动配乐。"
                   "首次安装会自动下载；若环境无法联网，可从 GitHub Release 手动下载"
                   "3 个 mp3 放进 assets/，或用 --bgm 传你自己的音乐")

    # python 依赖
    best = None
    best_score = -1
    envs = []
    for exe in _candidate_pythons():
        info = _mods_of(exe, allmods)
        if not info:
            continue
        miss_need = [m for m, _ in NEED_MODULES if not info.get(m)]
        score = len(allmods) - len(miss_need)
        envs.append((exe, info, miss_need))
        if score > best_score:
            best_score = score
            best = (exe, info, miss_need)

    if best is None:
        rep.append("[X] 找不到可用的 python 解释器")
        return None, rep

    exe, info, miss_need = best
    have = [m for m, _ in NEED_MODULES if info.get(m)]
    rep.append("[OK] 推荐 python：%s  (Python %s)" % (exe, info.get("ver", "?")))
    rep.append("     已具备：" + (", ".join(have) if have else "（无）"))
    if miss_need:
        rep.append("[!] 缺少：" + ", ".join(miss_need))
        rep.append("     补装命令：\"%s\" -m pip install %s" % (exe, " ".join(miss_need)))
    else:
        rep.append("[OK] 必需依赖齐全")
    miss_opt = [m for m, _ in OPT_MODULES if not info.get(m)]
    if miss_opt:
        rep.append("[!] 可选依赖缺：" + ", ".join(miss_opt) + "（不影响出片）")
    return exe, rep


def smoke_test(skill_dir, python_exe=None):
    """冒烟测试：真跑一次各脚本的 --help，确认装完不是残壳。

    区分两类失败：
      · 致命（安装失败）：脚本自身坏了 —— 语法错、缺同目录模块、参数解析崩
      · 警告（不算失败）：只是当前 python 缺第三方库（PIL/faster_whisper 等），
        那是环境问题，已在环境体检里单独提示，不该把刚装好的技能回滚掉
    返回 (是否致命失败, 报告行)
    """
    rep = []
    fatal = False
    py = python_exe or sys.executable
    scripts_dir = os.path.join(skill_dir, "scripts")

    def is_third_party_missing(txt):
        return ("ModuleNotFoundError" in txt and "No module named" in txt
                and not any(k in txt for k in ("fx_engine", "autoclip")))

    for mod in ("autoclip.py", "enhance.py", "effects.py"):
        p = os.path.join(scripts_dir, mod)
        if not os.path.isfile(p):
            rep.append("[X] %s 不存在，无法冒烟测试" % mod)
            fatal = True
            continue
        rc, out, err = run_capture([py, p, "--help"], timeout=90)
        if rc == 0:
            rep.append("[OK] %s --help 正常" % mod)
        else:
            blob = (err or out or "")
            if is_third_party_missing(blob):
                rep.append("[!] %s 需要第三方库（当前 python 缺依赖，不算安装失败）" % mod)
            else:
                rep.append("[X] %s --help 失败（rc=%s）：%s" % (mod, rc, blob[-160:]))
                fatal = True

    # fx_engine 必须和 effects.py 同目录，否则 effects.py 直接 ImportError
    if os.path.isfile(os.path.join(scripts_dir, "fx_engine.py")):
        code = ("import sys; sys.path.insert(0, %r); import fx_engine; "
                "print('fx_engine OK')" % scripts_dir)
        rc, out, err = run_capture([py, "-c", code], timeout=60)
        if rc == 0:
            rep.append("[OK] fx_engine.py 可被 effects.py 导入")
        else:
            rep.append("[X] fx_engine.py 导入失败：%s" % (err or "")[-160:])
            fatal = True
    else:
        rep.append("[X] 缺 fx_engine.py，effects.py（模式⑤动效卡点）会直接崩")
        fatal = True
    return fatal, rep


def do_install(src_root, dest_root, do_backup=True):
    os.makedirs(dest_root, exist_ok=True)
    target = os.path.join(dest_root, SKILL_NAME)
    backup = None

    if os.path.exists(target):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(dest_root, ".backup", "%s-%s" % (SKILL_NAME, ts))
        os.makedirs(os.path.dirname(bdir), exist_ok=True)
        if do_backup:
            shutil.move(target, bdir)
            backup = bdir
            log("  - 旧版本已备份到：%s" % backup)
        else:
            shutil.rmtree(target, ignore_errors=True)

    try:
        n = copy_clean(src_root, target)
        log("  - 已复制 %d 个文件到：%s" % (n, target))
    except Exception as e:
        log("  [X] 复制失败：%s" % e)
        if backup and os.path.isdir(backup):
            shutil.move(backup, target)
            log("  - 已回滚到原版本")
        return None

    ok, rep = verify(target)
    for r in rep:
        log("    " + r)
    if not ok:
        log("  [X] 自检未通过，自动回滚")
        shutil.rmtree(target, ignore_errors=True)
        if backup and os.path.isdir(backup):
            shutil.move(backup, target)
            log("  - 已回滚到原版本")
        return None
    # 自检通过后，首次安装自动拉取 BGM（联网失败仅提示，不阻断）
    try:
        download_bgm(target)
    except Exception as e:
        log("  [!] BGM 自动下载未成功（不影响剪辑）：%s" % e)
    return target


def main():
    ap = argparse.ArgumentParser(description="AI自动剪辑 技能安装器")
    ap.add_argument("--zip", dest="zip_path", help="技能包 zip 路径")
    ap.add_argument("--source", help="已解压的技能目录")
    ap.add_argument("--dest", default=None, help="技能安装目录（默认 ~/.workbuddy/skills）")
    ap.add_argument("--verify", action="store_true", help="只体检不安装")
    ap.add_argument("--no-backup", action="store_true", help="覆盖时不备份旧版本")
    ap.add_argument("--no-smoke", action="store_true", help="跳过冒烟测试")
    args = ap.parse_args()

    dest = os.path.abspath(args.dest) if args.dest else default_dest()
    log("【AI自动剪辑】技能安装器")
    log("  目标技能目录：%s" % dest)

    # 体检模式
    if args.verify:
        t = os.path.join(dest, SKILL_NAME)
        if not os.path.isdir(t):
            log("  [X] 未检测到已安装的技能")
            return 1
        ok, rep = verify(t)
        for r in rep:
            log("    " + r)
        log("  -- 环境体检 --")
        py, erp = probe_env(t)
        for r in erp:
            log("    " + r)
        if not args.no_smoke:
            log("  -- 冒烟测试 --")
            fatal, srep = smoke_test(t, py)
            for r in srep:
                log("    " + r)
            if fatal:
                ok = False
        log("")
        log("体检结果：%s" % ("通过" if ok else "不通过"))
        return 0 if ok else 1

    # 定位源
    tmpdir = None
    try:
        if args.zip_path:
            zp = os.path.abspath(args.zip_path)
            if not os.path.isfile(zp):
                log("  [X] 找不到 zip：%s" % zp)
                return 1
            tmpdir = tempfile.mkdtemp(prefix="wmy_autoclip_")
            log("  - 解压 %s" % os.path.basename(zp))
            safe_extract(zp, tmpdir)
            src = find_skill_root(tmpdir)
        elif args.source:
            src = find_skill_root(os.path.abspath(args.source))
            if not src:
                log("  [X] 在目录里没找到 SKILL.md，请确认这是完整的技能包")
                return 1
        else:
            # 自己在 zip 里 → 找旁边的 zip 或用自身目录
            here = os.path.dirname(os.path.abspath(__file__))
            parent = os.path.dirname(here)
            try:
                zips = sorted(f for f in os.listdir(parent) if f.lower().endswith(".zip"))
            except OSError:
                zips = []
            # 优先挑名字里带技能名的包，避免装错
            prefer = [z for z in zips if SKILL_NAME in z]
            if prefer:
                zp = os.path.join(parent, prefer[0])
            elif len(zips) == 1:
                zp = os.path.join(parent, zips[0])
            elif len(zips) > 1:
                log("  [!] 发现多个 zip，无法判断用哪个，请用 --zip 指定：")
                for z in zips:
                    log("      %s" % z)
                return 1
            else:
                zp = None
            if zp:
                tmpdir = tempfile.mkdtemp(prefix="wmy_autoclip_")
                log("  - 解压 %s" % os.path.basename(zp))
                safe_extract(zp, tmpdir)
                src = find_skill_root(tmpdir)
            else:
                src = find_skill_root(here)

        if not src:
            log("  [X] 在包里没找到 SKILL.md，请确认这是完整的技能包")
            return 1
        log("  - 技能源目录：%s" % src)

        target = do_install(src, dest, do_backup=not args.no_backup)
        if not target:
            log("")
            log("安装失败（已回滚，系统保持原状）")
            return 1

        # 环境体检 → 拿到能用的 python → 用它做冒烟测试
        log("  -- 环境体检 --")
        py, erp = probe_env(target)
        for r in erp:
            log("    " + r)

        if not args.no_smoke:
            log("  -- 冒烟测试 --")
            fatal, srep = smoke_test(target, py)
            for r in srep:
                log("    " + r)
            if fatal:
                log("  [X] 冒烟测试未通过，回滚")
                shutil.rmtree(target, ignore_errors=True)
                bdir = os.path.join(dest, ".backup")
                if os.path.isdir(bdir):
                    cands = sorted(os.listdir(bdir))
                    if cands:
                        shutil.move(os.path.join(bdir, cands[-1]), target)
                        log("  - 已回滚到原版本")
                return 1

        log("")
        log("安装完成：%s" % target)
        log("")
        log("下一步（请原样转告用户）：")
        if py:
            log("  1. 用这个 python 跑（已装好依赖）：")
            log("       \"%s\" \"%s\\autoclip.py\" 你的视频.mp4" % (py, os.path.join(target, "scripts")))
        else:
            log("  1. 先装依赖：pip install pillow imageio-ffmpeg faster-whisper")
        log("  2. 新开一个对话（技能需在新会话中生效）")
        log("  3. 首次剪视频会自动下载语音模型（约 483MB，一次性，需联网，不消耗积分；想更小可加 --model base 约 145MB）")
        log("  4. 以后直接说：把视频拖进对话框 + 发使用提示词即可")
        return 0
    finally:
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    _reconfigure_std()
    sys.exit(main())
