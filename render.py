"""Renderiza el dashboard del Kindle (y las notas para leer) como PNG en escala de grises.

Fuentes: Open-Meteo (clima), La Nación RSS (noticias, trae el texto completo de cada nota).
Salida:
  out/dash_<W>x<H>.png            dashboard por tamaño de pantalla (+ out/dash.png)
  out/<KEY>/zones_<W>x<H>.txt     una línea por noticia: "<y0> <y1> <id> <páginas>"
  out/<KEY>/<id>_<p>_<W>x<H>.png  páginas de cada nota
<KEY> sale del secret ARTICLES_KEY: las notas no quedan en una URL pública adivinable.
La franja superior del dashboard queda en blanco: ahí el Kindle dibuja la hora cada minuto.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
OUT = ROOT / "out"
FONTS = ROOT / "fonts"
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
LAT, LON = -34.61, -58.38  # CABA

# Kindle de 6"/7" más comunes (portrait). El Kindle elige el suyo por resolución.
SIZES = [(600, 800), (758, 1024), (1072, 1448), (1236, 1648), (1264, 1680)]
DEFAULT_SIZE = (1072, 1448)
CLOCK_BAND = 0.12  # fracción de alto reservada para la hora que dibuja el Kindle
BACK_BAND = 0.12   # en una nota, tocar esta franja de arriba vuelve al dashboard

NEWS_FEED = "https://www.lanacion.com.ar/arc/outboundfeeds/rss/?outputType=xml"
NEWS_MAX = 14            # noticias candidatas (se dibujan las que entren)
PER_CATEGORY = 2         # variedad: como mucho 2 por sección
SKIP_CATEGORIES = {"california", "florida", "texas", "nueva york", "estados unidos"}  # contenido para EE.UU.
ARTICLE_MAX_PAGES = 12

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/130 Safari/537.36"

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

WMO = {
    0: "Despejado", 1: "Mayormente despejado", 2: "Parcialmente nublado", 3: "Nublado",
    45: "Niebla", 48: "Niebla con escarcha",
    51: "Llovizna leve", 53: "Llovizna", 55: "Llovizna intensa",
    56: "Llovizna helada", 57: "Llovizna helada",
    61: "Lluvia leve", 63: "Lluvia", 65: "Lluvia intensa",
    66: "Lluvia helada", 67: "Lluvia helada",
    71: "Nieve leve", 73: "Nieve", 75: "Nieve intensa", 77: "Granizo fino",
    80: "Chaparrones leves", 81: "Chaparrones", 82: "Chaparrones fuertes",
    85: "Nevadas", 86: "Nevadas fuertes",
    95: "Tormenta", 96: "Tormenta con granizo", 99: "Tormenta con granizo",
}


def fetch(url: str, accept: str | None = None) -> str:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="ignore")


# ---------- datos ----------

def get_weather() -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&forecast_days=2&timezone=America%2FArgentina%2FBuenos_Aires"
    )
    d = json.loads(fetch(url))
    c, dl = d["current"], d["daily"]
    return {
        "temp": round(c["temperature_2m"]),
        "feels": round(c["apparent_temperature"]),
        "hum": c["relative_humidity_2m"],
        "wind": round(c["wind_speed_10m"]),
        "desc": WMO.get(c["weather_code"], "—"),
        "days": [
            {
                "max": round(dl["temperature_2m_max"][i]),
                "min": round(dl["temperature_2m_min"][i]),
                "rain": dl["precipitation_probability_max"][i],
                "desc": WMO.get(dl["weather_code"][i], "—"),
            }
            for i in range(2)
        ],
    }


class _Paragraphs(HTMLParser):
    """HTML de la nota -> párrafos de texto plano (sin tweets, embeds ni imágenes)."""

    BLOCK = {"p", "h1", "h2", "h3", "h4", "li"}
    SKIP = {"blockquote", "script", "style", "iframe", "figure", "figcaption", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras: list[tuple[str, bool]] = []  # (texto, es_subtítulo)
        self.cur: list[str] = []
        self.skip = 0
        self.heading = False

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        if t:
            self.paras.append((t, self.heading))
        self.cur, self.heading = [], False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self._flush()
            self.heading = tag.startswith("h")
        elif tag == "br":
            self.cur.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.cur.append(data)

    def close(self):
        super().close()
        self._flush()


def article_paragraphs(raw_html: str) -> list[tuple[str, bool]]:
    p = _Paragraphs()
    p.feed(raw_html)
    p.close()
    return p.paras


def get_news() -> list[dict]:
    root = ET.fromstring(fetch(NEWS_FEED))
    content_tag = "{http://purl.org/rss/1.0/modules/content/}encoded"
    out, per_cat = [], {}
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        cat = (item.findtext("category") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or cat.lower() in SKIP_CATEGORIES or per_cat.get(cat, 0) >= PER_CATEGORY:
            continue
        body = item.findtext(content_tag) or ""
        paras = article_paragraphs(body)
        if not paras:
            continue
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(TZ)
        except (TypeError, ValueError):
            when = None
        per_cat[cat] = per_cat.get(cat, 0) + 1
        out.append({
            # cambia si se edita la nota (ej. las "en vivo"): el Kindle solo baja ids nuevos
            "id": hashlib.sha1(f"{link or title}\n{body}".encode()).hexdigest()[:10],
            "title": title, "cat": cat, "when": when, "paras": paras,
        })
        if len(out) >= NEWS_MAX:
            break
    if not out:
        raise RuntimeError("La Nación: feed sin notas")
    return out


def safe(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:  # una fuente caída no rompe el resto del dashboard
        print(f"[warn] {fn.__name__}: {e}", file=sys.stderr)
        return None, str(e)


# ---------- dibujo ----------

_FONT_CACHE: dict = {}


def _font(name, px):
    key = (name, px)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(str(FONTS / name), px)
    return _FONT_CACHE[key]


class Painter:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.u = w / 1072  # unidad de escala relativa al tamaño base
        self.img = Image.new("L", (w, h), 255)
        self.d = ImageDraw.Draw(self.img)
        self.m = round(48 * self.u)  # margen

    def font(self, size: float, bold=False, cond=False):
        name = "DejaVuSans" + ("Condensed" if cond else "") + ("-Bold" if bold else "") + ".ttf"
        return _font(name, max(8, round(size * self.u)))

    def text(self, xy, s, size, bold=False, cond=False, fill=0, anchor="la"):
        f = self.font(size, bold, cond)
        self.d.text(xy, s, font=f, fill=fill, anchor=anchor)
        return f

    def width(self, s, size, bold=False, cond=False):
        return self.d.textlength(s, font=self.font(size, bold, cond))

    def hline(self, y, thick=2):
        self.d.rectangle([self.m, y, self.w - self.m, y + max(1, round(thick * self.u))], fill=0)

    def heading(self, y, label):
        self.text((self.m, y), label.upper(), 26, bold=True, fill=80)
        return y + round(40 * self.u)

    def wrap(self, s, size, maxw, bold=False, cond=False, max_lines=None):
        words, lines, cur = s.split(), [], ""
        for w_ in words:
            cand = (cur + " " + w_).strip()
            if self.width(cand, size, bold, cond) <= maxw:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = w_
        if cur:
            lines.append(cur)
        if max_lines and len(lines) > max_lines:
            lines = lines[:max_lines]
            while lines[-1] and self.width(lines[-1] + "…", size, bold, cond) > maxw:
                lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
            lines[-1] += "…"
        return lines

    def finish(self) -> Image.Image:
        # 16 grises: lo que muestra la tinta electrónica, y el PNG pesa mucho menos
        return self.img.point(lambda v: (v // 17) * 17)


def meta_line(n: dict) -> str:
    parts = [n["cat"]] if n["cat"] else []
    if n["when"]:
        parts.append(f"{n['when']:%H:%M}")
    return " · ".join(parts)


def render_dash(size, now, weather, news, errors):
    """Devuelve (imagen, zonas) — zonas = [(y0, y1, id)] de cada noticia dibujada."""
    w, h = size
    p = Painter(w, h)
    u, m = p.u, p.m
    y = round(h * CLOCK_BAND)

    # fecha (la hora la pone el Kindle arriba)
    fecha = f"{DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]}"
    p.text((w / 2, y), fecha, 40, anchor="ma", fill=40)
    y += round(56 * u)
    p.hline(y, 3)
    y += round(24 * u)

    # clima, compacto: temperatura a la izquierda, hoy/mañana a la derecha
    if weather:
        p.text((m, y - 6 * u), f"{weather['temp']}°", 96, bold=True)
        tx = m + p.width(f"{weather['temp']}°", 96, bold=True) + 28 * u
        p.text((tx, y + 4 * u), weather["desc"], 30, bold=True)
        p.text((tx, y + 44 * u), f"ST {weather['feels']}° · {weather['hum']}%", 26, fill=60)
        p.text((tx, y + 78 * u), f"Viento {weather['wind']} km/h", 26, fill=60)
        rx = w - m
        for i, (label, dday) in enumerate(zip(["Hoy", "Mañana"], weather["days"])):
            yy = y + 4 * u + i * 60 * u
            p.text((rx, yy), f"{label} {dday['min']}°/{dday['max']}°", 28, bold=True, anchor="ra")
            p.text((rx, yy + 32 * u), f"{dday['desc']} · {dday['rain']}%", 22, cond=True, fill=60, anchor="ra")
        y += round(126 * u)
    else:
        p.text((m, y), "Sin datos de clima", 30)
        y += round(56 * u)
    p.hline(y)
    y += round(24 * u)

    # noticias: todo el resto de la pantalla
    y = p.heading(y, "Noticias · La Nación · tocá una para leerla")
    footer_y = h - round(44 * u)
    zones = []
    if news:
        maxw = w - 2 * m - 30 * u
        line_h, gap = round(35 * u), round(16 * u)
        for n in news:
            lines = p.wrap(n["title"], 28, maxw, bold=True, max_lines=2)
            need = len(lines) * line_h + round(27 * u) + gap
            if y + need > footer_y - 10 * u:
                break
            y0 = y
            p.d.ellipse([m, y + 11 * u, m + 11 * u, y + 22 * u], fill=0)
            for ln in lines:
                p.text((m + 30 * u, y), ln, 28, bold=True)
                y += line_h
            p.text((m + 30 * u, y), meta_line(n), 21, fill=90)
            y += round(27 * u)
            zones.append((round(y0 - gap / 2), round(y + gap / 2), n["id"]))
            y += gap
    else:
        p.text((m, y), "Sin noticias", 32)

    # pie
    foot = f"Actualizado {now:%H:%M}"
    if errors:
        foot += " · sin datos: " + ", ".join(errors)
    p.text((w / 2, footer_y), foot, 22, fill=110, anchor="ma")
    return p.finish(), zones


def render_article(size, n) -> list[Image.Image]:
    """Pagina la nota. Tocar arriba vuelve; izquierda/derecha cambian de página."""
    w, h = size
    probe = Painter(w, h)
    u, m = probe.u, probe.m
    maxw = w - 2 * m
    top = round(h * BACK_BAND)
    bottom = h - round(70 * u)

    # bloques = (líneas, tamaño, bold, alto de línea, espacio después)
    blocks = [(probe.wrap(n["title"], 42, maxw, bold=True), 42, True, 54, 14),
              ([meta_line(n)], 24, False, 34, 26)]
    for text, is_head in n["paras"]:
        if is_head:
            blocks.append((probe.wrap(text, 32, maxw, bold=True), 32, True, 44, 12))
        else:
            blocks.append((probe.wrap(text, 30, maxw), 30, False, 44, 20))

    # repartir línea por línea en páginas
    pages: list[list] = [[]]
    y = top
    for lines, px, bold, lh, after in blocks:
        for ln in lines:
            if y + lh * u > bottom:
                pages.append([])
                y = top
            pages[-1].append((ln, px, bold, y))
            y += lh * u
        y += after * u
    truncated = len(pages) > ARTICLE_MAX_PAGES
    pages = pages[:ARTICLE_MAX_PAGES]

    imgs = []
    total = len(pages)
    head_y = round(h * BACK_BAND * 0.42)
    for i, items in enumerate(pages, 1):
        p = Painter(w, h)
        p.text((m, head_y), "‹ Volver (tocá arriba)", 26, bold=True, fill=80)
        p.text((w - m, head_y), f"{i}/{total}", 26, bold=True, fill=80, anchor="ra")
        p.hline(top - round(20 * u), 1)
        for ln, px, bold, yy in items:
            p.text((m, yy), ln, px, bold=bold)
        if i < total:
            hint = "← izquierda: anterior · derecha: siguiente →"
        else:
            hint = "Fin" + (" · sigue en lanacion.com.ar" if truncated else "") + " · tocá para volver"
        p.text((w / 2, h - round(46 * u)), hint, 22, fill=110, anchor="ma")
        imgs.append(p.finish())
    return imgs


def main():
    now = datetime.now(TZ)
    weather, e1 = safe(get_weather)
    news, e2 = safe(get_news)
    errors = [n for n, e in [("clima", e1), ("noticias", e2)] if e]

    key = os.environ.get("ARTICLES_KEY", "").strip()
    if not key:
        if os.environ.get("CI"):
            print("[warn] falta ARTICLES_KEY: no publico las notas", file=sys.stderr)
        else:
            key = "local"  # pruebas en la compu

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    shutil.copy(FONTS / "DejaVuSans-Bold.ttf", OUT)  # el Kindle la usa para la hora
    (OUT / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    art_dir = OUT / key if key else None
    if art_dir:
        art_dir.mkdir()
    by_id = {n["id"]: n for n in news or []}

    for size in SIZES:
        tag = f"{size[0]}x{size[1]}"
        img, zones = render_dash(size, now, weather, news, errors)
        img.save(OUT / f"dash_{tag}.png", optimize=True)
        if size == DEFAULT_SIZE:
            img.save(OUT / "dash.png", optimize=True)
        if not art_dir:
            continue
        rows = []
        for y0, y1, nid in zones:
            pages = render_article(size, by_id[nid])
            for i, page in enumerate(pages, 1):
                page.save(art_dir / f"{nid}_{i}_{tag}.png", optimize=True)
            rows.append(f"{y0} {y1} {nid} {len(pages)}")
        (art_dir / f"zones_{tag}.txt").write_text("".join(r + "\n" for r in rows))

    (OUT / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><meta name=robots content=noindex><title>Kindle dash</title>"
        f"<p>Actualizado {now:%Y-%m-%d %H:%M} ART</p><img src=dash.png style='max-width:100%'>",
        encoding="utf-8",
    )
    print("ok", now.isoformat(), "errores:", errors or "ninguno")
    # falla el job solo si TODAS las fuentes cayeron
    if len(errors) == 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
