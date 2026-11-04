"""Renderiza el dashboard del Kindle como PNG en escala de grises.

Fuentes: Open-Meteo (clima), Google News AR (noticias), El Cronista (dólar), Nasdaq (VOO).
Salida: out/dash_<W>x<H>.png para cada tamaño de pantalla soportado + out/dash.png.
La franja superior queda en blanco: ahí el Kindle dibuja la hora cada minuto.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
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


def get_dolar() -> list[tuple[str, str, str]]:
    s = fetch("https://www.cronista.com/MercadosOnline/dolar.html")
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", s))
    t = re.sub(r"(\|\s*)+", "|", t)
    found: dict[str, tuple[str, str]] = {}
    pat = r"\|(D[óo]lar [^|]{2,30})\|(?:Seguir\|)?Venta\|\$\|([\d.,]+)\|Compra\|\$\|([\d.,]+)"
    for name, venta, compra in re.findall(pat, t):
        key = html.unescape(name).strip().lower()
        found.setdefault(key, (compra, venta))
    wanted = [("Oficial BNA", "dólar bna"), ("Blue", "dólar blue"),
              ("MEP", "dólar mep"), ("CCL", "dólar ccl"), ("Tarjeta", "dólar turista")]
    rows = []
    for label, key in wanted:
        if key in found and found[key][1] != "0,00":
            c, v = (x.split(",")[0] for x in found[key])
            rows.append((label, c, v))
    if not rows:
        raise RuntimeError("Cronista: no encontré cotizaciones")
    return rows


def get_voo() -> dict:
    d = json.loads(fetch("https://api.nasdaq.com/api/quote/VOO/info?assetclass=etf",
                         accept="application/json"))["data"]
    p = d["primaryData"]
    return {
        "price": p["lastSalePrice"],
        "change": p["netChange"],
        "pct": p["percentageChange"],
        "up": p.get("deltaIndicator") == "up",
        "status": "Abierto" if d.get("marketStatus") == "Open" else "Cerrado",
        "when": p.get("lastTradeTimestamp", ""),
    }


def get_news(n: int = 8) -> list[tuple[str, str]]:
    xml = fetch("https://news.google.com/rss?hl=es-419&gl=AR&ceid=AR:es-419")
    root = ET.fromstring(xml)
    out = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        src = (item.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        if title:
            out.append((title, src))
        if len(out) >= n:
            break
    return out


def safe(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:  # una fuente caída no rompe el resto del dashboard
        print(f"[warn] {fn.__name__}: {e}", file=sys.stderr)
        return None, str(e)


# ---------- dibujo ----------

class Painter:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.u = w / 1072  # unidad de escala relativa al tamaño base
        self.img = Image.new("L", (w, h), 255)
        self.d = ImageDraw.Draw(self.img)
        self.m = round(48 * self.u)  # margen

    def font(self, size: float, bold=False, cond=False):
        name = "DejaVuSans" + ("Condensed" if cond else "") + ("-Bold" if bold else "") + ".ttf"
        return ImageFont.truetype(str(FONTS / name), max(8, round(size * self.u)))

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

    def wrap(self, s, size, maxw, bold=False, cond=False, max_lines=3):
        words, lines, cur = s.split(), [], ""
        for w_ in words:
            cand = (cur + " " + w_).strip()
            if self.width(cand, size, bold, cond) <= maxw:
                cur = cand
            else:
                lines.append(cur)
                cur = w_
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            while lines[-1] and self.width(lines[-1] + "…", size, bold, cond) > maxw:
                lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
            lines[-1] += "…"
        return lines


def render(size, now, weather, dolar, voo, news, errors) -> Image.Image:
    w, h = size
    p = Painter(w, h)
    u, m = p.u, p.m
    y = round(h * CLOCK_BAND)

    # fecha (la hora la pone el Kindle arriba)
    fecha = f"{DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]}"
    p.text((w / 2, y), fecha, 40, anchor="ma", fill=40)
    y += round(56 * u)
    p.hline(y, 3)
    y += round(26 * u)

    # clima
    y = p.heading(y, "Clima · Buenos Aires")
    if weather:
        p.text((m, y - 10 * u), f"{weather['temp']}°", 120, bold=True)
        tx = m + p.width(f"{weather['temp']}°", 120, bold=True) + 36 * u
        p.text((tx, y), weather["desc"], 36, bold=True)
        p.text((tx, y + 48 * u), f"Sensación {weather['feels']}° · Hum {weather['hum']}%", 30)
        p.text((tx, y + 86 * u), f"Viento {weather['wind']} km/h", 30)
        y += round(140 * u)
        labels = ["Hoy", "Mañana"]
        colw = (w - 2 * m) / 2
        for i, dday in enumerate(weather["days"]):
            x = m + i * colw
            p.text((x, y), f"{labels[i]}: {dday['min']}° / {dday['max']}°", 32, bold=True)
            p.text((x, y + 38 * u), f"{dday['desc']} · lluvia {dday['rain']}%", 26, cond=True)
        y += round(80 * u)
    else:
        p.text((m, y), "Sin datos de clima", 32)
        y += round(60 * u)
    p.hline(y)
    y += round(26 * u)

    # dólar (izq) + VOO (der)
    top = y
    colsplit = m + (w - 2 * m) * 0.60
    y = p.heading(y, "Dólar · El Cronista")
    if dolar:
        cx, vx = colsplit - 180 * u, colsplit - 30 * u
        p.text((cx, y), "Compra", 24, fill=90, anchor="ra")
        p.text((vx, y), "Venta", 24, fill=90, anchor="ra")
        y += round(36 * u)
        for label, compra, venta in dolar:
            p.text((m, y), label, 34, bold=True)
            p.text((cx, y), compra, 34, anchor="ra")
            p.text((vx, y), venta, 34, bold=True, anchor="ra")
            y += round(46 * u)
    else:
        p.text((m, y), "Sin datos", 32)
        y += round(46 * u)
    left_bottom = y

    y = top
    vx0 = colsplit + 10 * u
    p.d.rectangle([colsplit - 6 * u, top, colsplit - 4 * u, left_bottom - 10 * u], fill=160)
    p.text((vx0, y), "VOO · S&P 500", 26, bold=True, fill=80)
    y += round(48 * u)
    if voo:
        p.text((vx0, y), voo["price"], 58, bold=True)
        y += round(78 * u)
        arrow = "▲" if voo["up"] else "▼"
        p.text((vx0, y), f"{arrow} {voo['change']} ({voo['pct']})", 30, bold=True)
        y += round(46 * u)
        p.text((vx0, y), f"Mercado {voo['status'].lower()}", 24, fill=80)
        y += round(32 * u)
        p.text((vx0, y), voo["when"], 24, fill=80)
    else:
        p.text((vx0, y), "Sin datos", 32)
    y = max(left_bottom, y + 40 * u) + round(10 * u)
    p.hline(y)
    y += round(26 * u)

    # noticias
    y = p.heading(y, "Noticias · Argentina")
    footer_y = h - round(44 * u)
    if news:
        maxw = w - 2 * m - 30 * u
        for title, src in news:
            lines = p.wrap(title, 28, maxw, bold=True, max_lines=2)
            need = len(lines) * 35 * u + 30 * u + 14 * u
            if y + need > footer_y - 10 * u:
                continue
            p.d.ellipse([m, y + 11 * u, m + 11 * u, y + 22 * u], fill=0)
            for ln in lines:
                p.text((m + 30 * u, y), ln, 28, bold=True)
                y += round(35 * u)
            if src:
                p.text((m + 30 * u, y), src, 22, fill=90)
            y += round(30 * u + 14 * u)
    else:
        p.text((m, y), "Sin noticias", 32)

    # pie
    foot = f"Actualizado {now:%H:%M}"
    if errors:
        foot += " · sin datos: " + ", ".join(errors)
    p.text((w / 2, footer_y), foot, 22, fill=110, anchor="ma")
    return p.img


def main():
    now = datetime.now(TZ)
    weather, e1 = safe(get_weather)
    dolar, e2 = safe(get_dolar)
    voo, e3 = safe(get_voo)
    news, e4 = safe(get_news)
    errors = [n for n, e in [("clima", e1), ("dólar", e2), ("VOO", e3), ("noticias", e4)] if e]

    OUT.mkdir(exist_ok=True)
    shutil.copy(FONTS / "DejaVuSans-Bold.ttf", OUT)  # el Kindle la usa para la hora
    for size in SIZES:
        img = render(size, now, weather, dolar, voo, news, errors)
        img.save(OUT / f"dash_{size[0]}x{size[1]}.png", optimize=True)
        if size == DEFAULT_SIZE:
            img.save(OUT / "dash.png", optimize=True)
    (OUT / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Kindle dash</title>"
        f"<p>Actualizado {now:%Y-%m-%d %H:%M} ART</p><img src=dash.png style='max-width:100%'>",
        encoding="utf-8",
    )
    print("ok", now.isoformat(), "errores:", errors or "ninguno")
    # falla el job solo si TODAS las fuentes cayeron
    if len(errors) == 4:
        sys.exit(1)


if __name__ == "__main__":
    main()
