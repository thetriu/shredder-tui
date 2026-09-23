#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHREDDER  -  secure file & folder eraser TUI  (Termux + Linux)
=================================================================

Setup on Termux (Android):
    pkg install python python-pillow ffmpeg
    termux-setup-storage            # once, to reach /sdcard
    python shredder.py [start_dir]

Setup on Linux (Arch / CachyOS / Debian / etc.):
    sudo pacman -S python python-pillow ffmpeg     # or apt/dnf equivalents
    python shredder.py [start_dir]

Run from anywhere:
    Termux:  cp shredder.py $PREFIX/bin/shredder && chmod +x $PREFIX/bin/shredder
    Linux:   sudo install -m755 shredder.py /usr/local/bin/shredder

Pillow  -> image previews      (optional)
ffmpeg  -> video thumbnails    (optional)

Everything else is pure standard library (no pip installs needed).
Run `python shredder.py --help` for options.
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import random
import re
import select
import shutil
import signal
import stat
import string
import subprocess
import sys
import termios
import threading
import time
import traceback
import tty
import unicodedata

VERSION = "1.0"

# ════════════════════════════════════════════════════════════════════════════
#  THEME  (Claude-style: warm black, stone greys, terracotta orange)
# ════════════════════════════════════════════════════════════════════════════


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


BG = _rgb("#1a1918")       # app background
PANEL = _rgb("#242320")    # bars / panels
PANEL2 = _rgb("#2f2d29")   # cursor row / inputs
LINE = _rgb("#3f3c37")     # borders
TEXT = _rgb("#ece9e1")     # primary text
SUB = _rgb("#b7b2a7")      # secondary text
MUTED = _rgb("#87827a")    # labels
DIM = _rgb("#59564f")      # very quiet
ORANGE = _rgb("#d97757")   # brand accent
ORANGE_HI = _rgb("#f0a184")
ORANGE_LO = _rgb("#a55b40")
TINT = _rgb("#33251f")     # selected row
TINT2 = _rgb("#452e24")    # selected + cursor row
RED = _rgb("#e5604f")      # danger
INK = _rgb("#1a1918")      # dark text on orange

# ════════════════════════════════════════════════════════════════════════════
#  TEXT / WIDTH HELPERS
# ════════════════════════════════════════════════════════════════════════════

_wc = {}


def cw(ch):
    o = ord(ch)
    if o < 0x300:
        return 1 if o >= 32 and o != 127 else 0
    r = _wc.get(ch)
    if r is None:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf", "Cc"):
            r = 0
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            r = 2
        else:
            r = 1
        _wc[ch] = r
    return r


def swidth(s):
    return sum(cw(c) for c in s)


def fit(s, w, ell="…"):
    if w <= 0:
        return ""
    if swidth(s) <= w:
        return s
    out, cur = [], 0
    for c in s:
        k = cw(c)
        if cur + k > w - 1:
            break
        out.append(c)
        cur += k
    return "".join(out) + ell


def fit_left(s, w, ell="…"):
    """Truncate from the left (for paths)."""
    if swidth(s) <= w:
        return s
    out, cur = [], 0
    for c in reversed(s):
        k = cw(c)
        if cur + k > w - 1:
            break
        out.append(c)
        cur += k
    return ell + "".join(reversed(out))


def human(n):
    n = float(n)
    if n < 1024:
        return f"{int(n)}B"
    for u in ("K", "M", "G", "T"):
        n /= 1024
        if n < 1024 or u == "T":
            return f"{n:.1f}{u}" if n < 100 else f"{n:.0f}{u}"
    return "?"


def fmt_ts(ts, style=2):
    try:
        t = time.localtime(ts)
    except (OverflowError, OSError, ValueError):
        return "?"
    if style == 2:
        return time.strftime("%Y-%m-%d %H:%M", t)
    if style == 1:
        return time.strftime("%y-%m-%d", t)
    return ""


