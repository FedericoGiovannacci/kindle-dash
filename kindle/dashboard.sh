#!/bin/sh
# Name: Dashboard
# Author: Federico
# DontUseFBInk
#
# Tocalo una vez: apaga la interfaz del Kindle y muestra el dashboard.
# Cada minuto dibuja la hora; cada 15 minutos baja la imagen nueva y las notas.
# Tocá una noticia para leerla: derecha = página siguiente, izquierda = anterior,
# franja de arriba = volver. Si no tocás nada en 10 min, vuelve solo al dashboard.
# Para salir: reiniciá el Kindle (mantené el botón de encendido hasta que reinicie).
#
# Ajustes opcionales en /mnt/us/kindle-dash/config.sh (si los toques caen mal):
#   TOUCH_DEV=/dev/input/event1   SWAP_XY=1   MIRROR_X=1   MIRROR_Y=1
#   TOUCH_MAX_X=1072   TOUCH_MAX_Y=1448   (resolución del panel táctil, si no coincide con la pantalla)
# Cada toque queda en dash.log como "tap raw=X,Y pantalla=X,Y".

URL_BASE="https://federicogiovannacci.github.io/kindle-dash"
KEY="__ARTICLES_KEY__"   # carpeta de las notas (el secret ARTICLES_KEY del repo)
DIR=/mnt/us/kindle-dash
LOG="$DIR/dash.log"
IMG="$DIR/dash.png"
ZONES="$DIR/zones.txt"
ART="$DIR/art"
FONT="$DIR/DejaVuSans-Bold.ttf"
REFRESH_MIN=15
IDLE_BACK_MIN=10
export TZ=ART3   # Argentina, UTC-3 sin horario de verano

mkdir -p "$DIR" "$ART"
[ -f "$DIR/config.sh" ] && . "$DIR/config.sh"

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

# ---------- pantalla táctil ----------

find_touch() {
    awk 'BEGIN { RS = "" }
         tolower($0) ~ /name=[^\n]*(touch|zforce|cyttsp|pt_mt|elan|ektf|focal|goodix|_ts|tsc)/ {
             if (match($0, /event[0-9]+/)) { print "/dev/input/" substr($0, RSTART, RLENGTH); exit }
         }' /proc/bus/input/devices 2>/dev/null
}

# bytes del dispositivo -> un byte decimal por token (od o, si no está, hexdump)
bytes_cmd() {
    if [ "$(printf A | od -An -tu1 2>/dev/null | tr -d ' ')" = "65" ]; then
        echo "od -An -v -tu1"
    else
        echo "hexdump -v -e '1/1 \"%u\\n\"'"
    fi
}

