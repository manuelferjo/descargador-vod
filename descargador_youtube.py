"""
Descargador VOD (YouTube y Twitch) - Vídeo (varias calidades) y MP3, entero o solo un fragmento.
Interfaz con el estilo "Vortexs" (el mismo de Stream Delay): fondo casi negro, tarjetas oscuras,
degradado violeta (#C09AFD) -> amarillo (#EDE103) y fuentes Outfit / Plus Jakarta Sans / JetBrains Mono.
Todo se dibuja con Pillow para no depender de los controles grises de Windows.

Requisitos:  pip install yt-dlp pillow   +   FFmpeg instalado y en el PATH
Uso:         python descargador_youtube.py
"""

import ctypes
import io
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.parse
import urllib.request
import weakref
import zipfile
import zipimport
from tkinter import filedialog, messagebox

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageTk

VERSION = "1.3"
TITULO = "Descargador VOD · Vortexs"
APP_DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DescargadorVOD")
MOTOR_ZIP = os.path.join(APP_DATA, "yt-dlp.zip")      # motor actualizado desde la app
CONFIG = os.path.join(APP_DATA, "config.json")


# ======================================================================
#  Motor de descarga (yt-dlp) actualizable sin rehacer el .exe
# ======================================================================
def version_tupla(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def _leer_version(txt):
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", txt or "")
    return m.group(1) if m else None


def version_en_zip(ruta):
    """Versión de yt-dlp dentro de un zip descargado (o None si no es válido)."""
    try:
        with zipfile.ZipFile(ruta) as z:
            return _leer_version(z.read("yt_dlp/version.py").decode("utf-8", "replace"))
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def version_incluida():
    """Versión del yt-dlp que va dentro del programa, leída sin importarlo."""
    import importlib.util
    try:
        spec = importlib.util.find_spec("yt_dlp")
    except (ImportError, ValueError):
        return None
    for d in (spec.submodule_search_locations or []) if spec else []:
        p = os.path.join(d, "version.py")
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                return _leer_version(f.read())
    return None


class _MotorFinder:
    """Hace que 'import yt_dlp' cargue el zip actualizado antes que el incluido en el .exe."""

    def __init__(self, zpath):
        self.zpath, self.cache = zpath, {}

    def find_spec(self, name, path=None, target=None):
        if name != "yt_dlp" and not name.startswith("yt_dlp."):
            return None
        partes = name.split(".")
        sub = os.path.join(self.zpath, *partes[:-1]) if len(partes) > 1 else self.zpath
        zi = self.cache.get(sub)
        if zi is None:
            try:
                zi = zipimport.zipimporter(sub)
            except zipimport.ZipImportError:
                return None
            self.cache[sub] = zi
        return zi.find_spec(name, target)


def _cargar_motor():
    """Usa el motor actualizado si existe y es más nuevo que el incluido. Devuelve su versión o None."""
    zv = version_en_zip(MOTOR_ZIP) if os.path.isfile(MOTOR_ZIP) else None
    if not zv:
        return None
    iv = version_incluida()
    if iv and version_tupla(zv) <= version_tupla(iv):
        return None  # el que va dentro del programa es igual o más nuevo
    finder = _MotorFinder(MOTOR_ZIP)
    sys.meta_path.insert(0, finder)
    try:
        import yt_dlp  # noqa: F401
        import yt_dlp.utils  # noqa: F401
        return zv
    except Exception:
        # Si el actualizado falla, se vuelve al incluido.
        sys.meta_path.remove(finder)
        for k in [k for k in sys.modules if k == "yt_dlp" or k.startswith("yt_dlp.")]:
            del sys.modules[k]
        return None


MOTOR_ACTUALIZADO = _cargar_motor()

try:
    import yt_dlp
    import yt_dlp.version
    from yt_dlp.utils import download_range_func
except ImportError:
    yt_dlp = None

# Procesos (FFmpeg) que lanza yt-dlp, para poder pararlos al cancelar.
PROCESOS = weakref.WeakSet()
if yt_dlp is not None:
    try:
        _Popen = yt_dlp.utils.Popen
        _init_original = _Popen.__init__

        def _init_registrado(self, *a, **k):
            _init_original(self, *a, **k)
            PROCESOS.add(self)

        _Popen.__init__ = _init_registrado
    except AttributeError:
        pass


def matar_procesos():
    for p in list(PROCESOS):
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


def version_motor():
    return yt_dlp.version.__version__ if yt_dlp else "?"


def ultima_version_motor():
    req = urllib.request.Request("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
                                 headers={"User-Agent": "DescargadorVOD", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["tag_name"]



# ======================================================================
#  Rutas
# ======================================================================
def carpeta_app():
    """Carpeta del .exe (si está empaquetado) o del script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def carpeta_res():
    """Carpeta 'res' (logo, icono y fuentes). En el .exe va dentro de _internal."""
    for base in (getattr(sys, "_MEIPASS", ""), carpeta_app()):
        d = os.path.join(base, "res") if base else ""
        if d and os.path.isdir(d):
            return d
    return os.path.join(carpeta_app(), "res")


def ruta_ffmpeg():
    """Busca ffmpeg junto a la app (o en una subcarpeta 'ffmpeg') y si no, en el PATH."""
    nombre = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for d in (carpeta_app(), os.path.join(carpeta_app(), "ffmpeg"),
              getattr(sys, "_MEIPASS", "")):
        if d and os.path.isfile(os.path.join(d, nombre)):
            return d
    encontrado = shutil.which("ffmpeg")
    return os.path.dirname(encontrado) if encontrado else None


def formato_tiempo(seg):
    """3725 -> '1h02m05s' (apto para nombres de archivo)."""
    seg = int(seg)
    return f"{seg // 3600}h{seg % 3600 // 60:02d}m{seg % 60:02d}s"


CALIDADES_VIDEO = [("Máx", None), ("4K", 2160), ("1440p", 1440), ("1080p", 1080),
                   ("720p", 720), ("480p", 480), ("360p", 360)]
CALIDADES_AUDIO = [("320 kbps", "320"), ("256 kbps", "256"), ("192 kbps", "192"), ("128 kbps", "128")]


# ======================================================================
#  Tema (traducción de Theme.cs de Stream Delay)
# ======================================================================
BG = (9, 9, 12)
SURFACE = (17, 17, 22)
ELEVATED = (23, 23, 32)
INPUT = (14, 14, 19)
BORDER = (38, 38, 46)
BORDER_HI = (56, 54, 70)
TEXT = (244, 244, 245)
MUTED = (161, 161, 170)
DIM = (113, 113, 122)
VIOLET = (192, 154, 253)
VIOLET_LIGHT = (214, 191, 255)
VIOLET_PALE = (227, 213, 255)
YELLOW = (237, 225, 3)
RED = (255, 70, 85)
GREEN = (52, 211, 153)

SS = 4  # supermuestreo para bordes suaves


def hx(c):
    return "#%02x%02x%02x" % tuple(c[:3])


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class Theme:
    scale = 1.0
    _fonts = {}
    _masks = {}
    _grads = {}
    _radial = None
    FILES = {
        "display": ("Outfit-Bold.ttf", "segoeuib.ttf"),
        "body": ("PlusJakartaSans-Regular.ttf", "segoeui.ttf"),
        "semi": ("PlusJakartaSans-SemiBold.ttf", "seguisb.ttf"),
        "bold": ("PlusJakartaSans-Bold.ttf", "segoeuib.ttf"),
        "mono": ("JetBrainsMono-Medium.ttf", "consola.ttf"),
    }

    @classmethod
    def S(cls, v):
        return int(round(v * cls.scale))

    @classmethod
    def font(cls, kind, pt):
        """Tamaño en puntos a 96 PPP, escalado igual que el diseño (como Theme.Make)."""
        k = (kind, pt)
        if k not in cls._fonts:
            px = max(6, int(round(pt * 96 / 72 * cls.scale)))
            fichero, reserva = cls.FILES[kind]
            try:
                f = ImageFont.truetype(os.path.join(carpeta_res(), fichero), px)
            except OSError:
                try:
                    f = ImageFont.truetype(reserva, px)
                except OSError:
                    f = ImageFont.load_default()
            cls._fonts[k] = f
        return cls._fonts[k]

    # ---------- máscaras y degradados ----------
    @classmethod
    def rr_mask(cls, w, h, r, width=0):
        """Máscara (L) de un rectángulo redondeado relleno o solo el borde."""
        w, h = max(1, int(w)), max(1, int(h))
        k = (w, h, round(r, 2), round(width, 2))
        m = cls._masks.get(k)
        if m is None:
            big = Image.new("L", (w * SS, h * SS), 0)
            d = ImageDraw.Draw(big)
            rad = min(r, w / 2, h / 2) * SS
            box = (0, 0, w * SS - 1, h * SS - 1)
            if width:
                d.rounded_rectangle(box, rad, outline=255, width=max(1, int(round(width * SS))))
            else:
                d.rounded_rectangle(box, rad, fill=255)
            m = big.resize((w, h), Image.Resampling.LANCZOS)
            cls._masks[k] = m
        return m

    @classmethod
    def ellipse_mask(cls, d_px):
        d_px = max(1, int(round(d_px)))
        k = ("e", d_px)
        m = cls._masks.get(k)
        if m is None:
            big = Image.new("L", (d_px * SS, d_px * SS), 0)
            ImageDraw.Draw(big).ellipse((0, 0, d_px * SS - 1, d_px * SS - 1), fill=255)
            m = big.resize((d_px, d_px), Image.Resampling.LANCZOS)
            cls._masks[k] = m
        return m

    @classmethod
    def ring_mask(cls, d_px, width):
        d_px = max(1, int(round(d_px)))
        big = Image.new("L", (d_px * SS, d_px * SS), 0)
        ImageDraw.Draw(big).ellipse((0, 0, d_px * SS - 1, d_px * SS - 1), outline=255,
                                    width=max(1, int(width * SS)))
        return big.resize((d_px, d_px), Image.Resampling.LANCZOS)

    @classmethod
    def gradient(cls, w, h, stops, angle=20):
        """Degradado lineal RGBA con 'stops' [(pos, (r,g,b,a)), ...] a 'angle' grados."""
        w, h = max(1, int(w)), max(1, int(h))
        k = (w, h, tuple(stops), angle)
        g = cls._grads.get(k)
        if g is None:
            strip = Image.new("RGBA", (258, 1))
            for i in range(258):
                t = min(1.0, max(0.0, (i - 1) / 255))
                for j in range(len(stops) - 1):
                    p0, c0 = stops[j]
                    p1, c1 = stops[j + 1]
                    if t <= p1 or j == len(stops) - 2:
                        u = 0 if p1 == p0 else min(1, max(0, (t - p0) / (p1 - p0)))
                        strip.putpixel((i, 0), tuple(int(c0[n] + (c1[n] - c0[n]) * u) for n in range(4)))
                        break
            a = math.radians(angle)
            c, s = math.cos(a), math.sin(a)
            den = w * c + h * s or 1
            g = strip.transform((w, h), Image.Transform.AFFINE,
                                (255 * c / den, 255 * s / den, 1, 0, 0, 0),
                                resample=Image.Resampling.NEAREST)
            cls._grads[k] = g
        return g

    @classmethod
    def brand(cls, w, h, pale=False):
        if pale:
            stops = ((0, VIOLET + (255,)), (0.45, VIOLET_PALE + (255,)), (1, YELLOW + (255,)))
        else:
            stops = ((0, VIOLET + (255,)), (1, YELLOW + (255,)))
        return cls.gradient(w, h, stops)

    @classmethod
    def radial(cls):
        """Máscara 256x256: 255 en el centro y 0 en el borde de la elipse (como PathGradientBrush)."""
        if cls._radial is None:
            n = 256
            m = Image.new("L", (n, n), 0)
            px = m.load()
            c = (n - 1) / 2
            for y in range(n):
                for x in range(n):
                    d = math.hypot(x - c, y - c) / (n / 2)
                    if d < 1:
                        px[x, y] = int(255 * (1 - d))
            cls._radial = m
        return cls._radial


def comp(dst, src, x, y):
    """alpha_composite admitiendo posiciones negativas o que se salen."""
    x, y = int(round(x)), int(round(y))
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(src.width, dst.width - x), min(src.height, dst.height - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    if (sx0, sy0, sx1, sy1) != (0, 0, src.width, src.height):
        src = src.crop((sx0, sy0, sx1, sy1))
    dst.alpha_composite(src, (x + sx0, y + sy0))


def layer(mask, fill, alpha=255):
    """Capa RGBA con la forma de 'mask' rellena de un color o de una imagen (degradado)."""
    if isinstance(fill, Image.Image):
        lay = fill if fill.size == mask.size else fill.resize(mask.size)
        a = ImageChops.multiply(mask, lay.getchannel("A"))
        lay = lay.copy()
    else:
        if len(fill) == 4:
            alpha = fill[3] * alpha // 255
        lay = Image.new("RGBA", mask.size, tuple(fill[:3]) + (255,))
        a = mask
    if alpha < 255:
        a = a.point(lambda v, al=alpha: v * al // 255)
    lay.putalpha(a)
    return lay


def glow(dst, x, y, w, h, color, alpha):
    """Brillo radial suave (Card.Glow)."""
    w, h = max(1, int(w)), max(1, int(h))
    m = Theme.radial().resize((w, h), Image.Resampling.BILINEAR).point(lambda v: v * alpha // 255)
    lay = Image.new("RGBA", (w, h), color + (255,))
    lay.putalpha(m)
    comp(dst, lay, x, y)


def text(img, xy, s, font, color, anchor="lm", alpha=255):
    d = ImageDraw.Draw(img)
    d.text(xy, s, font=font, fill=tuple(color[:3]) + (alpha,), anchor=anchor)


def gradient_text(img, xy, s, font, pale=True, anchor="lm"):
    l, t, r, b = font.getbbox(s, anchor=anchor)
    w, h = r - l + 2, b - t + 2
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).text((-l + 1, -t + 1), s, font=font, fill=255, anchor=anchor)
    comp(img, layer(m, Theme.brand(w, h, pale)), xy[0] + l - 1, xy[1] + t - 1)


def ellipsize(s, font, maxw):
    if font.getlength(s) <= maxw:
        return s
    while s and font.getlength(s + "…") > maxw:
        s = s[:-1]
    return s + "…"


def paint_card(img, X, Y, W, H, accent=False):
    S = Theme.S
    r = S(18)
    mask = Theme.rr_mask(W, H, r)
    comp(img, layer(mask, SURFACE + (225,)), X, Y)
    if accent:
        gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glow(gl, -W * 0.3, -H * 0.6, W * 0.9, H * 1.1, VIOLET, 40)
        glow(gl, W * 0.55, H * 0.35, W * 0.7, H * 1.0, YELLOW, 26)
        gl.putalpha(ImageChops.multiply(gl.getchannel("A"), mask))
        comp(img, gl, X, Y)
        ring = Theme.rr_mask(W, H, r, width=max(1.2, 1.2 * Theme.scale))
        g = Theme.gradient(W, H, ((0, VIOLET + (120,)), (1, YELLOW + (80,))))
        comp(img, layer(ring, g), X, Y)
    else:
        comp(img, layer(Theme.rr_mask(W, H, r, width=max(1, Theme.scale)), BORDER), X, Y)


# ======================================================================
#  Controles dibujados a mano
# ======================================================================
class VW:
    """Base: un Label sin bordes que muestra una imagen hecha con Pillow sobre un recorte del fondo."""
    clickable = False

    def __init__(self, app, x, y, w, h, page=None):
        self.app, self.page = app, page
        self.x, self.y, self.w, self.h = x, y, w, h
        self.hover = self.pressed = self.visible = False
        self.enabled = True
        self.lbl = tk.Label(app, bd=0, highlightthickness=0, padx=0, pady=0, bg=hx(BG))
        if self.clickable:
            self.lbl.configure(cursor="hand2")
        self.lbl.bind("<Enter>", self._enter)
        self.lbl.bind("<Leave>", self._leave)
        self.lbl.bind("<ButtonPress-1>", self._down)
        self.lbl.bind("<ButtonRelease-1>", self._up)
        app.widgets.append(self)

    def box(self):
        S = Theme.S
        return S(self.x), S(self.y), S(self.w), S(self.h)

    def _enter(self, e):
        self.hover = True
        self.render()

    def _leave(self, e):
        self.hover = self.pressed = False
        self.render()

    def _down(self, e):
        if self.enabled and self.clickable:
            self.pressed = True
            self.render()

    def _up(self, e):
        fue = self.pressed
        self.pressed = False
        dentro = 0 <= e.x < self.lbl.winfo_width() and 0 <= e.y < self.lbl.winfo_height()
        self.render()
        if fue and dentro and self.enabled:
            self.click(e)

    def click(self, e):
        pass

    def set_enabled(self, v):
        self.enabled = v
        if self.clickable:
            self.lbl.configure(cursor="hand2" if v else "arrow")
        self.render()

    def show(self):
        X, Y, W, H = self.box()
        self.lbl.place(x=X, y=Y, width=W, height=H)
        self.visible = True
        self.render()

    def hide(self):
        self.lbl.place_forget()
        self.visible = False

    def render(self):
        if not self.visible:
            return
        X, Y, W, H = self.box()
        img = self.app.bg_img.crop((X, Y, X + W, Y + H)).copy()
        self.paint(img, W, H)
        self.photo = ImageTk.PhotoImage(img)
        self.lbl.configure(image=self.photo)

    def paint(self, img, W, H):
        pass


class VButton(VW):
    """Botón: 'primary' (degradado), 'ghost', 'pill' o 'link' (como VButton de Stream Delay)."""
    clickable = True

    def __init__(self, app, x, y, w, h, page, text, command=None, kind="ghost", size=9.5):
        super().__init__(app, x, y, w, h, page)
        self.text, self.command, self.kind, self.size = text, command, kind, size
        self.selected = False

    def click(self, e):
        if self.command:
            self.command()

    def set_text(self, t):
        self.text = t
        self.render()

    def paint(self, img, W, H):
        S = Theme.S
        x0 = y0 = 1 if self.pressed else 0
        w, h = W - 2 * x0, H - 2 * y0
        k = self.kind
        rad = h / 2 if k == "pill" else S(12)
        fg = TEXT
        font = Theme.font("mono", self.size) if k == "pill" else Theme.font("bold", self.size)
        mask = Theme.rr_mask(w, h, rad)
        ring = Theme.rr_mask(w, h, rad, width=max(1, Theme.scale))
        if k == "primary":
            if not self.enabled:
                comp(img, layer(mask, ELEVATED), x0, y0)
                comp(img, layer(ring, BORDER), x0, y0)
                fg = DIM
            else:
                comp(img, layer(mask, Theme.brand(w, h)), x0, y0)
                if self.hover:
                    comp(img, layer(mask, (255, 255, 255, 28)), x0, y0)
                fg = (0, 0, 0)
        elif k == "pill":
            if self.selected:
                comp(img, layer(mask, VIOLET + (38 if self.enabled else 20,)), x0, y0)
                comp(img, layer(Theme.rr_mask(w, h, rad, width=1.2 * Theme.scale),
                                VIOLET + (200 if self.enabled else 90,)), x0, y0)
                fg = VIOLET_LIGHT if self.enabled else mix(VIOLET_LIGHT, BG, 0.4)
            else:
                hov = self.hover and self.enabled
                comp(img, layer(mask, ELEVATED if hov else INPUT), x0, y0)
                comp(img, layer(ring, BORDER_HI if hov else BORDER), x0, y0)
                fg = DIM if not self.enabled else (TEXT if hov else MUTED)
        elif k == "danger":
            hov = self.hover and self.enabled
            comp(img, layer(mask, RED + (40 if hov else 18,)), x0, y0)
            comp(img, layer(Theme.rr_mask(w, h, rad, width=1.2 * Theme.scale),
                            RED + ((230 if hov else 150) if self.enabled else 70,)), x0, y0)
            fg = (255, 120, 130) if self.enabled else DIM
        elif k == "link":
            fg = VIOLET_LIGHT if self.hover else VIOLET
            font = Theme.font("semi", self.size)
            text(img, (W, H / 2), self.text, font, fg, anchor="rm")
            return
        else:  # ghost
            comp(img, layer(mask, (32, 32, 42) if self.hover and self.enabled else ELEVATED), x0, y0)
            comp(img, layer(ring, BORDER_HI if self.hover and self.enabled else BORDER), x0, y0)
            if not self.enabled:
                fg = DIM
        text(img, (W / 2, H / 2), self.text, font, fg, anchor="mm")


class Toggle(VW):
    """Interruptor tipo píldora con texto a la derecha."""
    clickable = True

    def __init__(self, app, x, y, w, h, page, text, on_change=None, checked=False):
        super().__init__(app, x, y, w, h, page)
        self.text, self.on_change, self.checked = text, on_change, checked

    def click(self, e):
        self.checked = not self.checked
        self.render()
        if self.on_change:
            self.on_change(self.checked)

    def paint(self, img, W, H):
        S = Theme.S
        tw, th = S(38), S(22)
        ty = (H - th) / 2
        mask = Theme.rr_mask(tw, th, th / 2)
        if self.checked and self.enabled:
            comp(img, layer(mask, Theme.brand(tw, th)), 1, ty)
        elif self.checked:
            comp(img, layer(mask, Theme.brand(tw, th), 90), 1, ty)
        else:
            comp(img, layer(mask, (44, 44, 56) if self.hover and self.enabled else (36, 36, 46)), 1, ty)
            comp(img, layer(Theme.rr_mask(tw, th, th / 2, width=max(1, Theme.scale)), BORDER_HI), 1, ty)
        k = th - S(6)
        kx = 1 + tw - k - S(3) if self.checked else 1 + S(3)
        comp(img, layer(Theme.ellipse_mask(k), (14, 14, 18) if self.checked else MUTED), kx, ty + S(3))
        text(img, (tw + S(12), H / 2), self.text, Theme.font("semi", 9), TEXT if self.enabled else DIM)


class Tabs(VW):
    """Pestañas tipo 'segmented control'."""
    clickable = True

    def __init__(self, app, x, y, w, h, page, items, on_change=None, selected=0):
        super().__init__(app, x, y, w, h, page)
        self.items, self.on_change, self.selected, self.hot = items, on_change, selected, -1
        self.lbl.bind("<Motion>", self._motion)

    def _at(self, x):
        pad = Theme.S(4)
        n = len(self.items)
        return max(0, min(n - 1, int((x - pad) * n / max(1, self.lbl.winfo_width() - 2 * pad))))

    def _motion(self, e):
        h = self._at(e.x)
        if h != self.hot:
            self.hot = h
            self.render()

    def _leave(self, e):
        self.hot = -1
        super()._leave(e)

    def click(self, e):
        i = self._at(e.x)
        if i != self.selected:
            self.selected = i
            self.render()
            if self.on_change:
                self.on_change(i)

    def paint(self, img, W, H):
        S = Theme.S
        comp(img, layer(Theme.rr_mask(W, H, H / 2), SURFACE + (230,)), 0, 0)
        comp(img, layer(Theme.rr_mask(W, H, H / 2, width=max(1, Theme.scale)), BORDER), 0, 0)
        pad = S(4)
        n = len(self.items)
        iw = (W - 2 * pad) / n
        for i, it in enumerate(self.items):
            ix = pad + i * iw
            ih = H - 2 * pad
            if i == self.selected:
                comp(img, layer(Theme.rr_mask(iw, ih, ih / 2), VIOLET + (34,)), ix, pad)
                comp(img, layer(Theme.rr_mask(iw, ih, ih / 2, width=max(1, Theme.scale)), VIOLET + (120,)), ix, pad)
            c = VIOLET_PALE if i == self.selected else (TEXT if i == self.hot else MUTED)
            text(img, (ix + iw / 2, H / 2), it, Theme.font("semi", 9), c, anchor="mm")


class Field(VW):
    """Campo de texto: Entry/Text nativo sin borde dentro de un recuadro redondeado."""

    def __init__(self, app, x, y, w, h, page, multiline=False, mono=False, center=False, readonly=False):
        super().__init__(app, x, y, w, h, page)
        self.focused, self.multiline = False, multiline
        comun = dict(bg=hx(INPUT), fg=hx(TEXT), insertbackground=hx(TEXT), relief="flat", bd=0,
                     highlightthickness=0, selectbackground="#3d3160", selectforeground=hx(TEXT),
                     font=app.font_mono if mono else app.font_native)
        if multiline:
            self.native = tk.Text(app, wrap="char", padx=0, pady=0, undo=True,
                                  inactiveselectbackground="#2c2545", **comun)
            if readonly:
                self.native.configure(state="disabled", cursor="arrow")
        else:
            self.native = tk.Entry(app, disabledbackground=hx(INPUT), disabledforeground=hx(DIM),
                                   readonlybackground=hx(INPUT), justify="center" if center else "left",
                                   **comun)
        self.readonly = readonly
        self.native.bind("<FocusIn>", lambda e: self._focus(True), add="+")
        self.native.bind("<FocusOut>", lambda e: self._focus(False), add="+")
        self.native.bind("<Enter>", self._enter, add="+")
        self.native.bind("<Leave>", self._leave, add="+")
        self.lbl.bind("<Button-1>", lambda e: self.native.focus_set(), add="+")
        self.lbl.configure(cursor="xterm")

    def _focus(self, v):
        self.focused = v and not self.readonly
        self.render()

    def _leave(self, e):
        # Al pasar del recuadro al campo nativo no se pierde el "hover".
        x, y = self.app.winfo_pointerxy()
        X, Y = self.lbl.winfo_rootx(), self.lbl.winfo_rooty()
        if not (X <= x < X + self.lbl.winfo_width() and Y <= y < Y + self.lbl.winfo_height()):
            super()._leave(e)

    def set_enabled(self, v):
        self.enabled = v
        if not self.multiline:
            self.native.configure(state="normal" if v else "disabled")
        self.render()

    def show(self):
        super().show()
        X, Y, W, H = self.box()
        S = Theme.S
        if self.multiline:
            self.native.place(x=X + S(12), y=Y + S(9), width=W - S(24), height=H - S(18))
        else:
            fh = tkfont.Font(font=self.native.cget("font")).metrics("linespace") + S(2)
            px = S(6) if W < S(70) else S(12)
            self.native.place(x=X + px, y=Y + (H - fh) // 2, width=W - 2 * px, height=fh)
        self.native.lift()

    def hide(self):
        super().hide()
        self.native.place_forget()

    def paint(self, img, W, H):
        S = Theme.S
        comp(img, layer(Theme.rr_mask(W, H, S(10)), INPUT), 0, 0)
        if self.focused:
            c, wd = VIOLET, 1.5 * Theme.scale
        else:
            c, wd = (BORDER_HI if self.hover and self.enabled and not self.readonly else BORDER), max(1, Theme.scale)
        comp(img, layer(Theme.rr_mask(W, H, S(10), width=wd), c), 0, 0)


class Progress(VW):
    """Barra de progreso fina con relleno de marca; modo indeterminado animado."""

    def __init__(self, app, x, y, w, h, page):
        super().__init__(app, x, y, w, h, page)
        self.value, self.indeterminate, self.phase = 0.0, False, 0.0

    def set(self, v):
        self.indeterminate = False
        self.value = max(0.0, min(100.0, v))
        self.render()

    def start(self):
        if not self.indeterminate:
            self.indeterminate = True
            self._tick()

    def _tick(self):
        if not self.indeterminate:
            return
        self.phase = (self.phase + 0.018) % 1.35
        self.render()
        self.app.after(30, self._tick)

    def paint(self, img, W, H):
        comp(img, layer(Theme.rr_mask(W, H, H / 2), (38, 38, 48)), 0, 0)
        if self.indeterminate:
            sw = W * 0.35
            x0 = (self.phase - 0.35) * W
            a, b = max(0, x0), min(W, x0 + sw)
        else:
            a, b = 0, W * self.value / 100
        if b - a >= 1:
            fill = Theme.brand(W, H).crop((int(a), 0, int(b), H))
            comp(img, layer(Theme.rr_mask(int(b) - int(a), H, H / 2), fill), int(a), 0)


class Label(VW):
    """Texto que cambia (estado)."""

    def __init__(self, app, x, y, w, h, page, text="", kind="body", size=8.5, color=MUTED, anchor="lm"):
        super().__init__(app, x, y, w, h, page)
        self.text, self.kind, self.size, self.color, self.anchor = text, kind, size, color, anchor

    def set(self, t, color=None):
        self.text = t
        if color is not None:
            self.color = color
        self.render()

    def paint(self, img, W, H):
        f = Theme.font(self.kind, self.size)
        x = 0 if self.anchor[0] == "l" else (W if self.anchor[0] == "r" else W / 2)
        text(img, (x, H / 2), ellipsize(self.text, f, W), f, self.color, anchor=self.anchor)


class StatusPill(VW):
    """Píldora de estado de la cabecera (APAGADO / EN DIRECTO… en Stream Delay)."""
    ESTADOS = {
        "listo": ("LISTO", DIM, False),
        "descargando": ("DESCARGANDO", VIOLET, True),
        "procesando": ("PROCESANDO", YELLOW, True),
        "terminado": ("TERMINADO", GREEN, True),
        "error": ("CON ERRORES", RED, True),
        "cancelado": ("CANCELADO", MUTED, True),
    }

    def __init__(self, app, x, y, w, h, page=None):
        super().__init__(app, x, y, w, h, page)
        self.estado, self.pulse = "listo", False

    def set(self, estado):
        if estado != self.estado:
            self.estado = estado
            self.render()

    def paint(self, img, W, H):
        S = Theme.S
        label, c, filled = self.ESTADOS[self.estado]
        f = Theme.font("mono", 7.5)
        tw = f.getlength(label)
        pw, ph = int(tw + S(36)), S(28)
        px, py = W - pw, (H - ph) / 2
        comp(img, layer(Theme.rr_mask(pw, ph, ph / 2), c + (28,)), px, py)
        comp(img, layer(Theme.rr_mask(pw, ph, ph / 2, width=max(1, Theme.scale)), c + (110,)), px, py)
        d = S(8)
        dx, dy = px + S(12), py + (ph - d) / 2
        if filled:
            if self.pulse and self.estado == "descargando":
                glow(img, dx - d, dy - d, 3 * d, 3 * d, c, 120)
            comp(img, layer(Theme.ellipse_mask(d), c), dx, dy)
        else:
            comp(img, layer(Theme.ring_mask(d, 1.5 * Theme.scale), c), dx, dy)
        text(img, (dx + d + S(7), py + ph / 2), label, f,
             mix(c, (255, 255, 255), 0.35) if filled else MUTED)


# ======================================================================
#  Utilidades de descarga
# ======================================================================
NAVEGADORES = [("Ninguno", None), ("Firefox", "firefox"), ("Chrome", "chrome"), ("Edge", "edge"),
               ("Brave", "brave"), ("Opera", "opera")]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Cancelado(Exception):
    pass


def limpiar_msg(s):
    return ANSI.sub("", str(s)).replace("ERROR: ", "").strip()


def leer_t(url):
    """Segundos del tiempo que lleva el enlace (?t=1h45m00s, ?t=6300, &start=90, #t=1:30) o None."""
    try:
        p = urllib.parse.urlparse(url.strip())
    except ValueError:
        return None
    vals = urllib.parse.parse_qs(p.query)
    v = (vals.get("t") or vals.get("start") or [None])[0]
    if v is None and p.fragment.startswith("t="):
        v = p.fragment[2:]
    if not v:
        return None
    v = v.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?s?", v):
        return int(float(v.rstrip("s")))
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", v)
    if m and any(m.groups()):
        h, mi, s = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + s
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+)", v)
    if m:
        h, mi, s = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def duracion_txt(seg):
    seg = int(seg or 0)
    h, m, s = seg // 3600, seg % 3600 // 60, seg % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def nombre_codec(vc):
    vc = (vc or "").lower()
    for clave, nombre in (("avc", "H.264"), ("h264", "H.264"), ("av01", "AV1"), ("vp09", "VP9"),
                          ("vp9", "VP9"), ("hev", "H.265"), ("hvc", "H.265")):
        if vc.startswith(clave):
            return nombre
    return vc.split(".")[0].upper() if vc and vc != "none" else ""


def pista_error(msg, url, navegador):
    """Consejo para el usuario según el error."""
    b = msg.lower()
    nombre = (navegador or "").capitalize()
    if navegador and ("could not copy" in b or "database is locked" in b or
                      ("permission denied" in b and "cookie" in b)):
        return f"Cierra {nombre} del todo y vuelve a intentarlo (tiene la sesión bloqueada)."
    if navegador and ("dpapi" in b or "app-bound" in b or "decrypt" in b):
        return f"{nombre} no deja leer su sesión. Prueba con Firefox en Opciones."
    if navegador and "could not find" in b and "cookie" in b:
        return f"No se encontró la sesión de {nombre}. ¿Está instalado y con la sesión iniciada?"
    if "sign in to confirm" in b or "not a bot" in b:
        return "YouTube pide iniciar sesión: en Opciones elige tu navegador en «Sesión del navegador»."
    if "twitch" in url.lower() and ("subscri" in b or "403" in b):
        return "Puede ser un VOD solo para suscriptores: activa la sesión del navegador en Opciones."
    if "private video" in b:
        return "El vídeo es privado."
    if "confirm your age" in b or "age-restricted" in b or "age restricted" in b:
        return "Tiene restricción de edad: activa la sesión del navegador en Opciones."
    if "unsupported url" in b:
        return "Ese enlace no es de un vídeo compatible."
    return ""


def limpiar_parciales(carpeta, desde):
    """Borra los archivos a medias que deja una descarga cancelada (solo los creados desde 'desde')."""
    patron = re.compile(r"(\.part(-Frag\d+)?(\.part)?|\.ytdl|\.temp\.\w+|\.f[0-9][\w-]*\.\w+(\.part)?)$")
    n = 0
    try:
        nombres = os.listdir(carpeta)
    except OSError:
        return 0
    for nombre in nombres:
        ruta = os.path.join(carpeta, nombre)
        try:
            if not os.path.isfile(ruta) or os.path.getmtime(ruta) < desde - 2:
                continue
            miniatura = " (audio)." in nombre and nombre.lower().endswith((".jpg", ".webp", ".png"))
            if patron.search(nombre) or miniatura:
                os.remove(ruta)
                n += 1
        except OSError:
            pass
    return n


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def exe_ffmpeg():
    d = ruta_ffmpeg()
    return os.path.join(d, "ffmpeg.exe" if os.name == "nt" else "ffmpeg") if d else "ffmpeg"


def codec_video(ruta):
    """Códec de vídeo real del archivo (hevc, h264, av1…), leído con ffmpeg (no hace falta ffprobe)."""
    try:
        r = subprocess.run([exe_ffmpeg(), "-hide_banner", "-i", ruta], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"Stream #\S+.*?Video: (\w+)", r.stderr)
    return m.group(1).lower() if m else ""


class Preview(VW):
    """Vista previa del enlace: miniatura, título y datos."""

    def __init__(self, app, x, y, w, h, page):
        super().__init__(app, x, y, w, h, page)
        self.estado, self.titulo, self.meta, self.thumb = "vacio", "Pega un enlace para ver la vista previa", "", None

    def set(self, estado, titulo, meta="", thumb=None):
        self.estado, self.titulo, self.meta, self.thumb = estado, titulo, meta, thumb
        self.render()

    def paint(self, img, W, H):
        S = Theme.S
        th = H
        tw = int(th * 16 / 9)
        mask = Theme.rr_mask(tw, th, S(8))
        if self.thumb is not None:
            t = ImageOps.fit(self.thumb, (tw, th), Image.Resampling.LANCZOS).convert("RGBA")
            comp(img, layer(mask, t), 0, 0)
        else:
            comp(img, layer(mask, ELEVATED), 0, 0)
            comp(img, layer(Theme.rr_mask(tw, th, S(8), width=max(1, Theme.scale)), BORDER), 0, 0)
            cx, cy, r = tw / 2, th / 2, S(7)
            ImageDraw.Draw(img).polygon([(cx - r * 0.6, cy - r), (cx - r * 0.6, cy + r), (cx + r, cy)],
                                        fill=DIM + (255,))
        x = tw + S(12)
        w = W - x
        colores = {"vacio": DIM, "cargando": MUTED, "ok": TEXT, "error": (255, 120, 130)}
        f1, f2 = Theme.font("semi", 9), Theme.font("mono", 7)
        if self.meta:
            text(img, (x, H * 0.32), ellipsize(self.titulo, f1, w), f1, colores[self.estado])
            text(img, (x, H * 0.74), ellipsize(self.meta, f2, w), f2, MUTED)
        else:
            text(img, (x, H / 2), ellipsize(self.titulo, f1, w), f1, colores[self.estado])


# ======================================================================
#  Ventana principal
# ======================================================================
W_LOG, H_LOG = 480, 760   # tamaño de diseño (px a 96 PPP)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cola = queue.Queue()
        self.descargando = False
        self.cancelar = threading.Event()
        self.widgets = []
        self.pagina = "dl"
        self.frag_on = False
        self.prefijo = ""
        self.info_actual = {}
        self.preview_url, self.preview_dur, self.preview_meta, self.preview_gen = None, None, "", 0
        self._t_url, self._debounce = None, None
        self.motor_nueva = None

        cfg = self.cargar_config()
        videos = [v for _, v in CALIDADES_VIDEO]
        audios = [v for _, v in CALIDADES_AUDIO]
        navs = [v for _, v in NAVEGADORES]
        self.tipo = cfg.get("tipo") if cfg.get("tipo") in ("video", "mp3") else "video"
        self.sel_video = cfg.get("video") if cfg.get("video", 0) in videos else 1080
        self.sel_audio = cfg.get("audio") if cfg.get("audio") in audios else "320"
        self.navegador = cfg.get("navegador") if cfg.get("navegador") in navs else None
        compat = cfg.get("compat", True) is not False
        carpeta_ini = cfg.get("carpeta") or os.path.join(os.path.expanduser("~"), "Downloads")

        # Escala: la de Windows, reducida si la pantalla no da para la ventana entera.
        escala = 1.0
        try:
            escala = ctypes.windll.user32.GetDpiForSystem() / 96
        except Exception:
            pass
        alto = self.winfo_screenheight()
        Theme.scale = max(0.75, min(escala, (alto - 90) / H_LOG))
        S = Theme.S

        self.title(TITULO)
        self.configure(bg=hx(BG))
        self.geometry(f"{S(W_LOG)}x{S(H_LOG)}")
        self.resizable(False, False)
        try:
            self.iconbitmap(os.path.join(carpeta_res(), "app.ico"))
        except tk.TclError:
            pass
        self.font_native = tkfont.Font(family="Segoe UI", size=-S(13))
        self.font_mono = tkfont.Font(family="Consolas", size=-S(13))
        try:
            self.logo = Image.open(os.path.join(carpeta_res(), "logo.png")).convert("RGBA")
        except OSError:
            self.logo = None

        self.fondo = tk.Label(self, bd=0, highlightthickness=0)
        self.fondo.place(x=0, y=0, relwidth=1, relheight=1)
        self.bg_base = self.fondo_base()
        self.bg_img = self.bg_base

        self.crear_controles(compat, carpeta_ini)
        self.mostrar_pagina("dl")
        self.protocol("WM_DELETE_WINDOW", self.cerrar)
        self.after(100, self.procesar_cola)
        self.after(600, self.latido)
        self.after(50, self.barra_oscura)
        self.after(200, self.comprobar_requisitos)
        if yt_dlp is not None:
            threading.Thread(target=self.buscar_actualizacion, args=(False,), daemon=True).start()

    # ---------- configuración ----------
    def cargar_config(self):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def guardar_config(self):
        d = {"carpeta": self.carpeta(), "navegador": self.navegador, "compat": self.t_compat.checked,
             "tipo": self.tipo, "video": self.sel_video, "audio": self.sel_audio}
        try:
            os.makedirs(APP_DATA, exist_ok=True)
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ---------- fondo ----------
    def fondo_base(self):
        """Negro con brillos violeta/amarillo y rejilla de puntos en la cabecera (como la web)."""
        S = Theme.S
        w, h = S(W_LOG), S(H_LOG)
        img = Image.new("RGBA", (w, h), BG + (255,))
        glow(img, -w * 0.35, -h * 0.22, w * 1.1, h * 0.55, VIOLET, 48)
        glow(img, w * 0.45, -h * 0.05, w * 0.9, h * 0.5, YELLOW, 30)
        glow(img, -w * 0.2, h * 0.7, w * 1.0, h * 0.45, VIOLET, 22)
        puntos = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(puntos)
        step, hh = S(22), S(70)
        y = step / 2
        while y < hh:
            a = int(40 * (1 - y / hh))
            x = step / 2
            while x < w:
                d.ellipse((x - 1, y - 1, x + 1, y + 1), fill=VIOLET + (max(0, a),))
                x += step
            y += step
        d.line((0, hh - 1, w, hh - 1), fill=(255, 255, 255, 18))
        img.alpha_composite(puntos)
        x = S(20)
        if self.logo:
            comp(img, self.logo.resize((S(38), S(38)), Image.Resampling.LANCZOS), x, S(16))
        tx = x + S(50)
        ft = Theme.font("display", 16)
        text(img, (tx, S(27)), "Descargador", ft, TEXT)
        gradient_text(img, (tx + ft.getlength("Descargador") + S(7), S(27)), "VOD", ft)
        text(img, (tx + 1, S(48)), f"BY VORTEXS · YOUTUBE · TWITCH · V{VERSION}",
             Theme.font("mono", 7), MUTED)
        return img

    def fondo_pagina(self):
        S = Theme.S
        img = self.bg_base.copy()
        cap = Theme.font("mono", 7)
        nota = Theme.font("body", 8)

        def card(x, y, w, h, titulo, accent=False):
            paint_card(img, S(x), S(y), S(w), S(h), accent)
            if titulo:
                text(img, (S(x + 20), S(y + 20)), titulo, cap, MUTED)

        if self.pagina == "dl":
            card(20, 134, 440, 170, "ENLACES · YOUTUBE O TWITCH")
            card(20, 316, 440, 136, "FORMATO")
            card(20, 464, 440, 164, "FRAGMENTO", accent=self.frag_on)
            col = MUTED if self.frag_on else DIM
            text(img, (S(36), S(514)), "DESDE", cap, col)
            text(img, (S(252), S(514)), "HASTA", cap, col)
            fc = Theme.font("display", 13)
            for x0 in (36, 252):
                for k in (0, 1):
                    text(img, (S(x0 + 50 + 56 * k), S(542)), ":", fc, col, anchor="mm")
            text(img, (S(36), S(578)), "Deja «Hasta» en 00:00:00 para descargar hasta el final del vídeo.", nota, col)
        elif self.pagina == "log":
            card(20, 134, 440, 542, "REGISTRO DE DESCARGAS")
        else:
            card(20, 134, 440, 150, "SESIÓN DEL NAVEGADOR")
            text(img, (S(36), S(176)), "Úsala si YouTube te pide iniciar sesión («no soy un bot»),", nota, MUTED)
            text(img, (S(36), S(193)), "o para vídeos +18 y VODs de Twitch solo para suscriptores.", nota, MUTED)
            text(img, (S(36), S(264)), "Firefox es el que mejor funciona. Con Chrome o Edge, ciérralos antes.", nota, DIM)
            card(20, 296, 440, 96, "COMPATIBILIDAD")
            text(img, (S(36), S(366)), "Sin esto, YouTube puede usar AV1, que va a tirones en PCs antiguos.", nota, DIM)
            card(20, 404, 440, 96, "CARPETA DE DESCARGA")
            card(20, 512, 440, 134, "MOTOR DE DESCARGA · YT-DLP")
        return img

    def mostrar_pagina(self, p):
        self.pagina = p
        self.bg_img = self.fondo_pagina()
        self.foto_fondo = ImageTk.PhotoImage(self.bg_img)
        self.fondo.configure(image=self.foto_fondo)
        for w in self.widgets:
            if w.page in (None, p):
                w.show()
            else:
                w.hide()

    def redibujar_fondo(self):
        self.mostrar_pagina(self.pagina)

    def barra_oscura(self):
        """Barra de título oscura (Windows 10 20H1+ / 11), como Theme.DarkTitleBar."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi
            on = ctypes.c_int(1)
            if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4) != 0:
                dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(on), 4)
            cap = ctypes.c_int(BG[0] | BG[1] << 8 | BG[2] << 16)
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)
            borde = ctypes.c_int(0x3A2A33)
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(borde), 4)
        except Exception:
            pass

    # ---------- controles ----------
    def grupo_pills(self, lista, x, y, w, h, page, seleccion, al_elegir, size=8):
        n, gap = len(lista), 6
        ancho = (w - gap * (n - 1)) / n
        grupo = []
        for i, (nombre, valor) in enumerate(lista):
            b = VButton(self, x + i * (ancho + gap), y, ancho, h, page, nombre, kind="pill", size=size)
            b.valor = valor
            b.selected = valor == seleccion
            grupo.append(b)
        for b in grupo:
            b.command = lambda b=b, g=grupo, cb=al_elegir: self._elegir(b, g, cb)
        return grupo

    def _elegir(self, boton, grupo, cb):
        for b in grupo:
            b.selected = b is boton
            b.render()
        cb(boton.valor)
        self.guardar_config()

    def crear_controles(self, compat, carpeta_ini):
        self.pill = StatusPill(self, 300, 21, 160, 28)
        self.tabs = Tabs(self, 20, 82, 440, 40, None, ["Descargar", "Registro", "Opciones"],
                         on_change=lambda i: self.mostrar_pagina(("dl", "log", "opt")[i]))

        # ----- Descargar: enlaces + vista previa -----
        VButton(self, 344, 144, 100, 20, "dl", "Pegar enlace", self.pegar, kind="link", size=8.5)
        self.f_urls = Field(self, 36, 168, 408, 64, "dl", multiline=True)
        self.placeholder = True
        self.f_urls.native.insert("1.0", "Pega aquí los enlaces, uno por línea…")
        self.f_urls.native.configure(fg=hx(DIM))
        self.f_urls.native.bind("<FocusIn>", self._urls_in, add="+")
        self.f_urls.native.bind("<FocusOut>", self._urls_out, add="+")
        self.f_urls.native.bind("<KeyRelease>", self._urls_cambio, add="+")
        self.f_urls.native.bind("<<Paste>>", self._urls_cambio, add="+")
        self.preview = Preview(self, 36, 242, 408, 48, "dl")

        # ----- Formato -----
        self.t_lista = Toggle(self, 300, 325, 150, 22, "dl", "Lista completa")
        self.t_tipo = Tabs(self, 36, 352, 408, 38, "dl", ["Vídeo  MP4", "Solo audio  MP3"],
                           on_change=self.cambiar_tipo, selected=0 if self.tipo == "video" else 1)
        self.p_video = self.grupo_pills(CALIDADES_VIDEO, 36, 400, 408, 34, "dl", self.sel_video,
                                        lambda v: setattr(self, "sel_video", v))
        self.p_audio = self.grupo_pills(CALIDADES_AUDIO, 36, 400, 408, 34, "dl", self.sel_audio,
                                        lambda v: setattr(self, "sel_audio", v))
        for b in (self.p_audio if self.tipo == "video" else self.p_video):
            b.page = "oculto"

        # ----- Fragmento -----
        self.t_frag = Toggle(self, 300, 473, 150, 22, "dl", "Solo una parte", on_change=self.cambiar_fragmento)
        self.tiempos = []
        vcmd = (self.register(lambda p: p == "" or (p.isdigit() and len(p) <= 2)), "%P")
        for x0 in (36, 252):
            grupo = []
            for k in range(3):
                f = Field(self, x0 + 56 * k, 524, 44, 38, "dl", mono=True, center=True)
                f.native.insert(0, "00")
                f.native.configure(validate="key", validatecommand=vcmd)
                f.native.bind("<FocusIn>", lambda e, w=f.native: w.after(1, lambda: w.select_range(0, "end")), add="+")
                grupo.append(f)
            self.tiempos.append(grupo)
        self.t_exacto = Toggle(self, 36, 592, 408, 24, "dl", "Cortes exactos al segundo (más lento, recodifica)")

        # ----- Progreso y acciones -----
        self.progreso = Progress(self, 20, 642, 440, 10, "dl")
        self.lbl_estado = Label(self, 20, 658, 356, 22, "dl", "Listo.")
        self.lbl_pct = Label(self, 380, 658, 80, 22, "dl", "", kind="display", size=11, color=TEXT, anchor="rm")
        self.btn = VButton(self, 20, 688, 290, 52, "dl", "Descargar", self.iniciar, kind="primary", size=10.5)
        self.btn2 = VButton(self, 318, 688, 142, 52, "dl", "Abrir carpeta", self.abrir_carpeta, size=9.5)

        # ----- Registro -----
        self.f_log = Field(self, 36, 170, 408, 490, "log", multiline=True, readonly=True)
        self.f_log.native.configure(font=tkfont.Font(family="Consolas", size=-Theme.S(12)))
        VButton(self, 20, 688, 214, 52, "log", "Copiar registro", self.copiar_log, size=9.5)
        VButton(self, 246, 688, 214, 52, "log", "Abrir carpeta", self.abrir_carpeta, size=9.5)

        # ----- Opciones -----
        self.p_nav = self.grupo_pills(NAVEGADORES, 36, 212, 408, 34, "opt", self.navegador,
                                      self.cambiar_navegador, size=7.5)
        self.t_compat = Toggle(self, 36, 330, 408, 24, "opt", "Priorizar H.264 (se abre en cualquier PC y editor)",
                               on_change=lambda v: self.guardar_config(), checked=compat)
        self.f_carpeta = Field(self, 36, 438, 300, 42, "opt")
        self.f_carpeta.native.insert(0, carpeta_ini)
        self.f_carpeta.native.bind("<FocusOut>", lambda e: self.guardar_config(), add="+")
        VButton(self, 344, 438, 100, 42, "opt", "Elegir…", self.elegir_carpeta, size=9)
        origen = "actualizado desde la app" if MOTOR_ACTUALIZADO else "incluido en el programa"
        self.lbl_motor = Label(self, 36, 548, 408, 22, "opt", f"Versión {version_motor()} · {origen}",
                               kind="semi", size=9, color=TEXT)
        self.lbl_motor2 = Label(self, 36, 570, 408, 20, "opt", "Comprobando si hay una versión nueva…", size=8)
        self.btn_motor = VButton(self, 36, 596, 220, 36, "opt", "Buscar actualización", self.actualizar_motor,
                                 kind="primary", size=9)
        VButton(self, 20, 688, 214, 52, "opt", "Guía de uso", self.abrir_ayuda, size=9.5)
        VButton(self, 246, 688, 214, 52, "opt", "Abrir carpeta", self.abrir_carpeta, size=9.5)

        self.cambiar_fragmento(False)

    # ---------- enlaces, vista previa y ?t= ----------
    def _urls_in(self, e):
        if self.placeholder:
            self.placeholder = False
            self.f_urls.native.delete("1.0", "end")
            self.f_urls.native.configure(fg=hx(TEXT))

    def _urls_out(self, e):
        if not self.f_urls.native.get("1.0", "end").strip():
            self.placeholder = True
            self.f_urls.native.insert("1.0", "Pega aquí los enlaces, uno por línea…")
            self.f_urls.native.configure(fg=hx(DIM))
        self._urls_cambio()

    def _urls_cambio(self, e=None):
        if self._debounce:
            self.after_cancel(self._debounce)
        self._debounce = self.after(700, self.revisar_enlace)

    def urls(self):
        if self.placeholder:
            return []
        return [u.strip() for u in self.f_urls.native.get("1.0", "end").splitlines() if u.strip()]

    def pegar(self):
        try:
            t = self.clipboard_get().strip()
        except tk.TclError:
            return
        if not t:
            return
        self._urls_in(None)
        actual = self.f_urls.native.get("1.0", "end").strip()
        self.f_urls.native.insert("end", ("\n" if actual else "") + t)
        self.f_urls.native.see("end")
        self._urls_cambio()

    def revisar_enlace(self):
        self._debounce = None
        urls = self.urls()
        if not urls:
            self.preview_url, self.preview_dur = None, None
            self.preview.set("vacio", "Pega un enlace para ver la vista previa")
            return
        url = urls[0]
        extra = f"  ·  +{len(urls) - 1} MÁS" if len(urls) > 1 else ""
        t = leer_t(url)
        if t and url != self._t_url and not self.descargando:
            self._t_url = url
            self.poner_desde(t)
        if url == self.preview_url:
            if self.preview.estado == "ok":
                self.preview.set("ok", self.preview.titulo, self.preview_meta + extra, self.preview.thumb)
            return
        if yt_dlp is None:
            return
        self.preview_url, self.preview_dur = url, None
        self.preview_gen += 1
        self.preview.set("cargando", "Cargando vista previa…")
        threading.Thread(target=self.cargar_preview, args=(url, self.preview_gen, self.navegador, extra),
                         daemon=True).start()

    def cargar_preview(self, url, gen, nav, extra):
        try:
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
            if nav:
                opts["cookiesfrombrowser"] = (nav,)
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=False, process=False)
                for _ in range(2):
                    if info.get("_type") in ("url", "url_transparent") and info.get("url"):
                        info = y.extract_info(info["url"], download=False, process=False)
                    else:
                        break
            es_lista = info.get("_type") == "playlist"
            titulo = info.get("title") or info.get("id") or url
            dur = None if es_lista else info.get("duration")
            sitio = (info.get("extractor_key") or info.get("ie_key") or "").lower()
            sitio = "TWITCH" if "twitch" in sitio else ("YOUTUBE" if "youtube" in sitio else sitio.upper())
            autor = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
            partes = [sitio, "LISTA" if es_lista else "", duracion_txt(dur) if dur else "", autor]
            meta = " · ".join(p for p in partes if p)
            thumb = self.bajar_miniatura(info)
            self.cola.put(("preview", (gen, "ok", titulo, meta, extra, thumb, dur)))
        except Exception as e:
            msg = limpiar_msg(e)
            self.cola.put(("preview", (gen, "error", "No se pudo leer el enlace",
                                       pista_error(msg, url, nav) or msg[:140], "", None, None)))

    @staticmethod
    def bajar_miniatura(info):
        try:
            url = None
            if (info.get("extractor_key") or "").lower().startswith("youtube") and info.get("id") \
                    and info.get("_type") != "playlist":
                url = f"https://i.ytimg.com/vi/{info['id']}/mqdefault.jpg"
            if not url:
                url = info.get("thumbnail")
            if not url:
                thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
                url = thumbs[-1]["url"] if thumbs else None
            if not url:
                return None
            for a, b in (("%{width}", "320"), ("%{height}", "180"), ("{width}", "320"), ("{height}", "180")):
                url = url.replace(a, b)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read(5_000_000)
            im = Image.open(io.BytesIO(data))
            im.load()
            return im.convert("RGB")
        except Exception:
            return None

    def poner_desde(self, t):
        """Rellena «Desde» con el tiempo del enlace y activa el fragmento."""
        if not self.frag_on:
            self.t_frag.checked = True
            self.cambiar_fragmento(True)
        for f, v in zip(self.tiempos[0], (t // 3600, t % 3600 // 60, t % 60)):
            f.native.delete(0, "end")
            f.native.insert(0, f"{v:02d}")
        self.lbl_estado.set(f"Inicio puesto en {duracion_txt(t)} (venía en el enlace).", VIOLET_LIGHT)

    # ---------- resto de la interfaz ----------
    def cambiar_tipo(self, i):
        self.tipo = "video" if i == 0 else "mp3"
        for b in self.p_video:
            b.page = "dl" if i == 0 else "oculto"
        for b in self.p_audio:
            b.page = "dl" if i == 1 else "oculto"
        for b in self.p_video + self.p_audio:
            b.show() if b.page == "dl" and self.pagina == "dl" else b.hide()
        self.guardar_config()

    def cambiar_navegador(self, v):
        self.navegador = v
        self.preview_url = None  # que la vista previa se repita con la sesión nueva
        self._urls_cambio()

    def cambiar_fragmento(self, on):
        self.frag_on = on
        for grupo in self.tiempos:
            for f in grupo:
                f.set_enabled(on)
        self.t_exacto.set_enabled(on)
        if self.tabs.visible:
            self.redibujar_fondo()

    def comprobar_requisitos(self):
        if yt_dlp is None:
            messagebox.showerror("Falta yt-dlp", "Instala yt-dlp con:\n\npip install -U yt-dlp")
        if ruta_ffmpeg() is None:
            messagebox.showwarning(
                "Falta FFmpeg",
                "No se encontró FFmpeg, necesario para el MP3, la alta calidad y los fragmentos.\n\n"
                "Si te han pasado el programa en un .zip: haz clic derecho en el zip → "
                "'Extraer todo' y abre DescargadorYouTube.exe desde la carpeta extraída, "
                "junto a ffmpeg.exe.")

    def carpeta(self):
        return self.f_carpeta.native.get().strip() or os.path.join(os.path.expanduser("~"), "Downloads")

    def abrir_carpeta(self):
        d = self.carpeta()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except OSError as e:
            messagebox.showerror("Carpeta", str(e))

    def abrir_ayuda(self):
        import webbrowser
        for nombre in ("LEEME - Guia de uso.html", "Guia.html"):
            ruta = os.path.join(carpeta_app(), nombre)
            if os.path.isfile(ruta):
                webbrowser.open("file:///" + ruta.replace("\\", "/"))
                return
        messagebox.showinfo(
            "Ayuda",
            "1. Copia el enlace del vídeo de YouTube o del VOD de Twitch.\n"
            "2. Pégalo arriba (Ctrl+V o 'Pegar enlace'). Puedes poner varios, uno por línea.\n"
            "3. Elige vídeo o MP3 y la calidad.\n"
            "4. Si solo quieres una parte, activa 'Solo una parte' y pon Desde / Hasta.\n"
            "5. Pulsa Descargar.")

    def elegir_carpeta(self):
        d = filedialog.askdirectory(initialdir=self.carpeta())
        if d:
            self.f_carpeta.native.delete(0, "end")
            self.f_carpeta.native.insert(0, os.path.normpath(d))
            self.guardar_config()

    def escribir_log(self, texto):
        t = self.f_log.native
        t.configure(state="normal")
        t.insert("end", texto + "\n")
        t.see("end")
        t.configure(state="disabled")

    def copiar_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.f_log.native.get("1.0", "end").strip())

    def latido(self):
        self.pill.pulse = not self.pill.pulse
        if self.pill.estado == "descargando":
            self.pill.render()
        self.after(600, self.latido)

    def modo_descarga(self, on):
        self.descargando = on
        if on:
            self.btn.set_text("Descargando…")
            self.btn.set_enabled(False)
            self.btn2.kind, self.btn2.text, self.btn2.command = "danger", "Cancelar", self.pedir_cancelar
            self.btn2.set_enabled(True)
            self.progreso.set(0)
            self.lbl_pct.set("")
            self.pill.set("descargando")
        else:
            self.btn.set_text("Descargar")
            self.btn.set_enabled(True)
            self.btn2.kind, self.btn2.text, self.btn2.command = "ghost", "Abrir carpeta", self.abrir_carpeta
            self.btn2.set_enabled(True)
            self.title(TITULO)

    def pedir_cancelar(self):
        if not self.descargando or self.cancelar.is_set():
            return
        self.cancelar.set()
        self.btn2.text = "Cancelando…"
        self.btn2.set_enabled(False)
        self.lbl_estado.set("Cancelando…", YELLOW)
        matar_procesos()

    def cerrar(self):
        if self.descargando:
            if not messagebox.askyesno("Descarga en curso", "Hay una descarga en marcha. ¿Cancelarla y salir?"):
                return
            self.cancelar.set()
            matar_procesos()
        self.guardar_config()
        self.destroy()

    # ---------- motor (yt-dlp) ----------
    def actualizar_motor(self):
        if self.motor_nueva:
            self.btn_motor.set_text("Descargando…")
            self.btn_motor.set_enabled(False)
            self.lbl_motor2.set(f"Descargando yt-dlp {self.motor_nueva}…", MUTED)
            threading.Thread(target=self.instalar_motor, args=(self.motor_nueva,), daemon=True).start()
        else:
            self.btn_motor.set_enabled(False)
            self.lbl_motor2.set("Comprobando…", MUTED)
            threading.Thread(target=self.buscar_actualizacion, args=(True,), daemon=True).start()

    def buscar_actualizacion(self, manual):
        try:
            tag = ultima_version_motor()
        except Exception as e:
            self.cola.put(("motor", ("error", f"No se pudo comprobar ({limpiar_msg(e)[:60]})." if manual
                                     else "No se pudo comprobar si hay versión nueva (¿sin internet?).")))
            return
        if version_tupla(tag) > version_tupla(version_motor()):
            self.cola.put(("motor", ("nueva", tag)))
        else:
            self.cola.put(("motor", ("aldia", tag)))

    def instalar_motor(self, tag):
        tmp = MOTOR_ZIP + ".descarga"
        try:
            os.makedirs(APP_DATA, exist_ok=True)
            url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/yt-dlp"
            req = urllib.request.Request(url, headers={"User-Agent": "DescargadorVOD"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            v = version_en_zip(tmp)
            if not v:
                raise ValueError("el archivo descargado no es válido")
            os.replace(tmp, MOTOR_ZIP)
            self.cola.put(("motor", ("instalado", v)))
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.cola.put(("motor", ("error", f"No se pudo actualizar ({limpiar_msg(e)[:60]}).")))

    def evento_motor(self, tipo, valor):
        self.btn_motor.set_enabled(True)
        if tipo == "nueva":
            self.motor_nueva = valor
            self.lbl_motor2.set(f"Hay una versión nueva: {valor}", VIOLET_LIGHT)
            self.btn_motor.set_text(f"Actualizar a {valor}")
            self.tabs.items[2] = "Opciones  •"
            self.tabs.render()
            if not self.descargando:
                self.lbl_estado.set("Hay una actualización del motor de descarga: ve a Opciones.", VIOLET_LIGHT)
        elif tipo == "aldia":
            self.motor_nueva = None
            self.lbl_motor2.set("Estás al día.", GREEN)
            self.btn_motor.set_text("Buscar actualización")
        elif tipo == "instalado":
            self.motor_nueva = None
            self.tabs.items[2] = "Opciones"
            self.tabs.render()
            self.lbl_motor2.set(f"Instalado yt-dlp {valor}. Se usará al reiniciar.", GREEN)
            self.btn_motor.set_text("Buscar actualización")
            if not self.descargando and messagebox.askyesno(
                    "Motor actualizado", f"Se ha instalado yt-dlp {valor}.\n\n¿Reiniciar el programa ahora para usarlo?"):
                self.reiniciar()
        elif tipo == "error":
            self.lbl_motor2.set(valor, (255, 120, 130))
            self.btn_motor.set_text(f"Actualizar a {self.motor_nueva}" if self.motor_nueva else "Buscar actualización")

    def reiniciar(self):
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        try:
            subprocess.Popen(args, env=env, close_fds=True)
        except OSError as e:
            messagebox.showerror("Reiniciar", f"Ábrelo tú de nuevo: {e}")
        self.guardar_config()
        self.destroy()

    # ---------- cola de mensajes del hilo de descarga ----------
    def procesar_cola(self):
        try:
            while True:
                tipo, valor = self.cola.get_nowait()
                if tipo == "progreso":
                    self.progreso.set(valor)
                    self.lbl_pct.set(f"{valor:.0f} %")
                    self.title(f"{valor:.0f} % · {TITULO}")
                elif tipo == "indeterminado":
                    self.progreso.start()
                    self.lbl_pct.set("")
                elif tipo == "estado":
                    self.lbl_estado.set(valor, MUTED)
                elif tipo == "pill":
                    self.pill.set(valor)
                elif tipo == "log":
                    self.escribir_log(valor)
                elif tipo == "preview":
                    gen, estado, titulo, meta, extra, thumb, dur = valor
                    if gen == self.preview_gen:
                        self.preview_dur = dur
                        self.preview_meta = meta if estado == "ok" else ""
                        self.preview.set(estado, titulo, (meta + extra) if estado == "ok" else meta, thumb)
                elif tipo == "motor":
                    self.evento_motor(*valor)
                elif tipo == "fin":
                    ok, total = valor
                    self.progreso.set(100 if ok else 0)
                    self.lbl_pct.set("100 %" if ok == total else "")
                    self.lbl_estado.set(f"Terminado: {ok}/{total} correctos.", GREEN if ok == total else RED)
                    self.pill.set("terminado" if ok == total else "error")
                    self.modo_descarga(False)
                elif tipo == "cancelado":
                    ok, total, borrados = valor
                    self.progreso.set(0)
                    self.lbl_pct.set("")
                    extra = f" Se completaron {ok} de {total}." if ok else ""
                    self.lbl_estado.set(f"Descarga cancelada.{extra}", MUTED)
                    self.pill.set("cancelado")
                    self.modo_descarga(False)
        except queue.Empty:
            pass
        self.after(100, self.procesar_cola)

    # ---------- descarga ----------
    def leer_rango(self):
        """None si no hay fragmento; (inicio, fin) en segundos (fin puede ser inf).
        Lanza ValueError con un mensaje para el usuario si el rango no es válido."""
        if not self.frag_on:
            return None
        valores = []
        for grupo in self.tiempos:
            h, m, s = (int(f.native.get().strip() or 0) for f in grupo)
            if m > 59 or s > 59:
                raise ValueError("Los minutos y los segundos van de 0 a 59.")
            valores.append(h * 3600 + m * 60 + s)
        ini, fin = valores
        if fin == 0:
            if ini == 0:
                raise ValueError("Pon al menos un \"Desde\" o un \"Hasta\" para el fragmento.")
            fin = float("inf")
        if fin <= ini:
            raise ValueError("\"Hasta\" tiene que ser mayor que \"Desde\".")
        return ini, fin

    def iniciar(self):
        if self.descargando or yt_dlp is None:
            return
        urls = self.urls()
        if not urls:
            messagebox.showinfo("Sin enlaces", "Pega al menos un enlace.")
            return
        try:
            rango = self.leer_rango()
        except ValueError as e:
            messagebox.showwarning("Fragmento no válido", str(e))
            return
        if rango and len(urls) == 1 and self.preview_url == urls[0] and self.preview_dur \
                and rango[0] >= self.preview_dur:
            messagebox.showwarning("Fragmento no válido",
                                   f"El vídeo dura {duracion_txt(self.preview_dur)} y «Desde» se pasa de esa duración.")
            return
        if rango and ruta_ffmpeg() is None:
            messagebox.showerror("Falta FFmpeg", "Para descargar un fragmento hace falta FFmpeg.")
            return
        carpeta = self.carpeta()
        try:
            os.makedirs(carpeta, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Carpeta no válida", str(e))
            return
        self.guardar_config()
        valor = self.sel_video if self.tipo == "video" else self.sel_audio
        self.cancelar.clear()
        self.modo_descarga(True)
        threading.Thread(target=self.descargar,
                         args=(urls, self.tipo, valor, carpeta, self.t_lista.checked, rango,
                               self.t_exacto.checked, self.navegador, self.t_compat.checked),
                         daemon=True).start()

    def _ver_info(self, info, *a, **k):
        """match_filter: guarda los datos del vídeo justo antes de descargarlo (para el % del fragmento)."""
        if info.get("duration"):
            self.info_actual = info
        return None

    def opciones(self, tipo, valor, carpeta, playlist, rango, exacto, nav=None, compat=True, progreso=None):
        sufijo = ""
        if rango:
            ini, fin = rango
            fin_txt = "final" if fin == float("inf") else formato_tiempo(fin)
            sufijo = f" ({formato_tiempo(ini)}-{fin_txt})"
        # Windows no admite rutas de más de 260 caracteres: el título se recorta según la carpeta.
        largo = max(20, min(120, 240 - len(os.path.abspath(carpeta)) - len(sufijo) - 40))
        titulo = f"%(title).{largo}s [%(id)s]"
        opts = {
            "outtmpl": os.path.join(carpeta, titulo + sufijo + ".%(ext)s"),
            "noplaylist": not playlist,
            "progress_hooks": [self.hook],
            "postprocessor_hooks": [self.hook_post],
            "match_filter": self._ver_info,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "windowsfilenames": True,
        }
        ff = ruta_ffmpeg()
        if ff:
            opts["ffmpeg_location"] = ff
        if nav:
            opts["cookiesfrombrowser"] = (nav,)
        if rango:
            opts["download_ranges"] = download_range_func(None, [rango])
            opts["force_keyframes_at_cuts"] = exacto
            if progreso:
                # FFmpeg no avisa a yt-dlp de su avance: le pedimos que lo escriba en un archivo.
                opts["external_downloader_args"] = {"ffmpeg_o": ["-progress", progreso, "-stats_period", "0.5"]}
        if tipo == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": valor},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ]
            opts["writethumbnail"] = True
            # En Twitch el audio llega como .mp4: con un nombre distinto evitamos que reutilice
            # (y luego borre) un vídeo .mp4 del mismo título que ya esté en la carpeta.
            opts["outtmpl"] = os.path.join(carpeta, titulo + sufijo + " (audio).%(ext)s")
        else:
            # 60 fps primero: se ordena por fps (hasta 60) y después por resolución.
            # "res:720" = la mayor que no pase de 720p; si a esa altura no hay 60 fps se elige la
            # más pequeña por encima que sí los tenga (p. ej. Twitch 480p30 -> 720p60).
            # Con "compat", a igual fps y resolución se prefiere H.264 (no AV1/VP9).
            orden = ["fps:60", f"res:{valor}" if valor else "res"]
            if compat:
                orden.append("vcodec:h264")
            opts["format_sort"] = orden
            # En Twitch el vídeo y el audio vienen juntos, así que entra por "b[...]".
            opts["format"] = ("bv[fps>=50][ext=mp4]+ba[ext=m4a]/bv[fps>=50]+ba/b[fps>=50]/"
                              "bv[ext=mp4]+ba[ext=m4a]/bv+ba/b")
            opts["merge_output_format"] = "mp4"
        return opts

    def hook(self, d):
        if self.cancelar.is_set():
            raise Cancelado()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            fi, fc = d.get("fragment_index"), d.get("fragment_count")
            if fc:
                self.cola.put(("progreso", min(100, 100 * (fi or 0) / fc)))
            elif total:
                self.cola.put(("progreso", min(100, d.get("downloaded_bytes", 0) * 100 / total)))
            info = d.get("info_dict") or {}
            parte = "audio" if info.get("vcodec") == "none" else ("vídeo" if info.get("acodec") == "none" else "")
            vel = ANSI.sub("", d.get("_speed_str") or "").strip()
            eta = ANSI.sub("", d.get("_eta_str") or "").strip()
            datos = "  ·  ".join(x for x in (vel, f"quedan {eta}" if eta and eta != "Unknown" else "") if x)
            self.cola.put(("estado", f"{self.prefijo} Descargando {parte}…  {datos}".replace("  …", "…")))
            self.cola.put(("pill", "descargando"))
        elif d["status"] == "finished":
            self.cola.put(("progreso", 100))
            self.cola.put(("estado", f"{self.prefijo} Procesando con FFmpeg…"))
            self.cola.put(("pill", "procesando"))

    def hook_post(self, d):
        if self.cancelar.is_set():
            raise Cancelado()
        if d["status"] == "started":
            self.cola.put(("estado", f"{self.prefijo} Procesando: {d.get('postprocessor', '')}…"))
            self.cola.put(("pill", "procesando"))

    def _dur_fragmento(self, rango):
        ini, fin = rango
        if fin != float("inf"):
            return fin - ini
        d = self.info_actual.get("duration")
        return d - ini if d and d > ini else None

    def vigilar_ffmpeg(self, ruta, rango, fin_evento, etiqueta="Descargando fragmento"):
        """Lee el archivo de progreso de FFmpeg y calcula el % del fragmento."""
        while not fin_evento.wait(0.4):
            try:
                with open(ruta, "rb") as f:
                    f.seek(0, 2)
                    f.seek(max(0, f.tell() - 4096))
                    txt = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            tiempos = re.findall(r"out_time_(?:us|ms)=(\d+)", txt)
            if not tiempos or self.cancelar.is_set():
                continue
            t = int(tiempos[-1]) / 1e6
            tam = re.findall(r"total_size=(\d+)", txt)
            vel = re.findall(r"speed=\s*([\d.]+)x", txt)
            vx = float(vel[-1]) if vel else 0
            dur = self._dur_fragmento(rango)
            datos = [f"{int(tam[-1]) / 1048576:.0f} MB" if tam else "", f"{vx:.1f}x" if vx else ""]
            if dur:
                self.cola.put(("progreso", max(0.0, min(99.9, 100 * t / dur))))
                if vx > 0:
                    datos.append(f"quedan {duracion_txt((dur - t) / vx)}")
            self.cola.put(("estado", f"{self.prefijo} {etiqueta}…  " +
                           "  ·  ".join(x for x in datos if x)))

    def convertir_h264(self, ruta, duracion, prog):
        """Recodifica a H.264 (para archivos HEVC/AV1 que muchos PCs no abren)."""
        tmp = os.path.splitext(ruta)[0] + ".h264.temp.mp4"
        try:
            os.remove(prog)
        except OSError:
            pass
        args = [exe_ffmpeg(), "-hide_banner", "-y", "-i", ruta, "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-movflags", "+faststart", "-progress", prog, "-stats_period", "0.5", tmp]
        fin_evento = threading.Event()
        if duracion:
            self.cola.put(("progreso", 0))
        else:
            self.cola.put(("indeterminado", None))
        self.cola.put(("pill", "procesando"))
        threading.Thread(target=self.vigilar_ffmpeg, args=(prog, (0, duracion or float("inf")), fin_evento,
                                                           "Convirtiendo a H.264"), daemon=True).start()
        try:
            p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            PROCESOS.add(p)
            rc = p.wait()
        finally:
            fin_evento.set()
        if self.cancelar.is_set() or rc != 0:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if self.cancelar.is_set():
                raise Cancelado()
            raise RuntimeError("no se pudo convertir el vídeo a H.264")
        os.replace(tmp, ruta)
        self.cola.put(("progreso", 100))

    def descargar(self, urls, tipo, valor, carpeta, playlist, rango, exacto, nav, compat):
        ok, n, cancelado = 0, len(urls), False
        inicio = time.time()
        prog = os.path.join(tempfile.gettempdir(), f"descargadorvod_{os.getpid()}.progress")
        for i, url in enumerate(urls, 1):
            if self.cancelar.is_set():
                cancelado = True
                break
            self.prefijo = f"[{i}/{n}]"
            self.info_actual = {}
            self.cola.put(("log", f"[{i}/{n}] {url}"))
            self.cola.put(("pill", "descargando"))
            fin_evento = threading.Event()
            if rango:
                ini, fin = rango
                fin_txt = "el final" if fin == float("inf") else formato_tiempo(fin)
                self.cola.put(("log", f"   Fragmento: de {formato_tiempo(ini)} a {fin_txt}"))
                try:
                    os.remove(prog)
                except OSError:
                    pass
                self.cola.put(("indeterminado", None))
                self.cola.put(("estado", f"[{i}/{n}] Preparando el fragmento…"))
                threading.Thread(target=self.vigilar_ffmpeg, args=(prog, rango, fin_evento), daemon=True).start()
            else:
                self.cola.put(("progreso", 0))
                self.cola.put(("estado", f"[{i}/{n}] Preparando…"))
            try:
                with yt_dlp.YoutubeDL(self.opciones(tipo, valor, carpeta, playlist, rango, exacto,
                                                    nav, compat, prog)) as ydl:
                    info = ydl.extract_info(url, download=True)
                if self.cancelar.is_set():
                    raise Cancelado()
                titulo = info.get("title", "") if info else ""
                codec = nombre_codec(info.get("vcodec")) if info else ""
                if info and tipo == "video":
                    ruta = (info.get("requested_downloads") or [{}])[0].get("filepath") or info.get("filepath")
                    real = codec_video(ruta) if ruta and os.path.isfile(ruta) else ""
                    if real:
                        codec = nombre_codec({"hevc": "hev1", "h264": "avc1", "av1": "av01"}.get(real, real))
                    if compat and real in ("hevc", "h265", "av1"):
                        self.cola.put(("log", f"   Viene en {codec}: convirtiendo a H.264 para que se abra en cualquier PC…"))
                        dur = self._dur_fragmento(rango) if rango else info.get("duration")
                        self.convertir_h264(ruta, dur, prog)
                        codec = "H.264 (convertido)"
                if info and tipo == "video" and info.get("height"):
                    alto, fps = info["height"], round(info.get("fps") or 0)
                    nota = ""
                    if valor and alto > valor:
                        nota = f"  (subido desde {valor}p para tener {fps} fps)"
                    elif fps and fps < 50:
                        nota = "  (el vídeo original no tiene 60 fps)"
                    self.cola.put(("log", f"   Formato: {alto}p · {fps or '?'} fps · {codec}{nota}"))
                self.cola.put(("log", f"   ✔ Completado: {titulo}"))
                ok += 1
            except Exception as e:
                if self.cancelar.is_set() or isinstance(e, Cancelado):
                    cancelado = True
                    break
                msg = limpiar_msg(e)
                self.cola.put(("log", f"   ✘ Error: {msg}"))
                pista = pista_error(msg, url, nav)
                if pista:
                    self.cola.put(("log", f"   → {pista}"))
                    self.cola.put(("estado", pista))
            finally:
                fin_evento.set()
        try:
            os.remove(prog)
        except OSError:
            pass
        if cancelado:
            time.sleep(0.5)  # que FFmpeg suelte los archivos antes de borrarlos
            borrados = limpiar_parciales(carpeta, inicio)
            extra = f" Borrados {borrados} archivos a medias." if borrados else ""
            self.cola.put(("log", f"Cancelado: {ok}/{n} completados.{extra}\n"))
            self.cola.put(("cancelado", (ok, n, borrados)))
        else:
            self.cola.put(("log", f"Terminado: {ok}/{n} correctos. Carpeta: {carpeta}\n"))
            self.cola.put(("fin", (ok, n)))


def activar_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if __name__ == "__main__":
    activar_dpi()
    App().mainloop()