def fmt_dur(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS  (cell grid + diff renderer, truecolor)
# ════════════════════════════════════════════════════════════════════════════


class Canvas:
    def __init__(self, w, h, bg=BG):
        self.w, self.h = w, h
        self.rows = [[(" ", TEXT, bg, False)] * w for _ in range(h)]

    def put(self, y, x, text, fg=TEXT, bg=None, bold=False, limit=None):
        if y < 0 or y >= self.h:
            return x
        lim = self.w if limit is None else min(limit, self.w)
        row = self.rows[y]
        for ch in text:
            k = cw(ch)
            if k == 0:
                continue
            if x + k > lim:
                break
            if x >= 0:
                if row[x][0] == "" and x > 0:      # overwriting right half of wide char
                    row[x - 1] = (" ",) + row[x - 1][1:]
                b = bg if bg is not None else row[x][2]
                row[x] = (ch, fg, b, bold)
                if k == 2:
                    row[x + 1] = ("", fg, b, bold)
            x += k
        return x

    def fill(self, y, x, w, h, bg):
        for yy in range(max(0, y), min(self.h, y + h)):
            row = self.rows[yy]
            for xx in range(max(0, x), min(self.w, x + w)):
                row[xx] = (" ", TEXT, bg, False)

    def box(self, y, x, w, h, title="", border=LINE, bg=PANEL, tfg=ORANGE, right=""):
        if w < 4 or h < 3:
            return
        self.fill(y, x, w, h, bg)
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", border, bg)
        for yy in range(y + 1, y + h - 1):
            self.put(yy, x, "│", border, bg)
            self.put(yy, x + w - 1, "│", border, bg)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", border, bg)
        if title:
            self.put(y, x + 2, " " + fit(title, w - 6) + " ", tfg, bg, bold=True)
        if right:
            r = " " + fit(right, max(1, w - 8)) + " "
            self.put(y, x + w - 2 - swidth(r), r, SUB, bg)

    def dim(self, k=0.42):
        for row in self.rows:
            for i, (ch, fg, bg, b) in enumerate(row):
                row[i] = (ch, tuple(int(c * k) for c in fg),
                          tuple(int(c * k) for c in bg), False)

    def center(self, y, x, w, text, fg=TEXT, bg=None, bold=False):
        tw = swidth(text)
        self.put(y, x + max(0, (w - tw) // 2), text, fg, bg, bold)


def render_diff(cv, prev):
    out = []
    for y, row in enumerate(cv.rows):
        t = tuple(row)
        if prev is not None and y < len(prev) and prev[y] == t:
            continue
        out.append(f"\x1b[{y + 1};1H")
        cur = None
        for ch, fg, bg, b in row:
            if ch == "":
                continue
            key = (fg, bg, b)
            if key != cur:
                out.append("\x1b[0;%s38;2;%d;%d;%d;48;2;%d;%d;%dm" %
                           ((("1;" if b else ""),) + fg + bg))
                cur = key
            out.append(ch)
    out.append("\x1b[0m")
    return "".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  INPUT PARSING
# ════════════════════════════════════════════════════════════════════════════

CSI = re.compile(rb"\x1b\[(<?)([0-9;]*)([@-~])")
TILDE = {"1": "home", "7": "home", "3": "delete", "4": "end", "8": "end",
         "5": "pgup", "6": "pgdn"}
LETTER = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home",
          "F": "end", "Z": "shift-tab"}


def parse_keys(buf):
    keys, i, n = [], 0, len(buf)
    while i < n:
        b = buf[i]
        if b == 0x1B:
            if i + 1 >= n:
                keys.append("esc")
                i += 1
                continue
            c = buf[i + 1]
            if c == 0x5B:
                m = CSI.match(buf, i)
                if not m:
                    if n - i < 32:
                        return keys, buf[i:]
                    i += 1
                    continue
                pre, params, fin = m.group(1), m.group(2).decode(), m.group(3).decode()
                i = m.end()
                if pre == b"<":
                    p = params.split(";")
                    if len(p) == 3 and fin in "Mm":
                        try:
                            keys.append(("mouse", int(p[0]), int(p[1]), int(p[2]), fin == "M"))
                        except ValueError:
                            pass
                elif fin == "~":
                    keys.append(TILDE.get(params.split(";")[0], "?"))
                else:
                    keys.append(LETTER.get(fin, "?"))
                continue
            if c == 0x4F:
                if i + 2 >= n:
                    return keys, buf[i:]
                keys.append(LETTER.get(chr(buf[i + 2]), "?"))
                i += 3
                continue
            keys.append("esc")
            i += 1
            continue
        if b < 0x80:
            if b in (0x0D, 0x0A):
                keys.append("enter")
            elif b == 0x09:
                keys.append("tab")
            elif b in (0x7F, 0x08):
                keys.append("backspace")
            elif b == 0x03:
                keys.append("ctrl-c")
            elif b < 0x20:
                keys.append("ctrl-" + chr(b + 96))
            else:
                keys.append(chr(b))
            i += 1
        else:
            ln = 2 if b >> 5 == 6 else 3 if b >> 4 == 14 else 4 if b >> 3 == 30 else 1
            if i + ln > n:
                return keys, buf[i:]
            keys.append(buf[i:i + ln].decode("utf-8", "ignore") or "?")
            i += ln
    return keys, b""


# ════════════════════════════════════════════════════════════════════════════
#  FILESYSTEM MODEL
# ════════════════════════════════════════════════════════════════════════════

EXT = {}
for _k, _v in {
    "image": "jpg jpeg png gif webp bmp tif tiff heic heif avif ico jfif",
    "video": "mp4 mkv mov avi webm 3gp m4v flv wmv mpg mpeg ts mts",
    "audio": "mp3 flac wav ogg m4a aac opus wma amr",
    "archive": "zip rar 7z tar gz bz2 xz zst apk",
    "doc": "txt md pdf doc docx xls xlsx ppt pptx csv json xml html log py sh js ini conf",
}.items():
    for _e in _v.split():
        EXT[_e] = _k

ICON = {"dir": "▸", "image": "◈", "video": "▶", "audio": "♪",
        "archive": "▣", "doc": "≡", "file": "·"}


def kind_of(name):
    if "." not in name:
        return "file"
    return EXT.get(name.rsplit(".", 1)[-1].lower(), "file")


class Entry:
    __slots__ = ("name", "path", "is_dir", "is_link", "size", "mtime", "kind")

    def __init__(self, name, path, is_dir, is_link, size, mtime):
        self.name, self.path = name, path
        self.is_dir, self.is_link = is_dir, is_link
        self.size, self.mtime = size, mtime
        self.kind = "dir" if is_dir else kind_of(name)


_numre = re.compile(r"(\d+)")


def _nat(s):
    return [int(t) if t.isdigit() else t for t in _numre.split(s.lower())]


def list_dir(path, hidden, sort):
    ents = []
    with os.scandir(path) as it:
        for e in it:
            if not hidden and e.name.startswith("."):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            is_link = stat.S_ISLNK(st.st_mode)
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            ents.append(Entry(e.name, e.path, is_dir, is_link,
                              -1 if is_dir else st.st_size, st.st_mtime))
    if sort == "date":
        ents.sort(key=lambda e: (not e.is_dir, -e.mtime))
    elif sort == "size":
        ents.sort(key=lambda e: (not e.is_dir, -e.size if not e.is_dir else _nat(e.name)))
    else:
        ents.sort(key=lambda e: (not e.is_dir, _nat(e.name)))
    return ents


def prune_nested(paths):
    S = set(paths)
    out = []
    for p in sorted(S):
        d, skip = os.path.dirname(p), False
        while d and d != os.path.dirname(d):
            if d in S:
                skip = True
                break
            d = os.path.dirname(d)
        if not skip:
            out.append(p)
    return out


def _protected_roots():
    home = os.path.expanduser("~")
    roots = {home, os.path.realpath(home)}
    prefix = os.environ.get("PREFIX")
    if prefix:
        roots |= {prefix, os.path.realpath(prefix)}
    for p in ("/sdcard", "/storage/emulated/0", os.path.join(home, "storage"),
              os.path.join(home, "storage", "shared")):
        roots |= {p, os.path.realpath(p)}
    return {r.rstrip("/") or "/" for r in roots}


PROTECTED_EXACT = {"/", "/storage", "/storage/emulated", "/data", "/system",
                   "/sdcard", "/storage/emulated/0", "/usr", "/bin", "/etc"}
PROTECTED_ROOTS = _protected_roots()


def is_protected(path):
    p = os.path.abspath(path)
    if p in PROTECTED_EXACT:
        return True
    for r in PROTECTED_ROOTS:
        if r == p or r.startswith(p.rstrip("/") + "/"):
            return True
    return False


# ── relative / absolute time parsing ────────────────────────────────────────

_REL = re.compile(r"^\s*(\d+)\s*(min|h|d|w|mo|y)\s*(?:ago)?\s*$", re.I)
_UNIT = {"min": 60, "h": 3600, "d": 86400, "w": 604800,
         "mo": 2592000, "y": 31557600}
_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M",
            "%Y/%m/%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"]


def parse_when(s):
    s = s.strip().lower().replace("t", " ", 1) if re.match(r"^\d{4}-\d\d-\d\dt", s.strip().lower()) else s.strip().lower()
    if not s:
        return None
    now = time.time()
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if s == "now":
        return now
    if s == "today":
        return today.timestamp()
    if s == "yesterday":
        return (today - datetime.timedelta(days=1)).timestamp()
    m = _REL.match(s)
    if m:
        return now - int(m.group(1)) * _UNIT[m.group(2).lower()]
    for f in _FORMATS:
        try:
            return datetime.datetime.strptime(s, f).timestamp()
        except ValueError:
            pass
    return None


# ════════════════════════════════════════════════════════════════════════════
#  SHRED ENGINE
# ════════════════════════════════════════════════════════════════════════════

METHODS = [
    ("Quick", "1 pass · random", [None]),
    ("Standard", "3 pass · 00 / FF / random", [0x00, 0xFF, None]),
    ("Paranoid", "7 pass · alternating + random", [0x00, 0xFF, None, 0x00, 0xFF, None, None]),
]
CHUNK = 1 << 20


class Aborted(Exception):
    pass


class Plan:
    def __init__(self):
        self.files = []       # (path, size)
        self.dirs = []        # post-order (children first)
        self.nbytes = 0


def build_plan(paths):
    plan = Plan()
    for root in paths:
        try:
            st = os.lstat(root)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            sz = st.st_size if stat.S_ISREG(st.st_mode) else 0
            plan.files.append((root, sz))
            plan.nbytes += sz
            continue
        stack = [(root, False)]
        while stack:
            p, done = stack.pop()
            if done:
                plan.dirs.append(p)
                continue
            stack.append((p, True))
            try:
                with os.scandir(p) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append((e.path, False))
                            else:
                                s = e.stat(follow_symlinks=False)
                                sz = s.st_size if stat.S_ISREG(s.st_mode) else 0
                                plan.files.append((e.path, sz))
                                plan.nbytes += sz
                        except OSError:
                            continue
            except OSError:
                continue
    return plan


def _rand_name():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def shred_file(path, patterns, job):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):           # symlink / fifo / socket: just unlink, never follow
        os.unlink(path)
        return
    size = st.st_size
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if size > 0:
        with open(path, "r+b", buffering=0) as f:
            for i, pat in enumerate(patterns):
                job.cur_pass = i + 1
                f.seek(0)
                left = size
                blk = None if pat is None else bytes([pat]) * CHUNK
                while left > 0:
                    if job.abort.is_set():
                        raise Aborted()
                    n = min(CHUNK, left)
                    data = os.urandom(n) if blk is None else (blk if n == CHUNK else blk[:n])
                    mv = memoryview(data)
                    while len(mv):
                        w = f.write(mv)
                        mv = mv[w:]
                    left -= n
                    job.done += n
                f.flush()
                os.fsync(f.fileno())
            f.truncate(0)
            os.fsync(f.fileno())
    d, cur = os.path.dirname(path), path
    for _ in range(3):                          # scramble the file name
        new = os.path.join(d, _rand_name())
        try:
            os.rename(cur, new)
            cur = new
        except OSError:
            break
    os.unlink(cur)
    _fsync_dir(d)


class Job:
    def __init__(self, app, plan, method, items):
        self.app, self.plan, self.items = app, plan, items
        self.name, _, self.patterns = METHODS[method]
        self.abort = threading.Event()
        self.finished = False
        self.total = max(1, plan.nbytes * len(self.patterns))
        self.done = 0
        self.cur = ""
        self.cur_pass = 0
        self.nfiles = 0
        self.ok_files = 0
        self.ok_bytes = 0
        self.errors = []
        self.aborted = False
        self.t0 = time.time()
        self.t1 = None

    def frac(self):
        if self.plan.nbytes > 0:
            return min(1.0, self.done / self.total)
        tot = len(self.plan.files) + len(self.plan.dirs)
        return min(1.0, (self.nfiles / tot) if tot else 1.0)

    def run(self):
        try:
            for path, size in self.plan.files:
                if self.abort.is_set():
                    break
                self.cur, self.cur_pass = path, 0
                base = self.done
                try:
                    shred_file(path, self.patterns, self)
                    self.ok_files += 1
                    self.ok_bytes += size
                except Aborted:
                    self.aborted = True
                    self.errors.append((path, "aborted mid-file (partially overwritten)"))
                    break
                except OSError as e:
                    self.errors.append((path, e.strerror or str(e)))
                self.done = base + size * len(self.patterns)
                self.nfiles += 1
            if not self.abort.is_set():
                for d in self.plan.dirs:
                    self.cur = d
                    try:
                        new = os.path.join(os.path.dirname(d), _rand_name())
                        try:
                            os.rename(d, new)
                            d = new
                        except OSError:
                            pass
                        os.rmdir(d)
                    except OSError as e:
                        self.errors.append((d, e.strerror or str(e)))
                    self.nfiles += 1
            else:
                self.aborted = True
        except Exception as e:  # noqa
            self.errors.append(("internal", repr(e)))
        finally:
            self.t1 = time.time()
            self.finished = True
            self.app.wake()