# Escribe "x y" (coordenadas crudas) cada vez que se levanta el dedo.
touch_reader() {
    BYTES="$(bytes_cmd)"
    # struct input_event: 16 bytes en userland de 32 bits, 24 en 64 bits
    EV=16
    [ "$(dd if=/bin/busybox bs=1 skip=4 count=1 2>/dev/null | od -An -tu1 2>/dev/null | tr -d ' ')" = "2" ] && EV=24
    log "touch: $TOUCH_DEV (evento de $EV bytes)"
    exec 3< "$TOUCH_DEV"
    while true; do
        dd bs=$((EV * 32)) count=1 <&3 2>/dev/null | eval "$BYTES"
    done | awk -v ev="$EV" '
        {
            for (i = 1; i <= NF; i++) {
                b[n++] = $i
                if (n < ev) continue
                n = 0
                type = b[ev-8] + 256 * b[ev-7]
                code = b[ev-6] + 256 * b[ev-5]
                val = b[ev-4] + 256 * b[ev-3] + 65536 * b[ev-2] + 16777216 * b[ev-1]
                if (val >= 2147483648) val -= 4294967296
                if (type == 3) {                                   # EV_ABS
                    if (code == 53 || code == 0) { x = val; down = 1 }  # MT_POSITION_X / ABS_X
                    else if (code == 54 || code == 1) { y = val; down = 1 }
                    else if (code == 57 && val == -1) up = 1      # MT_TRACKING_ID = -1
                } else if (type == 1 && code == 330 && val == 0) { # BTN_TOUCH suelto
                    up = 1
                } else if (type == 0 && code == 0 && up) {         # SYN_REPORT
                    if (down) { print x, y; fflush() }
                    up = 0; down = 0
                }
            }
        }'
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
    BACK_ZONE=$((H * 20 / 100))   # en una nota: tocar el 20% de arriba vuelve (título incluido)
    PX=$((H * 9 / 100))
    TAG="${W}x${H}"
    log "pantalla $TAG"

    lipc-set-prop com.lab126.powerd preventScreenSaver 1
    # sacar la interfaz del Kindle para que no dibuje encima
    if status lab126_gui >/dev/null 2>&1; then stop lab126_gui; else stop framework 2>/dev/null; fi
    sleep 3

    get() {  # get <archivo remoto> <destino>
        url="$URL_BASE/$1?t=$(date +%s)"
        if curl -fsSL --max-time 40 -o "$2.new" "$url" || curl -kfsSL --max-time 40 -o "$2.new" "$url"; then
            mv "$2.new" "$2"; return 0
        fi
        rm -f "$2.new"; return 1
    }

    fetch_article() {  # fetch_article <id> <páginas>
        p=1
        while [ "$p" -le "$2" ]; do
            f="$ART/${1}_$p.png"
            [ -f "$f" ] || get "$KEY/${1}_${p}_$ATAG.png" "$f" || return 1
            p=$((p + 1))
        done
    }

    fetch() {
        lipc-set-prop com.lab126.cmd wirelessEnable 1 2>/dev/null
        ATAG="$TAG"
        if ! get "dash_$TAG.png" "$IMG"; then
            ATAG=1072x1448   # pantalla no prevista: usa el tamaño por defecto
            get "dash.png" "$IMG" || { log "fallo descarga"; return 1; }
        fi
        log "ok dash ($ATAG)"
        if get "$KEY/zones_$ATAG.txt" "$ZONES.tmp"; then
            # baja solo las notas nuevas; borra las que ya no están (salvo la que estás leyendo)
            while read -r y0 y1 id pages; do
                [ -n "$id" ] && { fetch_article "$id" "$pages" || log "fallo nota $id"; }
            done < "$ZONES.tmp"
            for f in "$ART"/*.png; do
                [ -f "$f" ] || continue
                id="$(basename "$f" | cut -d_ -f1)"
                [ "$id" = "$CUR_ID" ] || grep -q " $id " "$ZONES.tmp" || rm -f "$f"
            done
            mv "$ZONES.tmp" "$ZONES"
        else
            log "fallo zonas"
        fi
        return 0
    }

    show() {  # show <png>
        [ -f "$1" ] || return 1
        if [ -n "$FBINK" ]; then "$FBINK" -q -c -f -g file="$1",halign=CENTER,valign=CENTER
        else eips -c; eips -f -g "$1"; fi
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
        CLOCK="$t"
    }

    show_dash() { MODE=dash; CUR_ID=""; show "$IMG"; draw_clock; }

    show_page() { show "$ART/${CUR_ID}_$CUR_PAGE.png"; }

    on_tap() {  # on_tap <x crudo> <y crudo>
        x=$1; y=$2
        [ "$SWAP_XY" = 1 ] && { t=$x; x=$y; y=$t; }
        [ -n "$TOUCH_MAX_X" ] && x=$((x * W / TOUCH_MAX_X))
        [ -n "$TOUCH_MAX_Y" ] && y=$((y * H / TOUCH_MAX_Y))
        [ "$MIRROR_X" = 1 ] && x=$((W - 1 - x))
        [ "$MIRROR_Y" = 1 ] && y=$((H - 1 - y))
        log "tap raw=$1,$2 pantalla=$x,$y modo=$MODE"
        LAST_TAP=$(date +%s)

        if [ "$MODE" = dash ]; then
            [ -f "$ZONES" ] || return
            hit="$(awk -v y="$y" '$1 <= y && y < $2 { print $3, $4; exit }' "$ZONES")"
            [ -n "$hit" ] || return
            set -- $hit
            if [ ! -f "$ART/${1}_1.png" ]; then
                fetch_article "$1" "$2" || { log "nota $1 no disponible"; return; }
            fi
            MODE=art; CUR_ID=$1; CUR_PAGES=$2; CUR_PAGE=1
            show_page
        elif [ "$y" -lt "$BACK_ZONE" ]; then
            show_dash
        elif [ "$x" -lt $((W / 3)) ]; then
            if [ "$CUR_PAGE" -le 1 ]; then show_dash; else CUR_PAGE=$((CUR_PAGE - 1)); show_page; fi
        else
            if [ "$CUR_PAGE" -ge "$CUR_PAGES" ]; then show_dash; else CUR_PAGE=$((CUR_PAGE + 1)); show_page; fi
        fi
    }

    # fuente para la hora (una sola vez)
    [ -f "$FONT" ] || get "DejaVuSans-Bold.ttf" "$FONT"

    # toques: un lector en segundo plano escribe en un FIFO; el loop espera con timeout
    [ -n "$TOUCH_DEV" ] || TOUCH_DEV="$(find_touch)"
    [ -n "$TOUCH_DEV" ] || TOUCH_DEV=/dev/input/event1
    FIFO=/tmp/kindle-dash.taps
    rm -f "$FIFO"; mkfifo "$FIFO"
    exec 4<> "$FIFO"
    touch_reader >&4 2>>"$LOG" &

    MODE=dash; CUR_ID=""; CLOCK=""
    last=0; LAST_TAP=0
    while true; do
        now=$(date +%s)
        if [ $((now - last)) -ge $((REFRESH_MIN * 60)) ]; then
            fetch && last=$now || last=$((now - REFRESH_MIN * 60 + 120))  # si falla, reintenta en 2 min
            [ "$MODE" = dash ] && { show "$IMG"; CLOCK=""; }
        fi
        if [ "$MODE" = art ] && [ $((now - LAST_TAP)) -ge $((IDLE_BACK_MIN * 60)) ]; then
            show_dash
        fi
        [ "$MODE" = dash ] && [ "$CLOCK" != "$(date +%H:%M)" ] && draw_clock
        wait_s=$((60 - $(date +%S | sed 's/^0//')))
        if read -t "$wait_s" tx ty <&4; then
            [ -n "$ty" ] && on_tap "$tx" "$ty"
        fi
    done
fi

# ---------- tap desde la biblioteca ----------
if [ -f /tmp/kindle-dash.pid ] && kill -0 "$(cat /tmp/kindle-dash.pid)" 2>/dev/null; then
    exit 0   # ya está corriendo
fi
nohup sh "$0" --loop >/dev/null 2>&1 &
echo $! > /tmp/kindle-dash.pid
