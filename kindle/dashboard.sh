#!/bin/sh
# Name: Dashboard
# Author: Federico
# DontUseFBInk
#
# Tocalo una vez: apaga la interfaz del Kindle y muestra el dashboard.
# Cada minuto dibuja la hora; cada 15 minutos baja la imagen nueva.
# Para salir: reiniciá el Kindle (mantené el botón de encendido hasta que reinicie).

URL_BASE="https://federicogiovannacci.github.io/kindle-dash"
DIR=/mnt/us/kindle-dash
LOG="$DIR/dash.log"
IMG="$DIR/dash.png"
FONT="$DIR/DejaVuSans-Bold.ttf"
REFRESH_MIN=15
export TZ=ART3   # Argentina, UTC-3 sin horario de verano

mkdir -p "$DIR"

log() {
    [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 200000 ] && : > "$LOG"
    echo "$(date '+%F %T') $*" >> "$LOG"
}

find_fbink() {
    for c in "$(command -v fbink 2>/dev/null)" /mnt/us/libkh/bin/fbink /mnt/us/libkh/fbink \
             /mnt/us/koreader/fbink /mnt/us/usbnet/bin/fbink /mnt/us/extensions/MRInstaller/bin/KHF/fbink; do
        [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }
    done
}

# ---------- loop en segundo plano ----------
if [ "$1" = "--loop" ]; then
    FBINK="$(find_fbink)"
    log "inicio (fbink=${FBINK:-ninguno})"

    W=1072; H=1448
    if [ -n "$FBINK" ]; then
        eval "$("$FBINK" -e 2>/dev/null | tr ';' '\n' | grep -E '^(viewWidth|viewHeight)=')"
        [ -n "$viewWidth" ] && W=$viewWidth && H=$viewHeight
    fi
    BAND=$((H * 12 / 100))
    PX=$((H * 9 / 100))
    log "pantalla ${W}x${H}"

    lipc-set-prop com.lab126.powerd preventScreenSaver 1
    # sacar la interfaz del Kindle para que no dibuje encima
    if status lab126_gui >/dev/null 2>&1; then stop lab126_gui; else stop framework 2>/dev/null; fi
    sleep 3

    fetch() {
        lipc-set-prop com.lab126.cmd wirelessEnable 1 2>/dev/null
        for name in "dash_${W}x${H}.png" "dash.png"; do
            url="$URL_BASE/$name?t=$(date +%s)"
            if curl -fsSL --max-time 40 -o "$IMG.new" "$url" || curl -kfsSL --max-time 40 -o "$IMG.new" "$url"; then
                mv "$IMG.new" "$IMG"; log "ok $name"; return 0
            fi
        done
        log "fallo descarga"; return 1
    }

    draw_image() {
        [ -f "$IMG" ] || return
        if [ -n "$FBINK" ]; then "$FBINK" -q -c -f -g file="$IMG",halign=CENTER,valign=CENTER
        else eips -c; eips -f -g "$IMG"; fi
    }

    draw_clock() {
        t="$(date +%H:%M)"
        if [ -n "$FBINK" ]; then
            "$FBINK" -q -k top=0,left=0,width="$W",height="$BAND"
            "$FBINK" -q -m -t regular="$FONT",px="$PX",top=$((BAND / 8)) "$t" 2>/dev/null \
                || "$FBINK" -q -m -y 1 -S $((W / 100)) "$t"
        else
            eips 22 1 "      "; eips 22 1 "$t"
        fi
    }

    # fuente para la hora (una sola vez)
    [ -f "$FONT" ] || curl -fsSL --max-time 40 -o "$FONT" "$URL_BASE/DejaVuSans-Bold.ttf" || curl -kfsSL -o "$FONT" "$URL_BASE/DejaVuSans-Bold.ttf"

    last=0
    while true; do
        now=$(date +%s)
        if [ $((now - last)) -ge $((REFRESH_MIN * 60)) ]; then
            fetch && last=$now || last=$((now - REFRESH_MIN * 60 + 120))  # si falla, reintenta en 2 min
            draw_image
        fi
        draw_clock
        sleep $((60 - $(date +%S | sed 's/^0//')))
    done
fi

# ---------- tap desde la biblioteca ----------
if [ -f /tmp/kindle-dash.pid ] && kill -0 "$(cat /tmp/kindle-dash.pid)" 2>/dev/null; then
    exit 0   # ya está corriendo
fi
nohup sh "$0" --loop >/dev/null 2>&1 &
echo $! > /tmp/kindle-dash.pid