# ════════════════════════════════════════════════════════════════════════════
#  PREVIEW ENGINE  (images via Pillow, videos via ffmpeg, half-block art)
# ════════════════════════════════════════════════════════════════════════════

try:
    from PIL import Image, ImageOps  # type: ignore
    HAVE_PIL = True
    _RES = getattr(Image, "Resampling", Image).LANCZOS
except Exception:  # noqa
    HAVE_PIL = False
HAVE_FFMPEG = bool(shutil.which("ffmpeg"))
HAVE_FFPROBE = bool(shutil.which("ffprobe"))


class Preview:
    __slots__ = ("cells", "meta", "lines", "err")

    def __init__(self):
        self.cells = None     # list of rows of (ch, fg, bg)
        self.meta = []        # [(label, value)]
        self.lines = None     # [(text, color)]
        self.err = None


def img_to_cells(img, w, h):
    """Render a PIL image as half-block cells fitting w x h cells."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        base = Image.new("RGBA", img.size, PANEL + (255,))
        img = Image.alpha_composite(base, img)
    img = img.convert("RGB")
    iw, ih = img.size
    pw, ph = max(1, w), max(2, h * 2)
    sc = min(pw / iw, ph / ih)
    nw = max(1, min(pw, int(round(iw * sc))))
    nh = max(2, min(ph, (int(round(ih * sc)) // 2) * 2))
    img = img.resize((nw, nh), _RES)
    px = img.load()
    rows = []
    for y in range(0, nh, 2):
        rows.append([("▀", px[x, y], px[x, y + 1]) for x in range(nw)])
    return rows


def ffprobe_info(path):
    if not HAVE_FFPROBE:
        return {}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height", "-i", path],
            capture_output=True, timeout=15)
        j = json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
        info = {}
        try:
            info["duration"] = float(j.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            pass
        for s in j.get("streams", []):
            if s.get("codec_type") == "video":
                info["w"], info["h"], info["codec"] = s.get("width"), s.get("height"), s.get("codec_name")
                break
        return info
    except Exception:  # noqa
        return {}


def ffmpeg_frame(path, t=0.0, width=480):
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=25)
    return r.stdout if r.stdout else None


def dir_stats(path, budget=0.7):
    t0 = time.time()
    total, files, dirs = 0, 0, 0
    complete = True
    stack = [path]
    while stack:
        if time.time() - t0 > budget:
            complete = False
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs += 1
                            stack.append(e.path)
                        else:
                            files += 1
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total, files, dirs, complete


def build_preview(path, kind, w, h, is_dir):
    pv = Preview()
    try:
        st = os.lstat(path)
    except OSError as e:
        pv.err = e.strerror or str(e)
        return pv
    meta = [("Name", os.path.basename(path))]
    info = None
    size_txt = human(st.st_size) if not is_dir else None
    try:
        if is_dir:
            lines, top = [], 0
            try:
                with os.scandir(path) as it:
                    names = sorted(((e.name, e.is_dir()) for e in it),
                                   key=lambda t: (not t[1], t[0].lower()))
            except OSError as e:
                names = []
                pv.err = e.strerror or str(e)
            top = len(names)
            for n, d in names[:80]:
                lines.append((("▸ " if d else "  ") + n, ORANGE_HI if d else TEXT))
            if not names and not pv.err:
                lines.append(("(empty)", MUTED))
            pv.lines = lines
            total, files, dirs, complete = dir_stats(path)
            info = f"{dirs} folders · {files} files" + ("" if complete else "+")
            size_txt = ("" if complete else "≥") + human(total)
        elif kind == "image":
            if HAVE_PIL:
                try:
                    im = Image.open(path)
                    fmt, dims = im.format or "?", im.size
                    info = f"{dims[0]}×{dims[1]} {fmt}"
                    try:
                        im.draft("RGB", (w * 4, h * 8))
                    except Exception:  # noqa
                        pass
                    try:
                        im = ImageOps.exif_transpose(im)
                    except Exception:  # noqa
                        pass
                    pv.cells = img_to_cells(im, w, h)
                except Exception as e:  # noqa
                    if HAVE_FFMPEG:
                        data = ffmpeg_frame(path, 0)
                        if data:
                            pv.cells = img_to_cells(Image.open(io.BytesIO(data)), w, h)
                    if pv.cells is None:
                        pv.err = "Can't decode image: " + str(e)[:60]
            elif HAVE_FFMPEG:
                pv.err = "Install Pillow for previews:  pkg install python-pillow"
            else:
                pv.err = "Install Pillow for previews:  pkg install python-pillow"
        elif kind == "video":
            vi = ffprobe_info(path)
            bits = []
            if vi.get("w"):
                bits.append(f"{vi['w']}×{vi['h']}")
            if vi.get("codec"):
                bits.append(vi["codec"])
            if vi.get("duration"):
                bits.append(fmt_dur(vi["duration"]))
            info = " · ".join(bits) or None
            if not HAVE_FFMPEG:
                pv.err = "Install ffmpeg for thumbnails:  pkg install ffmpeg"
            elif not HAVE_PIL:
                pv.err = "Install Pillow for thumbnails:  pkg install python-pillow"
            else:
                t = (vi.get("duration") or 0) * 0.1
                data = None
                try:
                    data = ffmpeg_frame(path, t)
                    if not data and t > 0:
                        data = ffmpeg_frame(path, 0)
                except subprocess.TimeoutExpired:
                    pass
                if data:
                    pv.cells = img_to_cells(Image.open(io.BytesIO(data)), w, h)
                else:
                    pv.err = "Couldn't extract a thumbnail"
        else:
            if kind == "audio":
                info = "audio file"
            elif kind == "archive":
                info = "archive"
            if st.st_size <= 4 * 1024 * 1024 and stat.S_ISREG(st.st_mode):
                with open(path, "rb") as f:
                    chunk = f.read(4096)
                if chunk and b"\0" not in chunk:
                    txt = chunk.decode("utf-8", "replace").replace("\t", "    ")
                    pv.lines = [(ln.replace("\r", ""), SUB) for ln in txt.split("\n")[:60]]
                    info = info or "text"
            if pv.lines is None:
                pv.err = "No preview available"
    except Exception as e:  # noqa
        pv.err = str(e)[:80]
    if info:
        meta.append(("Info", info))
    if size_txt:
        meta.append(("Size", size_txt))
    meta.append(("Modified", fmt_ts(st.st_mtime, 2)))
    meta.append(("Mode", stat.filemode(st.st_mode)))
    pv.meta = meta
    return pv


class PreviewWorker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.cv = threading.Condition()
        self.req = None

    def request(self, key, args):
        with self.cv:
            self.req = (key, args)
            self.cv.notify()

    def run(self):
        while True:
            with self.cv:
                while self.req is None:
                    self.cv.wait()
                key, args = self.req
                self.req = None
            time.sleep(0.07)                 # debounce fast scrolling
            with self.cv:
                if self.req is not None:
                    continue                 # a newer request arrived; skip this one
            res = build_preview(*args)
            cache = self.app.cache
            if len(cache) > 40:
                cache.pop(next(iter(cache)))
            cache[key] = res
            self.app.wake()


# ════════════════════════════════════════════════════════════════════════════
#  MODALS
# ════════════════════════════════════════════════════════════════════════════

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spinner():
    return SPIN[int(time.time() * 10) % len(SPIN)]


class Modal:
    def __init__(self, app):
        self.app = app

    def close(self):
        self.app.modal = None

    def key(self, k):
        pass

    def draw(self, cv):
        pass

    def frame(self, cv, w, h, title, border=ORANGE):
        W, H = cv.w, cv.h
        w, h = min(w, W - 2), min(h, H - 2)
        x, y = (W - w) // 2, max(0, (H - h) // 2)
        cv.box(y, x, w, h, title, border=border, bg=PANEL, tfg=ORANGE)
        return y, x, w, h

    @staticmethod
    def field(cv, y, x, w, text, focus):
        cv.fill(y, x, w, 1, PANEL2)
        vis = fit_left(text, w - 2, "…")
        ex = cv.put(y, x + 1, vis, TEXT, PANEL2)
        if focus:
            cv.put(y, min(ex, x + w - 1), "▌", ORANGE, PANEL2)

    @staticmethod
    def hint(cv, y, x, w, pairs):
        parts = []
        for k, v in pairs:
            parts.append((k, v))
        total = sum(swidth(k) + swidth(v) + 3 for k, v in parts) - 2
        cx = x + max(1, (w - total) // 2)
        for k, v in parts:
            cx = cv.put(y, cx, k, ORANGE, PANEL, bold=True)
            cx = cv.put(y, cx, " " + v + "  ", MUTED, PANEL)


class HelpModal(Modal):
    LINES = [
        ("h", "NAVIGATE"),
        ("↑ ↓  j k", "move"), ("→ l ⏎", "open folder / view file"), ("← h ⌫", "parent folder"),
        ("g  G", "top / bottom"), (":", "go to path (Tab completes)"), ("~  0", "home / shared storage"),
        ("h", "SELECT"),
        ("␣", "toggle + move down"), ("a  i  c", "all / invert / clear"),
        ("t", "select by date (before / after)"),
        ("/", "word lookup: search names, select, shred"),
        ("h", "VIEW"),
        ("v", "full-screen preview  (← → browse)"), ("p", "toggle preview panel"),
        ("s  .", "sort  /  hidden files"),
        ("h", "SHRED"),
        ("x  Del", "shred selection (or item under cursor)"),
        ("m", "cycle method: quick / standard / paranoid"),
        ("h", "TOUCH"),
        ("tap", "move cursor · tap left edge selects"),
        ("2× tap", "open folder / preview file"),
        ("h", "NOTE"),
        ("", "On flash storage (phones) overwriting is"),
        ("", "best-effort. Full-disk encryption is what"),
        ("", "truly protects deleted data."),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.off = 0

    def key(self, k):
        if k in ("up", "k"):
            self.off = max(0, self.off - 1)
        elif k in ("down", "j"):
            self.off += 1
        elif k in ("pgup",):
            self.off = max(0, self.off - 8)
        elif k in ("pgdn",):
            self.off += 8
        else:
            self.close()

    def draw(self, cv):
        n = len(self.LINES)
        h = min(n + 4, cv.h - 2)
        y, x, w, h = self.frame(cv, 58, h, "HELP")
        vis = h - 4
        self.off = max(0, min(self.off, max(0, n - vis)))
        for i in range(vis):
            idx = self.off + i
            if idx >= n:
                break
            k, v = self.LINES[idx]
            yy = y + 1 + i
            if k == "h":
                cv.put(yy, x + 2, v, ORANGE, PANEL, bold=True)
            else:
                cv.put(yy, x + 3, k, ORANGE_HI, PANEL, bold=True)
                cv.put(yy, x + 15, fit(v, w - 17), TEXT if k else MUTED, PANEL)
        if n > vis:
            cv.put(y + h - 2, x + w - 8, f"{self.off + 1}-{min(n, self.off + vis)}/{n}", DIM, PANEL)
        self.hint(cv, y + h - 2, x, w - 10 if n > vis else w, [("any key", "close")])


class GotoModal(Modal):
    def __init__(self, app):
        super().__init__(app)
        self.text = app.cwd.rstrip("/") + "/"
        self.err = ""

    def complete(self):
        p = os.path.expanduser(self.text)
        d, pre = os.path.split(p)
        d = d or "/"
        try:
            names = [n for n in os.listdir(d) if n.startswith(pre) and os.path.isdir(os.path.join(d, n))]
        except OSError:
            return
        if not names:
            return
        common = os.path.commonprefix(sorted(names))
        new = os.path.join(d, common)
        if len(names) == 1:
            new += "/"
        self.text = new

    def key(self, k):
        if k == "esc" or k == "ctrl-c":
            self.close()
        elif k == "enter":
            p = os.path.abspath(os.path.expanduser(self.text))
            if os.path.isdir(p):
                self.close()
                self.app.load(p)
            else:
                self.err = "Not a folder"
        elif k == "tab":
            self.complete()
        elif k == "backspace":
            self.text = self.text[:-1]
        elif k == "ctrl-u":
            self.text = ""
        elif isinstance(k, str) and len(k) == 1 and k.isprintable():
            self.text += k
            self.err = ""

    def draw(self, cv):
        y, x, w, h = self.frame(cv, 64, 7, "GO TO PATH")
        self.field(cv, y + 2, x + 2, w - 4, self.text, True)
        if self.err:
            cv.put(y + 3, x + 3, self.err, RED, PANEL)
        self.hint(cv, y + h - 2, x, w, [("⏎", "go"), ("Tab", "complete"), ("Esc", "cancel")])


class DateModal(Modal):
    def __init__(self, app):
        super().__init__(app)
        self.mode = 0             # 0 before, 1 after
        self.text = "7d"
        self.scope = 0            # 0 this folder, 1 + subfolders
        self.focus = 1
        self.busy = False

    def key(self, k):
        if self.busy:
            return
        if k in ("esc", "ctrl-c"):
            return self.close()
        if k in ("tab", "down"):
            self.focus = (self.focus + 1) % 3
        elif k in ("shift-tab", "up"):
            self.focus = (self.focus - 1) % 3
        elif k == "enter":
            return self.apply()
        elif self.focus == 1:
            if k == "backspace":
                self.text = self.text[:-1]
            elif k == "ctrl-u":
                self.text = ""
            elif isinstance(k, str) and len(k) == 1 and k.isprintable():
                self.text += k
        else:
            if k in ("left", "right", " ", "h", "l"):
                if self.focus == 0:
                    self.mode ^= 1
                else:
                    self.scope ^= 1

    def apply(self):
        ts = parse_when(self.text)
        if ts is None:
            return
        self.busy = True
        mode, scope, root, hidden = self.mode, self.scope, self.app.cwd, self.app.hidden

        def work():
            res = []
            try:
                if scope == 0:
                    for e in list_dir(root, hidden, "name"):
                        if (mode == 0 and e.mtime < ts) or (mode == 1 and e.mtime >= ts):
                            res.append(e.path)
                else:
                    for dp, dns, fns in os.walk(root, followlinks=False):
                        if not hidden:
                            dns[:] = [d for d in dns if not d.startswith(".")]
                            fns = [f for f in fns if not f.startswith(".")]
                        for fn in fns:
                            p = os.path.join(dp, fn)
                            try:
                                m = os.lstat(p).st_mtime
                            except OSError:
                                continue
                            if (mode == 0 and m < ts) or (mode == 1 and m >= ts):
                                res.append(p)
            except OSError:
                pass
            self.app.sel.update(res)
            self.app.flash(f"Selected {len(res)} item{'s' if len(res) != 1 else ''}  "
                           f"({'before' if mode == 0 else 'after'} {fmt_ts(ts, 2)})")
            self.close()
            self.app.wake()

        threading.Thread(target=work, daemon=True).start()

    def draw(self, cv):
        y, x, w, h = self.frame(cv, 60, 15, "SELECT BY DATE")
        iw = w - 4
        cx = x + 2

        def label(yy, text, focus):
            cv.put(yy, cx, text, ORANGE if focus else MUTED, PANEL, bold=focus)

        # mode toggle
        label(y + 2, "MATCH FILES MODIFIED", self.focus == 0)
        for i, name in enumerate(("Before", "After")):
            on = self.mode == i
            chip = f" {name} "
            xx = cx + i * 10
            cv.put(y + 3, xx, chip, INK if on else SUB, ORANGE if on else PANEL2, bold=on)
        # date
        label(y + 5, "DATE / TIME", self.focus == 1)
        self.field(cv, y + 6, cx, iw, self.text, self.focus == 1)
        ts = parse_when(self.text)
        if ts is None:
            cv.put(y + 7, cx, "✗ can't parse that" if self.text.strip() else "type a date…", RED if self.text.strip() else MUTED, PANEL)
        else:
            ago = time.time() - ts
            when = f"{fmt_dur(abs(ago))} {'ago' if ago > 0 else 'ahead'}" if abs(ago) < 86400 else (
                f"{int(abs(ago) // 86400)} days {'ago' if ago > 0 else 'ahead'}")
            cv.put(y + 7, cx, "→ " + fmt_ts(ts, 2) + f"  ({when})", ORANGE_HI, PANEL)
        cv.put(y + 8, cx, "7d · 12h · 2w · 3mo · yesterday · 2025-01-31", DIM, PANEL, limit=x + w - 2)
        # scope
        label(y + 10, "SCOPE", self.focus == 2)
        for i, name in enumerate(("This folder", "+ Subfolders")):
            on = self.scope == i
            chip = f" {name} "
            xx = cx + (0 if i == 0 else 15)
            cv.put(y + 11, xx, chip, INK if on else SUB, ORANGE if on else PANEL2, bold=on)
        if self.busy:
            cv.put(y + h - 2, x + 3, f"{spinner()} scanning…", ORANGE, PANEL)
        else:
            self.hint(cv, y + h - 2, x, w, [("⏎", "select"), ("Tab", "next"), ("←→", "toggle"), ("Esc", "cancel")])


class SearchModal(Modal):
    """Triggered by '/': recursive name search under cwd, live results,
    select-all-matches -> shred."""

    def __init__(self, app):
        super().__init__(app)
        self.text = ""
        self.results = []          # [(path, is_dir, size, mtime)]
        self.cur = 0
        self.top = 0
        self.scanning = False
        self.done_scanning = False
        self.token = 0
        self.truncated = False
        self.sel = set()

    def key(self, k):
        if k in ("esc", "ctrl-c"):
            return self.close()
        if k == "backspace":
            if self.text:
                self.text = self.text[:-1]
                self.cur = self.top = 0
                self.rescan()
        elif k == "ctrl-u":
            self.text = ""
            self.results = []
        elif k in ("up",):
            self.cur = max(0, self.cur - 1)
        elif k in ("down",):
            self.cur = min(max(0, len(self.results) - 1), self.cur + 1)
        elif k == "pgup":
            self.cur = max(0, self.cur - 10)
        elif k == "pgdn":
            self.cur = min(max(0, len(self.results) - 1), self.cur + 10)
        elif k == " ":
            if self.results:
                p = self.results[self.cur][0]
                if p in self.sel:
                    self.sel.discard(p)
                else:
                    self.sel.add(p)
                self.cur = min(len(self.results) - 1, self.cur + 1)
        elif k == "ctrl-a":
            self.sel = {r[0] for r in self.results}
        elif k == "tab":
            if self.results:
                p, is_dir = self.results[self.cur][0], self.results[self.cur][1]
                self.close()
                self.app.load(p if is_dir else os.path.dirname(p),
                              None if is_dir else os.path.basename(p))
        elif k == "enter":
            items = list(self.sel) if self.sel else ([self.results[self.cur][0]] if self.results else [])
            if items:
                items = prune_nested([p for p in items if os.path.lexists(p)])
                bad = [p for p in items if is_protected(p)]
                if bad:
                    self.app.flash("Refusing to shred protected location", err=True)
                    return
                self.close()
                self.app.modal = ConfirmModal(self.app, items)
        elif isinstance(k, str) and len(k) == 1 and k.isprintable():
            self.text += k
            self.cur = self.top = 0
            self.rescan()

    def rescan(self):
        self.token += 1
        tok = self.token
        needle = self.text.strip().lower()
        self.sel.clear()
        if not needle:
            self.results = []
            self.scanning = False
            self.done_scanning = True
            return
        self.scanning = True
        self.done_scanning = False
        self.truncated = False
        root, hidden = self.app.cwd, self.app.hidden

        def work():
            out = []
            trunc = False
            try:
                for dp, dns, fns in os.walk(root, followlinks=False):
                    if tok != self.token:
                        return
                    if not hidden:
                        dns[:] = [d for d in dns if not d.startswith(".")]
                        fns = [f for f in fns if not f.startswith(".")]
                    for dn in list(dns):
                        if needle in dn.lower():
                            p = os.path.join(dp, dn)
                            try:
                                st = os.lstat(p)
                                out.append((p, True, -1, st.st_mtime))
                            except OSError:
                                pass
                    for fn in fns:
                        if needle in fn.lower():
                            p = os.path.join(dp, fn)
                            try:
                                st = os.lstat(p)
                                out.append((p, False, st.st_size, st.st_mtime))
                            except OSError:
                                pass
                    if len(out) >= 2000:
                        trunc = True
                        break
                    if tok != self.token:
                        return
            except OSError:
                pass
            if tok != self.token:
                return
            out.sort(key=lambda t: (not t[1], _nat(os.path.basename(t[0]))))
            self.results = out
            self.truncated = trunc
            self.scanning = False
            self.done_scanning = True
            self.app.wake()

        threading.Thread(target=work, daemon=True).start()

    def draw(self, cv):
        W, H = cv.w, cv.h
        w = min(76, W - 2)
        h = min(H - 2, 22)
        y, x, w, h = self.frame(cv, w, h, "WORD LOOKUP  ·  search & shred", border=ORANGE)
        cx, iw = x + 2, w - 4
        cv.put(y + 1, cx, "MATCH NAMES CONTAINING", MUTED, PANEL, bold=True)
        self.field(cv, y + 2, cx, iw, self.text or "", True)
        under = fit_left(self.app.cwd, iw)
        cv.put(y + 3, cx, "in  " + under, DIM, PANEL)
        listy, listh = y + 5, h - 8
        cv.put(listy - 1, cx, "─" * iw, LINE, PANEL)
        n = len(self.results)
        if not self.text.strip():
            cv.center(listy + listh // 2, cx, iw, "type to search filenames & folders…", MUTED, PANEL)
        elif self.scanning and n == 0:
            cv.center(listy + listh // 2, cx, iw, f"{spinner()} searching…", ORANGE, PANEL)
        elif n == 0:
            cv.center(listy + listh // 2, cx, iw, "no matches", MUTED, PANEL)
        else:
            if self.cur < self.top:
                self.top = self.cur
            if self.cur >= self.top + listh:
                self.top = self.cur - listh + 1
            self.top = max(0, min(self.top, max(0, n - listh)))
            for i in range(listh):
                idx = self.top + i
                if idx >= n:
                    break
                p, is_dir, size, mtime = self.results[idx]
                iscur = idx == self.cur
                issel = p in self.sel
                bg = (TINT2 if issel else PANEL2) if iscur else (TINT if issel else PANEL)
                yy = listy + i
                if bg != PANEL:
                    cv.fill(yy, cx, iw, 1, bg)
                cv.put(yy, cx, "●" if issel else " ", ORANGE, bg, bold=True)
                rel = os.path.relpath(p, self.app.cwd)
                icon = "▸" if is_dir else ICON.get(kind_of(os.path.basename(p)), "·")
                cv.put(yy, cx + 2, icon, ORANGE if is_dir else MUTED, bg)
                name_w = iw - 4 - 8
                cv.put(yy, cx + 4, fit(rel + ("/" if is_dir else ""), name_w),
                       ORANGE_HI if issel else TEXT, bg, bold=(is_dir or iscur))
                sz = "" if is_dir else human(size)
                cv.put(yy, cx + iw - 7, sz.rjust(7), SUB, bg)
            if self.scanning:
                cv.put(listy + listh, cx, f"{spinner()} still searching…", ORANGE, PANEL)
        status = f"{len(self.sel)} selected" if self.sel else f"{n} match{'es' if n != 1 else ''}"
        if self.truncated:
            status += "  (showing first 2000)"
        cv.put(y + h - 3, cx, status, MUTED if not self.sel else ORANGE_HI, PANEL, bold=bool(self.sel))
        self.hint(cv, y + h - 1, x, w,
                  [("␣", "select"), ("⏎", "shred"), ("Tab", "open"), ("Esc", "cancel")])


class ConfirmModal(Modal):
    def __init__(self, app, items):
        super().__init__(app)
        self.items = items
        self.plan = None
        self.text = ""
        threading.Thread(target=self._plan, daemon=True).start()

    def _plan(self):
        self.plan = build_plan(self.items)
        self.app.wake()

    def key(self, k):
        if k in ("esc", "ctrl-c"):
            return self.close()
        if k == "backspace":
            self.text = self.text[:-1]
        elif k == "enter":
            if self.plan is not None and self.text.strip().lower() == "shred":
                job = Job(self.app, self.plan, self.app.method, self.items)
                threading.Thread(target=job.run, daemon=True).start()
                self.app.modal = ProgressModal(self.app, job)
        elif isinstance(k, str) and len(k) == 1 and k.isprintable() and len(self.text) < 10:
            self.text += k

    def draw(self, cv):
        n = len(self.items)
        show = min(4, n)
        h = 13 + show + (1 if n > show else 0)
        y, x, w, h = self.frame(cv, 62, h, "⚠  CONFIRM SHRED", border=RED)
        cx, iw = x + 3, w - 6
        cv.put(y + 2, cx, "This permanently destroys:", TEXT, PANEL, bold=True)
        yy = y + 3
        for p in self.items[:show]:
            isd = os.path.isdir(p) and not os.path.islink(p)
            cv.put(yy, cx, ("▸ " if isd else "· ") + fit(os.path.basename(p) + ("/" if isd else ""), iw - 2),
                   ORANGE_HI if isd else SUB, PANEL)
            yy += 1
        if n > show:
            cv.put(yy, cx, f"+ {n - show} more", MUTED, PANEL)
            yy += 1
        yy += 1
        if self.plan is None:
            cv.put(yy, cx, f"{spinner()} counting…", ORANGE, PANEL)
        else:
            p = self.plan
            cv.put(yy, cx, f"{len(p.files)} files · {len(p.dirs)} folders · {human(p.nbytes)}", ORANGE, PANEL, bold=True)
        name, desc, _ = METHODS[self.app.method]
        cv.put(yy + 1, cx, f"Method: {name} — {desc}", MUTED, PANEL, limit=x + w - 2)
        cv.put(yy + 3, cx, "Type SHRED to confirm", RED, PANEL, bold=True)
        self.field(cv, yy + 4, cx, iw, self.text, True)
        ok = self.plan is not None and self.text.strip().lower() == "shred"
        self.hint(cv, y + h - 2, x, w, [("⏎", "shred" if ok else "…"), ("Esc", "cancel")])


class ProgressModal(Modal):
    def __init__(self, app, job):
        super().__init__(app)
        self.job = job

    def key(self, k):
        if k in ("esc", "ctrl-c", "x", "q"):
            self.job.abort.set()

    def draw(self, cv):
        j = self.job
        y, x, w, h = self.frame(cv, 64, 12, "SHREDDING " + spinner())
        cx, iw = x + 3, w - 6
        cur = j.cur or "…"
        cv.put(y + 2, cx, fit_left(cur, iw), TEXT, PANEL)
        f = j.frac()
        filled = int(iw * f)
        cv.put(y + 4, cx, "━" * filled, ORANGE, PANEL)
        cv.put(y + 4, cx + filled, "━" * (iw - filled), DIM, PANEL)
        cv.put(y + 5, cx, f"{int(f * 100)}%", ORANGE_HI, PANEL, bold=True)
        cv.put(y + 5, cx + 6, f"pass {j.cur_pass}/{len(j.patterns)}  ·  {j.name}", SUB, PANEL)
        tot = len(j.plan.files) + len(j.plan.dirs)
        cv.put(y + 7, cx, f"{j.nfiles}/{tot} items", SUB, PANEL)
        el = time.time() - j.t0
        rate = (j.done / el) if el > 0.5 else 0
        cv.put(y + 7, cx + 18, f"{human(j.done)}/{human(j.total)}  {human(rate)}/s  {fmt_dur(el)}", MUTED, PANEL)
        if j.errors:
            cv.put(y + 8, cx, f"{len(j.errors)} error(s)", RED, PANEL)
        if j.abort.is_set():
            cv.put(y + h - 2, x + 3, "aborting after current chunk…", RED, PANEL)
        else:
            self.hint(cv, y + h - 2, x, w, [("x / Esc", "abort")])


class ResultModal(Modal):
    def __init__(self, app, job):
        super().__init__(app)
        self.job = job

    def key(self, k):
        self.close()
        self.app.after_job(self.job)

    def draw(self, cv):
        j = self.job
        ne = min(4, len(j.errors))
        y, x, w, h = self.frame(cv, 64, 8 + (ne + 2 if ne else 0),
                                "ABORTED" if j.aborted else "DONE",
                                border=RED if j.errors or j.aborted else ORANGE)
        cx, iw = x + 3, w - 6
        cv.put(y + 2, cx, ("■ Aborted" if j.aborted else "✓ Shredded") +
               f"  {j.ok_files} files · {human(j.ok_bytes)}", ORANGE_HI, PANEL, bold=True)
        cv.put(y + 3, cx, f"Method {j.name} · {fmt_dur((j.t1 or time.time()) - j.t0)}", MUTED, PANEL)
        if j.errors:
            cv.put(y + 5, cx, f"{len(j.errors)} problem(s):", RED, PANEL, bold=True)
            for i, (p, m) in enumerate(j.errors[:ne]):
                cv.put(y + 6 + i, cx, fit(os.path.basename(p) + " — " + m, iw), SUB, PANEL)
        self.hint(cv, y + h - 2, x, w, [("any key", "continue")])


# ════════════════════════════════════════════════════════════════════════════
#  APPLICATION
# ════════════════════════════════════════════════════════════════════════════


def is_termux():
    return "com.termux" in os.environ.get("PREFIX", "") or "TERMUX_VERSION" in os.environ


def default_start():
    if is_termux():
        cands = (os.path.expanduser("~/storage/shared"), "/sdcard", "/storage/emulated/0")
    else:
        cands = (os.path.expanduser("~/Downloads"), os.path.expanduser("~"))
    for p in cands:
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~") if os.path.isdir(os.path.expanduser("~")) else "/"


class App:
    def __init__(self, start, mouse=True):
        self.cwd = os.path.abspath(start)
        self.entries = []
        self.cur = self.top = 0
        self.sel = set()
        self.sort = "name"
        self.hidden = False
        self.method = 1
        self.show_preview = True
        self.full = False
        self.modal = None
        self.msg, self.msg_until, self.msg_err = "", 0, False
        self.cache = {}
        self.pv_req = None
        self.worker = PreviewWorker(self)
        self.prev = None
        self.dirty = True
        self.running = True
        self.resized = False
        self.animating = False
        self.mouse = mouse
        self.list_rect = (0, 0, 0, 0)
        self.hdr_y = -1
        self.path_y = 1
        self.click_t, self.click_i = 0.0, -1
        self.buf = b""
        self.wr, self.ww = os.pipe()
        for fd in (self.wr, self.ww):
            os.set_blocking(fd, False)
        self.size = (80, 24)

    # ── plumbing ────────────────────────────────────────────────────────────
    def wake(self):
        try:
            os.write(self.ww, b"x")
        except (BlockingIOError, OSError):
            pass

    def flash(self, text, err=False, dur=4.0):
        self.msg, self.msg_err, self.msg_until = text, err, time.time() + dur
        self.dirty = True

    def get_size(self):
        try:
            c, r = os.get_terminal_size(self.fd)
            return c, r
        except OSError:
            return shutil.get_terminal_size((80, 24))

    def out(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def setup(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa
            pass
        self.out("\x1b[?1049h\x1b[?25l\x1b[?7l" +
                 ("\x1b[?1000h\x1b[?1006h" if self.mouse else "") + "\x1b[2J")
        signal.signal(signal.SIGWINCH, lambda *_: (setattr(self, "resized", True), self.wake()))
        for sg in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sg, lambda *_: (setattr(self, "running", False), self.wake()))
        self.size = self.get_size()

    def restore(self):
        try:
            self.out("\x1b[0m\x1b[?1006l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[?1049l")
        finally:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old)

    # ── model ───────────────────────────────────────────────────────────────
    def load(self, path=None, focus=None):
        path = path or self.cwd
        try:
            ents = list_dir(path, self.hidden, self.sort)
        except OSError as e:
            self.flash(f"Can't open: {e.strerror or e}", err=True)
            return False
        self.cwd, self.entries = path, ents
        self.cur = self.top = 0
        if focus:
            for i, e in enumerate(ents):
                if e.name == focus:
                    self.cur = i
                    break
        self.dirty = True
        return True

    def reload(self):
        name = self.entries[self.cur].name if self.entries else None
        self.load(self.cwd, name)

    def curent(self):
        return self.entries[self.cur] if self.entries else None

    def move(self, d):
        if self.entries:
            self.cur = max(0, min(len(self.entries) - 1, self.cur + d))

    def go_up(self):
        parent = os.path.dirname(self.cwd)
        if parent and parent != self.cwd:
            self.load(parent, os.path.basename(self.cwd))

    def enter(self):
        e = self.curent()
        if e is None:
            return
        if e.is_dir:
            self.load(e.path)
        else:
            self.full = True

    def toggle(self, e):
        if e is None:
            return
        if e.path in self.sel:
            self.sel.discard(e.path)
        else:
            self.sel.add(e.path)

    def cycle_sort(self):
        order = ["name", "date", "size"]
        self.sort = order[(order.index(self.sort) + 1) % 3]
        self.reload()
        self.flash(f"Sort by {self.sort}" + (" (newest first)" if self.sort == "date" else " (largest first)" if self.sort == "size" else ""))

    def start_shred(self):
        items = prune_nested([p for p in self.sel if os.path.lexists(p)])
        if not items:
            e = self.curent()
            if e is None:
                return
            items = [e.path]
        bad = [p for p in items if is_protected(p)]
        if bad:
            self.flash("Refusing to shred protected location: " + os.path.basename(bad[0] or "/"), err=True)
            return
        self.modal = ConfirmModal(self, items)

    def after_job(self, job):
        self.sel = {p for p in self.sel if os.path.lexists(p)}
        self.cache.clear()
        self.pv_req = None
        if not os.path.isdir(self.cwd):
            self.cwd = os.path.dirname(self.cwd)
        n = self.cur
        self.load(self.cwd)
        self.cur = max(0, min(n, len(self.entries) - 1))
        self.full = False

    # ── input ───────────────────────────────────────────────────────────────
    def on_key(self, k):
        if isinstance(k, tuple):
            return self.on_mouse(k)
        if k == "?" and self.modal is None:
            k = "?"
        if self.modal:
            return self.modal.key(k)
        if self.full:
            return self.key_full(k)
        self.key_main(k)

    def key_main(self, k):
        page = max(1, self.size[1] - 8)
        if k in ("up", "k"):
            self.move(-1)
        elif k in ("down", "j"):
            self.move(1)
        elif k == "pgup":
            self.move(-page)
        elif k == "pgdn":
            self.move(page)
        elif k in ("home", "g"):
            self.cur = 0
        elif k in ("end", "G"):
            self.cur = max(0, len(self.entries) - 1)
        elif k in ("left", "h", "backspace"):
            self.go_up()
        elif k in ("right", "l", "enter"):
            self.enter()
        elif k == " ":
            self.toggle(self.curent())
            self.move(1)
        elif k == "a":
            self.sel.update(e.path for e in self.entries)
            self.flash(f"{len(self.sel)} selected")
        elif k == "i":
            allp = {e.path for e in self.entries}
            self.sel = (self.sel - allp) | (allp - self.sel)
        elif k == "c":
            self.sel.clear()
            self.flash("Selection cleared")
        elif k == "t":
            self.modal = DateModal(self)
        elif k == "/":
            self.modal = SearchModal(self)
        elif k in ("x", "delete"):
            self.start_shred()
        elif k == "s":
            self.cycle_sort()
        elif k == ".":
            self.hidden = not self.hidden
            self.reload()
            self.flash("Hidden files " + ("shown" if self.hidden else "hidden"))
        elif k == "m":
            self.method = (self.method + 1) % len(METHODS)
            n, d, _ = METHODS[self.method]
            self.flash(f"Method: {n} — {d}")
        elif k == "p":
            self.show_preview = not self.show_preview
        elif k == "v":
            if self.entries:
                self.full = True
        elif k == ":":
            self.modal = GotoModal(self)
        elif k == "~":
            self.load(os.path.expanduser("~"))
        elif k == "0":
            self.load(default_start())
        elif k == "r":
            self.cache.clear()
            self.pv_req = None
            self.reload()
        elif k == "?":
            self.modal = HelpModal(self)
        elif k in ("q", "ctrl-c"):
            self.running = False

    def key_full(self, k):
        if k in ("esc", "q", "v", "enter", "ctrl-c"):
            self.full = False
        elif k in ("up", "left", "k", "h"):
            self.move(-1)
        elif k in ("down", "right", "j", "l"):
            self.move(1)
        elif k == " ":
            self.toggle(self.curent())
        elif k in ("x", "delete"):
            self.start_shred()
        elif k == "?":
            self.modal = HelpModal(self)

    def on_mouse(self, ev):
        _, b, mx, my, press = ev
        mx, my = mx - 1, my - 1
        if self.modal:
            return
        if b in (64, 65):
            self.move(-3 if b == 64 else 3)
            return
        if not press or b != 0:
            return
        if self.full:
            return
        if my == self.path_y:
            self.go_up()
            return
        ly, lx, lh, lw = self.list_rect
        if my == self.hdr_y and lx <= mx < lx + lw:
            self.cycle_sort()
            return
        if ly <= my < ly + lh and lx <= mx < lx + lw:
            idx = self.top + (my - ly)
            if idx >= len(self.entries):
                return
            now = time.time()
            if mx - lx <= 2:
                self.cur = idx
                self.toggle(self.entries[idx])
                return
            if idx == self.click_i and now - self.click_t < 0.45:
                self.cur = idx
                self.enter()
                self.click_i = -1
            else:
                self.cur = idx
                self.click_i, self.click_t = idx, now

    # ── drawing ─────────────────────────────────────────────────────────────
    def draw(self):
        W, H = self.size
        self.animating = False
        cv = Canvas(W, H, BG)
        if W < 28 or H < 9:
            cv.center(H // 2, 0, W, "terminal too small", ORANGE)
        else:
            self.draw_header(cv)
            self.draw_pathbar(cv)
            body_y, body_h = 2, H - 4
            if self.full:
                self.list_rect = (0, 0, 0, 0)
                self.draw_preview(cv, body_y, 0, W, body_h, full=True)
            else:
                self.draw_body(cv, body_y, body_h)
            self.draw_status(cv)
            self.draw_hints(cv)
            if self.modal:
                cv.dim()
                self.modal.draw(cv)
                if isinstance(self.modal, (ProgressModal, ConfirmModal, DateModal)):
                    self.animating = True
        self.out(render_diff(cv, self.prev))
        self.prev = [tuple(r) for r in cv.rows]

    def draw_header(self, cv):
        W = cv.w
        cv.fill(0, 0, W, 1, PANEL)
        x = cv.put(0, 1, " ◆ SHREDDER ", INK, ORANGE, bold=True)
        if W >= 50:
            cv.put(0, x + 1, "secure eraser", MUTED, PANEL)
        right = []
        if self.sel:
            right.append((f"● {len(self.sel)} selected", ORANGE_HI))
        mn = METHODS[self.method]
        right.append((f"{mn[0].upper()} {len(mn[2])}×", SUB))
        s = "   ".join(t for t, _ in right)
        rx = W - swidth(s) - 2
        for t, c in right:
            rx = cv.put(0, rx, t, c, PANEL, bold=(c == ORANGE_HI)) + 3

    def draw_pathbar(self, cv):
        W = cv.w
        cv.fill(1, 0, W, 1, BG)
        cv.put(1, 1, "⌂ ", ORANGE, BG, bold=True)
        n = len(self.entries)
        info = f"{n} item{'s' if n != 1 else ''} · {self.sort}"
        pw = W - 5 - swidth(info) - 2
        cv.put(1, 3, fit_left(self.cwd, max(4, pw)), TEXT, BG, bold=True)
        cv.put(1, W - swidth(info) - 1, info, MUTED, BG)

    def draw_body(self, cv, y, h):
        W = cv.w
        pv_mode = None
        if self.show_preview:
            if W >= 100:
                pv_mode = "side"
            elif h >= 20:
                pv_mode = "stack"
        if pv_mode == "side":
            lw = W * 11 // 20
            self.draw_list(cv, y, 0, lw, h)
            self.draw_preview(cv, y, lw, W - lw, h)
        elif pv_mode == "stack":
            ph = max(9, h * 2 // 5)
            self.draw_list(cv, y, 0, W, h - ph)
            self.draw_preview(cv, y + h - ph, 0, W, ph)
        else:
            self.draw_list(cv, y, 0, W, h)

    def draw_list(self, cv, y, x, w, h):
        rows = h - 1
        self.hdr_y = y
        # ─ column header ─
        show_size = w >= 34
        dstyle = 2 if w >= 72 else (1 if w >= 46 else 0)
        date_w = {2: 16, 1: 8, 0: 0}[dstyle]
        size_w = 7 if show_size else 0
        right_w = (size_w + 1 if show_size else 0) + (date_w + 1 if date_w else 0)
        name_w = max(4, w - 5 - 1 - right_w)
        act = self.sort
        cv.put(y, x + 4, "NAME" + (" ▾" if act == "name" else ""), ORANGE if act == "name" else DIM, BG, bold=True)
        rx = x + w - 1
        if date_w:
            rx -= date_w
            cv.put(y, rx, ("DATE ▾" if act == "date" else "DATE").rjust(date_w), ORANGE if act == "date" else DIM, BG, bold=True)
            rx -= 1
        if show_size:
            rx -= size_w
            cv.put(y, rx, ("SIZE ▾" if act == "size" else "SIZE").rjust(size_w), ORANGE if act == "size" else DIM, BG, bold=True)
        self.list_rect = (y + 1, x, rows, w)
        # ─ viewport ─
        if self.cur < self.top:
            self.top = self.cur
        if self.cur >= self.top + rows:
            self.top = self.cur - rows + 1
        self.top = max(0, min(self.top, max(0, len(self.entries) - rows)))
        if not self.entries:
            cv.center(y + 1 + rows // 2, x, w, "∅  empty folder", MUTED)
        for i in range(rows):
            idx = self.top + i
            if idx >= len(self.entries):
                break
            e = self.entries[idx]
            yy = y + 1 + i
            iscur = idx == self.cur
            issel = e.path in self.sel
            bg = (TINT2 if issel else PANEL2) if iscur else (TINT if issel else BG)
            if bg != BG:
                cv.fill(yy, x, w - 1, 1, bg)
            if iscur:
                cv.put(yy, x, "▌", ORANGE, bg)
            cv.put(yy, x + 1, "●" if issel else " ", ORANGE, bg, bold=True)
            icol = ORANGE if e.is_dir else (ORANGE_HI if e.kind in ("image", "video") else MUTED)
            cv.put(yy, x + 2, ICON[e.kind], icol, bg)
            nm = e.name + ("/" if e.is_dir else "") + (" →" if e.is_link else "")
            ncol = ORANGE_HI if issel else (MUTED if e.name.startswith(".") else TEXT)
            cv.put(yy, x + 4, fit(nm, name_w), ncol, bg, bold=(e.is_dir or iscur))
            rx = x + w - 1
            if date_w:
                rx -= date_w
                cv.put(yy, rx, fmt_ts(e.mtime, dstyle).rjust(date_w), MUTED, bg)
                rx -= 1
            if show_size:
                rx -= size_w
                cv.put(yy, rx, ("—" if e.size < 0 else human(e.size)).rjust(size_w), SUB, bg)
        n = len(self.entries)
        if n > rows > 0:                                    # scrollbar
            th = max(1, rows * rows // n)
            tp = (rows - th) * self.top // max(1, n - rows)
            for i in range(rows):
                on = tp <= i < tp + th
                cv.put(y + 1 + i, x + w - 1, "┃" if on else "│", ORANGE if on else LINE, BG)

    @staticmethod
    def meta_h(ih):
        return 5 if ih >= 16 else 3 if ih >= 10 else 0

    def draw_preview(self, cv, y, x, w, h, full=False):
        e = self.curent()
        title = "PREVIEW"
        right = f"{self.cur + 1}/{len(self.entries)}" if full and self.entries else ""
        cv.box(y, x, w, h, title, border=ORANGE_LO if full else LINE, bg=PANEL, tfg=ORANGE, right=right)
        iy, ix, iw, ih = y + 1, x + 1, w - 2, h - 2
        if e is None:
            cv.center(iy + ih // 2, ix, iw, "nothing selected", MUTED, PANEL)
            return
        if e.path in self.sel:
            cv.put(y, x + 12, " ● SELECTED ", INK, ORANGE, bold=True)
        mh = self.meta_h(ih)
        img_h = ih - mh - (1 if mh else 0)
        if img_h < 2:
            img_h, mh = ih, 0
        key = (e.path, e.mtime, e.size, iw, img_h)
        pv = self.cache.get(key)
        if pv is None:
            if self.pv_req != key:
                self.pv_req = key
                self.worker.request(key, (e.path, e.kind, iw, img_h, e.is_dir))
            self.animating = True
            cv.center(iy + img_h // 2, ix, iw, f"{spinner()} rendering", ORANGE, PANEL)
            return
        if pv.cells:
            oy = iy + max(0, (img_h - len(pv.cells)) // 2)
            ox = ix + max(0, (iw - len(pv.cells[0])) // 2)
            for r, row in enumerate(pv.cells):
                yy = oy + r
                for c, (ch, fg, bg) in enumerate(row):
                    cv.rows[yy][ox + c] = (ch, fg, bg, False)
        elif pv.lines is not None:
            for i, (t, col) in enumerate(pv.lines[:img_h]):
                cv.put(iy + i, ix + 1, fit(t, iw - 2), col, PANEL)
            if pv.err:
                cv.put(iy, ix + 1, fit("⚠ " + pv.err, iw - 2), RED, PANEL)
        else:
            ic = ICON.get(e.kind, "·")
            cv.center(iy + img_h // 2 - 1, ix, iw, ic, ORANGE, PANEL, bold=True)
            if pv.err:
                msg = pv.err
                lines = [msg[i:i + iw - 4] for i in range(0, len(msg), max(4, iw - 4))][:3]
                for i, ln in enumerate(lines):
                    cv.center(iy + img_h // 2 + 1 + i, ix, iw, ln, MUTED, PANEL)
        if mh:
            cv.put(iy + img_h, ix, "─" * iw, LINE, PANEL)
            for i, (k, v) in enumerate(pv.meta[:mh]):
                cv.put(iy + img_h + 1 + i, ix + 1, k.upper().ljust(9), MUTED, PANEL)
                cv.put(iy + img_h + 1 + i, ix + 10, fit(v, iw - 11), TEXT if k == "Name" else SUB, PANEL,
                       bold=(k == "Name"))

    def draw_status(self, cv):
        W, H = cv.w, cv.h
        y = H - 2
        cv.fill(y, 0, W, 1, BG)
        if self.msg and time.time() < self.msg_until:
            cv.put(y, 1, ("✗ " if self.msg_err else "▪ ") + fit(self.msg, W - 5),
                   RED if self.msg_err else ORANGE_HI, BG, bold=True)
            return
        e = self.curent()
        if e:
            cv.put(y, 1, fit(e.name, W - 3), SUB, BG)

    def draw_hints(self, cv):
        W, H = cv.w, cv.h
        y = H - 1
        cv.fill(y, 0, W, 1, PANEL)
        if self.full:
            pairs = [("←→", "browse"), ("␣", "select"), ("x", "shred"), ("Esc", "back")]
        else:
            pairs = [("␣", "select"), ("/", "lookup"), ("t", "date"), ("x", "shred"),
                     ("v", "view"), ("m", "method"), ("s", "sort"), ("?", "help"), ("q", "quit")]
        x = 1
        for k, v in pairs:
            need = swidth(k) + swidth(v) + 3
            if x + need > W:
                break
            x = cv.put(y, x, k, ORANGE, PANEL, bold=True)
            x = cv.put(y, x, " " + v + "  ", MUTED, PANEL)

    # ── main loop ───────────────────────────────────────────────────────────
    def tick(self):
        m = self.modal
        if isinstance(m, ProgressModal) and m.job.finished:
            self.modal = ResultModal(self, m.job)
        if self.msg and time.time() >= self.msg_until:
            self.msg = ""
            self.dirty = True

    def run(self):
        self.setup()
        self.worker.start()
        try:
            self.load(self.cwd)
            while self.running:
                if self.resized:
                    self.resized = False
                    self.size = self.get_size()
                    self.prev = None
                    self.out("\x1b[2J")
                    self.dirty = True
                self.tick()
                if self.dirty or self.animating:
                    self.draw()
                    self.dirty = False
                to = 0.1 if self.animating else (0.3 if self.msg else None)
                r, _, _ = select.select([self.fd, self.wr], [], [], to)
                if self.wr in r:
                    try:
                        os.read(self.wr, 4096)
                    except (BlockingIOError, OSError):
                        pass
                    self.dirty = True
                if self.fd in r:
                    data = os.read(self.fd, 4096)
                    if not data:
                        break
                    keys, self.buf = parse_keys(self.buf + data)
                    for k in keys:
                        if k == "?" and not isinstance(k, tuple):
                            pass
                        try:
                            self.on_key(k)
                        except Exception as ex:  # noqa
                            self.flash(f"error: {ex!r}", err=True)
                    self.dirty = True
        finally:
            self.restore()


def main():
    ap = argparse.ArgumentParser(description="SHREDDER - secure file & folder eraser TUI for Termux")
    ap.add_argument("path", nargs="?", help="start folder (default: shared storage or ~)")
    ap.add_argument("--no-mouse", action="store_true", help="disable touch/mouse support")
    ap.add_argument("-V", "--version", action="version", version=f"shredder {VERSION}")
    a = ap.parse_args()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("shredder needs an interactive terminal.")
    start = os.path.expanduser(a.path) if a.path else default_start()
    if not os.path.isdir(start):
        sys.exit(f"not a folder: {start}")
    app = App(start, mouse=not a.no_mouse)
    try:
        app.run()
    except Exception:  # noqa
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
