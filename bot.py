"""
================================================================
  US500 MARKET MONITOR BOT v3.9 — RAILWAY PRODUCTION
  Autor: Amalec (revisado y mejorado por Claude)

  NOVEDADES v3.9 (sobre v3.8):
  NUEVAS SEÑALES INSTITUCIONALES:
  1. COT Report CFTC — sesgo semanal smart money en futuros S&P500
  2. McClellan Oscillator + A/D Volume NYSE — breadth anticipatorio
  3. VVIX — VIX del VIX, señal adelantada institucional
  4. Put/Call Ratio SPY — flujo nuevo de dinero en opciones
  5. SPY vs SHY — rotación defensiva vs liquidación total
  6. Breadth sectores apertura — 11 sectores en verde/rojo
  7. Fear/Greed implícito — calculado VIX/VVIX/Put-Call en tiempo real

  NUEVAS FUNCIONALIDADES:
  8. Pre-apertura 15min antes 9:30 ET
  9. Detector de rango — silencia señales en consolidación
  10. Macro post-evento — actualización 15-20min después Fed/NFP/CPI
  11. Resumen dominical — domingo 6-7PM Honduras
  12. Monitor overnight — alertas de gaps anticipados

  MEJORAS DOMINGO 31/05/2026:
  A. COT REAL CFTC — datos federales reales desde cftc.gov
     (antes: proxy /ES=F vs SPY — 30-40% precisión)
  B. GEX REAL option_chain() — Gamma Flip/Call Wall/Put Wall reales
     (antes: FlashAlpha caía siempre al estimado geométrico)
  C. Dark Pool GRANULAR — bloques anómalos intradía 5min
     (antes: ratio estático 62.5% casi fijo)

  LIMPIEZA:
  - Eliminado: EVENTOS_CALENDARIO, evento_acaba_de_ocurrir,
    es_dia_evento, aceleracion_volumen, zscore_volumen_hora,
    vix_momentum (redundantes o bajo impacto)

  Variables de entorno requeridas en Railway:
  - TELEGRAM_TOKEN
  - TELEGRAM_CHAT_ID
  - ANTHROPIC_API_KEY
================================================================
"""

import os
import time
import pytz
import anthropic
import telebot
import yfinance as yf
import pandas as pd
import numpy as np
import urllib.request
import json
import threading
from datetime import datetime, timedelta

# ── Credenciales desde variables de entorno (Railway) ────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TRADIER_TOKEN     = os.environ.get("TRADIER_TOKEN")  # Opcional — greeks reales
# URL del Apps Script que recibe cada lectura de sweeps para la app web
# (dashboard de seguimiento). Opcional: si está vacía, no se envía nada.
DASHBOARD_URL     = os.environ.get("DASHBOARD_URL", "")

for var, nombre in [
    (TELEGRAM_TOKEN,    "TELEGRAM_TOKEN"),
    (TELEGRAM_CHAT_ID,  "TELEGRAM_CHAT_ID"),
    (ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY"),
]:
    if not var:
        raise EnvironmentError(f"❌ Variable de entorno faltante: {nombre}")

if TRADIER_TOKEN:
    print("  [TRADIER] ✅ Token disponible — greeks reales habilitados")
else:
    print("  [TRADIER] 📊 Sin token — usando Black-Scholes")

bot           = telebot.TeleBot(TELEGRAM_TOKEN)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Modelos ──────────────────────────────────────────────────
MODELO_SEÑALES = "claude-haiku-4-5-20251001"
MODELO_MACRO   = "claude-sonnet-4-5"

# ── Configuración ────────────────────────────────────────────
UMBRAL_SCORE             = 7
# Señales de score DESACTIVADAS (9-ago): win rate 35% en 168 señales.
# El score se sigue calculando (lo usan el journal, agotamiento y rango)
# pero ya NO se envía a Telegram ni se consulta a Claude por señal.
# Poner en True para reactivarlas.
ENVIAR_SENALES_SCORE     = False
TIEMPO_MIN_ALERTAS       = 15
UMBRAL_PRECIO_CAMBIO     = 0.003
SALTO_SCORE_MINIMO       = 2
MINUTOS_VIX_RATIO_FATIGA = 45
AGOTAMIENTO_CONDICIONES  = 3
RANGO_MAXIMO_PUNTOS      = 20
RANGO_MINUTOS_MINIMO     = 30
RANGO_ALEJAMIENTO_MIN    = 10

# ── Tiempo ───────────────────────────────────────────────────
def hora_ny():
    return datetime.now(pytz.timezone("America/New_York"))

def descargar_futuros(period="2d", interval="5m"):
    """
    Descarga futuros E-mini S&P500 con múltiples tickers de fallback.
    yfinance a veces falla con /ES=F los fines de semana o por delisting.
    Orden: ES=F → /ES=F → ESM26.CME → SPY como último recurso (×10)
    """
    tickers_futuros = ["ES=F", "/ES=F", "ESM26.CME"]
    for ticker in tickers_futuros:
        try:
            data = yf.download(ticker, period=period, interval=interval,
                               progress=False, auto_adjust=True)
            if not data.empty and len(data) >= 2:
                print(f"  [FUTUROS] ✅ {ticker} OK")
                return data
        except Exception as e:
            print(f"  [FUTUROS] {ticker} falló: {e}")
            continue
    # Último recurso: SPY ×10
    try:
        spy = yf.download("SPY", period=period, interval=interval,
                          progress=False, auto_adjust=True)
        if not spy.empty:
            spy_scaled = spy.copy()
            for col in ["Open","High","Low","Close"]:
                if col in spy_scaled.columns:
                    spy_scaled[col] = spy_scaled[col] * 10
            print("  [FUTUROS] 📊 Usando SPY×10 como proxy de futuros")
            return spy_scaled
    except Exception as e:
        print(f"  [FUTUROS] SPY fallback error: {e}")
    return pd.DataFrame()

# Días festivos NYSE 2025-2027
NYSE_FESTIVOS = {
    (2025, 1,  1), (2025, 1, 20), (2025, 2, 17), (2025, 4, 18),
    (2025, 5, 26), (2025, 6, 19), (2025, 7,  4), (2025, 9,  1),
    (2025,11, 27), (2025,12, 25),
    (2026, 1,  1), (2026, 1, 19), (2026, 2, 16), (2026, 4,  3),
    (2026, 5, 25), (2026, 6, 19), (2026, 7,  3), (2026, 9,  7),
    (2026,11, 26), (2026,12, 25),
    (2027, 1,  1), (2027, 1, 18), (2027, 2, 15), (2027, 3, 26),
    (2027, 5, 31), (2027, 6, 18), (2027, 7,  5), (2027, 9,  6),
    (2027,11, 25), (2027,12, 24),
}

def mercado_abierto():
    ahora = hora_ny()
    if ahora.weekday() > 4: return False
    if (ahora.year, ahora.month, ahora.day) in NYSE_FESTIVOS: return False
    apertura = ahora.replace(hour=9,  minute=30, second=0, microsecond=0)
    cierre   = ahora.replace(hour=16, minute=0,  second=0, microsecond=0)
    return apertura <= ahora <= cierre

def minutos_desde_apertura():
    ahora    = hora_ny()
    apertura = ahora.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(0, int((ahora - apertura).total_seconds() / 60))

def es_dia_habil(fecha):
    """True si la fecha NO es fin de semana ni festivo NYSE."""
    if fecha.weekday() > 4:
        return False
    if (fecha.year, fecha.month, fecha.day) in NYSE_FESTIVOS:
        return False
    return True

def es_festivo_hoy():
    """True si HOY es festivo NYSE (no fin de semana, sino festivo federal)."""
    ahora = hora_ny()
    return (ahora.year, ahora.month, ahora.day) in NYSE_FESTIVOS

def proximo_dia_habil_texto():
    """
    Devuelve el texto del próximo día hábil para el mensaje de cierre.
    Salta fines de semana Y festivos. Ej: 'mañana', 'el lunes', 'el martes'.
    """
    ahora = hora_ny()
    dias_es = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    # Buscar el próximo día hábil empezando por mañana
    siguiente = ahora.date() + timedelta(days=1)
    intentos = 0
    while not es_dia_habil(siguiente) and intentos < 10:
        siguiente += timedelta(days=1)
        intentos += 1
    # Si el próximo hábil es literalmente mañana → "mañana"
    if siguiente == ahora.date() + timedelta(days=1):
        return "mañana"
    return f"el {dias_es[siguiente.weekday()]}"

# ================================================================
# === MEJORA A: COT REAL CFTC =====================================
# ================================================================

cot_cache = {
    "neto_largo":           None,
    "sesgo":                "NEUTRAL",
    "ultima_actualizacion": None,
    "disponible":           False,
    "fuente":               None,
    "fecha_reporte":        None,
}

# ── COT Estimado — módulo experimental ───────────────────────
# Lógica:
# 1. El viernes llega el COT real (datos del martes anterior)
# 2. Desde ese viernes acumulamos señales miércoles→martes siguiente
# 3. El próximo viernes comparamos nuestro estimado vs el nuevo COT real
# Ejemplo: COT real viernes 13 jun (datos martes 9 jun)
#          → estimamos cambio miérc 10 → martes 16 jun
#          → validamos el viernes 20 jun

cot_estimado_cache = {
    "disponible":           False,
    "cot_base":             0,       # COT real de la semana anterior (punto de partida)
    "cambio_estimado":      0,       # Cambio estimado para la semana actual
    "neto_estimado":        0,       # cot_base + cambio_estimado
    "sesgo":                "NEUTRAL",
    "confianza":            0.0,     # 0.0-1.0 según historial de precisión
    "componentes": {
        "sweep":            0,
        "dark_pool":        0,
        "rotacion":         0,
        "pc_semanal":       0,
    },
    "historial_error":      [],      # % error últimas 4 semanas
    "semana_estimando":     None,    # Semana ISO que estamos estimando
    "fecha_inicio":         None,    # Miércoles desde cuando acumulamos
    "ultima_fecha_validada": None,   # fecha del último COT ya validado (anti-loop)
    "ultima_actualizacion": None,
}

# Acumulador de señales desde el miércoles post-COT
cot_senales_semana = {
    "sweep_prima_calls":  0.0,
    "sweep_prima_puts":   0.0,
    "dp_dias_alcista":    0,
    "dp_dias_bajista":    0,
    "pc_ratios":          [],
    "dias_acumulados":    0,
    "semana":             None,    # Semana ISO que estamos acumulando
    "fecha_inicio":       None,    # Primer día de acumulación
}

def obtener_cot_report():
    """
    Descarga el COT Report REAL de la CFTC para E-mini S&P 500.
    Publicado cada viernes 3:30 PM ET con datos del martes anterior.
    URL: https://www.cftc.gov/dea/newcot/FinFutWk.txt
    Fallback: proxy via /ES=F vs SPY si CFTC no disponible.
    """
    try:
        import csv, io
        url = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            contenido = resp.read().decode("latin-1")

        lineas = contenido.strip().split("\n")
        linea_emini = None
        for linea in lineas:
            # S&P 500 CONSOLIDATED — combina estándar + e-mini + micro.
            # Es el posicionamiento COMPLETO de los tiburones (el que validamos
            # a mano). Antes se leía el E-MINI solo (13874A), ahora el
            # Consolidated (13874+).
            U = linea.upper()
            if U.startswith('"S&P 500 CONSOLIDATED') or U.startswith("S&P 500 CONSOLIDATED"):
                linea_emini = linea
                break

        if linea_emini is None:
            print("  [COT] No se encontró S&P 500 Consolidated en el CSV")
            return _cot_proxy_fallback()

        # csv.reader respeta las comillas del nombre (que contiene comas/guiones)
        campos = next(csv.reader(io.StringIO(linea_emini)))
        if len(campos) < 14:
            print(f"  [COT] CSV con formato inesperado ({len(campos)} campos)")
            return _cot_proxy_fallback()

        def _num(i):
            return int(campos[i].strip().replace('"', '').replace(',', ''))

        fecha_str = campos[2].strip().strip('"')
        nombre_contrato = campos[0].strip().strip('"')   # nombre tal cual del CSV
        # ── Mapeo TFF (Traders in Financial Futures) CORREGIDO ─────────
        # El formato TFF incluye columna SPREADING por cada categoría.
        # Tras la metadata (nombre, fechas, código, CME, 00, 138):
        # [7] OI total
        # [8/9/10]   Dealer        Long/Short/Spreading
        # [11/12/13] Asset Manager Long/Short/Spreading
        # [14/15/16] Leveraged     Long/Short/Spreading  ← TIBURONES
        # [17/18/19] Other         Long/Short/Spreading
        # El mapeo viejo usaba [12/13] (= Asset Mgr Short/Spread) por error,
        # dando un neto falso (+85,492). Lev Funds reales están en [14/15].
        lev_long   = _num(14)   # Leveraged Funds long  — hedge funds
        lev_short  = _num(15)   # Leveraged Funds short
        am_long    = _num(11)   # Asset Managers long   — institucional (contexto)
        am_short   = _num(12)   # Asset Managers short
        oi_total   = _num(7)

        neto       = lev_long - lev_short        # NETO de los tiburones
        neto_am    = am_long - am_short          # neto institucional (contexto)

        # ── Umbrales recalibrados a la escala real de Lev Funds ──
        # (Lev Funds neto típico E-mini: rango -50k a +150k aprox)
        if   neto >  100000: sesgo = "ALCISTA_FUERTE"
        elif neto >   30000: sesgo = "ALCISTA_MODERADO"
        elif neto <  -80000: sesgo = "BAJISTA_FUERTE"
        elif neto <  -15000: sesgo = "BAJISTA_MODERADO"
        else:                sesgo = "NEUTRAL"

        cot_cache.update({
            "neto_largo":           neto,
            "sesgo":                sesgo,
            "longs":                lev_long,
            "shorts":               lev_short,
            "am_long":              am_long,
            "am_short":             am_short,
            "neto_am":              neto_am,
            "oi_total":             oi_total,
            "ultima_actualizacion": hora_ny(),
            "disponible":           True,
            "fuente":               "CFTC_REAL",
            "fecha_reporte":        fecha_str,
            "contrato":             nombre_contrato,
        })
        print(f"  [COT] ✅ REAL CFTC (Lev Funds) — Fecha:{fecha_str} | "
              f"Long:{lev_long:,} | Short:{lev_short:,} | Neto:{neto:+,} | Sesgo:{sesgo} "
              f"| AssetMgr neto:{neto_am:+,}")
        return True

    except Exception as e:
        print(f"  [COT] CFTC error: {e}")
        return _cot_proxy_fallback()

def _cot_proxy_fallback():
    """Proxy COT via /ES=F vs SPY cuando CFTC no disponible."""
    try:
        es  = descargar_futuros(period="5d", interval="1d")
        spy = yf.download("SPY", period="5d", interval="1d", progress=False)
        if es.empty or spy.empty:
            cot_cache["disponible"] = False
            return False

        ret_es  = float((es["Close"].iloc[-1]  / es["Close"].iloc[-5]  - 1) * 100) if len(es)  >= 5 else 0
        ret_spy = float((spy["Close"].iloc[-1] / spy["Close"].iloc[-5] - 1) * 100) if len(spy) >= 5 else 0
        diferencia = ret_es - ret_spy

        vol_reciente = float(es["Volume"].iloc[-1]) if not es["Volume"].empty else 0
        vol_promedio = float(es["Volume"].mean())   if not es["Volume"].empty else 1
        ratio_vol    = vol_reciente / vol_promedio if vol_promedio > 0 else 1.0

        if   diferencia > 0.3 and ratio_vol > 1.2: sesgo = "ALCISTA_FUERTE";   neto = round(diferencia * 1000)
        elif diferencia > 0.1:                      sesgo = "ALCISTA_MODERADO"; neto = round(diferencia * 500)
        elif diferencia < -0.3 and ratio_vol > 1.2: sesgo = "BAJISTA_FUERTE";   neto = round(diferencia * 1000)
        elif diferencia < -0.1:                     sesgo = "BAJISTA_MODERADO"; neto = round(diferencia * 500)
        else:                                        sesgo = "NEUTRAL";          neto = 0

        cot_cache.update({
            "neto_largo": neto, "sesgo": sesgo,
            "ultima_actualizacion": hora_ny(), "disponible": True,
            "fuente": "PROXY", "fecha_reporte": None,
        })
        print(f"  [COT] 📊 PROXY — Sesgo:{sesgo} | Neto estimado:{neto:+,}")
        return True

    except Exception as e:
        print(f"  [COT] Proxy error: {e}")
        cot_cache["disponible"] = False
        return False



# ── CME ES Open Interest desde GitHub ────────────────────────
# URL del archivo JSON actualizado diariamente por GitHub Actions
# Repo PRIVADO — se usa api.github.com con GH_TOKEN
# (raw.githubusercontent.com devuelve 404 en repos privados)
CME_OI_URL = "https://api.github.com/repos/amalec17avila-sudo/us500-bot/contents/data/es_oi.json"
GH_TOKEN   = os.environ.get("GH_TOKEN", "")

cme_oi_cache = {
    "disponible":        False,
    "oi_actual":         0,
    "cambio_diario":     0,
    "cambio_semanal":    0,
    "fecha":             None,
    "ultima_actualizacion": None,
}

# ════════════════════════════════════════════════════════════════
# ÍNDICE DE POSICIONAMIENTO DE TIBURONES 🦈
# Infiere el sesgo de los Leveraged Funds por CAUSA-EFECTO: si los
# tiburones se posicionan, dejan huellas observables. Combina las
# huellas en un sesgo direccional con confluencia (NO un número de
# contratos — eso es imposible de replicar sin los datos privados
# de la CFTC). Cada huella vota dirección + fuerza; suma ponderada
# da el sesgo; la dispersión distingue neutral-consenso de
# neutral-conflicto.
# ════════════════════════════════════════════════════════════════
indice_tiburones_cache = {
    "disponible":     False,
    "sesgo":          None,     # ALCISTA / BAJISTA / NEUTRAL
    "tipo_neutral":   None,     # "consenso" / "conflicto" / None
    "score":          0.0,      # suma ponderada de votos (-1 a +1 aprox)
    "confluencia":    0,        # cuántas huellas activas votan igual
    "huellas_activas":0,        # cuántas huellas tienen dato hoy
    "confianza":      "BAJA",   # ALTA / MEDIA / BAJA
    "huellas":        {},       # detalle por huella: {nombre: {voto, fuerza, texto}}
    "ultima_actualizacion": None,
}

def obtener_cme_oi():
    """
    Lee el Open Interest diario de E-Mini S&P 500 desde GitHub.
    El archivo es actualizado automáticamente cada día al cierre
    por el workflow de GitHub Actions.
    Usa la API de GitHub con GH_TOKEN porque el repo es privado.
    """
    global cme_oi_cache
    try:
        import urllib.request, json, base64

        gh_headers = {
            "User-Agent":    "Mozilla/5.0",
            "Accept":        "application/vnd.github.v3+json",
            "Cache-Control": "no-cache",
        }
        if GH_TOKEN:
            gh_headers["Authorization"] = f"Bearer {GH_TOKEN}"

        req = urllib.request.Request(CME_OI_URL, headers=gh_headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            api_response = json.loads(resp.read().decode())

        # La API devuelve el contenido en base64
        content_b64 = api_response.get("content", "")
        if not content_b64:
            print("  [CME_OI] ⚠️ Respuesta API sin contenido")
            return
        datos = json.loads(base64.b64decode(content_b64).decode("utf-8"))

        oi_actual      = datos.get("oi_actual", 0)
        cambio_diario  = datos.get("cambio_diario", 0)
        cambio_semanal = datos.get("cambio_semanal", 0)
        fecha          = datos.get("fecha", "N/D")

        if oi_actual > 0:
            cme_oi_cache.update({
                "disponible":        True,
                "oi_actual":         oi_actual,
                "cambio_diario":     cambio_diario,
                "cambio_semanal":    cambio_semanal,
                "fecha":             fecha,
                "ultima_actualizacion": hora_ny(),
            })
            print(f"  [CME_OI] ✅ ES OI: {oi_actual:,} | "
                  f"Diario: {cambio_diario:+,} | Semanal: {cambio_semanal:+,} ({fecha})")
        else:
            print("  [CME_OI] ⚠️ OI=0 en el archivo")

    except Exception as e:
        print(f"  [CME_OI] Error: {e}")

# ================================================================
# === SISTEMA DE APRENDIZAJE — JOURNAL DE SEÑALES ================
# ================================================================
# Cada señal ≥|7| se registra con sus componentes activos.
# 30 y 60 min después se etiqueta como acierto/fallo según el precio.
# Con el historial se calcula win rate por componente → base para
# ajustar pesos. Persiste en GitHub (data/journal_senales.json).

JOURNAL_URL          = "https://api.github.com/repos/amalec17avila-sudo/us500-bot/contents/data/journal_senales.json"
JOURNAL_UMBRAL_30MIN = 3.0   # pts a favor para acierto a 30 min
JOURNAL_UMBRAL_60MIN = 5.0   # pts a favor para acierto a 60 min
JOURNAL_MAX_SENALES  = 500

signal_journal = {
    "senales":    [],     # historial completo
    "pendientes": [],     # referencias a señales sin etiquetar
    "sha":        None,   # sha del archivo en GitHub para updates
    "dirty":      False,  # hay cambios sin guardar
}

def _gh_headers():
    h = {"User-Agent": "Mozilla/5.0",
         "Accept": "application/vnd.github.v3+json"}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h

def cargar_journal_github():
    """Carga el journal desde GitHub al arrancar el bot."""
    try:
        import base64
        req = urllib.request.Request(JOURNAL_URL, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            api_resp = json.loads(resp.read().decode())
        signal_journal["sha"] = api_resp.get("sha")
        contenido = base64.b64decode(api_resp.get("content", "")).decode("utf-8")
        datos = json.loads(contenido)
        signal_journal["senales"] = datos.get("senales", [])[-JOURNAL_MAX_SENALES:]
        # Reconstruir pendientes: señales sin resultado_60 ni expiradas
        signal_journal["pendientes"] = [
            s for s in signal_journal["senales"]
            if s.get("resultado_60") is None and not s.get("expirada")
        ]
        print(f"  [JOURNAL] ✅ Cargado — {len(signal_journal['senales'])} señales, "
              f"{len(signal_journal['pendientes'])} pendientes")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("  [JOURNAL] 📊 Sin journal previo — se creará al cierre")
        else:
            print(f"  [JOURNAL] Error cargando: HTTP {e.code}")
    except Exception as e:
        print(f"  [JOURNAL] Error cargando: {e}")

def guardar_journal_github():
    """Persiste el journal en GitHub. Se llama al cierre del mercado."""
    if not signal_journal["dirty"] and signal_journal["sha"]:
        return
    if not GH_TOKEN:
        print("  [JOURNAL] ⚠️ Sin GH_TOKEN — journal solo en memoria")
        return
    try:
        import base64
        payload_datos = {"senales": signal_journal["senales"][-JOURNAL_MAX_SENALES:]}
        contenido_b64 = base64.b64encode(
            json.dumps(payload_datos, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        body = {
            "message": f"Journal señales {hora_ny().strftime('%Y-%m-%d %H:%M')}",
            "content": contenido_b64,
        }
        if signal_journal["sha"]:
            body["sha"] = signal_journal["sha"]
        req = urllib.request.Request(
            JOURNAL_URL, data=json.dumps(body).encode("utf-8"),
            headers={**_gh_headers(), "Content-Type": "application/json"},
            method="PUT")
        with urllib.request.urlopen(req, timeout=15) as resp:
            api_resp = json.loads(resp.read().decode())
        signal_journal["sha"]   = api_resp.get("content", {}).get("sha")
        signal_journal["dirty"] = False
        print(f"  [JOURNAL] 💾 Guardado en GitHub — {len(signal_journal['senales'])} señales")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("  [JOURNAL] ⚠️ GH_TOKEN sin permiso de ESCRITURA — dale 'contents: write'")
        else:
            print(f"  [JOURNAL] Error guardando: HTTP {e.code}")
    except Exception as e:
        print(f"  [JOURNAL] Error guardando: {e}")

def registrar_senal_journal(resultado):
    """Registra snapshot de una señal emitida para evaluarla después."""
    try:
        ahora = hora_ny()
        comps_activos = {k: v for k, v in resultado["componentes"].items() if v != 0}
        senal = {
            "id":           ahora.strftime("%Y%m%d_%H%M%S"),
            "fecha":        ahora.strftime("%Y-%m-%d"),
            "hora":         ahora.strftime("%H:%M ET"),
            "ts":           time.time(),
            "direccion":    "ALCISTA" if resultado["score"] > 0 else "BAJISTA",
            "score":        resultado["score"],
            "precio":       resultado["detalle"]["precio"],
            "componentes":  comps_activos,
            "resultado_30": None, "delta_30": None,
            "resultado_60": None, "delta_60": None,
        }
        signal_journal["senales"].append(senal)
        signal_journal["pendientes"].append(senal)
        if len(signal_journal["senales"]) > JOURNAL_MAX_SENALES:
            signal_journal["senales"] = signal_journal["senales"][-JOURNAL_MAX_SENALES:]
        signal_journal["dirty"] = True
        print(f"  [JOURNAL] 📝 Señal registrada — {senal['direccion']} {senal['score']:+d} @ {senal['precio']}")
    except Exception as e:
        print(f"  [JOURNAL] Error registrando: {e}")

def verificar_senales_pendientes(precio_actual):
    """Etiqueta señales pendientes a 30 y 60 min según el precio actual."""
    if not signal_journal["pendientes"]:
        return
    ahora_ts   = time.time()
    completadas = []
    for s in signal_journal["pendientes"]:
        try:
            mins = (ahora_ts - s["ts"]) / 60
            # Delta a favor de la dirección de la señal
            if s["direccion"] == "ALCISTA":
                delta = precio_actual - s["precio"]
            else:
                delta = s["precio"] - precio_actual

            if mins >= 30 and s["resultado_30"] is None:
                s["delta_30"]    = round(delta, 2)
                s["resultado_30"] = delta >= JOURNAL_UMBRAL_30MIN
                signal_journal["dirty"] = True
                print(f"  [JOURNAL] 30min {s['id']}: {'✅' if s['resultado_30'] else '❌'} ({delta:+.1f} pts)")

            if mins >= 60 and s["resultado_60"] is None:
                s["delta_60"]    = round(delta, 2)
                s["resultado_60"] = delta >= JOURNAL_UMBRAL_60MIN
                signal_journal["dirty"] = True
                completadas.append(s)
                print(f"  [JOURNAL] 60min {s['id']}: {'✅' if s['resultado_60'] else '❌'} ({delta:+.1f} pts)")

            # Señal vieja sin etiquetar (reinicio largo) → expirar
            if mins > 90 and s["resultado_60"] is None:
                s["expirada"] = True
                signal_journal["dirty"] = True
                completadas.append(s)
        except Exception as e:
            print(f"  [JOURNAL] Error verificando {s.get('id','?')}: {e}")
            completadas.append(s)
    for s in completadas:
        if s in signal_journal["pendientes"]:
            signal_journal["pendientes"].remove(s)

def calcular_win_rates():
    """Win rate por componente sobre señales con resultado_60 conocido."""
    stats = {}
    total_senales = 0
    wins_senales  = 0
    for s in signal_journal["senales"]:
        if s.get("expirada") or s.get("resultado_60") is None:
            continue
        win = s["resultado_60"]
        total_senales += 1
        if win: wins_senales += 1
        dir_alcista = s["direccion"] == "ALCISTA"
        for comp, val in s.get("componentes", {}).items():
            # Solo componentes alineados con la dirección de la señal
            alineado = (val > 0 and dir_alcista) or (val < 0 and not dir_alcista)
            if not alineado:
                continue
            st = stats.setdefault(comp, {"wins": 0, "total": 0})
            st["total"] += 1
            if win: st["wins"] += 1
    return stats, total_senales, wins_senales

def texto_win_rates():
    """Resumen de win rates para Telegram (resumen dominical)."""
    stats, total, wins = calcular_win_rates()
    if total == 0:
        return ""
    wr_global = wins / total * 100
    lineas = [f"\n{'─'*28}\n🧠 *Aprendizaje ({total} señales evaluadas):*",
              f"  Win rate global 60min: `{wr_global:.0f}%`"]
    # Ordenar por win rate, solo componentes con n>=3
    orden = sorted(
        [(c, st["wins"]/st["total"]*100, st["total"])
         for c, st in stats.items() if st["total"] >= 3],
        key=lambda x: -x[1])
    for comp, wr, n in orden[:6]:
        emoji = "🟢" if wr >= 65 else ("⚪" if wr >= 45 else "🔴")
        lineas.append(f"  {emoji} {comp.replace('_',' ')}: `{wr:.0f}%` (n={n})")
    if orden:
        peor = orden[-1]
        if peor[1] < 40:
            lineas.append(f"  💡 Candidato a bajar peso: {peor[0].replace('_',' ')}")
    return "\n".join(lineas)

# ── Persistencia del estado COT Estimado en GitHub ───────────
# Sin esto, cada deploy de Railway borra la acumulación de la semana.
COT_ESTADO_URL = "https://api.github.com/repos/amalec17avila-sudo/us500-bot/contents/data/cot_estado.json"
_cot_estado_sha = {"sha": None}

def guardar_estado_cot_github():
    """Persiste cot_estimado_cache + cot_senales_semana en GitHub."""
    if not GH_TOKEN:
        return
    try:
        import base64
        estado = {
            "cot_estimado_cache": {k: v for k, v in cot_estimado_cache.items()
                                   if k != "ultima_actualizacion"},
            "cot_senales_semana": dict(cot_senales_semana),
            "guardado":           hora_ny().strftime("%Y-%m-%d %H:%M ET"),
        }
        contenido_b64 = base64.b64encode(
            json.dumps(estado, ensure_ascii=False, default=str).encode("utf-8")
        ).decode("ascii")
        body = {"message": f"Estado COT {hora_ny().strftime('%Y-%m-%d %H:%M')}",
                "content": contenido_b64}
        if _cot_estado_sha["sha"]:
            body["sha"] = _cot_estado_sha["sha"]
        req = urllib.request.Request(
            COT_ESTADO_URL, data=json.dumps(body).encode("utf-8"),
            headers={**_gh_headers(), "Content-Type": "application/json"},
            method="PUT")
        with urllib.request.urlopen(req, timeout=15) as resp:
            api_resp = json.loads(resp.read().decode())
        _cot_estado_sha["sha"] = api_resp.get("content", {}).get("sha")
        print("  [COT_ESTADO] 💾 Estado COT guardado en GitHub")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("  [COT_ESTADO] ⚠️ GH_TOKEN sin permiso de escritura")
        else:
            print(f"  [COT_ESTADO] Error guardando: HTTP {e.code}")
    except Exception as e:
        print(f"  [COT_ESTADO] Error guardando: {e}")

def cargar_estado_cot_github():
    """Restaura el estado COT al arrancar — sobrevive a los deploys."""
    try:
        import base64
        req = urllib.request.Request(COT_ESTADO_URL, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            api_resp = json.loads(resp.read().decode())
        _cot_estado_sha["sha"] = api_resp.get("sha")
        estado = json.loads(base64.b64decode(api_resp.get("content", "")).decode("utf-8"))
        cec = estado.get("cot_estimado_cache", {})
        if cec.get("disponible"):
            cot_estimado_cache.update(cec)
            cot_estimado_cache["ultima_actualizacion"] = None  # fuerza recálculo
        css = estado.get("cot_senales_semana", {})
        if css:
            cot_senales_semana.update(css)
        print(f"  [COT_ESTADO] ✅ Restaurado — base:{cot_estimado_cache.get('cot_base',0):+,} | "
              f"días acumulados:{cot_senales_semana.get('dias_acumulados',0)} | "
              f"guardado: {estado.get('guardado','?')}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("  [COT_ESTADO] 📊 Sin estado previo — se creará el viernes")
        else:
            print(f"  [COT_ESTADO] Error cargando: HTTP {e.code}")
    except Exception as e:
        print(f"  [COT_ESTADO] Error cargando: {e}")

def iniciar_acumulacion_cot():
    """
    Se llama cada viernes cuando llega el nuevo COT real.
    Guarda el COT real como base e inicia acumulación para la semana siguiente.
    Miércoles de esta semana → martes de la próxima = datos del próximo COT.
    """
    global cot_senales_semana, cot_estimado_cache

    if not cot_cache["disponible"] or not cot_cache["neto_largo"]:
        return

    ahora  = hora_ny()
    semana = ahora.isocalendar()[1]

    # Guardar el COT real como nuevo punto de partida
    cot_estimado_cache["cot_base"]          = cot_cache["neto_largo"]
    cot_estimado_cache["semana_estimando"]  = semana + 1  # Estimamos la SIGUIENTE semana
    cot_estimado_cache["fecha_inicio"]      = ahora.date()
    cot_estimado_cache["cambio_estimado"]   = 0
    cot_estimado_cache["neto_estimado"]     = cot_cache["neto_largo"]
    cot_estimado_cache["disponible"]        = True
    # Marcar el reporte base como consumido — evita re-validar el mismo COT
    cot_estimado_cache["ultima_fecha_validada"] = cot_cache.get("fecha_reporte")

    # Resetear acumulador para la nueva semana
    cot_senales_semana.update({
        "sweep_prima_calls": 0.0,
        "sweep_prima_puts":  0.0,
        "dp_dias_alcista":   0,
        "dp_dias_bajista":   0,
        "pc_ratios":         [],
        "dias_acumulados":   0,
        "semana":            semana + 1,
        "fecha_inicio":      ahora.date(),
    })

    print(f"  [COT_EST] 🚀 Iniciando estimación semana {semana+1} — base: {cot_cache['neto_largo']:+,}")



def acumular_senales_cot():
    """
    JUBILADA (sesión 22-jun): reemplazada por el Índice de Tiburones.
    El COT estimado viejo producía un número de contratos imposible de
    replicar (+915,218 vs real +57,307). Se conserva la firma para no
    romper las llamadas del loop, pero no hace nada.
    """
    return
    # --- código viejo deshabilitado debajo ---
    global cot_senales_semana

    if not cot_estimado_cache["disponible"]:
        return
    if not cot_senales_semana["fecha_inicio"]:
        return

    ahora = hora_ny()

    # Acumular sweep neto del día (prima calls vs puts)
    if sweep_cache.get("ultimo_sweep") and sweep_cache["ultimo_sweep"].date() == ahora.date():
        prima_calls_hoy = sweep_cache.get("prima_calls_hoy", 0)
        prima_puts_hoy  = sweep_cache.get("prima_puts_hoy", 0)
        cot_senales_semana["sweep_prima_calls"] += prima_calls_hoy
        cot_senales_semana["sweep_prima_puts"]  += prima_puts_hoy

    # Acumular tendencia Dark Pool del día
    if dark_pool_cache["disponible"]:
        tend = dark_pool_cache.get("tendencia", "NEUTRAL")
        if tend in ["ACUMULANDO", "MOMENTUM_ALCISTA"]:
            cot_senales_semana["dp_dias_alcista"] += 1
        elif tend in ["DISTRIBUYENDO", "MOMENTUM_BAJISTA"]:
            cot_senales_semana["dp_dias_bajista"] += 1

    # Acumular PC ratio del día
    if pc_semanal_cache["disponible"]:
        cot_senales_semana["pc_ratios"].append(pc_semanal_cache["ratio_semanal"])

    cot_senales_semana["dias_acumulados"] += 1

    print(f"  [COT_EST] 📅 Día {cot_senales_semana['dias_acumulados']} acumulado — "
          f"Calls: ${cot_senales_semana['sweep_prima_calls']:,.0f} | "
          f"Puts: ${cot_senales_semana['sweep_prima_puts']:,.0f} | "
          f"DP: +{cot_senales_semana['dp_dias_alcista']}d/-{cot_senales_semana['dp_dias_bajista']}d")

    # Actualizar el estimado con los datos acumulados
    actualizar_cot_estimado()


def actualizar_cot_estimado():
    """
    JUBILADA (sesión 22-jun): reemplazada por el Índice de Tiburones.
    Se conserva la firma para no romper llamadas del loop.
    """
    return
    # --- código viejo deshabilitado debajo ---
    global cot_estimado_cache

    if not cot_estimado_cache["disponible"]:
        return
    if not cot_cache["disponible"] or not cot_cache["neto_largo"]:
        return

    cot_base = cot_estimado_cache["cot_base"]

    # ── 1. Ajuste por Sweep neto acumulado ───────────────────
    # $100M neto en calls ≈ +50K contratos institucionales comprando
    # $100M neto en puts  ≈ -50K contratos institucionales vendiendo
    sweep_balance = (cot_senales_semana["sweep_prima_calls"] -
                     cot_senales_semana["sweep_prima_puts"])
    ajuste_sweep  = int(sweep_balance / 100_000_000 * 50_000)

    # ── 2. Ajuste por Dark Pool sostenido ────────────────────
    # Cada día alcista ≈ +25K contratos, cada día bajista ≈ -25K
    dias_dp   = cot_senales_semana["dp_dias_alcista"] - cot_senales_semana["dp_dias_bajista"]
    ajuste_dp = dias_dp * 25_000

    # ── 3. Ajuste por Put/Call ratio promedio ─────────────────
    ajuste_pc = 0
    if cot_senales_semana["pc_ratios"]:
        pc_promedio = sum(cot_senales_semana["pc_ratios"]) / len(cot_senales_semana["pc_ratios"])
        if pc_promedio < 0.8:
            ajuste_pc = int((0.8 - pc_promedio) * 150_000)
        elif pc_promedio > 1.2:
            ajuste_pc = -int((pc_promedio - 1.2) * 150_000)

    # ── 4. Ajuste por CME OI real (si disponible) o SPY/SHY ──
    ajuste_rotacion = 0
    if cme_oi_cache["disponible"] and cme_oi_cache["cambio_semanal"] != 0:
        # Usar cambio semanal real del OI de /ES — dato directo del CME
        # OI sube → institucionales abriendo largos → ajuste positivo
        # OI baja → institucionales cerrando o abriendo cortos → ajuste negativo
        cambio_oi_semana = cme_oi_cache["cambio_semanal"]
        ajuste_rotacion  = int(cambio_oi_semana * 0.5)  # 50% del cambio real como ajuste
        print(f"  [COT_EST] CME OI semanal: {cambio_oi_semana:+,} → ajuste rotación: {ajuste_rotacion:+,}")
    else:
        # Fallback: SPY/SHY rotation
        try:
            spy_data = yf.download("SPY", period="5d", interval="1d", progress=False)
            shy_data = yf.download("SHY", period="5d", interval="1d", progress=False)
            if not spy_data.empty and not shy_data.empty:
                spy_close = spy_data["Close"].squeeze()
                shy_close = shy_data["Close"].squeeze()
                spy_ret   = float((spy_close.iloc[-1] / spy_close.iloc[0] - 1) * 100)
                shy_ret   = float((shy_close.iloc[-1] / shy_close.iloc[0] - 1) * 100)
                diferencia = spy_ret - shy_ret
                if abs(diferencia) > 2:
                    ajuste_rotacion = int(diferencia * 20_000)
        except: pass

    # ── Calcular cambio y COT estimado ───────────────────────
    cambio_estimado = ajuste_sweep + ajuste_dp + ajuste_pc + ajuste_rotacion
    neto_estimado   = cot_base + cambio_estimado

    # ── Confianza basada en historial ─────────────────────────
    historial = cot_estimado_cache["historial_error"]
    if len(historial) >= 2:
        error_prom = sum(abs(e) for e in historial) / len(historial)
        confianza  = max(0.0, min(1.0, 1.0 - (error_prom / 10.0)))
    elif len(historial) == 1:
        confianza = 0.3
    else:
        confianza = 0.1  # Sin historial aún

    # ── Sesgo estimado ───────────────────────────────────────
    umbral = 500_000
    if neto_estimado > umbral * 2:   sesgo_est = "ALCISTA_FUERTE"
    elif neto_estimado > umbral:     sesgo_est = "ALCISTA_MODERADO"
    elif neto_estimado < -umbral*2:  sesgo_est = "BAJISTA_FUERTE"
    elif neto_estimado < -umbral:    sesgo_est = "BAJISTA_MODERADO"
    else:                            sesgo_est = "NEUTRAL"

    dias_acc = cot_senales_semana["dias_acumulados"]
    cot_estimado_cache.update({
        "cambio_estimado":      cambio_estimado,
        "neto_estimado":        neto_estimado,
        "sesgo":                sesgo_est,
        "confianza":            confianza,
        "componentes": {
            "sweep":     ajuste_sweep,
            "dark_pool": ajuste_dp,
            "rotacion":  ajuste_rotacion,
            "pc_semanal":ajuste_pc,
        },
        "ultima_actualizacion": hora_ny(),
    })

    print(f"  [COT_EST] 📊 Base:{cot_base:+,} | Cambio:{cambio_estimado:+,} | "
          f"Estimado:{neto_estimado:+,} | Días:{dias_acc} | Confianza:{confianza:.0%}")


# ════════════════════════════════════════════════════════════════
# ÍNDICE DE POSICIONAMIENTO DE TIBURONES — cálculo de las huellas
# ════════════════════════════════════════════════════════════════
def _huella_basis():
    """
    Huella 5: Basis = futuro /ES vs spot ^GSPC.
    Futuro caro vs spot (contango fuerte) → presión compradora en
    futuros (donde juegan los hedge funds) → voto ALCISTA.
    Futuro barato vs spot (backwardation) → presión vendedora → BAJISTA.
    yfinance da ambos limpios.
    """
    try:
        es  = yf.download("ES=F",  period="2d", interval="1d", progress=False)
        spx = yf.download("^GSPC", period="2d", interval="1d", progress=False)
        if es.empty or spx.empty:
            return None
        es_close  = float(es["Close"].squeeze().iloc[-1])
        spx_close = float(spx["Close"].squeeze().iloc[-1])
        # Basis en puntos. El futuro normalmente cotiza con una pequeña
        # prima por costo de acarreo; lo relevante es la DESVIACIÓN.
        basis = es_close - spx_close
        # Normalizar como % del índice para juzgar magnitud
        basis_pct = basis / spx_close * 100
        # Umbral: >0.15% prima fuerte = alcista; <-0.05% descuento = bajista
        if basis_pct > 0.15:
            return {"voto": +1, "fuerza": min(1.0, basis_pct / 0.30),
                    "texto": f"futuro caro +{basis:.1f}pts (presión compradora)"}
        elif basis_pct < -0.05:
            return {"voto": -1, "fuerza": min(1.0, abs(basis_pct) / 0.20),
                    "texto": f"futuro barato {basis:.1f}pts (presión vendedora)"}
        else:
            return {"voto": 0, "fuerza": 0.2,
                    "texto": f"basis neutro {basis:+.1f}pts"}
    except Exception as e:
        print(f"  [TIBURONES] Error basis: {e}")
        return None


def _huella_volumen_futuros():
    """
    Huella 2: Volumen direccional de futuros /ES.
    Volumen alto + vela alcista → institucionales comprando con tamaño.
    Volumen alto + vela bajista → vendiendo con tamaño.
    yfinance da volumen de /ES con cierto retraso pero usable.
    """
    try:
        es = yf.download("ES=F", period="5d", interval="1d", progress=False)
        if es.empty or len(es) < 2:
            return None
        cierres = es["Close"].squeeze()
        vols    = es["Volume"].squeeze()
        apert   = es["Open"].squeeze()
        vol_hoy   = float(vols.iloc[-1])
        vol_prom  = float(vols.iloc[:-1].mean())
        cambio    = float(cierres.iloc[-1] - apert.iloc[-1])  # vela del día
        if vol_prom <= 0:
            return None
        ratio_vol = vol_hoy / vol_prom
        # Solo cuenta si el volumen es notable (>1.1x promedio)
        if ratio_vol < 1.1:
            return {"voto": 0, "fuerza": 0.2,
                    "texto": f"volumen normal ({ratio_vol:.1f}x)"}
        # Volumen alto: la dirección de la vela manda
        fuerza = min(1.0, (ratio_vol - 1.0))
        if cambio > 0:
            return {"voto": +1, "fuerza": fuerza,
                    "texto": f"volumen alto {ratio_vol:.1f}x + vela alcista"}
        elif cambio < 0:
            return {"voto": -1, "fuerza": fuerza,
                    "texto": f"volumen alto {ratio_vol:.1f}x + vela bajista"}
        else:
            return {"voto": 0, "fuerza": 0.2, "texto": "volumen alto sin dirección"}
    except Exception as e:
        print(f"  [TIBURONES] Error volumen: {e}")
        return None


def calcular_indice_tiburones():
    """
    Combina las huellas observables del posicionamiento de Leveraged
    Funds en un sesgo direccional con confluencia.

    Huellas ACTIVAS (cada una vota -1/0/+1 con una fuerza 0-1):
      1. CME OI   (sube/baja)            — peso 1.2 (directa)
      2. Sweeps   (calls vs puts netos)  — peso 1.5-2.0 (la más directa;
                  solo cuenta con mercado abierto, ver fix pre-market)

    Huellas RETIRADAS por datos no confiables (yfinance):
      - Volumen fut. (5-jul): 0.1-0.4x clavado, sin relación con el flujo
      - Dark Pool (23-jun): proxy 20% clavado por bug de clamp
      - Basis (23-jun): swings imposibles por desfase horario /ES vs ^GSPC
      - Roll/term: nunca implementada (sin fuente confiable en yfinance)

    Suma ponderada → dirección. Dispersión de votos → distingue
    neutral por consenso (todos tibios) de neutral por conflicto
    (huellas peleando).
    """
    global indice_tiburones_cache

    huellas = {}   # nombre → {voto, fuerza, peso, texto}

    # ── Huella 1: CME OI (sube = abriendo, baja = cerrando) ──────
    if cme_oi_cache.get("disponible") and cme_oi_cache.get("cambio_diario", 0) != 0:
        cambio_oi = cme_oi_cache["cambio_diario"]
        # Normalizar: un cambio diario típico del E-mini es ~10-50k contratos
        fuerza = min(1.0, abs(cambio_oi) / 50_000)
        voto = 1 if cambio_oi > 0 else -1
        signo = "subiendo" if cambio_oi > 0 else "bajando"
        huellas["CME OI"] = {"voto": voto, "fuerza": fuerza, "peso": 1.2,
                             "texto": f"OI {signo} ({cambio_oi:+,})"}

    # ── Huella 3: Sweeps (magnitud + aceleración) ────────────────
    # Implementa los dos patrones que el usuario observó operando:
    #  (A) MAGNITUD: si el balance neto se acerca a ~$500M, el precio
    #      sigue esa dirección (pared de dinero sin contraparte).
    #      Bajo ~$80M puede ser ruido absorbible.
    #  (B) ACELERACIÓN: si el balance viene creciendo y luego DESACELERA
    #      (ej. +220M → +190M), hay agotamiento aunque el signo siga
    #      igual — el precio deja de seguir el movimiento. La clave no
    #      es el signo sino la DERIVADA del balance.
    #  (C) PRE-MARKET: antes de la apertura (9:30 ET) los sweeps 0DTE que
    #      quedan en el cache son del día ANTERIOR — contratos ya vencidos,
    #      data muerta que no dice nada del día de hoy. Solo se cuenta la
    #      huella de sweeps con el mercado ABIERTO. Así el índice del
    #      pre-market se arma con las demás huellas (CME OI, etc.) sin
    #      contaminarse con flujo caduco.
    prima_c = sweep_cache.get("prima_calls_hoy", 0)
    prima_p = sweep_cache.get("prima_puts_hoy", 0)
    if (mercado_abierto() and sweep_cache.get("ultimo_sweep") and prima_c + prima_p > 0):
        balance = prima_c - prima_p
        abs_bal = abs(balance)

        # (A) Fuerza por MAGNITUD — escalones según lo observado
        UMBRAL_DOMINANTE = 500_000_000   # ~$500M = convicción dominante
        UMBRAL_RUIDO     = 80_000_000    # bajo esto = ruido absorbible
        if abs_bal >= UMBRAL_DOMINANTE:
            fuerza_mag = 1.0
            nivel_mag  = "DOMINANTE"
        elif abs_bal <= UMBRAL_RUIDO:
            fuerza_mag = 0.2
            nivel_mag  = "ruido"
        else:
            # Interpolar entre ruido y dominante
            fuerza_mag = 0.3 + 0.7 * (abs_bal - UMBRAL_RUIDO) / (UMBRAL_DOMINANTE - UMBRAL_RUIDO)
            nivel_mag  = "moderado"

        # (B) ACELERACIÓN — comparar el balance actual vs lecturas previas
        hist = sweep_cache.get("historial_balance", [])
        acel_txt = ""
        factor_acel = 1.0
        if len(hist) >= 3:
            # Tendencia de la magnitud en la misma dirección del balance
            # Tomamos las últimas 3 lecturas y vemos si |balance| crece o cae
            ult3 = hist[-3:]
            # ¿Vienen todas del mismo signo que el balance actual?
            mismo_signo = all((b > 0) == (balance > 0) for b in ult3 if b != 0)
            if mismo_signo and len(ult3) == 3:
                mag_prev = abs(ult3[-2])
                mag_now  = abs(ult3[-1])
                if mag_now > mag_prev * 1.05:
                    # Acelerando: el flujo gana fuerza → confirma dirección
                    factor_acel = 1.15
                    acel_txt = " ▲acelerando"
                elif mag_now < mag_prev * 0.95:
                    # Desacelerando: AGOTAMIENTO → el precio puede no seguir
                    factor_acel = 0.55
                    acel_txt = " ▼desacelera (agotamiento)"
                else:
                    acel_txt = " →estable"

        fuerza = min(1.0, fuerza_mag * factor_acel)

        if balance > 0:
            voto = +1
            txt  = f"calls +${balance/1e6:.0f}M [{nivel_mag}]{acel_txt}"
        elif balance < 0:
            voto = -1
            txt  = f"puts -${abs_bal/1e6:.0f}M [{nivel_mag}]{acel_txt}"
        else:
            voto, txt = 0, "equilibrado"

        # Peso mayor si la magnitud es dominante (el usuario confía más
        # en los sweeps grandes que en cualquier otra señal)
        peso_sweep = 2.0 if nivel_mag == "DOMINANTE" else 1.5
        huellas["Sweeps"] = {"voto": voto, "fuerza": fuerza, "peso": peso_sweep,
                             "texto": txt}

    # ── Huella Dark Pool: ELIMINADA (martes 23-jun) ──────────────
    # El proxy de yfinance daba 20% clavado todos los días (bug de
    # clamp + lógica de índices mal alineada). El dark pool REAL
    # requiere servicio pago — anotado como mejora futura. Mientras
    # tanto los sweeps (Tradier, datos reales) son la huella
    # institucional confiable.

    # ── Huella Basis: ELIMINADA (martes 23-jun) ──────────────────
    # Cálculo roto por desfase horario: comparaba /ES de hoy (cotiza
    # 24h) contra ^GSPC de ayer (solo horario de mercado). Daba
    # swings imposibles (-31.8pts un día, +81.8pts el otro). El basis
    # real del S&P es de pocos puntos; su aporte direccional era
    # marginal incluso bien calculado. Se quita por ruido.

    # ── Huella 2: Volumen direccional de futuros — RETIRADA (5-jul) ──
    # El volumen de /ES vía yfinance da valores no confiables (0.1-0.4x
    # clavado, sin relación con el flujo real), así que su voto era ruido.
    # Se retira del índice igual que Dark Pool y Basis. La función
    # _huella_volumen_futuros() se conserva definida por si en el futuro
    # se conecta una fuente de volumen confiable (CME directo o de pago),
    # pero ya no vota.
    # hv = _huella_volumen_futuros()
    # if hv:
    #     huellas["Volumen"] = {**hv, "peso": 0.8}

    # ── Huella 6: Roll/term structure — PENDIENTE ────────────────
    # No hay fuente confiable de contratos por vencimiento en yfinance.
    # Se marca pendiente para no meter ruido (decisión del usuario).

    # ── Combinar votos ───────────────────────────────────────────
    if not huellas:
        indice_tiburones_cache.update({
            "disponible": False, "sesgo": None, "ultima_actualizacion": hora_ny()})
        return

    # Suma ponderada: cada huella aporta voto × fuerza × peso
    suma_pond = sum(h["voto"] * h["fuerza"] * h["peso"] for h in huellas.values())
    peso_total = sum(h["peso"] for h in huellas.values())
    score = suma_pond / peso_total if peso_total > 0 else 0.0

    # Confluencia: cuántas huellas votan en la dirección dominante
    direccion = 1 if score > 0 else (-1 if score < 0 else 0)
    votos_a_favor = sum(1 for h in huellas.values() if h["voto"] == direccion and direccion != 0)
    votos_total   = sum(1 for h in huellas.values() if h["voto"] != 0)

    # Dispersión: desviación de los votos (distingue consenso de conflicto)
    votos_lista = [h["voto"] for h in huellas.values()]
    n_pos = sum(1 for v in votos_lista if v > 0)
    n_neg = sum(1 for v in votos_lista if v < 0)

    # ── Determinar sesgo y tipo de neutral ───────────────────────
    UMBRAL_SESGO = 0.15  # |score| mínimo para declarar dirección
    tipo_neutral = None
    if abs(score) >= UMBRAL_SESGO:
        sesgo = "ALCISTA" if score > 0 else "BAJISTA"
    else:
        sesgo = "NEUTRAL"
        # Conflicto: hay votos fuertes en AMBAS direcciones
        if n_pos >= 1 and n_neg >= 1 and (n_pos + n_neg) >= 2:
            tipo_neutral = "conflicto"
        else:
            tipo_neutral = "consenso"

    # ── Confianza según confluencia y fuerza ─────────────────────
    if abs(score) >= 0.40 and votos_a_favor >= 3:
        confianza = "ALTA"
    elif abs(score) >= 0.20 and votos_a_favor >= 2:
        confianza = "MEDIA"
    else:
        confianza = "BAJA"

    indice_tiburones_cache.update({
        "disponible":     True,
        "sesgo":          sesgo,
        "tipo_neutral":   tipo_neutral,
        "score":          round(score, 3),
        "confluencia":    votos_a_favor,
        "huellas_activas":votos_total,
        "confianza":      confianza,
        "huellas":        huellas,
        "ultima_actualizacion": hora_ny(),
    })

    print(f"  [TIBURONES] 🦈 Sesgo:{sesgo}"
          f"{'/'+tipo_neutral if tipo_neutral else ''} | "
          f"Score:{score:+.2f} | Confluencia:{votos_a_favor}/{votos_total} | "
          f"Confianza:{confianza} | Huellas:{len(huellas)}")


def texto_indice_tiburones():
    """Arma el mensaje del Índice de Tiburones para Telegram."""
    ic = indice_tiburones_cache
    if not ic.get("disponible"):
        return ""

    sesgo = ic["sesgo"]
    if sesgo == "ALCISTA":
        emoji, cab = "🟢", "ALCISTA"
    elif sesgo == "BAJISTA":
        emoji, cab = "🔴", "BAJISTA"
    else:
        emoji = "⚪"
        cab = ("NEUTRAL (señales en conflicto)" if ic["tipo_neutral"] == "conflicto"
               else "NEUTRAL (consenso débil)")

    lineas = [f"\n🦈 *ÍNDICE DE TIBURONES*",
              f"{emoji} Sesgo: *{cab}*"]

    # Confluencia (solo si hay dirección)
    if sesgo != "NEUTRAL":
        lineas.append(f"📊 Confluencia: {ic['confluencia']}/{ic['huellas_activas']} huellas • "
                      f"Confianza: {ic['confianza']}")
    elif ic["tipo_neutral"] == "conflicto":
        lineas.append(f"⚠️ Huellas peleando — esperar resolución")

    # Detalle de cada huella
    nombres_emoji = {"Sweeps": "🌊", "CME OI": "📈", "Volumen": "📊"}
    for nombre, h in ic["huellas"].items():
        v = h["voto"]
        flecha = "🟢↑" if v > 0 else ("🔴↓" if v < 0 else "⚪–")
        em = nombres_emoji.get(nombre, "•")
        lineas.append(f"{em} {nombre}: {flecha} {h['texto']}")

    # Huellas retiradas y pendientes (transparencia)
    lineas.append("🔄 Roll/term: _pendiente de datos_")

    return "\n".join(lineas)


def validar_cot_estimado_vs_real():
    """
    JUBILADA (sesión 22-jun): reemplazada por el Índice de Tiburones.
    El COT estimado viejo producía un número de contratos imposible de
    replicar fielmente (estimó +915,218 vs real +57,307, error +857,911
    y dirección equivocada). El COT REAL de la CFTC sigue descargándose
    y mostrándose aparte — eso NO se tocó. Esta función se conserva como
    no-op para no romper las llamadas que quedan en el loop.
    """
    return


def evaluar_cot():
    if not cot_cache["disponible"]:
        return {"disponible": False, "score": 0, "sesgo": "N/D"}
    sesgo  = cot_cache["sesgo"]
    fuente = cot_cache.get("fuente", "PROXY")
    score_map = {
        "ALCISTA_FUERTE": 2, "ALCISTA_MODERADO": 1,
        "NEUTRAL": 0, "BAJISTA_MODERADO": -1, "BAJISTA_FUERTE": -2
    }
    return {
        "disponible":    True,
        "score":         score_map.get(sesgo, 0),
        "sesgo":         sesgo,
        "neto":          cot_cache.get("neto_largo", 0),
        "fuente":        fuente,
        "fecha_reporte": cot_cache.get("fecha_reporte"),
        "longs":         cot_cache.get("longs"),
        "shorts":        cot_cache.get("shorts"),
    }

# ================================================================
# === MEJORA B: GEX REAL DESDE OPTION_CHAIN() ====================
# ================================================================

gex_niveles = {
    "gamma_flip":           None,
    "call_wall":            None,
    "put_wall":             None,
    "ultima_actualizacion": None,
    "disponible":           False,
    "es_estimado":          True,
    "fuente":               None,
    # OI y prima aproximada en cada wall (se llenan si hay datos de Tradier)
    "call_wall_oi":         None,
    "call_wall_prima":      None,
    "put_wall_oi":          None,
    "put_wall_prima":       None,
}

# OI y precio por strike SPY del ciclo actual (para mostrar OI en los walls).
# Solo se llena cuando la fuente es Tradier (trae open_interest + precio).
# Clave: strike (float, escala SPY) → {"call_oi", "put_oi", "call_px", "put_px"}
oi_por_strike_cache = {"data": {}, "fuente": None}


def _obtener_gex_tradier(precio_spy):
    """
    Obtiene greeks reales de opciones SPY desde Tradier API.
    Devuelve diccionario {strike: gex_neto} con gamma real del exchange.
    Mucho más preciso que Black-Scholes — greeks calculados por el exchange.
    """
    if not TRADIER_TOKEN:
        return None

    try:
        import urllib.request
        import json
        from datetime import datetime, timedelta

        # Obtener expiraciones disponibles
        url_exp = "https://api.tradier.com/v1/markets/options/expirations?symbol=SPY&includeAllRoots=true"
        req = urllib.request.Request(url_exp, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data_exp = json.loads(resp.read().decode())

        expiraciones = data_exp.get("expirations", {}).get("date", [])
        if not expiraciones:
            print("  [TRADIER] Sin expiraciones disponibles")
            return None

        # Usar las 3 primeras expiraciones
        if isinstance(expiraciones, str):
            expiraciones = [expiraciones]
        expiraciones = expiraciones[:3]

        gex_por_strike = {}
        strikes_procesados = 0

        for exp in expiraciones:
            try:
                url_chain = (f"https://api.tradier.com/v1/markets/options/chains"
                           f"?symbol=SPY&expiration={exp}&greeks=true")
                req = urllib.request.Request(url_chain, headers={
                    "Authorization": f"Bearer {TRADIER_TOKEN}",
                    "Accept": "application/json"
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data_chain = json.loads(resp.read().decode())

                opciones = data_chain.get("options", {}).get("option", [])
                if not opciones:
                    continue

                # Reiniciar el cache de OI por strike para este ciclo
                oi_por_strike_cache["data"]   = {}
                oi_por_strike_cache["fuente"]  = "TRADIER"

                for opcion in opciones:
                    strike    = float(opcion.get("strike", 0))
                    oi        = float(opcion.get("open_interest", 0))
                    tipo      = opcion.get("option_type", "")
                    greeks    = opcion.get("greeks", {})

                    if not greeks or oi <= 0 or strike <= 0:
                        continue

                    gamma = float(greeks.get("gamma", 0) or 0)
                    if gamma <= 0:
                        continue

                    # Precio de la opción: last si existe, sino punto medio bid/ask
                    px_last = float(opcion.get("last", 0) or 0)
                    px_bid  = float(opcion.get("bid", 0) or 0)
                    px_ask  = float(opcion.get("ask", 0) or 0)
                    if px_last > 0:
                        precio_op = px_last
                    elif px_bid > 0 and px_ask > 0:
                        precio_op = (px_bid + px_ask) / 2
                    else:
                        precio_op = px_bid or px_ask or 0.0

                    # Guardar OI + precio por strike (acumula varias expiraciones)
                    reg = oi_por_strike_cache["data"].setdefault(
                        strike, {"call_oi": 0.0, "put_oi": 0.0,
                                 "call_px": 0.0, "put_px": 0.0})

                    gex = gamma * oi * 100 * strike

                    if tipo == "call":
                        gex_por_strike[strike] = gex_por_strike.get(strike, 0) + gex
                        reg["call_oi"] += oi
                        if precio_op > 0: reg["call_px"] = precio_op
                    elif tipo == "put":
                        gex_por_strike[strike] = gex_por_strike.get(strike, 0) - gex
                        reg["put_oi"] += oi
                        if precio_op > 0: reg["put_px"] = precio_op

                    strikes_procesados += 1

                print(f"  [TRADIER] {exp}: {strikes_procesados} strikes con greeks reales")

            except Exception as e:
                print(f"  [TRADIER] Error en {exp}: {e}")
                continue

        if gex_por_strike:
            print(f"  [TRADIER] ✅ {len(gex_por_strike)} strikes únicos procesados")
            return gex_por_strike
        return None

    except Exception as e:
        print(f"  [TRADIER] Error general: {e}")
        return None

def _gamma_black_scholes(S, K, T, sigma, r=0.05):
    """
    Calcula gamma usando Black-Scholes.
    S=precio actual, K=strike, T=tiempo en años,
    sigma=volatilidad implícita, r=tasa libre riesgo
    """
    try:
        import math
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0: return 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        gamma = math.exp(-0.5 * d1**2) / (S * sigma * math.sqrt(2 * math.pi * T))
        return max(0.0, gamma)
    except:
        return 0.0

def obtener_gex():
    """
    Calcula GEX real desde opciones SPY.
    Jerarquía:
    1. Tradier API — greeks reales del exchange (95% confiable)
    2. yfinance option_chain() + Black-Scholes (75% confiable)
    3. Fallback estimado geométrico
    """
    try:
        import math
        from datetime import datetime
        spy        = yf.Ticker("SPY")
        precio_spy = spy.fast_info.last_price
        if not precio_spy:
            return _gex_fallback()

        precio_us500 = precio_spy * 10

        # ── Intentar Tradier primero si token disponible ──────
        if TRADIER_TOKEN:
            gex_por_strike = _obtener_gex_tradier(precio_spy)
            if gex_por_strike:
                return _procesar_gex(gex_por_strike, precio_spy, precio_us500, "TRADIER")

        # ── Fallback: yfinance + Black-Scholes ────────────────
        expiraciones = spy.options[:3] if len(spy.options) >= 3 else spy.options
        if not expiraciones:
            return _gex_fallback()

        gex_por_strike = {}
        strikes_procesados = 0

        for exp in expiraciones:
            try:
                chain = spy.option_chain(exp)
                # Calcular tiempo a expiración en años
                try:
                    fecha_exp = datetime.strptime(exp, "%Y-%m-%d")
                    T = max(1/365, (fecha_exp - datetime.now()).days / 365)
                except:
                    T = 30/365  # default 30 días

                tiene_gamma = "gamma" in chain.calls.columns

                # ── Calls ─────────────────────────────────────
                for _, row in chain.calls.iterrows():
                    strike = float(row["strike"])
                    oi     = float(row["openInterest"]) if not pd.isna(row.get("openInterest", 0)) else 0
                    if oi <= 0: continue

                    # Intentar gamma real primero, sino Black-Scholes
                    if tiene_gamma and not pd.isna(row.get("gamma", float("nan"))):
                        gamma = float(row["gamma"])
                    else:
                        iv = float(row["impliedVolatility"]) if not pd.isna(row.get("impliedVolatility", float("nan"))) else 0
                        gamma = _gamma_black_scholes(precio_spy, strike, T, iv) if iv > 0 else 0

                    if gamma <= 0: continue
                    gex = gamma * oi * 100 * strike
                    gex_por_strike[strike] = gex_por_strike.get(strike, 0) + gex
                    strikes_procesados += 1

                # ── Puts ──────────────────────────────────────
                for _, row in chain.puts.iterrows():
                    strike = float(row["strike"])
                    oi     = float(row["openInterest"]) if not pd.isna(row.get("openInterest", 0)) else 0
                    if oi <= 0: continue

                    if tiene_gamma and not pd.isna(row.get("gamma", float("nan"))):
                        gamma = float(row["gamma"])
                    else:
                        iv = float(row["impliedVolatility"]) if not pd.isna(row.get("impliedVolatility", float("nan"))) else 0
                        gamma = _gamma_black_scholes(precio_spy, strike, T, iv) if iv > 0 else 0

                    if gamma <= 0: continue
                    gex = gamma * oi * 100 * strike
                    gex_por_strike[strike] = gex_por_strike.get(strike, 0) - gex
                    strikes_procesados += 1

                fuente_gamma = "yfinance" if tiene_gamma else "Black-Scholes"
                print(f"  [GEX] {exp}: {strikes_procesados} strikes procesados ({fuente_gamma})")

            except Exception as e:
                print(f"  [GEX] Error en expiración {exp}: {e}")
                continue

        if not gex_por_strike:
            return _gex_fallback()

        return _procesar_gex(gex_por_strike, precio_spy, precio_us500, "OPTION_CHAIN")

    except Exception as e:
        print(f"  [GEX] option_chain error: {e}")
        return _gex_fallback()


def _procesar_gex(gex_por_strike, precio_spy, precio_us500, fuente):
    """
    Procesa el diccionario {strike: gex_neto} y extrae Flip, Call Wall, Put Wall.
    Función compartida entre Tradier y yfinance para evitar duplicar código.
    """
    try:
        rango_min    = precio_spy * 0.90
        rango_max    = precio_spy * 1.10
        gex_filtrado = {k: v for k, v in gex_por_strike.items()
                        if rango_min <= k <= rango_max}

        if not gex_filtrado:
            return _gex_fallback()

        strikes_ordenados = sorted(gex_filtrado.keys())

        # ── Gamma Flip — cruce más cercano al precio actual ───
        cruces        = []
        gex_acumulado = 0
        gex_acum_ant  = 0
        for strike in strikes_ordenados:
            gex_acum_ant   = gex_acumulado
            gex_acumulado += gex_filtrado[strike]
            if gex_acum_ant != 0 and gex_acum_ant * gex_acumulado < 0:
                cruces.append(strike)

        if cruces:
            gamma_flip = min(cruces, key=lambda k: abs(k - precio_spy))
        else:
            gamma_flip = min(gex_filtrado.keys(),
                            key=lambda k: abs(gex_filtrado[k]))

        # ── Call Wall — mayor GEX positivo SOBRE el precio ───
        strikes_arriba = {k: v for k, v in gex_filtrado.items()
                          if k > precio_spy and v > 0}
        call_wall = max(strikes_arriba, key=strikes_arriba.get) if strikes_arriba else None

        # ── Put Wall — mayor GEX negativo BAJO el precio ─────
        strikes_abajo = {k: v for k, v in gex_filtrado.items()
                         if k < precio_spy and v < 0}
        put_wall = min(strikes_abajo, key=strikes_abajo.get) if strikes_abajo else None

        # ── Validar coherencia Put Wall < Flip < Call Wall ───
        if gamma_flip and call_wall and put_wall:
            if not (put_wall <= gamma_flip <= call_wall):
                gamma_flip = min(gex_filtrado.keys(),
                                key=lambda k: abs(k - precio_spy))
                print(f"  [GEX] ⚠️ Flip reajustado: {gamma_flip:.1f}")

        # ── Convertir a US500 (×10) ───────────────────────────
        gamma_flip_us500 = round(gamma_flip * 10, 0) if gamma_flip else None
        call_wall_us500  = round(call_wall  * 10, 0) if call_wall  else None
        put_wall_us500   = round(put_wall   * 10, 0) if put_wall   else None

        if not gamma_flip_us500:
            return _gex_fallback()

        # ── OI y prima aproximada en cada wall (si hay datos Tradier) ──
        # call_wall y put_wall están en escala SPY = claves del cache.
        cw_oi = cw_prima = pw_oi = pw_prima = None
        if oi_por_strike_cache.get("fuente") == "TRADIER" and oi_por_strike_cache["data"]:
            if call_wall is not None:
                reg_cw = oi_por_strike_cache["data"].get(call_wall)
                if reg_cw:
                    cw_oi    = int(reg_cw["call_oi"])
                    # Prima aproximada = precio opción × OI × 100 (multiplicador)
                    cw_prima = reg_cw["call_px"] * reg_cw["call_oi"] * 100
            if put_wall is not None:
                reg_pw = oi_por_strike_cache["data"].get(put_wall)
                if reg_pw:
                    pw_oi    = int(reg_pw["put_oi"])
                    pw_prima = reg_pw["put_px"] * reg_pw["put_oi"] * 100

        gex_niveles.update({
            "gamma_flip":           gamma_flip_us500,
            "call_wall":            call_wall_us500,
            "put_wall":             put_wall_us500,
            "ultima_actualizacion": hora_ny(),
            "disponible":           True,
            "es_estimado":          False,
            "fuente":               fuente,
            "strikes_totales":      len(gex_filtrado),
            "call_wall_oi":         cw_oi,
            "call_wall_prima":      cw_prima,
            "put_wall_oi":          pw_oi,
            "put_wall_prima":       pw_prima,
        })
        oi_txt = ""
        if cw_oi is not None or pw_oi is not None:
            oi_txt = f" | OI Call:{cw_oi or 0:,} Put:{pw_oi or 0:,}"
        print(f"  [GEX] ✅ {fuente} — Flip:{gamma_flip_us500} | Call:{call_wall_us500} | Put:{put_wall_us500} | Strikes:{len(gex_filtrado)}{oi_txt}")
        return True

    except Exception as e:
        print(f"  [GEX] _procesar_gex error: {e}")
        return _gex_fallback()

def texto_oi_walls():
    """
    Devuelve texto con el OI (contratos) y prima aproximada en cada wall.
    Solo si hay datos de Tradier. Marca la prima como aproximación.
    Formato similar al sweep: contratos + monto en dólares.
    """
    if not gex_niveles.get("disponible"):
        return ""
    cw_oi    = gex_niveles.get("call_wall_oi")
    cw_prima = gex_niveles.get("call_wall_prima")
    pw_oi    = gex_niveles.get("put_wall_oi")
    pw_prima = gex_niveles.get("put_wall_prima")
    if cw_oi is None and pw_oi is None:
        return ""  # sin datos de OI (fuente no-Tradier)
    lineas = ["\n📊 *OI en los muros* (prima ≈ aprox.):"]
    if cw_oi is not None:
        cw = gex_niveles.get("call_wall", "N/D")
        if cw_prima and cw_prima > 0:
            lineas.append(f"🟢 Call Wall `{cw}`: `{cw_oi:,}` contratos — ≈`${cw_prima:,.0f}`")
        else:
            lineas.append(f"🟢 Call Wall `{cw}`: `{cw_oi:,}` contratos")
    if pw_oi is not None:
        pw = gex_niveles.get("put_wall", "N/D")
        if pw_prima and pw_prima > 0:
            lineas.append(f"🔴 Put Wall `{pw}`: `{pw_oi:,}` contratos — ≈`${pw_prima:,.0f}`")
        else:
            lineas.append(f"🔴 Put Wall `{pw}`: `{pw_oi:,}` contratos")
    # Balance neto de OI (cuál muro tiene más contratos)
    if cw_oi is not None and pw_oi is not None:
        if cw_oi > pw_oi:
            lineas.append(f"⚖️ Neto OI: Call domina (+{cw_oi - pw_oi:,} contratos)")
        elif pw_oi > cw_oi:
            lineas.append(f"⚖️ Neto OI: Put domina (+{pw_oi - cw_oi:,} contratos)")
        else:
            lineas.append("⚖️ Neto OI: equilibrado")
    return "\n".join(lineas)

def _gex_fallback():
    """Fallback: intenta FlashAlpha, luego estimado geométrico."""
    try:
        url = "https://flashalpha.io/api/v1/gex?ticker=SPY"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        gamma_flip = data.get("gamma_flip") or data.get("gammaFlip") or data.get("flip")
        call_wall  = data.get("call_wall")  or data.get("callWall")  or data.get("call_resistance")
        put_wall   = data.get("put_wall")   or data.get("putWall")   or data.get("put_support")
        factor = 10.0
        if gamma_flip: gamma_flip = round(float(gamma_flip) * factor, 2)
        if call_wall:  call_wall  = round(float(call_wall)  * factor, 2)
        if put_wall:   put_wall   = round(float(put_wall)   * factor, 2)
        if gamma_flip or call_wall or put_wall:
            gex_niveles.update({
                "gamma_flip": gamma_flip, "call_wall": call_wall,
                "put_wall": put_wall, "ultima_actualizacion": hora_ny(),
                "disponible": True, "es_estimado": True, "fuente": "FLASHALPHA"
            })
            print(f"  [GEX] ⚡ FlashAlpha — Flip:{gamma_flip} | Call:{call_wall} | Put:{put_wall}")
            return True
    except:
        pass

    try:
        spy    = yf.Ticker("SPY")
        precio = spy.fast_info.last_price
        if not precio: return False
        precio_us500 = precio * 10
        redondeo     = 50
        gamma_flip   = round(precio_us500 / redondeo) * redondeo
        call_wall    = (round(precio_us500 / redondeo) + 2) * redondeo
        put_wall     = (round(precio_us500 / redondeo) - 2) * redondeo
        gex_niveles.update({
            "gamma_flip": gamma_flip, "call_wall": call_wall,
            "put_wall": put_wall, "ultima_actualizacion": hora_ny(),
            "disponible": True, "es_estimado": True, "fuente": "ESTIMADO"
        })
        print(f"  [GEX] 📊 Estimado — Flip:{gamma_flip} | Call:{call_wall} | Put:{put_wall}")
        return True
    except Exception as e:
        print(f"  [GEX] Fallback error: {e}")
        return False

def evaluar_gex(precio_actual):
    if not gex_niveles["disponible"]:
        return {"disponible": False, "score": 0, "señal": "NO DISPONIBLE",
                "distancia_flip": None, "distancia_call": None, "distancia_put": None}
    gamma_flip  = gex_niveles["gamma_flip"]
    call_wall   = gex_niveles["call_wall"]
    put_wall    = gex_niveles["put_wall"]
    es_estimado = gex_niveles.get("es_estimado", False)
    fuente      = gex_niveles.get("fuente", "ESTIMADO")
    señales = []
    score   = 0
    dist_flip = precio_actual - gamma_flip if gamma_flip else None
    dist_call = call_wall - precio_actual  if call_wall  else None
    dist_put  = precio_actual - put_wall   if put_wall   else None
    if gamma_flip:
        if precio_actual > gamma_flip:
            score += 1; señales.append(f"Sobre GammaFlip({gamma_flip})")
        else:
            score -= 2; señales.append(f"Bajo GammaFlip({gamma_flip}) ⚠️")
    if call_wall and dist_call is not None:
        pct_call = dist_call / precio_actual * 100
        if pct_call < 0.3:   score -= 1; señales.append(f"Cerca CallWall({call_wall})")
        elif pct_call > 1.0: score += 1; señales.append(f"Lejos CallWall({call_wall})")
    if put_wall and dist_put is not None:
        pct_put = dist_put / precio_actual * 100
        if pct_put < 0.3: score += 1; señales.append(f"Cerca PutWall({put_wall})")
    señal_texto = " | ".join(señales) if señales else "Zona neutral GEX"
    if es_estimado: señal_texto += f" ({fuente})"
    return {
        "disponible": True, "score": max(-3, min(3, score)), "señal": señal_texto,
        "gamma_flip": gamma_flip, "call_wall": call_wall, "put_wall": put_wall,
        "distancia_flip": round(dist_flip, 2) if dist_flip is not None else None,
        "distancia_call": round(dist_call, 2) if dist_call is not None else None,
        "distancia_put":  round(dist_put,  2) if dist_put  is not None else None,
        "es_estimado": es_estimado, "fuente": fuente,
    }

# ================================================================
# === GEX 0DTE — FLUJO DEALER INTRADÍA ===========================
# ================================================================
# Más del 50% del volumen de opciones es 0DTE. El hedging de los
# dealers sobre ESOS contratos es lo que empuja el precio HOY.
# Gamma positiva sobre el flip 0DTE = dealers amortiguan (rango).
# Gamma negativa bajo el flip 0DTE = dealers amplifican (tendencia).

gex_0dte_cache = {
    "disponible":           False,
    "neto":                 0.0,
    "neto_anterior":        0.0,
    "flip_0dte":            None,   # en escala US500
    "flip_confiable":       True,
    "expiracion":           None,
    "ultima_actualizacion": None,
    "ultimo_giro_signo":    0,      # signo del último giro notificado (+1/-1/0)
    "ultimo_giro_hora":     None,   # hora del último giro notificado (cooldown)
    "signo_pendiente":      0,      # signo candidato a giro (esperando persistencia)
    "ciclos_persistencia":  0,      # ciclos consecutivos que el signo nuevo se mantiene
    "dia":                  None,
}

def obtener_gex_0dte():
    """Calcula GEX solo de la expiración de HOY usando OI + volumen."""
    if not TRADIER_TOKEN:
        return
    try:
        ahora   = hora_ny()
        hoy_str = ahora.strftime("%Y-%m-%d")

        # Reset diario
        if gex_0dte_cache["dia"] != ahora.date():
            gex_0dte_cache.update({"dia": ahora.date(), "neto_anterior": 0.0,
                                   "ultimo_giro_signo": 0, "ultimo_giro_hora": None})

        url_exp = "https://api.tradier.com/v1/markets/options/expirations?symbol=SPY&includeAllRoots=true"
        req = urllib.request.Request(url_exp, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data_exp = json.loads(resp.read().decode())
        expiraciones = data_exp.get("expirations", {}).get("date", [])
        if isinstance(expiraciones, str): expiraciones = [expiraciones]
        if not expiraciones or expiraciones[0] != hoy_str:
            gex_0dte_cache["disponible"] = False
            return

        url_chain = (f"https://api.tradier.com/v1/markets/options/chains"
                     f"?symbol=SPY&expiration={hoy_str}&greeks=true")
        req = urllib.request.Request(url_chain, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data_chain = json.loads(resp.read().decode())
        opciones = data_chain.get("options", {}).get("option", [])
        if not opciones:
            gex_0dte_cache["disponible"] = False
            return

        spy_t = yf.Ticker("SPY")
        precio_spy = spy_t.fast_info.last_price or 0
        if not precio_spy:
            return

        gex_por_strike = {}
        for op in opciones:
            strike = float(op.get("strike", 0))
            if strike <= 0 or abs(strike - precio_spy) / precio_spy > 0.03:
                continue  # solo ±3% del precio — donde vive el 0DTE
            oi      = float(op.get("open_interest", 0) or 0)
            vol     = float(op.get("volume", 0) or 0)
            greeks  = op.get("greeks", {}) or {}
            gamma   = float(greeks.get("gamma", 0) or 0)
            if gamma <= 0:
                continue
            # En 0DTE el volumen del día pesa más que el OI matinal
            exposicion = oi + vol
            if exposicion <= 0:
                continue
            gex = gamma * exposicion * 100 * strike
            tipo = op.get("option_type", "")
            if tipo == "call":
                gex_por_strike[strike] = gex_por_strike.get(strike, 0) + gex
            elif tipo == "put":
                gex_por_strike[strike] = gex_por_strike.get(strike, 0) - gex

        if not gex_por_strike:
            gex_0dte_cache["disponible"] = False
            return

        neto = sum(gex_por_strike.values())

        # Flip 0DTE — cruce del acumulado más cercano al precio
        strikes_ord  = sorted(gex_por_strike.keys())
        cruces, acum = [], 0.0
        for st in strikes_ord:
            prev  = acum
            acum += gex_por_strike[st]
            if prev != 0 and prev * acum < 0:
                cruces.append(st)
        if cruces:
            flip = min(cruces, key=lambda k: abs(k - precio_spy))
        else:
            flip = min(gex_por_strike, key=lambda k: abs(gex_por_strike[k]))

        # ── Validar fiabilidad del flip ──────────────────────
        # Un flip muy lejos del precio = ruido de Tradier o pocos strikes
        # válidos en ese ciclo. >40 pts US500 (=4 pts SPY) no es fiable.
        MAX_DIST_FLIP_SPY = 4.0  # 4 pts SPY = 40 pts US500
        dist_flip_spy = abs(flip - precio_spy)
        flip_confiable = dist_flip_spy <= MAX_DIST_FLIP_SPY

        neto_ant = gex_0dte_cache.get("neto", 0.0)
        gex_0dte_cache.update({
            "disponible":           True,
            "neto":                 neto,
            "neto_anterior":        neto_ant,
            "flip_0dte":            round(flip * 10, 0),
            "flip_confiable":       flip_confiable,
            "expiracion":           hoy_str,
            "ultima_actualizacion": ahora,
        })
        regimen = "AMORTIGUA (rango)" if neto > 0 else "AMPLIFICA (tendencia)"
        conf_txt = "" if flip_confiable else " ⚠️ FLIP LEJANO (no fiable)"
        print(f"  [GEX_0DTE] ✅ Neto:{neto:+,.0f} | Flip0DTE:{flip*10:.0f} | "
              f"Dealers: {regimen}{conf_txt}")

        # ── Alerta de giro de régimen (CON PERSISTENCIA) ─────
        # El RÉGIMEN lo define el NETO (suma de gamma). PERO un solo
        # cruce de signo puede ser un PICO DE RUIDO (caso lunes 22-jun:
        # giro falso a negativa 9:35, precio quedó en rango 4+ horas).
        # SOLUCIÓN: exigir PERSISTENCIA — el neto debe mantener el signo
        # nuevo por 2 ciclos consecutivos (3 lecturas) antes de declarar
        # el giro. Un pico aislado de 1 ciclo ya no dispara la alerta.
        CICLOS_REQUERIDOS = 2   # ciclos consecutivos manteniendo el signo nuevo
        COOLDOWN_GIRO_MIN = 20
        signo_actual = 1 if neto > 0 else (-1 if neto < 0 else 0)
        ultimo_signo = gex_0dte_cache.get("ultimo_giro_signo", 0)

        if signo_actual != 0 and signo_actual != ultimo_signo:
            # El signo actual difiere del último régimen notificado:
            # candidato a giro. ¿Se está mantieniendo?
            if signo_actual == gex_0dte_cache.get("signo_pendiente", 0):
                # Mismo candidato que el ciclo anterior → suma persistencia
                gex_0dte_cache["ciclos_persistencia"] += 1
            else:
                # Candidato nuevo → reinicia el conteo
                gex_0dte_cache["signo_pendiente"]     = signo_actual
                gex_0dte_cache["ciclos_persistencia"] = 1

            ciclos = gex_0dte_cache["ciclos_persistencia"]
            ultima_hora = gex_0dte_cache.get("ultimo_giro_hora")
            mins_desde  = ((ahora - ultima_hora).total_seconds() / 60
                           if ultima_hora else 9999)

            if ciclos >= CICLOS_REQUERIDOS and mins_desde >= COOLDOWN_GIRO_MIN:
                # Persistencia confirmada → declarar el giro
                gex_0dte_cache["ultimo_giro_signo"]   = signo_actual
                gex_0dte_cache["ultimo_giro_hora"]    = ahora
                gex_0dte_cache["signo_pendiente"]     = 0
                gex_0dte_cache["ciclos_persistencia"] = 0
                nuevo_reg = "🟢 GAMMA POSITIVA — dealers frenarán los movimientos" \
                            if neto > 0 else "🔴 GAMMA NEGATIVA — dealers amplificarán los movimientos"
                if flip_confiable:
                    flip_linea = f"\nFlip 0DTE: `{gex_0dte_cache['flip_0dte']}`"
                else:
                    flip_linea = "\n_(Flip 0DTE no fiable este ciclo — omitido)_"
                try:
                    bot.send_message(TELEGRAM_CHAT_ID,
                        f"⚡ *GIRO DE RÉGIMEN GEX 0DTE*\n"
                        f"El flujo dealer de HOY cambió de signo "
                        f"_(confirmado {ciclos+1} lecturas)_.\n{nuevo_reg}{flip_linea}",
                        parse_mode="Markdown")
                    print(f"  [GEX_0DTE] ⚡ Giro CONFIRMADO (persistencia {ciclos}) → "
                          f"{'POSITIVA' if neto>0 else 'NEGATIVA'}"
                          f"{' (flip omitido)' if not flip_confiable else ''}")
                except Exception as e:
                    print(f"  [GEX_0DTE] Error alerta: {e}")
            elif ciclos < CICLOS_REQUERIDOS:
                print(f"  [GEX_0DTE] 🕐 Posible giro a {'POSITIVA' if signo_actual>0 else 'NEGATIVA'} "
                      f"— esperando persistencia ({ciclos}/{CICLOS_REQUERIDOS} ciclos)")
            elif mins_desde < COOLDOWN_GIRO_MIN:
                print(f"  [GEX_0DTE] ⏭ Giro persistente pero en cooldown ({mins_desde:.0f}/{COOLDOWN_GIRO_MIN} min)")
        else:
            # El signo volvió al régimen actual → el candidato se cae
            # (esto es lo que filtra los picos: si el pico dura 1 ciclo
            #  y vuelve, el conteo se descarta y no hubo falsa alarma).
            if gex_0dte_cache.get("signo_pendiente", 0) != 0:
                print(f"  [GEX_0DTE] 🔁 Candidato a giro descartado (volvió al régimen actual — era ruido)")
            gex_0dte_cache["signo_pendiente"]     = 0
            gex_0dte_cache["ciclos_persistencia"] = 0

    except Exception as e:
        print(f"  [GEX_0DTE] Error: {e}")

def evaluar_gex_0dte(precio_actual):
    if not gex_0dte_cache["disponible"]:
        return {"disponible": False, "score": 0, "señal": "N/D"}
    flip = gex_0dte_cache["flip_0dte"]
    neto = gex_0dte_cache["neto"]
    if not flip:
        return {"disponible": False, "score": 0, "señal": "N/D"}
    if precio_actual > flip:
        score = 1
        señal = f"Sobre Flip0DTE({flip:.0f}) — dealers soportan"
    else:
        score = -1
        señal = f"Bajo Flip0DTE({flip:.0f}) — dealers presionan"
    if neto < 0:
        señal += " | GAMMA NEG: movimientos amplificados"
    return {"disponible": True, "score": score, "señal": señal,
            "flip_0dte": flip, "neto": neto}

# ================================================================
# === MEJORA C: DARK POOL GRANULAR ================================
# ================================================================

dark_pool_cache = {
    "ratio":                None,
    "ultima_actualizacion": None,
    "disponible":           False,
    "bloques":              [],
    "tendencia":            None,
    "fuente":               None,
}

# Estado para alertas de proximidad GEX
gex_proximidad_cache = {
    "ultima_alerta_flip":      None,
    "ultima_alerta_call_wall": None,
    "ultima_alerta_put_wall":  None,
    "ultima_alerta_flip_0dte": None,
}

# Estado para detector de squeeze de volatilidad
squeeze_cache = {
    "activo":          False,
    "inicio":          None,
    "rango_promedio":  None,
    "alerta_enviada":  False,
    "dia":             None,
}

# Estado para Options Sweep Detection via Tradier
sweep_cache = {
    "ultimo_sweep":   None,
    "tipo":           None,   # "CALL" o "PUT"
    "contratos":      0,
    "strikes":        0,
    "prima_total":    0.0,
    "prima_calls_hoy": 0.0,
    "prima_puts_hoy":  0.0,
    "alerta_enviada": False,
    "dia":            None,
    "balance_ultima_alerta": 0.0,  # balance neto cuando se envió la última alerta
    # Historial de balances netos (para medir aceleración/desaceleración).
    # Cada entrada es el balance neto (calls - puts) de un sweep sucesivo.
    # El usuario observó: si el balance se desacelera entre sweeps
    # consecutivos (ej. +220M → +190M), el precio ya no sigue el
    # movimiento aunque el signo siga igual = AGOTAMIENTO.
    "historial_balance": [],
}

# Estado para Put/Call ratio semanal via Tradier
pc_semanal_cache = {
    "disponible":           False,
    "ratio_semanal":        1.0,
    "ultima_actualizacion": None,
    "sesgo":                "NEUTRAL",
}


def _post_dashboard(payload):
    """
    Trabajo real del envío al dashboard — corre en un thread daemon para
    no bloquear el loop principal. Timeout corto y errores silenciosos:
    si el Apps Script está caído, el bot sigue operando igual.
    """
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            DASHBOARD_URL, data=data,
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()  # drena la respuesta; Apps Script devuelve JSON simple
        print(f"  [DASHBOARD] 📤 Lectura enviada — balance ${payload.get('balance',0):,.0f}")
    except Exception as e:
        # Silencioso a propósito: el dashboard es secundario, nunca debe
        # tumbar ni frenar el envío de señales a Telegram.
        print(f"  [DASHBOARD] Envío falló (ignorado): {e}")


def enviar_sweep_dashboard(sweep, balance_actual, ahora_ny, precio_us500=None):
    """
    Manda UNA lectura de sweeps al Apps Script (Google Sheets) para la app
    web de seguimiento. Se llama en CADA ciclo de 5 min con sweep detectado
    (no solo en las alertas), para que la web tenga la trayectoria completa
    del balance. El POST se hace en un thread daemon: no bloquea el loop.
    Si DASHBOARD_URL no está configurada, no hace nada.

    precio_us500: precio del US500 (^GSPC) en el momento de la lectura. La
    web lo usa para ver la RELACIÓN sweep→precio: si el balance crece y el
    precio lo acompaña, el hedge del dealer apareció (el tren sigue); si el
    balance crece pero el precio no se mueve, el sweep se absorbió (el tren
    se fue). Es el "espejo" del lado de acciones, inferido del propio precio.
    """
    if not DASHBOARD_URL:
        return
    try:
        # Hora de Honduras (UTC-6) junto a la de NY, como pidió el usuario
        hora_hn = ahora_ny.astimezone(pytz.timezone("America/Tegucigalpa"))
        payload = {
            "fecha":       ahora_ny.strftime("%Y-%m-%d"),
            "hora_et":     ahora_ny.strftime("%H:%M:%S"),
            "hora_hn":     hora_hn.strftime("%H:%M:%S"),
            "ts":          time.time(),
            "calls_usd":   round(float(sweep.get("prima_calls", 0)), 2),
            "puts_usd":    round(float(sweep.get("prima_puts", 0)), 2),
            "balance":     round(float(balance_actual), 2),
            "precio_us500": round(float(precio_us500), 2) if precio_us500 is not None else "",
            "tipo":        sweep.get("tipo", "NEUTRAL"),
            "contratos_calls": int(sweep.get("contratos_calls", 0)),
            "contratos_puts":  int(sweep.get("contratos_puts", 0)),
            "strikes_calls":   int(sweep.get("strikes_calls", 0)),
            "strikes_puts":    int(sweep.get("strikes_puts", 0)),
            "expiracion":  sweep.get("expiracion", ""),
        }
        # ── Estructura: GEX + crédito ─────────────────────────
        def _v(x):
            return round(float(x), 2) if x is not None else ""

        if gex_niveles.get("disponible"):
            payload.update({
                "flip":      _v(gex_niveles.get("gamma_flip")),
                "call_wall": _v(gex_niveles.get("call_wall")),
                "put_wall":  _v(gex_niveles.get("put_wall")),
                "call_oi":   int(gex_niveles.get("call_wall_oi") or 0) or "",
                "put_oi":    int(gex_niveles.get("put_wall_oi")  or 0) or "",
            })
        else:
            payload.update({"flip": "", "call_wall": "", "put_wall": "",
                            "call_oi": "", "put_oi": ""})

        if gex_0dte_cache.get("disponible"):
            payload.update({
                "gex0_neto": _v(gex_0dte_cache.get("neto")),
                "flip0":     _v(gex_0dte_cache.get("flip_0dte"))
                             if gex_0dte_cache.get("flip_confiable") else "",
            })
        else:
            payload.update({"gex0_neto": "", "flip0": ""})

        if credito_cache.get("disponible"):
            payload.update({
                "hyg_spy3":  _v(credito_cache.get("spy_3d")),
                "hyg_hyg3":  _v(credito_cache.get("hyg_3d")),
                "hyg_score": int(credito_cache.get("score", 0)),
                "hyg_senal": credito_cache.get("señal", ""),
            })
        else:
            payload.update({"hyg_spy3": "", "hyg_hyg3": "",
                            "hyg_score": "", "hyg_senal": ""})
        # Disparar en thread daemon — no esperamos la respuesta
        threading.Thread(target=_post_dashboard, args=(payload,), daemon=True).start()
    except Exception as e:
        print(f"  [DASHBOARD] Error armando payload: {e}")

def precio_us500_tradier():
    """
    Precio del US500 desde Tradier (SPY × 10).

    Tradier es fuente de pago con datos reales y UN solo ticker, así que
    no sufre el problema de yfinance (donde una fila se descartaba si
    cualquiera de los 7 tickers venía atrasado — causa del precio
    congelado 75 min el 6-ago).

    Devuelve el precio en escala US500, o None si falla. El que llama
    debe tener fallback a yfinance: nunca quedarse sin precio.
    """
    if not TRADIER_TOKEN:
        return None
    try:
        url = "https://api.tradier.com/v1/markets/quotes?symbols=SPY"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        q = data.get("quotes", {}).get("quote", {})
        if isinstance(q, list):
            q = q[0] if q else {}
        px = q.get("last") or q.get("close")
        if not px:
            return None
        px = float(px)
        if px <= 0:
            return None
        return round(px * 10, 2)
    except Exception as e:
        print(f"  [PRECIO_TRADIER] Error: {e}")
        return None
# ════════════════════════════════════════════════════════════════
# WEEKLY DIRECCIONAL + SERVIDOR HTTP
#
# QUÉ HACE
#   1. Calcula el weekly direccional (max pain, walls, ratio P:C) una
#      vez al día, con la misma lógica del weekly_test.py que ya usás.
#   2. Guarda la trayectoria diaria en GitHub — así sobrevive a los
#      redeploys de Railway (igual que el journal y el estado COT).
#   3. Levanta un servidor HTTP mínimo que sirve esos datos como JSON
#      al dashboard, sin pasar por Google Sheets.
#
# DÓNDE PEGARLO
#   Justo ANTES de la línea:   def _regimen_gamma(vix_nivel=None):
#
# QUÉ MÁS HAY QUE HACER (ver instrucciones aparte):
#   - Agregar 2 líneas al arranque para levantar el servidor
#   - Agregar 1 línea en el loop para calcular el weekly cada día
# ════════════════════════════════════════════════════════════════

from http.server import BaseHTTPRequestHandler, HTTPServer

PUERTO_HTTP  = int(os.environ.get("PORT", 8080))
WEEKLY_URL   = "https://api.github.com/repos/amalec17avila-sudo/us500-bot/contents/data/weekly.json"

weekly_cache = {
    "disponible":    False,
    "expiracion":    None,    # viernes que vence
    "us500":         None,
    "max_pain":      None,
    "call_wall":     None,
    "put_wall":      None,
    "call_oi":       None,
    "put_oi":        None,
    "ratio_pc":      None,
    "top_calls":     [],      # [[strike, oi], ...]
    "top_puts":      [],
    "trayectoria":   {},      # {"2026-08-10": {...}, ...} por expiración
    "dia_calculado": None,
    "sha":           None,    # sha del archivo en GitHub
}


def _viernes_de_la_semana():
    """Viernes de esta semana en hora ET. Sáb/dom → viernes siguiente."""
    ahora = hora_ny()
    wd = ahora.weekday()          # lun=0 ... dom=6
    delta = (4 - wd) if wd <= 4 else (4 + (7 - wd))
    return (ahora + timedelta(days=delta)).strftime("%Y-%m-%d")


def _tradier_get(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TRADIER_TOKEN}",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def cargar_weekly_github():
    """Restaura la trayectoria semanal al arrancar el bot."""
    try:
        import base64
        req = urllib.request.Request(WEEKLY_URL, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            api = json.loads(resp.read().decode())
        weekly_cache["sha"] = api.get("sha")
        datos = json.loads(base64.b64decode(api.get("content", "")).decode("utf-8"))
        weekly_cache["trayectoria"] = datos.get("trayectoria", {})
        print(f"  [WEEKLY] ✅ Trayectoria restaurada — "
              f"{len(weekly_cache['trayectoria'])} expiraciones")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("  [WEEKLY] 📊 Sin trayectoria previa — se creará al calcular")
        else:
            print(f"  [WEEKLY] Error cargando: HTTP {e.code}")
    except Exception as e:
        print(f"  [WEEKLY] Error cargando: {e}")


def guardar_weekly_github():
    """Persiste la trayectoria en GitHub — sobrevive a los deploys."""
    if not GH_TOKEN:
        return
    try:
        import base64
        cuerpo = {"trayectoria": weekly_cache["trayectoria"],
                  "guardado": hora_ny().strftime("%Y-%m-%d %H:%M ET")}
        b64 = base64.b64encode(
            json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")).decode("ascii")
        body = {"message": f"Weekly {hora_ny().strftime('%Y-%m-%d')}", "content": b64}
        if weekly_cache["sha"]:
            body["sha"] = weekly_cache["sha"]
        req = urllib.request.Request(
            WEEKLY_URL, data=json.dumps(body).encode("utf-8"),
            headers={**_gh_headers(), "Content-Type": "application/json"},
            method="PUT")
        with urllib.request.urlopen(req, timeout=15) as resp:
            api = json.loads(resp.read().decode())
        weekly_cache["sha"] = api.get("content", {}).get("sha")
        print("  [WEEKLY] 💾 Trayectoria guardada en GitHub")
    except Exception as e:
        print(f"  [WEEKLY] Error guardando: {e}")


# ════════════════════════════════════════════════════════════════
# WEEKLY DIRECCIONAL v2 — DOS IMANES
#
# QUÉ CAMBIA respecto a la v1
#   SPY vence casi todos los días, no solo los viernes. Eso significa
#   que hay DOS imanes distintos y conviene no mezclarlos:
#
#   1. IMÁN DEL DÍA (próximo vencimiento, rango ±3%)
#      Es el que pinnea mañana. El precio no recorre 10% en un día,
#      así que los strikes lejanos son ruido para este cálculo.
#
#   2. IMÁN ESTRUCTURAL (viernes / mensual, rango ±10%)
#      Es el sesgo de fondo. Acá SÍ hay que mirar ancho: en un OPEX
#      mensual la protección lejana es enorme y define el ratio P:C
#      real (comprobado 16-ago: con ±5% daba 0.83, con la cadena
#      completa el ratio real era 3.02).
#
# REEMPLAZA
#   La función calcular_weekly() completa de la v1, y el bloque
#   "/weekly" del servidor HTTP.
# ════════════════════════════════════════════════════════════════

def _expiraciones_disponibles():
    """Lista [(fecha, dias_hasta)] de vencimientos SPY futuros."""
    d = _tradier_get("https://api.tradier.com/v1/markets/options/"
                     "expirations?symbol=SPY&includeAllRoots=true")
    exp = d.get("expirations", {}).get("date", [])
    if isinstance(exp, str):
        exp = [exp]
    hoy = hora_ny().date()
    out = []
    for e in exp:
        try:
            f = datetime.strptime(e, "%Y-%m-%d").date()
        except ValueError:
            continue
        dias = (f - hoy).days
        if dias >= 0:
            out.append((e, dias))
    return sorted(out, key=lambda x: x[1])


def _analizar_expiracion(exp, spot, rango_pct):
    """
    Max pain, walls y ratio P:C de UNA expiración.
    rango_pct define qué tan lejos del precio se miran los strikes:
    estrecho para el imán del día, ancho para el estructural.
    Devuelve dict en escala US500, o None si no hay datos.
    """
    try:
        d = _tradier_get(f"https://api.tradier.com/v1/markets/options/chains"
                         f"?symbol=SPY&expiration={exp}&greeks=false")
        ops = d.get("options", {}).get("option", []) or []
        if not ops:
            return None

        call_oi, put_oi = {}, {}
        for op in ops:
            k = float(op.get("strike", 0))
            oi = float(op.get("open_interest", 0) or 0)
            if k <= 0 or oi <= 0 or abs(k - spot) / spot > rango_pct:
                continue
            t = op.get("option_type", "")
            if t == "call":  call_oi[k] = call_oi.get(k, 0) + oi
            elif t == "put": put_oi[k]  = put_oi.get(k, 0) + oi

        if not call_oi and not put_oi:
            return None

        strikes = sorted(set(call_oi) | set(put_oi))

        def payout(S):
            pc = sum(oi * max(0.0, S - k) for k, oi in call_oi.items())
            pp = sum(oi * max(0.0, k - S) for k, oi in put_oi.items())
            return pc + pp
        max_pain = min(strikes, key=payout)

        cw = max(call_oi, key=call_oi.get) if call_oi else None
        pw = max(put_oi,  key=put_oi.get)  if put_oi  else None
        tot_c = sum(call_oi.values())
        tot_p = sum(put_oi.values())

        x10 = lambda v: round(v * 10) if v else None
        us500 = round(spot * 10, 2)
        mp = x10(max_pain)

        return {
            "expiracion": exp,
            "max_pain":   mp,
            "call_wall":  x10(cw),
            "put_wall":   x10(pw),
            "call_oi":    int(call_oi.get(cw, 0)) if cw else 0,
            "put_oi":     int(put_oi.get(pw, 0))  if pw else 0,
            "oi_calls_total": int(tot_c),
            "oi_puts_total":  int(tot_p),
            "ratio_pc":   round(tot_p / tot_c, 2) if tot_c else 0,
            "brecha":     round(mp - us500) if mp else None,
            "rango_pct":  rango_pct,
            "top_calls":  [[x10(k), int(v)] for k, v in
                           sorted(call_oi.items(), key=lambda kv: -kv[1])[:3]],
            "top_puts":   [[x10(k), int(v)] for k, v in
                           sorted(put_oi.items(), key=lambda kv: -kv[1])[:3]],
        }
    except Exception as e:
        print(f"  [WEEKLY] Error analizando {exp}: {e}")
        return None


def calcular_weekly():
    """
    Calcula los DOS imanes: el del próximo vencimiento (pinning de
    mañana) y el estructural del viernes/mensual (sesgo de fondo).
    El OI se actualiza una vez al día, así que basta una corrida diaria.
    """
    if not TRADIER_TOKEN:
        return
    ahora = hora_ny()
    if weekly_cache["dia_calculado"] == ahora.date():
        return

    try:
        # Precio SPY
        q = _tradier_get("https://api.tradier.com/v1/markets/quotes?symbols=SPY")
        quote = q.get("quotes", {}).get("quote", {})
        if isinstance(quote, list):
            quote = quote[0] if quote else {}
        spot = float(quote.get("last") or quote.get("close") or 0)
        if spot <= 0:
            print("  [WEEKLY] Sin precio SPY")
            return
        us500 = round(spot * 10, 2)

        exps = _expiraciones_disponibles()
        if not exps:
            print("  [WEEKLY] Sin expiraciones")
            return

        # ── 1. Imán del día: el PRÓXIMO vencimiento con al menos 1 día.
        # Se descarta el de hoy (0 días): a esa altura ya está resuelto
        # y no sirve para operar mañana. Si no hay festivos de por medio
        # suele ser el día siguiente, pero no se asume — se toma el
        # primero disponible de la lista real de Tradier.
        exp_dia = next((e for e, d in exps if d >= 1), None)
        # ── 2. Imán estructural: el viernes de esta semana (o el
        # siguiente vencimiento en viernes que exista en la cadena).
        exp_viernes = None
        for e, d in exps:
            try:
                if datetime.strptime(e, "%Y-%m-%d").weekday() == 4 and d >= 1:
                    exp_viernes = e
                    break
            except ValueError:
                continue

        dia  = _analizar_expiracion(exp_dia, spot, 0.03) if exp_dia else None
        estr = _analizar_expiracion(exp_viernes, spot, 0.10) if exp_viernes else None

        if not dia and not estr:
            print("  [WEEKLY] Sin datos en ninguna expiración")
            return

        weekly_cache.update({
            "disponible": True,
            "us500": us500,
            "dia": dia,
            "estructural": estr,
            "dia_calculado": ahora.date(),
        })

        # Trayectoria: se guarda la del ESTRUCTURAL, que es la que tiene
        # sentido seguir día a día (el del día cambia de vencimiento).
        if estr:
            hoy = ahora.strftime("%Y-%m-%d")
            weekly_cache["trayectoria"].setdefault(estr["expiracion"], {})[hoy] = {
                "max_pain": estr["max_pain"], "call_wall": estr["call_wall"],
                "put_wall": estr["put_wall"], "cw_oi": estr["call_oi"],
                "pw_oi": estr["put_oi"], "ratio_pc": estr["ratio_pc"],
                "us500": us500, "brecha": estr["brecha"],
            }
            if len(weekly_cache["trayectoria"]) > 6:
                for k in sorted(weekly_cache["trayectoria"])[:-6]:
                    weekly_cache["trayectoria"].pop(k, None)
            guardar_weekly_github()

        if dia:
            print(f"  [WEEKLY] 📌 Imán del día {dia['expiracion']} — "
                  f"MaxPain:{dia['max_pain']} | brecha {dia['brecha']:+} pts")
        if estr:
            print(f"  [WEEKLY] 📊 Estructural {estr['expiracion']} — "
                  f"MaxPain:{estr['max_pain']} | brecha {estr['brecha']:+} pts | "
                  f"P:C {estr['ratio_pc']}")
    except Exception as e:
        print(f"  [WEEKLY] Error calculando: {e}")



# ── Servidor HTTP ────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        cuerpo = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # El dashboard vive en otro dominio (Cloudflare) → hace falta CORS
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta = self.path.split("?")[0].rstrip("/")
        if ruta in ("", "/health"):
            self._json({"ok": True, "hora": hora_ny().strftime("%H:%M:%S ET")})
        # ════════════════════════════════════════════════════════════════
# RUTA /weekly DEL SERVIDOR — v2 (dos imanes)
#
# REEMPLAZA el bloque:
#     elif ruta == "/weekly":
#         ...hasta antes de...
#     else:
#         self._json({"ok": False, "error": "ruta no encontrada"}, 404)
#
# dentro de la clase _Handler, método do_GET.
# ════════════════════════════════════════════════════════════════

        elif ruta == "/weekly":
            if not weekly_cache["disponible"]:
                self._json({"ok": False, "error": "weekly aún no calculado"})
                return
            estr = weekly_cache.get("estructural")
            self._json({
                "ok":     True,
                "us500":  weekly_cache["us500"],
                # Imán del día: el que pinnea la próxima sesión (±3%)
                "dia":    weekly_cache.get("dia"),
                # Imán estructural: viernes / OPEX mensual (±10%)
                "estructural": estr,
                "trayectoria": (weekly_cache["trayectoria"].get(estr["expiracion"], {})
                                if estr else {}),
            })
        else:
            self._json({"ok": False, "error": "ruta no encontrada"}, 404)

    def log_message(self, *args):
        pass          # silenciar el log por petición — ensucia Railway


def iniciar_servidor_http():
    """Servidor en thread daemon. Si falla, el bot sigue igual."""
    try:
        srv = HTTPServer(("0.0.0.0", PUERTO_HTTP), _Handler)
        print(f"  [HTTP] ✅ Servidor escuchando en puerto {PUERTO_HTTP}")
        srv.serve_forever()
    except Exception as e:
        print(f"  [HTTP] Error: {e}")
    
def _regimen_gamma(vix_nivel=None):
    """
    Determina si los muros FRENAN (gamma+) o ACELERAN (gamma-).
    Fuente principal: GEX 0DTE neto. Fallback: VIX.
    Devuelve (es_positiva, fuente_texto).
    """
    if gex_0dte_cache.get("disponible") and gex_0dte_cache.get("neto") is not None:
        return (gex_0dte_cache["neto"] >= 0, "0DTE")
    # Fallback por VIX: VIX alto suele implicar gamma negativa (dealers cortos)
    if vix_nivel is not None:
        return (vix_nivel < 20, "VIX")
    return (True, "?")  # default conservador: asumir rebote

def verificar_proximidad_gex(precio_actual, vix_nivel=None):
    """
    Alerta cuando el precio está a 5 puntos del Flip, Call Wall o Put Wall.
    Dice explícitamente REBOTE (gamma+) o RUPTURA (gamma-) según el régimen.
    """
    if not gex_niveles["disponible"]:
        return

    ahora      = hora_ny()
    flip       = gex_niveles.get("gamma_flip")
    call_wall  = gex_niveles.get("call_wall")
    put_wall   = gex_niveles.get("put_wall")
    UMBRAL_PTS = 5  # puntos US500

    gamma_pos, fuente_reg = _regimen_gamma(vix_nivel)

    niveles = [
        (flip,      "Gamma Flip",  "⚡", "ultima_alerta_flip"),
        (call_wall, "Call Wall",   "🟢", "ultima_alerta_call_wall"),
        (put_wall,  "Put Wall",    "🔴", "ultima_alerta_put_wall"),
    ]

    for nivel, nombre, emoji, cache_key in niveles:
        if not nivel:
            continue
        distancia = abs(precio_actual - nivel)
        if distancia <= UMBRAL_PTS:
            ultima = gex_proximidad_cache[cache_key]
            if ultima and (ahora - ultima).total_seconds() / 60 < 15:
                continue
            gex_proximidad_cache[cache_key] = ahora
            direccion = "↑" if precio_actual < nivel else "↓"

            # ── Lectura explícita según muro + régimen de gamma ──
            if gamma_pos:
                regimen_txt = "🟢 Gamma POSITIVA — dealers amortiguan"
                if nombre == "Call Wall":
                    pronostico = "🛑 REBOTE probable — el Call Wall suele frenar el alza (resistencia)"
                elif nombre == "Put Wall":
                    pronostico = "🛑 REBOTE probable — el Put Wall suele frenar la caída (soporte)"
                else:  # Gamma Flip
                    pronostico = "⚖️ Zona de imán — el precio tiende a gravitar aquí"
            else:
                regimen_txt = "🔴 Gamma NEGATIVA — dealers amplifican"
                if nombre == "Call Wall":
                    pronostico = "🚀 RUPTURA probable — si supera el Call Wall, los dealers impulsan al alza"
                elif nombre == "Put Wall":
                    pronostico = "⚠️ RUPTURA probable — si pierde el Put Wall, los dealers aceleran la caída"
                else:  # Gamma Flip
                    pronostico = "💥 Cruce de Flip en gamma negativa — movimiento explosivo probable"

            try:
                bot.send_message(TELEGRAM_CHAT_ID,
                    f"{emoji} *PROXIMIDAD {nombre.upper()}*\n"
                    f"Precio: `{precio_actual:.1f}` {direccion} `{nivel}` ({distancia:.1f} pts)\n"
                    f"{regimen_txt} _(fuente: {fuente_reg})_\n"
                    f"{pronostico}",
                    parse_mode="Markdown")
                print(f"  [GEX_PROX] {emoji} {nombre}: {precio_actual:.1f} a {distancia:.1f} pts | "
                      f"{'REBOTE' if gamma_pos else 'RUPTURA'} ({fuente_reg})")
            except Exception as e:
                print(f"  [GEX_PROX] Error: {e}")

    # ── Proximidad al FLIP 0DTE (nivel intradía separado del semanal) ──
    # El flip 0DTE es el gatillo de aceleración del día — distinto del
    # flip semanal estructural de arriba. Solo se alerta si es confiable
    # (no lejano/ruidoso) y el precio está dentro del umbral.
    try:
        if (gex_0dte_cache.get("disponible") and
                gex_0dte_cache.get("flip_0dte") and
                gex_0dte_cache.get("flip_confiable", False)):
            flip_0dte = gex_0dte_cache["flip_0dte"]
            dist_0dte = abs(precio_actual - flip_0dte)
            if dist_0dte <= UMBRAL_PTS:
                ultima_0dte = gex_proximidad_cache["ultima_alerta_flip_0dte"]
                if not (ultima_0dte and (ahora - ultima_0dte).total_seconds() / 60 < 15):
                    gex_proximidad_cache["ultima_alerta_flip_0dte"] = ahora
                    dir_0dte = "↑" if precio_actual < flip_0dte else "↓"
                    neto_0dte = gex_0dte_cache.get("neto", 0)
                    # El flip 0DTE define el régimen por sí mismo (su neto)
                    if neto_0dte >= 0:
                        reg_0dte = "🟢 Gamma 0DTE POSITIVA — sobre el flip los dealers soportan"
                        pron_0dte = ("⚖️ Cerca del flip 0DTE — zona de decisión intradía.\n"
                                     "Sobre el flip: soporte. Bajo el flip: presión.")
                    else:
                        reg_0dte = "🔴 Gamma 0DTE NEGATIVA — dealers amplifican el cruce"
                        pron_0dte = ("💥 GATILLO DE ACELERACIÓN — un cruce decidido del flip 0DTE\n"
                                     "suele disparar movimiento explosivo en la dirección del cruce.\n"
                                     "⚠️ Ojo con cruces falsos: esperá que lo atraviese con convicción.")
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"⚡ *PROXIMIDAD FLIP 0DTE* (intradía)\n"
                            f"Precio: `{precio_actual:.1f}` {dir_0dte} `{flip_0dte:.0f}` ({dist_0dte:.1f} pts)\n"
                            f"{reg_0dte}\n"
                            f"{pron_0dte}",
                            parse_mode="Markdown")
                        print(f"  [GEX_PROX] ⚡ FLIP 0DTE: {precio_actual:.1f} a {dist_0dte:.1f} pts del flip 0DTE {flip_0dte:.0f}")
                    except Exception as e:
                        print(f"  [GEX_PROX] Error flip 0DTE: {e}")
        elif (gex_0dte_cache.get("disponible") and
              gex_0dte_cache.get("flip_0dte") and
              not gex_0dte_cache.get("flip_confiable", True)):
            # Flip 0DTE lejano/ruidoso — no alertar (evita falsos cruces)
            pass
    except Exception as e:
        print(f"  [GEX_PROX] Error bloque flip 0DTE: {e}")


def detectar_squeeze_volatilidad(datos, precio_actual):
    """
    Detecta cuando el precio consolida en rango estrecho por 15+ minutos.
    Anticipa movimiento explosivo inminente en cualquier dirección.
    Rango estrecho = menos de 0.1% del precio actual durante 15 min.
    """
    ahora = hora_ny()
    hoy   = ahora.date()

    # Resetear si es nuevo día
    if squeeze_cache["dia"] != hoy:
        squeeze_cache.update({
            "activo": False, "inicio": None,
            "rango_promedio": None, "alerta_enviada": False, "dia": hoy
        })

    try:
        spy   = datos["close"]["^GSPC"]
        high  = datos["high"]["^GSPC"]
        low   = datos["low"]["^GSPC"]

        if len(high) < 15 or len(low) < 15:
            return

        # Calcular rango de los últimos 15 minutos
        rango_15min = float(high.iloc[-15:].max() - low.iloc[-15:].min())
        umbral_squeeze = precio_actual * 0.001  # 0.1% del precio

        if rango_15min <= umbral_squeeze:
            if not squeeze_cache["activo"]:
                squeeze_cache["activo"]   = True
                squeeze_cache["inicio"]   = ahora
                squeeze_cache["rango_promedio"] = rango_15min
                print(f"  [SQUEEZE] 🔄 Compresión detectada — rango {rango_15min:.1f} pts")

            # Alertar si lleva 15+ minutos en squeeze y no ha alertado
            if squeeze_cache["inicio"]:
                mins_squeeze = (ahora - squeeze_cache["inicio"]).total_seconds() / 60
                if mins_squeeze >= 15 and not squeeze_cache["alerta_enviada"]:
                    squeeze_cache["alerta_enviada"] = True
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"🔄 *SQUEEZE DE VOLATILIDAD DETECTADO*\n"
                            f"────────────────────────────\n"
                            f"📊 Rango comprimido: `{rango_15min:.1f} pts` ({mins_squeeze:.0f} min)\n"
                            f"💵 Precio actual: `{precio_actual:.1f}`\n"
                            f"────────────────────────────\n"
                            f"⚡ Movimiento explosivo inminente en cualquier dirección.\n"
                            f"👀 Preparate para la ruptura.",
                            parse_mode="Markdown")
                        print(f"  [SQUEEZE] ⚡ Alerta enviada — {mins_squeeze:.0f} min en compresión")
                    except Exception as e:
                        print(f"  [SQUEEZE] Error: {e}")
        else:
            # Rango se expandió — squeeze terminó
            if squeeze_cache["activo"]:
                print(f"  [SQUEEZE] ✅ Compresión terminada — rango expandido a {rango_15min:.1f} pts")
            squeeze_cache["activo"]        = False
            squeeze_cache["inicio"]        = None
            squeeze_cache["alerta_enviada"] = False

    except Exception as e:
        print(f"  [SQUEEZE] Error: {e}")


def detectar_divergencia_dark_pool(precio_actual, resultado):
    """
    Detecta cuando precio sube pero Dark Pool distribuye
    o precio baja pero Dark Pool acumula.
    Señal de que institucionales van en contra del precio visible.
    """
    try:
        if not dark_pool_cache["disponible"]:
            return

        dp_tendencia = dark_pool_cache.get("tendencia", "NEUTRAL")
        detalle      = resultado.get("detalle", {})
        tendencia    = detalle.get("tendencia", {})
        ema_score    = tendencia.get("score", 0) if isinstance(tendencia, dict) else 0

        # Precio subiendo (EMA positiva) pero Dark Pool distribuyendo
        if ema_score > 0 and dp_tendencia in ["DISTRIBUYENDO", "MOMENTUM_BAJISTA"]:
            print(f"  [DIV_DP] ⚠️ Divergencia bajista — precio ↑ pero DP distribuyendo")
            return "BAJISTA"

        # Precio bajando (EMA negativa) pero Dark Pool acumulando
        if ema_score < 0 and dp_tendencia in ["ACUMULANDO", "MOMENTUM_ALCISTA"]:
            print(f"  [DIV_DP] ⚠️ Divergencia alcista — precio ↓ pero DP acumulando")
            return "ALCISTA"

        return None

    except Exception as e:
        print(f"  [DIV_DP] Error: {e}")
        return None

def detectar_options_sweep():
    """
    Detecta barridos institucionales de opciones SPY via Tradier.
    Un sweep ocurre cuando se compran/venden muchos contratos en
    múltiples strikes en pocos segundos — señal de movimiento inminente.
    Returns: dict con tipo, contratos, strikes, prima_total o None
    """
    if not TRADIER_TOKEN:
        return None
    try:
        import urllib.request, json
        from datetime import datetime

        # Obtener primera expiración (weekly más cercana)
        url_exp = "https://api.tradier.com/v1/markets/options/expirations?symbol=SPY&includeAllRoots=true"
        req = urllib.request.Request(url_exp, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data_exp = json.loads(resp.read().decode())

        expiraciones = data_exp.get("expirations", {}).get("date", [])
        if not expiraciones:
            return None
        if isinstance(expiraciones, str):
            expiraciones = [expiraciones]

        # Usar solo la expiración más cercana (weekly)
        exp = expiraciones[0]

        # Obtener cadena de opciones con volumen y OI
        url_chain = f"https://api.tradier.com/v1/markets/options/chains?symbol=SPY&expiration={exp}&greeks=false"
        req = urllib.request.Request(url_chain, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data_chain = json.loads(resp.read().decode())

        opciones = data_chain.get("options", {}).get("option", [])
        if not opciones:
            return None

        spy = yf.Ticker("SPY")
        precio_spy = spy.fast_info.last_price or 500

        # Analizar calls y puts por separado
        calls_activos = []
        puts_activos  = []

        for op in opciones:
            strike   = float(op.get("strike", 0))
            volumen  = float(op.get("volume", 0) or 0)
            oi       = float(op.get("open_interest", 0) or 0)
            ask      = float(op.get("ask", 0) or 0)
            tipo     = op.get("option_type", "")

            # Filtrar strikes cercanos al precio (±5%)
            if abs(strike - precio_spy) / precio_spy > 0.05:
                continue

            # Volumen anómalo: volumen > 2x el OI promedio O > 500 contratos
            if volumen > 500 and (oi == 0 or volumen > oi * 0.5):
                datos_op = {
                    "strike":    strike,
                    "volumen":   volumen,
                    "prima":     ask * volumen * 100,
                }
                if tipo == "call":
                    calls_activos.append(datos_op)
                elif tipo == "put":
                    puts_activos.append(datos_op)

        # ── Calcular totales calls y puts ────────────────────
        total_calls     = sum(o["volumen"] for o in calls_activos) if len(calls_activos) >= 3 else 0
        total_puts      = sum(o["volumen"] for o in puts_activos)  if len(puts_activos)  >= 3 else 0
        prima_calls     = sum(o["prima"]   for o in calls_activos) if len(calls_activos) >= 3 else 0
        prima_puts      = sum(o["prima"]   for o in puts_activos)  if len(puts_activos)  >= 3 else 0

        # Solo retornar si hay sweep real (mínimo 1000 contratos en alguna dirección)
        hay_sweep_calls = total_calls >= 1000 and len(calls_activos) >= 3
        hay_sweep_puts  = total_puts  >= 1000 and len(puts_activos)  >= 3

        if not hay_sweep_calls and not hay_sweep_puts:
            return None

        # Balance neto
        balance_neto = prima_calls - prima_puts
        if balance_neto > 0:
            direccion_neta = "ALCISTA"
        elif balance_neto < 0:
            direccion_neta = "BAJISTA"
        else:
            direccion_neta = "NEUTRAL"

        return {
            "tipo":           direccion_neta,
            "hay_calls":      hay_sweep_calls,
            "hay_puts":       hay_sweep_puts,
            "contratos_calls": int(total_calls),
            "contratos_puts":  int(total_puts),
            "prima_calls":    prima_calls,
            "prima_puts":     prima_puts,
            "balance_neto":   abs(balance_neto),
            "strikes_calls":  len(calls_activos),
            "strikes_puts":   len(puts_activos),
            "expiracion":     exp,
        }

    except Exception as e:
        print(f"  [SWEEP] Error: {e}")
        return None


def obtener_pc_ratio_semanal():
    """
    Obtiene Put/Call ratio de la expiración semanal más cercana via Tradier.
    Más sensible que el ratio general — refleja posicionamiento intradía real.
    """
    if not TRADIER_TOKEN:
        return
    try:
        import urllib.request, json

        url_exp = "https://api.tradier.com/v1/markets/options/expirations?symbol=SPY&includeAllRoots=true"
        req = urllib.request.Request(url_exp, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data_exp = json.loads(resp.read().decode())

        expiraciones = data_exp.get("expirations", {}).get("date", [])
        if not expiraciones:
            return
        if isinstance(expiraciones, str):
            expiraciones = [expiraciones]

        exp = expiraciones[0]  # Weekly más cercana

        url_chain = f"https://api.tradier.com/v1/markets/options/chains?symbol=SPY&expiration={exp}&greeks=false"
        req = urllib.request.Request(url_chain, headers={
            "Authorization": f"Bearer {TRADIER_TOKEN}",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data_chain = json.loads(resp.read().decode())

        opciones = data_chain.get("options", {}).get("option", [])
        if not opciones:
            return

        vol_calls = sum(float(o.get("volume", 0) or 0) for o in opciones if o.get("option_type") == "call")
        vol_puts  = sum(float(o.get("volume", 0) or 0) for o in opciones if o.get("option_type") == "put")

        if vol_calls == 0:
            return

        ratio = vol_puts / vol_calls

        if ratio > 1.3:
            sesgo = "BAJISTA_FUERTE"
        elif ratio > 1.1:
            sesgo = "BAJISTA_MODERADO"
        elif ratio < 0.7:
            sesgo = "ALCISTA_FUERTE"
        elif ratio < 0.9:
            sesgo = "ALCISTA_MODERADO"
        else:
            sesgo = "NEUTRAL"

        pc_semanal_cache.update({
            "disponible":           True,
            "ratio_semanal":        round(ratio, 3),
            "ultima_actualizacion": hora_ny(),
            "sesgo":                sesgo,
            "expiracion":           exp,
        })
        print(f"  [PC_SEMANAL] ✅ Ratio weekly {exp}: {ratio:.3f} → {sesgo}")

    except Exception as e:
        print(f"  [PC_SEMANAL] Error: {e}")

def obtener_dark_pool():
    """
    Dark Pool proxy mejorado con yfinance granular 5min.
    Detecta bloques de volumen anómalos intradía en tiempo real.
    Más dinámico que el ratio estático 62.5% anterior.
    """
    try:
        spy_5m = yf.download("SPY", period="2d", interval="5m",
                             progress=False, auto_adjust=True)
        if spy_5m.empty or len(spy_5m) < 20:
            return _dark_pool_fallback()

        close  = spy_5m["Close"]
        volume = spy_5m["Volume"]
        high   = spy_5m["High"]
        low    = spy_5m["Low"]
        open_  = spy_5m["Open"]

        ahora   = hora_ny()
        hoy     = ahora.date()
        idx_hoy = [i for i, t in enumerate(spy_5m.index) if t.date() == hoy]

        if len(idx_hoy) < 5:
            idx_hoy = list(range(max(0, len(spy_5m) - 40), len(spy_5m)))

        close_hoy  = close.iloc[idx_hoy]
        volume_hoy = volume.iloc[idx_hoy]
        high_hoy   = high.iloc[idx_hoy]
        low_hoy    = low.iloc[idx_hoy]
        open_hoy   = open_.iloc[idx_hoy]

        # Asegurar que son Series simples no DataFrames
        if hasattr(close_hoy,  "columns"): close_hoy  = close_hoy.iloc[:,  0]
        if hasattr(volume_hoy, "columns"): volume_hoy = volume_hoy.iloc[:, 0]
        if hasattr(high_hoy,   "columns"): high_hoy   = high_hoy.iloc[:,   0]
        if hasattr(low_hoy,    "columns"): low_hoy    = low_hoy.iloc[:,    0]
        if hasattr(open_hoy,   "columns"): open_hoy   = open_hoy.iloc[:,   0]
        if hasattr(close,      "columns"): close      = close.iloc[:,      0]
        if hasattr(volume,     "columns"): volume     = volume.iloc[:,     0]
        if hasattr(high,       "columns"): high       = high.iloc[:,       0]
        if hasattr(low,        "columns"): low        = low.iloc[:,        0]

        vol_historico = volume.iloc[:-len(idx_hoy)] if len(volume) > len(idx_hoy) else volume
        vol_media     = float(vol_historico.mean())
        vol_std       = float(vol_historico.std())
        umbral_bloque = vol_media + (2.0 * vol_std)

        bloques = []
        for i in range(len(close_hoy)):
            vol_vela    = float(volume_hoy.iloc[i])
            precio_vela = float(close_hoy.iloc[i])
            open_vela   = float(open_hoy.iloc[i])
            high_vela   = float(high_hoy.iloc[i])
            low_vela    = float(low_hoy.iloc[i])

            if vol_vela < umbral_bloque:
                continue

            ratio_vol  = vol_vela / vol_media if vol_media > 0 else 1.0
            rango_vela = (high_vela - low_vela) / precio_vela if precio_vela > 0 else 0
            rango_hist = float((high.iloc[-78:] - low.iloc[-78:]).mean()) / float(close.iloc[-1])
            direccion  = "ALCISTA" if precio_vela >= open_vela else "BAJISTA"
            es_silencioso = rango_vela < rango_hist * 0.7

            if   es_silencioso and direccion == "ALCISTA":  tipo = "ACUMULACION"
            elif es_silencioso and direccion == "BAJISTA":  tipo = "DISTRIBUCION"
            elif not es_silencioso and direccion == "ALCISTA": tipo = "MOMENTUM_ALCISTA"
            else:                                            tipo = "MOMENTUM_BAJISTA"

            bloques.append({
                "tipo":      tipo,
                "ratio_vol": round(ratio_vol, 2),
                "rango_pct": round(rango_vela * 100, 3),
                "direccion": direccion,
            })

        if bloques:
            acumulaciones = sum(1 for b in bloques if b["tipo"] == "ACUMULACION")
            distribuciones = sum(1 for b in bloques if b["tipo"] == "DISTRIBUCION")
            momentum_alc  = sum(1 for b in bloques if b["tipo"] == "MOMENTUM_ALCISTA")
            momentum_baj  = sum(1 for b in bloques if b["tipo"] == "MOMENTUM_BAJISTA")

            if   acumulaciones  > distribuciones and acumulaciones  >= 2: tendencia = "ACUMULANDO"
            elif distribuciones > acumulaciones  and distribuciones >= 2: tendencia = "DISTRIBUYENDO"
            elif momentum_alc   > momentum_baj:                           tendencia = "MOMENTUM_ALCISTA"
            elif momentum_baj   > momentum_alc:                           tendencia = "MOMENTUM_BAJISTA"
            else:                                                          tendencia = "NEUTRAL"
        else:
            tendencia = "NEUTRAL"

        vol_silencioso = sum(float(volume_hoy.iloc[i]) for i in range(len(volume_hoy))
                            if i < len(bloques) and
                            bloques[i]["tipo"] in ["ACUMULACION", "DISTRIBUCION"])
        vol_total_hoy  = float(volume_hoy.sum())
        ratio_oculto   = vol_silencioso / vol_total_hoy if vol_total_hoy > 0 else 0.35
        ratio_oculto   = max(0.20, min(0.65, ratio_oculto))

        dark_pool_cache.update({
            "ratio":                round(ratio_oculto, 4),
            "ultima_actualizacion": hora_ny(),
            "disponible":           True,
            "bloques":              bloques[-5:],
            "tendencia":            tendencia,
            "fuente":               "YFINANCE_GRANULAR",
            "es_estimado":          False,
            "bloques_total":        len(bloques),
        })
        print(f"  [DARK_POOL] ✅ Granular — Ratio:{ratio_oculto:.1%} | "
              f"Tendencia:{tendencia} | Bloques:{len(bloques)}")
        return True

    except Exception as e:
        print(f"  [DARK_POOL] Granular error: {e}")
        return _dark_pool_fallback()

def _dark_pool_fallback():
    """Fallback: intenta FINRA, luego estimado estático."""
    try:
        url = ("https://api.finra.org/data/group/OTCMarket/name/weeklySummary"
               "?compareFilters=symbol==SPY&limit=1")
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        if data and len(data) > 0:
            vol_dp    = float(data[0].get("totalWeeklyShareQuantity", 0))
            vol_total = vol_dp * 1.6
            ratio     = vol_dp / vol_total if vol_total > 0 else 0.35
            dark_pool_cache.update({
                "ratio": round(ratio, 4), "ultima_actualizacion": hora_ny(),
                "disponible": True, "tendencia": "NEUTRAL",
                "fuente": "FINRA", "es_estimado": True, "bloques": [],
            })
            print(f"  [DARK_POOL] ⚡ FINRA — Ratio:{ratio:.1%}")
            return True
    except Exception as e:
        print(f"  [DARK_POOL] FINRA error: {e}")

    try:
        spy = yf.download("SPY", period="5d", interval="1d", progress=False)
        if not spy.empty:
            vol_rec  = float(spy["Volume"].iloc[-1])
            vol_prom = float(spy["Volume"].mean())
            ratio_v  = vol_rec / vol_prom if vol_prom > 0 else 1.0
            ratio_e  = max(0.25, min(0.55, 0.38 + (1 - ratio_v) * 0.05))
            dark_pool_cache.update({
                "ratio": round(ratio_e, 4), "ultima_actualizacion": hora_ny(),
                "disponible": True, "tendencia": "NEUTRAL",
                "fuente": "ESTIMADO", "es_estimado": True, "bloques": [],
            })
            print(f"  [DARK_POOL] 📊 Estimado — Ratio:{ratio_e:.1%}")
            return True
    except Exception as e:
        print(f"  [DARK_POOL] Estimado error: {e}")
    return False

def evaluar_dark_pool(datos, ventana=10):
    if not dark_pool_cache["disponible"]:
        return {"disponible": False, "score": 0, "señal": "NO DISPONIBLE",
                "ratio": None, "interpretacion": ""}

    ratio     = dark_pool_cache["ratio"]
    tendencia = dark_pool_cache.get("tendencia", "NEUTRAL")
    fuente    = dark_pool_cache.get("fuente", "ESTIMADO")
    bloques   = dark_pool_cache.get("bloques_total", 0)
    es_estim  = dark_pool_cache.get("es_estimado", False)

    if fuente == "YFINANCE_GRANULAR":
        if   tendencia == "ACUMULANDO":       score = 2;  interpretacion = "ACUMULACION INSTITUCIONAL"
        elif tendencia == "DISTRIBUYENDO":    score = -2; interpretacion = "DISTRIBUCION INSTITUCIONAL"
        elif tendencia == "MOMENTUM_ALCISTA": score = 1;  interpretacion = "MOMENTUM ALCISTA VISIBLE"
        elif tendencia == "MOMENTUM_BAJISTA": score = -1; interpretacion = "MOMENTUM BAJISTA VISIBLE"
        else:                                 score = 0;  interpretacion = "FLUJO NEUTRAL"
    else:
        spy    = datos["close"]["^GSPC"]
        high   = datos["high"]["^GSPC"]
        low    = datos["low"]["^GSPC"]
        rango_ventana = float((high.iloc[-ventana:].max() - low.iloc[-ventana:].min()))
        precio_ref    = float(spy.iloc[-1])
        rango_pct     = rango_ventana / precio_ref if precio_ref > 0 else 0
        if ratio > 0.45:
            if rango_pct < 0.002: score = 2;  interpretacion = "ACUMULACION INSTITUCIONAL OCULTA"
            else:                 score = -1; interpretacion = "DISTRIBUCION EN DARK POOL"
        elif ratio > 0.38:        score = 1;  interpretacion = "ACTIVIDAD DARK POOL ELEVADA"
        elif ratio < 0.30:        score = 0;  interpretacion = "MOVIMIENTO ORGANICO"
        else:                     score = 0;  interpretacion = "DARK POOL NORMAL"

    señal = f"{interpretacion} ({fuente}:{ratio:.1%})"
    if bloques > 0: señal += f" [{bloques} bloques]"

    return {
        "disponible":     True,
        "score":          score,
        "señal":          señal,
        "ratio":          ratio,
        "interpretacion": interpretacion,
        "tendencia":      tendencia,
        "fuente":         fuente,
        "bloques":        bloques,
        "es_estimado":    es_estim,
    }

# ================================================================
# === SEÑALES INSTITUCIONALES v3.9 (sin cambios) =================
# ================================================================

mcclellan_cache = {"oscilador": None, "ad_ratio": None, "disponible": False}

def calcular_mcclellan():
    try:
        sectores  = ["XLK","XLF","XLV","XLI","XLC","XLY","XLP","XLE","XLB","XLRE","XLU"]
        datos_sec = yf.download(sectores, period="30d", interval="1d", progress=False)
        if datos_sec.empty: return False
        close = datos_sec["Close"]
        avances = 0; declives = 0; vol_avances = 0; vol_declives = 0
        try: volume = datos_sec["Volume"]
        except: volume = None
        for sec in sectores:
            if sec in close.columns and len(close[sec].dropna()) >= 2:
                ret = float(close[sec].iloc[-1]) - float(close[sec].iloc[-2])
                vol = float(volume[sec].iloc[-1]) if volume is not None and sec in volume.columns else 1
                if ret > 0: avances += 1; vol_avances += vol
                elif ret < 0: declives += 1; vol_declives += vol
        total = avances + declives
        if total == 0: return False
        ratio_neto = (avances - declives) / total * 100
        ad_series = []
        for i in range(min(30, len(close))):
            av = sum(1 for s in sectores if s in close.columns and
                     len(close[s].dropna()) > i+1 and
                     float(close[s].iloc[-(i+1)]) > float(close[s].iloc[-(i+2)]))
            dc = sum(1 for s in sectores if s in close.columns and
                     len(close[s].dropna()) > i+1 and
                     float(close[s].iloc[-(i+1)]) < float(close[s].iloc[-(i+2)]))
            ad_series.append(av - dc)
        ad_series = ad_series[::-1]
        if len(ad_series) >= 19:
            ema19     = pd.Series(ad_series).ewm(span=19, adjust=False).mean().iloc[-1]
            ema39     = pd.Series(ad_series).ewm(span=min(39,len(ad_series)), adjust=False).mean().iloc[-1]
            oscilador = round(float(ema19 - ema39), 2)
        else:
            oscilador = round(ratio_neto, 2)
        ad_ratio = round(ratio_neto, 1)
        mcclellan_cache.update({"oscilador": oscilador, "ad_ratio": ad_ratio,
                                "avances": avances, "declives": declives,
                                "vol_avances": vol_avances, "vol_declives": vol_declives,
                                "disponible": True})
        print(f"  [McC] ✅ Oscilador:{oscilador} | A/D:{avances}/{declives} | ratio:{ad_ratio:.1f}%")
        return True
    except Exception as e:
        print(f"  [McC] Error: {e}")
        return False

def evaluar_mcclellan():
    if not mcclellan_cache["disponible"]:
        return {"disponible": False, "score": 0, "oscilador": None, "señal": "N/D"}
    osc = mcclellan_cache["oscilador"]
    ad  = mcclellan_cache["ad_ratio"]
    if   osc > 50:  score = 2;  señal = "BREADTH ALCISTA FUERTE"
    elif osc > 20:  score = 1;  señal = "BREADTH ALCISTA"
    elif osc < -50: score = -2; señal = "BREADTH BAJISTA FUERTE"
    elif osc < -20: score = -1; señal = "BREADTH BAJISTA"
    else:           score = 0;  señal = "BREADTH NEUTRAL"
    return {"disponible": True, "score": score, "oscilador": osc, "ad_ratio": ad,
            "señal": señal, "avances": mcclellan_cache.get("avances", 0),
            "declives": mcclellan_cache.get("declives", 0)}

def evaluar_vvix(datos):
    try:
        vvix_data = yf.download("^VVIX", period="5d", interval="1d", progress=False)
        if vvix_data.empty:
            return {"disponible": False, "score": 0, "nivel": None, "señal": "N/D"}
        vvix_actual   = float(vvix_data["Close"].iloc[-1])
        vvix_anterior = float(vvix_data["Close"].iloc[-2]) if len(vvix_data) >= 2 else vvix_actual
        cambio_pct    = (vvix_actual / vvix_anterior - 1) * 100
        if   vvix_actual > 120 and cambio_pct > 5:  score = -3; señal = "PROTECCION EXTREMA — CRASH POSIBLE"
        elif vvix_actual > 100 and cambio_pct > 3:  score = -2; señal = "INSTITUCIONALES COMPRANDO PROTECCION"
        elif vvix_actual > 90  and cambio_pct > 2:  score = -1; señal = "VVIX ELEVADO — CAUTELA"
        elif vvix_actual < 80  and cambio_pct < -2: score =  1; señal = "VVIX BAJO — COMPLACENCIA ALCISTA"
        elif cambio_pct > 5:                         score = -2; señal = "VVIX ACELERANDO — SEÑAL ADELANTADA"
        elif cambio_pct < -3:                        score =  1; señal = "VVIX CAYENDO — RIESGO REDUCIDO"
        else:                                         score =  0; señal = "VVIX NEUTRAL"
        return {"disponible": True, "score": score, "nivel": round(vvix_actual, 2),
                "cambio_pct": round(cambio_pct, 2), "señal": señal}
    except Exception as e:
        return {"disponible": False, "score": 0, "nivel": None, "señal": f"ERROR:{e}"}

put_call_cache = {"ratio": None, "disponible": False, "ultima_actualizacion": None}

def obtener_put_call_ratio():
    try:
        spy     = yf.Ticker("SPY")
        opciones = spy.options
        if not opciones: return False
        exp      = opciones[0]
        chain    = spy.option_chain(exp)
        vol_puts  = float(chain.puts["volume"].sum())  if not chain.puts.empty  else 0
        vol_calls = float(chain.calls["volume"].sum()) if not chain.calls.empty else 0
        if vol_calls == 0: return False
        ratio = round(vol_puts / vol_calls, 3)
        put_call_cache.update({"ratio": ratio, "disponible": True,
                               "ultima_actualizacion": hora_ny()})
        print(f"  [PC] ✅ Put/Call ratio SPY: {ratio:.3f}")
        return True
    except Exception as e:
        print(f"  [PC] Error: {e}")
        try:
            vix = yf.download("^VIX", period="2d", interval="1d", progress=False)
            if not vix.empty:
                vix_nivel = float(vix["Close"].iloc[-1])
                ratio_est = max(0.5, min(2.0, vix_nivel / 20))
                put_call_cache.update({"ratio": round(ratio_est, 3), "disponible": True,
                                       "es_estimado": True, "ultima_actualizacion": hora_ny()})
                print(f"  [PC] 📊 Put/Call estimado via VIX: {ratio_est:.3f}")
                return True
        except: pass
        return False

def evaluar_put_call():
    if not put_call_cache["disponible"]:
        return {"disponible": False, "score": 0, "ratio": None, "señal": "N/D"}
    ratio  = put_call_cache["ratio"]
    es_est = put_call_cache.get("es_estimado", False)
    if   ratio > 1.5:  score = -3; señal = "PÁNICO — PUTS EXTREMAS"
    elif ratio > 1.2:  score = -2; señal = "SESGO BAJISTA INSTITUCIONAL"
    elif ratio > 1.0:  score = -1; señal = "MÁS PUTS QUE CALLS"
    elif ratio < 0.6:  score =  2; señal = "EUFORIA ALCISTA — COMPLACENCIA"
    elif ratio < 0.8:  score =  1; señal = "SESGO ALCISTA EN OPCIONES"
    else:              score =  0; señal = "PUT/CALL NEUTRAL"
    if es_est: señal += " est."
    return {"disponible": True, "score": score, "ratio": ratio, "señal": señal}

def evaluar_rotacion_defensiva(datos):
    try:
        shy_data = yf.download("SHY", period="5d", interval="1d", progress=False)
        if shy_data.empty:
            return {"disponible": False, "score": 0, "señal": "N/D"}
        ret_shy = float((shy_data["Close"].iloc[-1] / shy_data["Close"].iloc[-2] - 1) * 100) \
                  if len(shy_data) >= 2 else 0
        spy_data = yf.download("SPY", period="5d", interval="1d", progress=False)
        ret_spy  = float((spy_data["Close"].iloc[-1] / spy_data["Close"].iloc[-2] - 1) * 100) \
                   if len(spy_data) >= 2 else 0
        if   ret_spy > 0.2  and ret_shy < -0.05: score =  2; señal = "RISK-ON REAL — SPY SUBE SHY CAE"
        elif ret_spy > 0.1  and ret_shy < 0:     score =  1; señal = "ROTACION HACIA RIESGO"
        elif ret_spy < -0.2 and ret_shy > 0.05:  score = -1; señal = "ROTACION DEFENSIVA — PRECAUCION"
        elif ret_spy < -0.2 and ret_shy < -0.05: score = -3; señal = "LIQUIDACION TOTAL — PELIGRO"
        elif ret_spy < 0    and ret_shy > 0.1:   score = -2; señal = "HUIDA A BONOS CORTOS"
        else:                                     score =  0; señal = "FLUJO NEUTRAL"
        return {"disponible": True, "score": score, "señal": señal,
                "ret_spy": round(ret_spy, 3), "ret_shy": round(ret_shy, 3)}
    except Exception as e:
        return {"disponible": False, "score": 0, "señal": f"ERROR:{e}"}

breadth_cache = {"verdes": 0, "rojos": 0, "total": 0, "disponible": False,
                 "ultima_actualizacion": None}
SECTORES_SP500 = ["XLK","XLF","XLV","XLI","XLC","XLY","XLP","XLE","XLB","XLRE","XLU"]

def calcular_breadth_sectores():
    try:
        datos = yf.download(SECTORES_SP500, period="2d", interval="1d", progress=False)
        if datos.empty: return False
        close  = datos["Close"]
        verdes = 0; rojos = 0
        for sec in SECTORES_SP500:
            if sec in close.columns and len(close[sec].dropna()) >= 2:
                ret = float(close[sec].iloc[-1]) - float(close[sec].iloc[-2])
                if ret > 0: verdes += 1
                elif ret < 0: rojos += 1
        total = verdes + rojos
        breadth_cache.update({"verdes": verdes, "rojos": rojos, "total": total,
                              "disponible": True, "ultima_actualizacion": hora_ny()})
        print(f"  [BREADTH] ✅ {verdes}/11 sectores en verde | {rojos} en rojo")
        return True
    except Exception as e:
        print(f"  [BREADTH] Error: {e}")
        return False

def evaluar_breadth():
    if not breadth_cache["disponible"]:
        return {"disponible": False, "score": 0, "señal": "N/D", "verdes": 0, "rojos": 0}
    verdes = breadth_cache["verdes"]
    rojos  = breadth_cache["rojos"]
    total  = breadth_cache["total"]
    if total == 0:
        return {"disponible": False, "score": 0, "señal": "N/D", "verdes": 0, "rojos": 0}
    pct = verdes / total * 100
    if   pct >= 82: score =  2; señal = f"BREADTH FUERTE — {verdes}/11 sectores alcistas"
    elif pct >= 64: score =  1; señal = f"BREADTH POSITIVO — {verdes}/11 sectores alcistas"
    elif pct <= 18: score = -2; señal = f"BREADTH DÉBIL — solo {verdes}/11 sectores alcistas"
    elif pct <= 36: score = -1; señal = f"BREADTH NEGATIVO — {verdes}/11 sectores alcistas"
    else:           score =  0; señal = f"BREADTH NEUTRAL — {verdes}/11 sectores alcistas"
    return {"disponible": True, "score": score, "señal": señal,
            "verdes": verdes, "rojos": rojos, "pct": round(pct, 1)}

def calcular_fear_greed(vix_nivel, vvix_resultado, pc_resultado):
    try:
        vix_score  = max(0, min(100, 100 - (vix_nivel - 10) * 3.33))
        vvix_nivel = vvix_resultado.get("nivel") if vvix_resultado.get("disponible") else 90
        vvix_score = max(0, min(100, 100 - (vvix_nivel - 70) * 2)) if vvix_nivel else 50
        pc_ratio   = pc_resultado.get("ratio") if pc_resultado.get("disponible") else 1.0
        pc_score   = max(0, min(100, (1.5 - pc_ratio) / 0.9 * 100)) if pc_ratio else 50
        fg = round(vix_score * 0.4 + vvix_score * 0.3 + pc_score * 0.3, 1)
        if   fg >= 75: etiqueta = "CODICIA EXTREMA"; score =  1
        elif fg >= 55: etiqueta = "CODICIA";          score =  1
        elif fg >= 45: etiqueta = "NEUTRAL";          score =  0
        elif fg >= 25: etiqueta = "MIEDO";            score = -1
        else:          etiqueta = "MIEDO EXTREMO";    score = -2
        return {"disponible": True, "valor": fg, "etiqueta": etiqueta, "score": score}
    except:
        return {"disponible": False, "valor": 50, "etiqueta": "N/D", "score": 0}

detector_rango = {"activo": False, "inicio": None,
                  "precio_centro": None, "señales_suspendidas": False}

def evaluar_detector_rango(precio_actual, gamma_flip):
    global detector_rango
    ahora = hora_ny()

    # Centro dinámico: Gamma Flip si GEX es real, precio promedio si es estimado
    gex_es_real = not gex_niveles.get("es_estimado", True)
    if gex_es_real and gamma_flip is not None:
        centro_rango = gamma_flip
    else:
        # GEX estimado — usar precio promedio dinámico
        if detector_rango["activo"] and detector_rango.get("precio_centro"):
            centro_rango = detector_rango["precio_centro"] * 0.8 + precio_actual * 0.2
            detector_rango["precio_centro"] = centro_rango
        else:
            centro_rango = precio_actual

    distancia_flip = abs(precio_actual - centro_rango)
    en_rango = distancia_flip <= RANGO_MAXIMO_PUNTOS / 2
    if en_rango:
        if not detector_rango["activo"]:
            detector_rango["activo"]        = True
            detector_rango["inicio"]        = ahora
            detector_rango["precio_centro"] = precio_actual
        minutos_en_rango = (ahora - detector_rango["inicio"]).total_seconds() / 60 \
                           if detector_rango["inicio"] else 0
        suspender = minutos_en_rango >= RANGO_MINUTOS_MINIMO
        if suspender and not detector_rango["señales_suspendidas"]:
            detector_rango["señales_suspendidas"] = True
            print(f"  [RANGO] ⏸ Señales suspendidas — {minutos_en_rango:.0f} min en rango de {distancia_flip:.1f}pts")
        return {"en_rango": True, "suspender": suspender,
                "minutos": round(minutos_en_rango, 1), "distancia_flip": round(distancia_flip, 1)}
    else:
        ruptura = distancia_flip >= RANGO_ALEJAMIENTO_MIN
        if ruptura and detector_rango["señales_suspendidas"]:
            print(f"  [RANGO] ✅ Ruptura detectada — precio se alejó {distancia_flip:.1f}pts del flip")
            detector_rango["activo"]              = False
            detector_rango["señales_suspendidas"] = False
            detector_rango["inicio"]              = None
        return {"en_rango": False, "suspender": False, "minutos": 0,
                "distancia_flip": round(distancia_flip, 1)}

# ================================================================
# === MÓDULOS INSTITUCIONALES AVANZADOS (v4.0) ===================
# ================================================================

# ── Vol-Control / CTA proxy ──────────────────────────────────
# Los fondos volatility-targeting y CTAs compran/venden MECÁNICAMENTE
# según la vol realizada. Vol cayendo → compra forzada días siguientes.
vol_control_cache = {"disponible": False, "rv10": None, "rv20": None,
                     "ratio": None, "señal": "N/D", "score": 0, "dia": None}

def evaluar_vol_control():
    ahora = hora_ny()
    if vol_control_cache["dia"] == ahora.date() and vol_control_cache["disponible"]:
        return dict(vol_control_cache)
    try:
        spy = yf.download("SPY", period="40d", interval="1d", progress=False)
        if spy.empty or len(spy) < 22:
            return {"disponible": False, "score": 0, "señal": "N/D"}
        close = spy["Close"]
        if hasattr(close, "columns"): close = close.iloc[:, 0]
        close = close.squeeze().dropna()
        rets  = np.log(close / close.shift(1)).dropna()
        rv10  = float(rets.iloc[-10:].std() * np.sqrt(252) * 100)
        rv20  = float(rets.iloc[-20:].std() * np.sqrt(252) * 100)
        if rv20 <= 0:
            return {"disponible": False, "score": 0, "señal": "N/D"}
        ratio = rv10 / rv20
        if ratio < 0.80:
            score = 1;  señal = "VOL CAYENDO — compra mecánica vol-control próxima"
        elif ratio > 1.30:
            score = -1; señal = "VOL SUBIENDO — venta mecánica CTA/vol-control"
        else:
            score = 0;  señal = "Flujo vol-control neutral"
        vol_control_cache.update({
            "disponible": True, "rv10": round(rv10, 2), "rv20": round(rv20, 2),
            "ratio": round(ratio, 3), "señal": señal, "score": score, "dia": ahora.date()})
        print(f"  [VOL_CTL] ✅ RV10:{rv10:.1f}% RV20:{rv20:.1f}% ratio:{ratio:.2f} → {señal}")
        return dict(vol_control_cache)
    except Exception as e:
        print(f"  [VOL_CTL] Error: {e}")
        return {"disponible": False, "score": 0, "señal": "N/D"}

# ── Divergencia de crédito (HYG) ─────────────────────────────
# El crédito huele el riesgo antes que las acciones.
# SPY en máximos con HYG sin acompañar = distribución encubierta.
credito_cache = {"disponible": False, "score": 0, "señal": "N/D",
                 "spy_3d": None, "hyg_3d": None, "ultima_actualizacion": None}

def evaluar_credito_hyg():
    ahora = hora_ny()
    if (credito_cache["ultima_actualizacion"] and
        (ahora - credito_cache["ultima_actualizacion"]).total_seconds() / 60 < 30):
        return dict(credito_cache)
    try:
        spy = yf.download("SPY", period="6d", interval="1d", progress=False)
        hyg = yf.download("HYG", period="6d", interval="1d", progress=False)
        if spy.empty or hyg.empty or len(spy) < 4 or len(hyg) < 4:
            return {"disponible": False, "score": 0, "señal": "N/D"}
        spy_c = spy["Close"];  hyg_c = hyg["Close"]
        if hasattr(spy_c, "columns"): spy_c = spy_c.iloc[:, 0]
        if hasattr(hyg_c, "columns"): hyg_c = hyg_c.iloc[:, 0]
        spy_c = spy_c.squeeze(); hyg_c = hyg_c.squeeze()
        spy_3d = float((spy_c.iloc[-1] / spy_c.iloc[-4] - 1) * 100)
        hyg_3d = float((hyg_c.iloc[-1] / hyg_c.iloc[-4] - 1) * 100)
        if spy_3d > 0.3 and hyg_3d < -0.2:
            score = -2; señal = "CRÉDITO NO CONFIRMA — distribución encubierta"
        elif spy_3d > 0.1 and hyg_3d < 0:
            score = -1; señal = "Crédito rezagado — rally frágil"
        elif spy_3d < -0.3 and hyg_3d >= 0:
            score = 1;  señal = "Crédito ignora la caída — sobrerreacción equity"
        elif spy_3d > 0.1 and hyg_3d > 0.1:
            score = 1;  señal = "Crédito confirma el alza"
        else:
            score = 0;  señal = "Crédito neutral"
        credito_cache.update({
            "disponible": True, "score": score, "señal": señal,
            "spy_3d": round(spy_3d, 2), "hyg_3d": round(hyg_3d, 2),
            "ultima_actualizacion": ahora})
        print(f"  [HYG] ✅ SPY3d:{spy_3d:+.2f}% HYG3d:{hyg_3d:+.2f}% → {señal}")
        return dict(credito_cache)
    except Exception as e:
        print(f"  [HYG] Error: {e}")
        return {"disponible": False, "score": 0, "señal": "N/D"}

# ── VWAP del día ─────────────────────────────────────────────
# Los algos institucionales ejecutan contra VWAP. Precio sobre/bajo
# el VWAP dice quién controla la sesión.
def calcular_vwap_dia(datos, minutos_apertura):
    try:
        close  = datos["close"]["^GSPC"]
        volume = datos["volume"]["^GSPC"]
        velas  = max(2, min(int(minutos_apertura), len(close)))
        p = close.iloc[-velas:]
        v = volume.iloc[-velas:]
        v_sum = float(v.sum())
        if v_sum <= 0:
            return {"disponible": False, "score": 0, "señal": "N/D", "vwap": None}
        vwap   = float((p * v).sum() / v_sum)
        precio = float(close.iloc[-1])
        if precio > vwap * 1.0005:
            score = 1;  señal = f"Sobre VWAP({vwap:.1f}) — compradores controlan"
        elif precio < vwap * 0.9995:
            score = -1; señal = f"Bajo VWAP({vwap:.1f}) — vendedores controlan"
        else:
            score = 0;  señal = f"En VWAP({vwap:.1f}) — batalla"
        return {"disponible": True, "score": score, "señal": señal,
                "vwap": round(vwap, 2)}
    except Exception as e:
        return {"disponible": False, "score": 0, "señal": f"ERR:{e}", "vwap": None}

# ── OPEX / Charm ─────────────────────────────────────────────
# Vencimientos mensuales: pinning antes, liberación direccional después.
# Viernes 14:30-16:00 ET: flujo de charm favorece la tendencia del día.
OPEX_FECHAS_2026 = {
    "2026-01-16", "2026-02-20", "2026-03-20", "2026-04-17",
    "2026-05-15", "2026-06-18",  # 19-jun festivo → vence jueves 18
    "2026-07-17", "2026-08-21", "2026-09-18", "2026-10-16",
    "2026-11-20", "2026-12-18",
}
TRIPLE_WITCHING_2026 = {"2026-03-20", "2026-06-18", "2026-09-18", "2026-12-18"}

# ── Cierres de trimestre y fin de mes 2026 ───────────────────
# En estas fechas hay rebalanceo institucional (fondos ajustan
# posiciones al cierre del periodo) → flujos grandes y a veces
# movimientos bruscos al final del día. El cierre de TRIMESTRE es
# el más fuerte (rebalanceo trimestral de pensiones/índices).
# Nota: son el último día HÁBIL del periodo (no el 31 si cae finde).
CIERRE_TRIMESTRE_2026 = {"2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31"}
FIN_DE_MES_2026 = {
    "2026-01-30", "2026-02-27", "2026-03-31", "2026-04-30",
    "2026-05-29", "2026-06-30", "2026-07-31", "2026-08-31",
    "2026-09-30", "2026-10-30", "2026-11-30", "2026-12-31",
}

def contexto_periodo():
    """
    Detecta si HOY es cierre de trimestre o fin de mes (rebalanceo).
    Devuelve el texto de aviso para el pre-market, o "" si no aplica.
    El trimestre tiene prioridad sobre el fin de mes (es más fuerte).
    """
    hoy_str = hora_ny().strftime("%Y-%m-%d")
    if hoy_str in CIERRE_TRIMESTRE_2026:
        return ("\n📅 *HOY CIERRE DE TRIMESTRE* — rebalanceo institucional fuerte.\n"
                "   Esperá flujos grandes y posibles movimientos bruscos al cierre.")
    if hoy_str in FIN_DE_MES_2026:
        return ("\n📅 *HOY FIN DE MES* — rebalanceo de carteras.\n"
                "   Puede haber flujos institucionales al cierre.")
    return ""

charm_alertado = {"dia": None}

def contexto_opex():
    ahora = hora_ny()
    hoy   = ahora.date()
    ctx = {"es_opex_hoy": False, "es_semana_opex": False,
           "es_post_opex": False, "es_triple": False, "proximo_opex": None}
    try:
        fechas = sorted(datetime.strptime(f, "%Y-%m-%d").date() for f in OPEX_FECHAS_2026)
        futuras = [f for f in fechas if f >= hoy]
        pasadas = [f for f in fechas if f < hoy]
        if futuras:
            prox = futuras[0]
            ctx["proximo_opex"] = prox.strftime("%d %b")
            ctx["es_opex_hoy"]  = (prox == hoy)
            ctx["es_triple"]    = prox.strftime("%Y-%m-%d") in TRIPLE_WITCHING_2026
            # Misma semana ISO que el próximo vencimiento
            ctx["es_semana_opex"] = (prox.isocalendar()[:2] == hoy.isocalendar()[:2])
        if pasadas:
            dias_desde = (hoy - pasadas[-1]).days
            ctx["es_post_opex"] = 1 <= dias_desde <= 5 and hoy.weekday() <= 2
    except Exception as e:
        print(f"  [OPEX] Error: {e}")
    return ctx

# ── Liquidez neta de la Fed (FRED) ───────────────────────────
# Liquidez neta = Balance Fed (WALCL) - RRP - TGA. Es la marea de
# fondo: cuando cae sostenida, las correcciones llegan semanas después.
fed_liquidez_cache = {"disponible": False, "neta": None, "cambio_4w": None,
                      "tendencia": "N/D", "semana": None}

def _fred_serie(serie_id):
    """Descarga CSV público de FRED y devuelve [(fecha, valor), ...]."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        texto = resp.read().decode("utf-8", errors="ignore")
    datos = []
    for linea in texto.strip().split("\n")[1:]:
        partes = linea.split(",")
        if len(partes) >= 2 and partes[1].strip() not in (".", ""):
            try:
                datos.append((datetime.strptime(partes[0].strip(), "%Y-%m-%d").date(),
                              float(partes[1].strip())))
            except: continue
    return datos

def _fred_valor_en(datos, fecha_objetivo):
    """Último valor con fecha <= fecha_objetivo."""
    candidatos = [v for f, v in datos if f <= fecha_objetivo]
    return candidatos[-1] if candidatos else None

def obtener_liquidez_fed():
    ahora  = hora_ny()
    semana = ahora.isocalendar()[1]
    if fed_liquidez_cache["semana"] == semana and fed_liquidez_cache["disponible"]:
        return
    try:
        hoy      = ahora.date()
        hace_4w  = hoy - timedelta(days=28)
        walcl    = _fred_serie("WALCL")       # balance Fed — millones $
        rrp      = _fred_serie("RRPONTSYD")   # reverse repo — billones $
        tga      = _fred_serie("WTREGEN")     # cuenta Tesoro — billones $
        if not walcl or not rrp or not tga:
            print("  [FED_LIQ] ⚠️ Series FRED incompletas")
            return
        def neta_en(fecha):
            w = _fred_valor_en(walcl, fecha)
            r = _fred_valor_en(rrp,   fecha)
            t = _fred_valor_en(tga,   fecha)
            if w is None or r is None or t is None:
                return None
            return w / 1000.0 - r - t  # todo en billones $
        neta_now = neta_en(hoy)
        neta_4w  = neta_en(hace_4w)
        if neta_now is None or neta_4w is None:
            print("  [FED_LIQ] ⚠️ Sin datos suficientes")
            return
        cambio = neta_now - neta_4w
        if cambio > 50:
            tendencia = "EXPANDIENDO 🟢"
        elif cambio < -50:
            tendencia = "CONTRAYENDO 🔴"
        else:
            tendencia = "ESTABLE ⚪"
        fed_liquidez_cache.update({
            "disponible": True, "neta": round(neta_now, 0),
            "cambio_4w": round(cambio, 0), "tendencia": tendencia, "semana": semana})
        print(f"  [FED_LIQ] ✅ Neta: ${neta_now:,.0f}B | Δ4sem: {cambio:+,.0f}B → {tendencia}")
    except Exception as e:
        print(f"  [FED_LIQ] Error: {e}")

# ================================================================
# === DESCARGA Y INDICADORES BASE ================================
# ================================================================

def descargar_datos():
    try:
        tickers = ["^GSPC", "QQQ", "TLT", "^VIX", "^VIX3M", "^MOVE", "DX-Y.NYB"]
        raw = yf.download(tickers, period="2d", interval="1m", progress=False, auto_adjust=True)
        if raw.empty or len(raw) < 50: return None
        # ── Fix precio congelado (9-ago) ──────────────────────
        # Antes: .dropna() borraba la fila entera si CUALQUIER ticker
        # faltaba. ^MOVE, ^VIX3M y DXY publican más lento que ^GSPC, así
        # que en la primera hora de sesión se descartaban las filas
        # nuevas del S&P y el bot se quedaba con un precio viejo
        # (jueves 6-ago: 75 min congelado). Ahora solo se descarta la
        # fila si falta el dato del PROPIO S&P; los demás quedan con su
        # último valor conocido vía ffill.
        close  = raw["Close"].ffill()
        volume = raw["Volume"].ffill()
        high   = raw["High"].ffill()
        low    = raw["Low"].ffill()
        open_  = raw["Open"].ffill()
        if "^GSPC" not in close.columns:
            return None
        vivo   = close["^GSPC"].notna()
        close  = close[vivo]
        volume = volume[vivo]
        high   = high[vivo]
        low    = low[vivo]
        open_  = open_[vivo]
        if len(close) < 50:
            return None
        
        
        
        
        return {
            "close": close, "volume": volume, "high": high, "low": low, "open": open_,
            "tiene_vix3m": "^VIX3M"   in close.columns and not close["^VIX3M"].isna().all(),
            "tiene_move":  "^MOVE"    in close.columns and not close["^MOVE"].isna().all(),
            "tiene_dxy":   "DX-Y.NYB" in close.columns and not close["DX-Y.NYB"].isna().all(),
        }
    except Exception as e:
        print(f"[ERROR descarga] {e}")
        return None

def rsi(serie, periodos=14):
    delta    = serie.diff()
    ganancia = delta.where(delta > 0, 0.0).rolling(periodos).mean()
    perdida  = (-delta.where(delta < 0, 0.0)).rolling(periodos).mean()
    rs = ganancia / perdida
    return 100 - (100 / (1 + rs))

def ema(serie, span):
    return serie.ewm(span=span, adjust=False).mean()

# ================================================================
# === CAPAS DE SEÑALES ===========================================
# ================================================================

def delta_volumen(close, open_, volume, ventana=10):
    direccion = np.sign(close - open_)
    vol_dir   = volume * direccion
    delta_sum = vol_dir.iloc[-ventana:].sum()
    vol_total = volume.iloc[-ventana:].sum()
    ratio = delta_sum / vol_total if vol_total > 0 else 0.0
    if   ratio >  0.40: score =  3
    elif ratio >  0.20: score =  2
    elif ratio >  0.05: score =  1
    elif ratio < -0.40: score = -3
    elif ratio < -0.20: score = -2
    elif ratio < -0.05: score = -1
    else:               score =  0
    return {"ratio": round(float(ratio), 4), "score": score}

def absorcion_silenciosa(close, volume, ventana=5):
    ultimas   = close.iloc[-ventana:]
    rango_pct = (ultimas.max() - ultimas.min()) / close.iloc[-1]
    vol_rec   = volume.iloc[-ventana:].mean()
    vol_hist  = volume.iloc[-60:-ventana].mean() if len(volume) > 65 else vol_rec
    ratio_vol = vol_rec / vol_hist if vol_hist > 0 else 1.0
    if ratio_vol >= 1.5 and rango_pct < 0.0008:
        tendencia = close.iloc[-1] - close.iloc[-ventana]
        score = 2 if tendencia >= 0 else -2
        tipo  = "ACUMULACION" if tendencia >= 0 else "DISTRIBUCION"
    else:
        score, tipo = 0, "NINGUNA"
    return {"tipo": tipo, "ratio_vol": round(float(ratio_vol), 2),
            "rango_pct": round(float(rango_pct * 100), 3), "score": score}

def monitor_liquidez(close, volume, high, low, ventana=10):
    if len(close) < ventana + 2:
        return {"nivel": "NORMAL", "ratio_vol_mov": None, "volatilidad_velas": None,
                "alerta": False, "score": 0}
    try:
        movimientos   = (high.iloc[-ventana:] - low.iloc[-ventana:]).abs()
        vol_reciente  = volume.iloc[-ventana:]
        mov_promedio  = float(movimientos.mean())
        vol_promedio  = float(vol_reciente.mean())
        ratio_vm      = vol_promedio / mov_promedio if mov_promedio > 0 else 0
        mov_hist      = (high.iloc[-60:-ventana] - low.iloc[-60:-ventana]).abs().mean()
        vol_hist      = volume.iloc[-60:-ventana].mean()
        ratio_vm_hist = float(vol_hist) / float(mov_hist) if float(mov_hist) > 0 else ratio_vm
        ratio_relativo = ratio_vm / ratio_vm_hist if ratio_vm_hist > 0 else 1.0
        gaps              = close.iloc[-ventana:].diff().abs()
        volatilidad_velas = float(gaps.mean())
        volatilidad_hist  = float(close.iloc[-60:-ventana].diff().abs().mean())
        ratio_volatilidad = volatilidad_velas / volatilidad_hist if volatilidad_hist > 0 else 1.0
        if   ratio_relativo < 0.4 or ratio_volatilidad > 3.0: nivel = "MUY BAJA"; alerta = True;  score = -2
        elif ratio_relativo < 0.6 or ratio_volatilidad > 2.0: nivel = "BAJA";     alerta = True;  score = -1
        elif ratio_relativo < 0.8:                             nivel = "REDUCIDA"; alerta = False; score = 0
        else:                                                   nivel = "NORMAL";   alerta = False; score = 0
        return {"nivel": nivel, "ratio_vol_mov": round(ratio_relativo, 2),
                "volatilidad_velas": round(ratio_volatilidad, 2), "alerta": alerta, "score": score}
    except:
        return {"nivel": "NORMAL", "ratio_vol_mov": None, "volatilidad_velas": None,
                "alerta": False, "score": 0}

def divergencia_spy_qqq(spy, qqq, ventana=15):
    if len(spy) < ventana + 1 or len(qqq) < ventana + 1:
        return {"divergencia": 0.0, "score": 0}
    ret_spy = (spy.iloc[-1] / spy.iloc[-ventana] - 1) * 100
    ret_qqq = (qqq.iloc[-1] / qqq.iloc[-ventana] - 1) * 100
    div = float(ret_spy - ret_qqq)
    if   div >  0.20: score =  2
    elif div >  0.08: score =  1
    elif div < -0.20: score = -2
    elif div < -0.08: score = -1
    else:             score =  0
    return {"divergencia": round(div, 4), "score": score}

def divergencia_spy_tlt(spy, tlt, ventana=15):
    if len(spy) < ventana + 1 or len(tlt) < ventana + 1:
        return {"correlacion": 0.0, "score": 0}
    ret_spy = (spy.iloc[-1] / spy.iloc[-ventana] - 1) * 100
    ret_tlt = (tlt.iloc[-1] / tlt.iloc[-ventana] - 1) * 100
    if   ret_spy >  0.08 and ret_tlt < -0.03: score =  2
    elif ret_spy >  0.04 and ret_tlt <  0:    score =  1
    elif ret_spy < -0.08 and ret_tlt >  0.03: score = -2
    elif ret_spy < -0.04 and ret_tlt >  0:    score = -1
    else:                                      score =  0
    return {"ret_spy": round(ret_spy, 4), "ret_tlt": round(ret_tlt, 4), "score": score}

vix_ratio_historia = []

def ratio_vix_vix3m(datos, minutos_sin_cambio=0):
    if not datos.get("tiene_vix3m", False):
        return {"ratio": None, "score": 0, "disponible": False, "fatiga": False}
    try:
        vix_actual   = float(datos["close"]["^VIX"].iloc[-1])
        vix3m_actual = float(datos["close"]["^VIX3M"].iloc[-1])
        if vix3m_actual == 0:
            return {"ratio": None, "score": 0, "disponible": False, "fatiga": False}
        ratio = vix_actual / vix3m_actual
        if   ratio > 1.10: score_base = -3
        elif ratio > 1.05: score_base = -2
        elif ratio > 1.02: score_base = -1
        elif ratio < 0.90: score_base =  3
        elif ratio < 0.95: score_base =  2
        elif ratio < 0.98: score_base =  1
        else:              score_base =  0
        fatiga      = minutos_sin_cambio >= MINUTOS_VIX_RATIO_FATIGA
        score_final = score_base // 2 if fatiga and score_base != 0 else score_base
        return {"ratio": round(ratio, 4), "vix": round(vix_actual, 2),
                "vix3m": round(vix3m_actual, 2), "score": score_final,
                "score_base": score_base, "fatiga": fatiga, "disponible": True}
    except:
        return {"ratio": None, "score": 0, "disponible": False, "fatiga": False}

def move_index(datos, ventana=10):
    if not datos.get("tiene_move", False):
        return {"disponible": False, "nivel": None, "cambio_pct": None,
                "score": 0, "señal": "NO DISPONIBLE"}
    try:
        move_serie = datos["close"]["^MOVE"]
        vix_serie  = datos["close"]["^VIX"]
        if len(move_serie) < ventana + 1:
            return {"disponible": False, "nivel": None, "cambio_pct": None,
                    "score": 0, "señal": "DATOS INSUFICIENTES"}
        move_actual   = float(move_serie.iloc[-1])
        move_anterior = float(move_serie.iloc[-ventana])
        vix_actual    = float(vix_serie.iloc[-1])
        cambio_move   = (move_actual / move_anterior - 1) * 100
        cambio_vix    = (vix_actual  / float(vix_serie.iloc[-ventana]) - 1) * 100
        if   cambio_move > 2.0 and cambio_vix > 2.0:      score = -3; señal = "PANICO SINCRONIZADO"
        elif cambio_move > 2.0 and abs(cambio_vix) < 1.0: score = -2; señal = "BONOS ANTICIPAN CAIDA"
        elif cambio_move > 1.0 and abs(cambio_vix) < 0.5: score = -1; señal = "TENSION EN BONOS"
        elif cambio_move < -2.0 and vix_actual > 20:       score =  3; señal = "BONOS ANTICIPAN REBOTE"
        elif cambio_move < -1.0 and vix_actual > 18:       score =  2; señal = "ALIVIO EN BONOS"
        elif cambio_move < -0.5:                           score =  1; señal = "BONOS CALMANDOSE"
        elif abs(cambio_move) < 0.5 and move_actual < 100: score =  1; señal = "BONOS ESTABLES"
        else:                                              score =  0; señal = "NEUTRAL"
        return {"disponible": True, "nivel": round(move_actual, 2),
                "cambio_pct": round(cambio_move, 2), "cambio_vix": round(cambio_vix, 2),
                "score": score, "señal": señal}
    except Exception as e:
        return {"disponible": False, "nivel": None, "cambio_pct": None,
                "score": 0, "señal": f"ERROR: {e}"}

def dxy_señal(datos, ventana=15):
    if not datos.get("tiene_dxy", False):
        return {"disponible": False, "nivel": None, "cambio_pct": None,
                "score": 0, "señal": "NO DISPONIBLE"}
    try:
        dxy_serie  = datos["close"]["DX-Y.NYB"]
        spy_serie  = datos["close"]["^GSPC"]
        if len(dxy_serie) < ventana + 1:
            return {"disponible": False, "nivel": None, "cambio_pct": None,
                    "score": 0, "señal": "DATOS INSUFICIENTES"}
        dxy_actual   = float(dxy_serie.iloc[-1])
        dxy_anterior = float(dxy_serie.iloc[-ventana])
        spy_actual   = float(spy_serie.iloc[-1])
        spy_anterior = float(spy_serie.iloc[-ventana])
        cambio_dxy = (dxy_actual / dxy_anterior - 1) * 100
        cambio_spy = (spy_actual / spy_anterior  - 1) * 100
        if   cambio_dxy < -0.20 and cambio_spy > 0.10:  score =  3; señal = "RALLY CONFIRMADO"
        elif cambio_dxy < -0.10 and cambio_spy > 0:     score =  2; señal = "ROTACION HACIA RIESGO"
        elif cambio_dxy < -0.05:                         score =  1; señal = "DOLAR DEBILITANDOSE"
        elif cambio_dxy > 0.20  and cambio_spy > 0.10:  score = -2; señal = "RALLY FRAGIL"
        elif cambio_dxy > 0.20  and cambio_spy < -0.10: score = -3; señal = "HUIDA AL EFECTIVO"
        elif cambio_dxy > 0.10  and cambio_spy < 0:     score = -2; señal = "PRESION BAJISTA"
        elif cambio_dxy > 0.05:                          score = -1; señal = "DOLAR FORTALECIENDOSE"
        else:                                            score =  0; señal = "NEUTRAL"
        return {"disponible": True, "nivel": round(dxy_actual, 3),
                "cambio_pct": round(cambio_dxy, 3), "cambio_spy": round(cambio_spy, 3),
                "score": score, "señal": señal}
    except Exception as e:
        return {"disponible": False, "nivel": None, "cambio_pct": None,
                "score": 0, "señal": f"ERROR: {e}"}

def patron_primera_media_hora(spy, minutos_apertura):
    if minutos_apertura > 90 or minutos_apertura < 3:
        return {"activo": False, "score": 0}
    velas_hoy = spy.iloc[-minutos_apertura:]
    if len(velas_hoy) < 2: return {"activo": False, "score": 0}
    ret = (velas_hoy.iloc[-1] / velas_hoy.iloc[0] - 1) * 100
    if   ret >  0.25: score =  2
    elif ret >  0.08: score =  1
    elif ret < -0.25: score = -2
    elif ret < -0.08: score = -1
    else:             score =  0
    return {"activo": True, "ret_apertura": round(float(ret), 3),
            "minutos": minutos_apertura, "score": score}

def posicion_rango_diario(spy, high, low, minutos_apertura):
    velas   = max(2, minutos_apertura)
    max_dia = float(high.iloc[-velas:].max())
    min_dia = float(low.iloc[-velas:].min())
    precio  = float(spy.iloc[-1])
    rango   = max_dia - min_dia
    if rango == 0:
        return {"posicion_pct": 50.0, "score": 0, "max_dia": max_dia, "min_dia": min_dia}
    posicion = (precio - min_dia) / rango * 100
    score    = 1 if posicion > 80 else (-1 if posicion < 20 else 0)
    return {"posicion_pct": round(float(posicion), 1),
            "max_dia": round(max_dia, 2), "min_dia": round(min_dia, 2), "score": score}

def filtro_tendencia(spy, ventana_ema=20, ventana_minimos=30):
    if len(spy) < ventana_ema + 1:
        return {"precio_vs_ema": 0.0, "sobre_ema": True, "penalizacion": 0,
                "minimos_bajistas": False, "tendencia_bajista_fuerte": False}
    ema20      = float(ema(spy, ventana_ema).iloc[-1])
    precio_act = float(spy.iloc[-1])
    diff_pct   = (precio_act - ema20) / ema20 * 100
    sobre_ema  = precio_act > ema20
    minimos_bajistas = False
    tendencia_bajista_fuerte = False
    if len(spy) >= ventana_minimos:
        seg  = ventana_minimos // 3
        min1 = float(spy.iloc[-ventana_minimos:-2*seg].min())
        min2 = float(spy.iloc[-2*seg:-seg].min())
        min3 = float(spy.iloc[-seg:].min())
        if min3 < min2 < min1:
            minimos_bajistas = True
            if not sobre_ema:
                tendencia_bajista_fuerte = True
    return {
        "ema20": round(ema20, 2), "precio_vs_ema": round(diff_pct, 3),
        "sobre_ema": sobre_ema, "minimos_bajistas": minimos_bajistas,
        "tendencia_bajista_fuerte": tendencia_bajista_fuerte, "penalizacion": 0,
    }

def calcular_score_total(datos, minutos_apertura):
    global vix_ratio_historia
    spy    = datos["close"]["^GSPC"]
    qqq    = datos["close"]["QQQ"]
    tlt    = datos["close"]["TLT"]
    vix    = datos["close"]["^VIX"]
    volume = datos["volume"]["^GSPC"]
    high   = datos["high"]["^GSPC"]
    low    = datos["low"]["^GSPC"]
    open_  = datos["open"]["^GSPC"]

    minutos_vix_fatiga = 0
    if len(vix_ratio_historia) >= 2 and datos.get("tiene_vix3m"):
        try:
            vix_act   = float(datos["close"]["^VIX"].iloc[-1])
            vix3m_act = float(datos["close"]["^VIX3M"].iloc[-1])
            ratio_act = vix_act / vix3m_act if vix3m_act > 0 else 1.0
            for ratio_previo in reversed(vix_ratio_historia):
                if abs(ratio_act - ratio_previo) / ratio_previo < 0.005:
                    minutos_vix_fatiga += 1
                else: break
        except: pass

    precio_actual = float(spy.iloc[-1])
    vix_nivel_act = float(vix.iloc[-1])

    d_vol      = delta_volumen(spy, open_, volume)
    absorc     = absorcion_silenciosa(spy, volume)
    div_qqq    = divergencia_spy_qqq(spy, qqq)
    div_tlt    = divergencia_spy_tlt(spy, tlt)
    vix_ratio  = ratio_vix_vix3m(datos, minutos_vix_fatiga)
    move       = move_index(datos)
    dxy        = dxy_señal(datos)
    p_hora     = patron_primera_media_hora(spy, minutos_apertura)
    pos_rango  = posicion_rango_diario(spy, high, low, minutos_apertura)
    tendencia  = filtro_tendencia(spy)
    liquidez   = monitor_liquidez(spy, volume, high, low)
    gex        = evaluar_gex(precio_actual)
    dark_pool  = evaluar_dark_pool(datos)
    val_rsi    = float(rsi(spy).iloc[-1])
    cot        = evaluar_cot()
    mcclellan  = evaluar_mcclellan()
    vvix       = evaluar_vvix(datos)
    put_call   = evaluar_put_call()
    rotacion   = evaluar_rotacion_defensiva(datos)
    breadth    = evaluar_breadth()
    fear_greed = calcular_fear_greed(vix_nivel_act, vvix, put_call)
    gex_0dte   = evaluar_gex_0dte(precio_actual)
    vol_ctl    = evaluar_vol_control()
    credito    = evaluar_credito_hyg()
    vwap_r     = calcular_vwap_dia(datos, minutos_apertura)

    if vix_ratio.get("disponible") and vix_ratio.get("ratio"):
        vix_ratio_historia.append(vix_ratio["ratio"])
        if len(vix_ratio_historia) > 120: vix_ratio_historia.pop(0)

    # ── Peso dinámico del COT — versión mejorada ────────────
    # El COT tiene 6 días de retraso — puede no reflejar la realidad actual
    # Reglas de reducción de peso:
    # Nivel 1: COT contradice Dark Pool Y Macro → peso baja a ±1
    # Nivel 2: COT contradice Dark Pool O Macro Y precio va en contra → peso baja a ±1
    # Nivel 3: COT contradice sweep neto Y precio cae/sube consistentemente → peso baja a 0
    cot_score_raw   = cot["score"]
    macro_bajista   = "BAJISTA" in contexto_macro.get("impacto", "").upper()
    macro_alcista   = "ALCISTA" in contexto_macro.get("impacto", "").upper()
    dp_tendencia    = dark_pool.get("tendencia", "NEUTRAL")
    dp_bajista      = dp_tendencia in ["DISTRIBUYENDO", "MOMENTUM_BAJISTA"]
    dp_alcista      = dp_tendencia in ["ACUMULANDO", "MOMENTUM_ALCISTA"]

    # Verificar dirección del precio vs COT
    # precio_actual ya calculado arriba — no sobreescribir con datos dict
    tendencia_score = resultado_tendencia.get("score", 0) if "resultado_tendencia" in dir() else 0

    # Verificar sweep neto si está disponible
    sweep_bajista_neto = (sweep_cache.get("tipo") == "BAJISTA" and
                         sweep_cache.get("ultimo_sweep") and
                         (hora_ny() - sweep_cache["ultimo_sweep"]).total_seconds() / 60 <= 60)
    sweep_alcista_neto = (sweep_cache.get("tipo") == "ALCISTA" and
                         sweep_cache.get("ultimo_sweep") and
                         (hora_ny() - sweep_cache["ultimo_sweep"]).total_seconds() / 60 <= 60)

    cot_score_ajustado = cot_score_raw  # Default: peso completo

    # ═══════════════════════════════════════════════════════════
    # ÍNDICE DE TIBURONES vs COT REAL — el flujo fresco destrona
    # a la foto vieja. El COT real es posicionamiento de hasta 6
    # días atrás; el Índice de Tiburones es flujo institucional
    # del día. Cuando el índice tiene sesgo CLARO y lo contradice,
    # el índice gana (esto resuelve los fallos del 16 y 22-jun:
    # COT alcista viejo vs sweeps bajistas frescos masivos).
    # ═══════════════════════════════════════════════════════════
    idx = indice_tiburones_cache
    idx_destrono = False
    if idx.get("disponible") and idx.get("sesgo") in ("ALCISTA", "BAJISTA"):
        idx_alcista  = idx["sesgo"] == "ALCISTA"
        idx_bajista  = idx["sesgo"] == "BAJISTA"
        idx_conf     = idx.get("confianza", "BAJA")
        idx_confluencia = idx.get("confluencia", 0)

        # COT alcista contradicho por índice bajista
        if cot_score_raw > 0 and idx_bajista:
            if idx_conf == "ALTA":
                # Destronado: el COT se invierte parcialmente (el flujo
                # fresco no solo anula, sino que pesa en su dirección)
                cot_score_ajustado = -1
                idx_destrono = True
                print(f"  [COT] 🦈 DESTRONADO → Índice Tiburones BAJISTA (conf.{idx_conf}, "
                      f"confluencia {idx_confluencia}) invierte el COT alcista a -1")
            elif idx_conf == "MEDIA":
                cot_score_ajustado = 0
                idx_destrono = True
                print(f"  [COT] 🦈 Anulado → Índice Tiburones BAJISTA (conf.{idx_conf}) "
                      f"neutraliza el COT alcista")

        # COT bajista contradicho por índice alcista
        elif cot_score_raw < 0 and idx_alcista:
            if idx_conf == "ALTA":
                cot_score_ajustado = 1
                idx_destrono = True
                print(f"  [COT] 🦈 DESTRONADO → Índice Tiburones ALCISTA (conf.{idx_conf}, "
                      f"confluencia {idx_confluencia}) invierte el COT bajista a +1")
            elif idx_conf == "MEDIA":
                cot_score_ajustado = 0
                idx_destrono = True
                print(f"  [COT] 🦈 Anulado → Índice Tiburones ALCISTA (conf.{idx_conf}) "
                      f"neutraliza el COT bajista")

        # Confirmación: índice y COT coinciden → el COT mantiene su peso
        elif (cot_score_raw > 0 and idx_alcista) or (cot_score_raw < 0 and idx_bajista):
            if idx_conf in ("ALTA", "MEDIA"):
                print(f"  [COT] ✅ Confirmado por Índice Tiburones ({idx['sesgo']}, "
                      f"conf.{idx_conf}) — COT mantiene peso completo")

    # ── Lógica de respaldo (dark pool / macro) — solo si el índice
    #    NO ya destronó al COT. Usa SWEEPS + MACRO (el dark pool fue
    #    neutralizado por estar roto). Red de seguridad adicional por si
    #    el índice no tiene datos suficientes.
    if not idx_destrono:
        # ── COT alcista + sweep bajista neto fuerte → peso 0
        if cot_score_raw > 0 and sweep_bajista_neto:
            cot_score_ajustado = 0
            print(f"  [COT] 🚫 Peso eliminado — contradice Sweep BAJISTA neto")

        # ── COT bajista + sweep alcista neto fuerte → peso 0
        elif cot_score_raw < 0 and sweep_alcista_neto:
            cot_score_ajustado = 0
            print(f"  [COT] 🚫 Peso eliminado — contradice Sweep ALCISTA neto")

        # ── COT contradice Macro → peso reducido ±1
        elif cot_score_raw > 0 and macro_bajista:
            cot_score_ajustado = 1
            print(f"  [COT] ⚠️ Peso reducido ±1 — contradice Macro bajista")

        elif cot_score_raw < 0 and macro_alcista:
            cot_score_ajustado = -1
            print(f"  [COT] ⚠️ Peso reducido ±1 — contradice Macro alcista")

    componentes = {
        "delta_volumen":   d_vol["score"],
        "absorcion":       absorc["score"],
        # divergencia_qqq NEUTRALIZADA (5-jul): journal 88 señales → 29% win
        # rate. Voto puesto en 0. Se sigue calculando y mostrando en detalle,
        # pero ya no suma al score. (La función divergencia_spy_qqq sigue viva.)
        "divergencia_qqq": 0,
        "divergencia_tlt": div_tlt["score"],   # intacto (útil vs tasas/UST10Y)
        "vix_ratio":       vix_ratio["score"],
        "move_index":      move["score"],
        "dxy":             dxy["score"],
        "gex":             gex["score"],
        "dark_pool":       0,  # NEUTRALIZADO 24-jun: el proxy yfinance da 20% clavado (bug clamp), daba MOMENTUM ALCISTA falso y contaminaba el score con sesgo alcista. Se pone en 0 hasta integrar dark pool REAL (servicio pago). La función obtener_dark_pool sigue viva para no romper otras referencias, pero su voto ya no suma al score.
        # patron_apertura NEUTRALIZADO (5-jul): journal → 38% win rate. Voto 0.
        "patron_apertura": 0,
        "posicion_rango":  pos_rango["score"],
        "liquidez":        liquidez["score"],
        "cot":             cot_score_ajustado,
        "mcclellan":       mcclellan["score"],
        "vvix":            vvix["score"],
        "put_call":        put_call["score"],
        "rotacion":        rotacion["score"],
        # breadth NEUTRALIZADO (5-jul): journal → 30% win rate. Voto 0.
        "breadth":         0,
        "fear_greed":      fear_greed["score"],
        "gex_0dte":        gex_0dte["score"],
        # vol_control NEUTRALIZADO (5-jul): journal → 25% win rate. Voto 0.
        "vol_control":     0,
        "credito_hyg":     credito["score"],   # intacto (avisó bien; se usa para tendencias)
        "vwap":            vwap_r["score"],
    }

    score_raw = sum(componentes.values())

    penalizacion_rsi = 0
    if score_raw > 0 and val_rsi > 75:   penalizacion_rsi = -2
    elif score_raw < 0 and val_rsi < 25: penalizacion_rsi =  2
    score_raw += penalizacion_rsi

    penalizacion_tendencia = 0
    if tendencia["tendencia_bajista_fuerte"]:
        if score_raw > 0: penalizacion_tendencia = -4
        if gex.get("disponible") and gex.get("distancia_flip") is not None:
            if gex["distancia_flip"] < 0: penalizacion_tendencia = -6
    elif tendencia["minimos_bajistas"] and not tendencia["sobre_ema"]:
        if score_raw > 0: penalizacion_tendencia = -3
    elif not tendencia["sobre_ema"]:
        if score_raw > 0: penalizacion_tendencia = -2
    elif tendencia["sobre_ema"] and score_raw < 0:
        penalizacion_tendencia = 2
    score_raw += penalizacion_tendencia

    penalizacion_rally = 0
    try:
        min_dia = float(low.iloc[-minutos_apertura:].min())  if minutos_apertura > 0 else float(low.iloc[-30:].min())
        max_dia = float(high.iloc[-minutos_apertura:].max()) if minutos_apertura > 0 else float(high.iloc[-30:].max())
        distancia_desde_minimo = precio_actual - min_dia
        distancia_desde_maximo = max_dia - precio_actual
        if   score_raw > 0 and distancia_desde_minimo > 50: penalizacion_rally = -3
        elif score_raw > 0 and distancia_desde_minimo > 30: penalizacion_rally = -2
        elif score_raw < 0 and distancia_desde_maximo > 50: penalizacion_rally =  3
        elif score_raw < 0 and distancia_desde_maximo > 30: penalizacion_rally =  2
    except: pass
    score_raw += penalizacion_rally
    score_final = max(-10, min(10, score_raw))

    return {
        "score": score_final, "penalizacion_rsi": penalizacion_rsi,
        "penalizacion_tendencia": penalizacion_tendencia,
        "penalizacion_rally": penalizacion_rally,
        "componentes": componentes, "minutos_vix_fatiga": minutos_vix_fatiga,
        "detalle": {
            "delta_volumen": d_vol, "absorcion": absorc,
            "div_qqq": div_qqq, "div_tlt": div_tlt,
            "vix_ratio": vix_ratio, "move_index": move, "dxy": dxy,
            "gex": gex, "dark_pool": dark_pool, "tendencia": tendencia,
            "liquidez": liquidez, "patron_apertura": p_hora,
            "posicion_rango": pos_rango, "rsi": round(val_rsi, 1),
            "precio": round(precio_actual, 2), "vix_nivel": round(vix_nivel_act, 2),
            "cot": cot, "mcclellan": mcclellan, "vvix": vvix,
            "put_call": put_call, "rotacion": rotacion,
            "breadth": breadth, "fear_greed": fear_greed,
            "gex_0dte": gex_0dte, "vol_control": vol_ctl,
            "credito_hyg": credito, "vwap": vwap_r,
        }
    }

# ================================================================
# === CONTEXTO MACROECONÓMICO ====================================
# ================================================================

contexto_macro = {
    "resumen": "Sin contexto macro disponible.", "impacto": "NEUTRAL",
    "noticias": [], "sesgo": "", "ultima_actualizacion": None,
    "actualizaciones_hoy": 0,
    "fecha_conteo": None,
}

def buscar_contexto_macro():
    ahora    = hora_ny()
    fecha    = ahora.strftime("%B %d, %Y")
    hora_str = ahora.strftime("%H:%M ET")
    print(f"  [MACRO] Actualizando contexto ({hora_str})...")
    try:
        respuesta = claude_client.messages.create(
            model=MODELO_MACRO, max_tokens=600,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system="""Eres un analista macroeconómico especializado en el mercado de valores de EE.UU.
Responde ÚNICAMENTE con este formato exacto (sin markdown, sin asteriscos):

IMPACTO: [ALCISTA_FUERTE | ALCISTA_MODERADO | NEUTRAL | BAJISTA_MODERADO | BAJISTA_FUERTE]

NOTICIAS:
1. [noticia más importante en 1 línea]
2. [segunda noticia en 1 línea]
3. [tercera noticia en 1 línea]

RESUMEN: [2-3 oraciones sobre impacto en S&P 500 hoy]

SESGO: [1 oración directa sobre si operar largo, corto o esperar]""",
            messages=[{"role": "user", "content":
                f"Noticias más relevantes del mercado de EE.UU. hoy {fecha} a las {hora_str}. "
                f"Incluye: geopolítica, Fed, datos económicos, earnings, eventos que muevan el mercado."}]
        )
        texto = "".join(b.text for b in respuesta.content if b.type == "text")
        if not texto.strip(): return None
        lineas  = texto.strip().split("\n")
        impacto = "NEUTRAL"; noticias = []; resumen = ""; sesgo = ""
        for linea in lineas:
            linea = linea.strip()
            if   linea.startswith("IMPACTO:"):         impacto = linea.replace("IMPACTO:", "").strip()
            elif linea.startswith(("1.", "2.", "3.")): noticias.append(linea[2:].strip())
            elif linea.startswith("RESUMEN:"):         resumen = linea.replace("RESUMEN:", "").strip()
            elif linea.startswith("SESGO:"):           sesgo   = linea.replace("SESGO:", "").strip()
        if not sesgo:
            sesgo = f"Sesgo {impacto.lower()} — ver contexto arriba"
        return {"impacto": impacto, "noticias": noticias,
                "resumen": resumen, "sesgo": sesgo, "hora": hora_str}
    except Exception as e:
        print(f"  [MACRO] Error: {e}")
        return None

def actualizar_contexto_macro(enviar_telegram=True):
    global contexto_macro
    resultado = buscar_contexto_macro()
    if resultado is None:
        print("  [MACRO] No se pudo actualizar."); return
    impacto_anterior = contexto_macro.get("impacto", "NEUTRAL")
    impacto_nuevo    = resultado.get("impacto", "NEUTRAL")
    contexto_macro.update({
        "resumen": resultado.get("resumen", ""), "impacto": impacto_nuevo,
        "noticias": resultado.get("noticias", []), "sesgo": resultado.get("sesgo", ""),
        "ultima_actualizacion": hora_ny(),
    })
    contexto_macro["actualizaciones_hoy"] = contexto_macro.get("actualizaciones_hoy", 0) + 1
    print(f"  [MACRO] Actualizado: {impacto_nuevo} (anterior: {impacto_anterior}) | Actualización {contexto_macro['actualizaciones_hoy']}/2 del día")
    if enviar_telegram and (impacto_anterior != impacto_nuevo or impacto_anterior == "NEUTRAL"):
        _enviar_macro_telegram(resultado)
    elif enviar_telegram:
        print(f"  [MACRO] Sin cambio — no se envía a Telegram")

def _enviar_macro_telegram(resultado):
    impacto  = resultado.get("impacto", "NEUTRAL")
    noticias = resultado.get("noticias", [])
    resumen  = resultado.get("resumen", "")
    sesgo    = resultado.get("sesgo", "")
    hora_str = resultado.get("hora", "")
    emoji_map = {"ALCISTA_FUERTE": "🟢🟢", "ALCISTA_MODERADO": "🟢",
                 "NEUTRAL": "⚪", "BAJISTA_MODERADO": "🔴", "BAJISTA_FUERTE": "🔴🔴"}
    emoji        = emoji_map.get(impacto, "⚪")
    noticias_str = "\n".join(f"  • {n}" for n in noticias) if noticias else "  • Sin noticias"
    msg = (f"📰 *CONTEXTO MACRO — {hora_str}*\n{'─'*28}\n"
           f"{emoji} *Impacto:* {impacto.replace('_', ' ')}\n{'─'*28}\n"
           f"*Noticias:*\n{noticias_str}\n{'─'*28}\n"
           f"*Contexto:* {resumen}\n\n*Sesgo:* {sesgo}")
    if len(msg) > 4096: msg = msg[:4090] + "..."
    try: bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
    except:
        try: bot.send_message(TELEGRAM_CHAT_ID, msg.replace("*","").replace("`",""))
        except Exception as e: print(f"  [MACRO] Telegram error: {e}")

def necesita_actualizar_macro():
    ahora       = hora_ny()
    hoy         = ahora.date()
    hora_actual = ahora.hour * 60 + ahora.minute
    apertura    = 9 * 60 + 30
    mediodia    = 12 * 60 + 30
    evento_8h30 = 8 * 60 + 30  # Hora de eventos macro (CPI/NFP/PCE)

    # Resetear contador si es un nuevo día
    if contexto_macro.get("fecha_conteo") != hoy:
        contexto_macro["actualizaciones_hoy"] = 0
        contexto_macro["fecha_conteo"]        = hoy

    # Máximo 2 actualizaciones por día — límite absoluto
    if contexto_macro["actualizaciones_hoy"] >= 2:
        return False

    ultima     = contexto_macro["ultima_actualizacion"]
    mismo_dia  = ultima.date() == hoy if ultima else False

    # Cooldown mínimo absoluto de 60 minutos entre cualquier actualización
    if ultima:
        mins_desde = (ahora - ultima).total_seconds() / 60
        if mins_desde < 60: return False

    # ── Fix macro desactualizado en días de evento ────────────
    # Si hay evento hoy a las 8:30 ET (CPI/NFP/PCE), no buscar
    # contexto antes de que salga el dato — llegaría desactualizado
    hoy_str = ahora.strftime("%Y-%m-%d")
    hay_evento_hoy = any(
        fecha == hoy_str and hora == evento_8h30
        for fecha, hora in EVENTOS_MACRO_REALES.values()
    )
    if hay_evento_hoy and hora_actual < evento_8h30 + 15:
        print(f"  [MACRO] ⏸ Día de evento — esperando hasta {evento_8h30 + 15} ET para buscar contexto")
        return False

    # Actualización 1 — solo en ventana de apertura
    if contexto_macro["actualizaciones_hoy"] == 0:
        if apertura <= hora_actual <= apertura + 15:
            return True
        # Reinicio tardío — solo actualizar si es antes del mediodía
        if not mismo_dia and apertura + 15 < hora_actual < mediodia:
            return True
        return False

    # Actualización 2 — solo en ventana de mediodía
    if contexto_macro["actualizaciones_hoy"] == 1:
        if (mediodia <= hora_actual <= mediodia + 15
                and mismo_dia and ultima
                and (ahora - ultima).total_seconds() / 60 >= 60):
            return True
        return False

    return False

# ================================================================
# === FUNCIONES v3.9 ============================================
# ================================================================

ultimo_evento_procesado = {"tipo": None, "hora": None}

# Estado para penalización post-evento macro
penalizacion_macro_activa = {
    "activa":     False,
    "tipo":       None,
    "hora_inicio": None,
    "desviacion": 0,   # % de desviación vs previsión
}

# Calendario de eventos reales con fecha exacta — se actualiza semanalmente
EVENTOS_MACRO_REALES = {
    # Formato: "NOMBRE": ("YYYY-MM-DD", hora_ET_en_minutos)
    # Actualizar cada semana con el calendario económico real
    "NFP":  ("2026-06-05", 8*60+30),   # NFP mayo — ya ocurrió
    "CPI":  ("2026-06-10", 8*60+30),   # CPI mayo — ocurrió 10-jun
    "FOMC": ("2026-06-17", 14*60+0),   # FOMC junio
    "PCE":  ("2026-06-27", 8*60+30),   # PCE mayo
}

def detectar_evento_reciente():
    """
    Detecta eventos macro de alto impacto REALES — solo si ocurrieron HOY
    y en la ventana de tiempo correcta (15-20 min después del evento).
    Nunca se activa por mencionar "Fed" o "FOMC" en noticias.
    """
    ahora   = hora_ny()
    hoy_str = ahora.strftime("%Y-%m-%d")
    hora_et = ahora.hour * 60 + ahora.minute

    for nombre, (fecha_str, hora_evento) in EVENTOS_MACRO_REALES.items():
        # Solo si el evento es HOY
        if fecha_str != hoy_str:
            continue
        # Solo en ventana 15-20 min después del evento
        minutos_desde = hora_et - hora_evento
        if not (15 <= minutos_desde <= 20):
            continue
        # Solo si no fue procesado ya hoy
        if (ultimo_evento_procesado["tipo"] == nombre and
            ultimo_evento_procesado.get("dia") == ahora.date()):
            continue
        return nombre
    return None

def procesar_macro_post_evento(nombre_evento):
    """
    Actualiza macro post-evento. Si hay gran desviación vs previsión
    activa penalización de 45 minutos en señales de ambas direcciones.
    """
    global ultimo_evento_procesado, penalizacion_macro_activa
    print(f"  [POST-EVENTO] Actualizando macro después de {nombre_evento}...")
    actualizar_contexto_macro(enviar_telegram=True)
    ultimo_evento_procesado = {"tipo": nombre_evento, "hora": hora_ny(), "dia": hora_ny().date()}

    # Detectar gran desviación en texto macro
    macro_texto = (contexto_macro.get("resumen", "") +
                   " ".join(contexto_macro.get("noticias", []))).lower()
    sorpresa_keywords = [
        "superó expectativas", "supera expectativas", "duplica", "triplica",
        "muy por encima", "muy por debajo", "sorprende", "sorpresa",
        "vs estimado", "vs esperado", "mayor caída", "mayor subida",
        "récord", "histórico", "beats", "misses", "dobla", "dobló"
    ]
    hay_sorpresa = any(k in macro_texto for k in sorpresa_keywords)
    if hay_sorpresa:
        # Solo activar si no está ya activa para evitar spam
        if not penalizacion_macro_activa["activa"]:
            penalizacion_macro_activa.update({
                "activa": True, "tipo": nombre_evento,
                "hora_inicio": hora_ny(), "desviacion": 1,
            })
            print(f"  [POST-EVENTO] ⚠️ Gran desviación — penalización activa 45 min")
            try:
                bot.send_message(TELEGRAM_CHAT_ID,
                    f"⚠️ *POST-{nombre_evento} — SEÑALES PENALIZADAS 45 MIN*\n"
                    f"Gran desviación vs previsión detectada.\n"
                    f"Evita entradas durante los próximos 45 minutos.",
                    parse_mode="Markdown")
            except: pass
        else:
            print(f"  [POST-EVENTO] Penalización ya activa — ignorando duplicado")

pre_apertura_enviado = {"dia": None}


def obtener_calendario_economico():
    """
    Obtiene eventos económicos de alto impacto para la semana actual
    via web search usando el módulo de Claude.
    Retorna lista de eventos con hora ET y descripción.
    """
    try:
        from datetime import datetime, timedelta
        import urllib.request, json

        ahora     = hora_ny()
        hoy       = ahora.date()
        lunes     = hoy - timedelta(days=hoy.weekday())
        viernes   = lunes + timedelta(days=4)

        # Eventos fijos de alto impacto que siempre buscamos
        eventos_conocidos = {
            "NFP":     "Nóminas No Agrícolas (NFP)",
            "CPI":     "Índice de Precios al Consumidor (CPI)",
            "FOMC":    "Decisión de Tasas Fed (FOMC)",
            "PCE":     "Gasto Consumo Personal (PCE)",
            "JOLTS":   "Ofertas de Empleo (JOLTS)",
            "ADP":     "Empleo Privado ADP",
            "GDP":     "PIB Trimestral (GDP)",
            "PPI":     "Índice Precios Productor (PPI)",
            "CLAIMS":  "Solicitudes Desempleo",
            "ISM":     "ISM Manufacturero/Servicios",
        }

        # Usar el cliente Claude para buscar eventos de la semana
        prompt = (f"Dame solo los eventos económicos de ALTO IMPACTO para EEUU "
                 f"de la semana del {lunes} al {viernes} de 2026. "
                 f"Formato exacto por línea: HH:MM ET | NOMBRE_EVENTO | PREVISION "
                 f"Solo eventos con impacto 3 toros (máximo impacto). "
                 f"Si no hay eventos importantes esa semana, responde: NINGUNO. "
                 f"Máximo 6 eventos. No incluyas explicaciones.")

        try:
            respuesta = claude_client.messages.create(
                model=MODELO_SEÑALES, max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )
            texto = respuesta.content[0].text.strip()
            if "NINGUNO" in texto.upper():
                return []

            eventos = []
            for linea in texto.split("\n"):
                linea = linea.strip()
                if "|" in linea and len(linea) > 5:
                    partes = [p.strip() for p in linea.split("|")]
                    if len(partes) >= 2:
                        eventos.append({
                            "hora":    partes[0] if len(partes) > 0 else "TBD",
                            "nombre":  partes[1] if len(partes) > 1 else linea,
                            "prevision": partes[2] if len(partes) > 2 else "N/D",
                        })
            return eventos[:6]

        except Exception as e:
            print(f"  [CALENDARIO] Error Claude: {e}")
            return []

    except Exception as e:
        print(f"  [CALENDARIO] Error general: {e}")
        return []

def enviar_pre_apertura():
    ahora = hora_ny()
    if pre_apertura_enviado["dia"] == ahora.date(): return
    if not es_dia_habil(ahora.date()):  # fin de semana O festivo NYSE — sin sesión
        print("  [PRE-APERTURA] ⏭ Hoy no es día hábil (fin de semana o festivo) — sin pre-apertura")
        return
    hora_et = ahora.hour * 60 + ahora.minute
    if not (8 * 60 + 30 <= hora_et <= 9 * 60 + 29): return  # 8:30-9:29 ET (ampliada)
    print(f"  [PRE-APERTURA] Preparando contexto... (hora_et={hora_et//60}:{hora_et%60:02d})")
    try:
        try:
            es_data       = descargar_futuros(period="2d", interval="5m")
        except Exception as ef:
            print(f"  [PRE-APERTURA] Error futuros: {ef}")
            es_data = pd.DataFrame()
        if not es_data.empty:
            close_es = es_data["Close"]
            if hasattr(close_es, "columns"): close_es = close_es.iloc[:, 0]
            close_es = close_es.squeeze().dropna()
            try:
                futuro_precio = float(close_es.iloc[-1])
                if len(close_es) >= 12:
                    futuro_cambio = float((close_es.iloc[-1] / close_es.iloc[-12] - 1) * 100)
                else:
                    futuro_cambio = 0.0
            except Exception as ep:
                print(f"  [PRE-APERTURA] Error procesando futuros: {ep}")
                futuro_precio = 0; futuro_cambio = 0.0
        else:
            futuro_precio = 0; futuro_cambio = 0.0
        cot_info  = cot_cache
        # Mostrar fuente COT en pre-apertura
        cot_fuente = cot_info.get("fuente", "N/D")
        cot_texto  = f"COT ({cot_fuente}): {cot_info.get('sesgo','N/D')}" \
                     if cot_info.get("disponible") else "COT: N/D"
        gex_texto = ""
        if gex_niveles["disponible"]:
            fuente_gex = gex_niveles.get("fuente", "EST")
            gex_texto  = (f"\n⚡ GEX ({fuente_gex}): Flip:`{gex_niveles['gamma_flip']}` | "
                         f"Call:`{gex_niveles['call_wall']}` | Put:`{gex_niveles['put_wall']}`")
        if not breadth_cache["disponible"]: calcular_breadth_sectores()
        breadth_texto = f"Breadth: {breadth_cache.get('verdes',0)}/11 sectores en verde" \
                       if breadth_cache["disponible"] else "Breadth: N/D"
        try:
            vix_d  = yf.download("^VIX", period="2d", interval="1d", progress=False)
            if not vix_d.empty:
                vix_close = vix_d["Close"]
                if hasattr(vix_close, "columns"): vix_close = vix_close.iloc[:, 0]
                vix_n = float(vix_close.squeeze().iloc[-1])
            else:
                vix_n = 20
        except Exception as ev:
            print(f"  [PRE-APERTURA] Error VIX: {ev}")
            vix_n = 20
        fg     = calcular_fear_greed(vix_n, {"disponible": False}, {"disponible": False})
        fg_texto   = f"Fear/Greed: {fg['valor']} — {fg['etiqueta']}"
        macro_imp  = contexto_macro.get("impacto", "calculando...")
        emoji_dir  = "📈" if futuro_cambio > 0 else "📉"

        # ── Calendario económico de la semana ─────────────────
        calendario_texto = ""
        try:
            eventos = obtener_calendario_economico()
            if eventos:
                calendario_texto = "\n📅 *Eventos esta semana:*\n"
                for ev in eventos:
                    calendario_texto += f"  • `{ev['hora']}` — {ev['nombre']}"
                    if ev['prevision'] != "N/D":
                        calendario_texto += f" (prev: {ev['prevision']})"
                    calendario_texto += "\n"
        except Exception as e:
            print(f"  [CALENDARIO] Error en pre-apertura: {e}")

        # ── COT Estimado texto (JUBILADO — reemplazado por Índice Tiburones) ─
        # El índice va en su propio bloque más abajo, no en la línea del COT.
        cot_est_texto = ""

        # ── Countdown dinámico ────────────────────────────────
        mins_para_open = max(0, 9 * 60 + 30 - hora_et)
        open_txt = f"en ~{mins_para_open} min" if mins_para_open > 0 else "¡AHORA!"

        # ── Contexto OPEX ─────────────────────────────────────
        opex_texto = ""
        try:
            ctx_opex = contexto_opex()
            if ctx_opex["es_opex_hoy"]:
                tw = " TRIPLE WITCHING" if ctx_opex["es_triple"] else ""
                opex_texto = f"\n📌 *HOY VENCE OPEX{tw}* — pinning probable cerca de los walls"
            elif ctx_opex["es_semana_opex"]:
                opex_texto = f"\n📌 Semana OPEX (vence {ctx_opex['proximo_opex']}) — gravitación hacia walls"
            elif ctx_opex["es_post_opex"]:
                opex_texto = "\n📌 Post-OPEX — flujos liberados, más dirección probable"
        except: pass

        # ── Contexto de periodo (cierre trimestre / fin de mes) ──
        periodo_texto = ""
        try:
            periodo_texto = contexto_periodo()
        except Exception as e:
            print(f"  [PERIODO] Error: {e}")

        # ── Índice de Tiburones (reemplaza al COT estimado viejo) ──
        tiburones_texto = ""
        try:
            calcular_indice_tiburones()
            tiburones_texto = texto_indice_tiburones()
        except Exception as e:
            print(f"  [TIBURONES] Error en pre-apertura: {e}")

        msg = (f"🌅 *PRE-APERTURA — US500 v3.9*\n{'─'*28}\n"
               f"⏰ Mercado abre {open_txt}\n"
               f"{emoji_dir} Futuros S&P: `{futuro_precio:.0f}` ({futuro_cambio:+.2f}%)\n"
               f"📊 {cot_texto}{cot_est_texto}\n"
               f"🌡️ {fg_texto}\n"
               f"📉 {breadth_texto}\n"
               f"🌍 Macro: `{macro_imp}`"
               f"{gex_texto}"
               f"{opex_texto}"
               f"{periodo_texto}"
               f"{calendario_texto}"
               f"{tiburones_texto}")
        try:
            bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        except Exception:
            bot.send_message(TELEGRAM_CHAT_ID, msg.replace("*","").replace("`",""))
        pre_apertura_enviado["dia"] = ahora.date()
        print("  [PRE-APERTURA] ✅ Enviado")
    except Exception as e:
        print(f"  [PRE-APERTURA] Error: {e}")

resumen_dominical_enviado = {"semana": None}

def enviar_resumen_dominical():
    ahora = hora_ny()
    if ahora.weekday() != 6: return
    hora_et = ahora.hour * 60 + ahora.minute
    if not (20 * 60 <= hora_et <= 22 * 60): return
    semana_actual = ahora.isocalendar()[1]
    if resumen_dominical_enviado["semana"] == semana_actual: return
    print("  [DOMINICAL] Preparando resumen semanal...")
    try:
        obtener_cot_report()
        es_data       = descargar_futuros(period="5d", interval="1d")
        if not es_data.empty:
            close_es      = es_data["Close"]
            if hasattr(close_es, "columns"): close_es = close_es.iloc[:, 0]
            close_es      = close_es.squeeze()
            futuro_precio = float(close_es.iloc[-1])
            futuro_cambio_semana = float((close_es.iloc[-1] / close_es.iloc[0] - 1) * 100)                                    if len(close_es) >= 5 else 0.0
        else:
            futuro_precio = 0; futuro_cambio_semana = 0.0
        cot_sesgo  = cot_cache.get("sesgo", "N/D") if cot_cache.get("disponible") else "N/D"
        cot_neto   = cot_cache.get("neto_largo", 0)
        cot_fuente = cot_cache.get("fuente", "N/D")
        cot_fecha  = cot_cache.get("fecha_reporte", "N/D")
        # Mostrar longs/shorts de tiburones (Leveraged Funds) si son reales
        cot_detalle = ""
        if cot_fuente == "CFTC_REAL":
            longs   = cot_cache.get("longs", 0)
            shorts  = cot_cache.get("shorts", 0)
            am_l    = cot_cache.get("am_long", 0)
            am_s    = cot_cache.get("am_short", 0)
            am_neto = cot_cache.get("neto_am", 0)
            ratio_lf = (longs / shorts) if shorts else 0
            ratio_am = (am_l / am_s) if am_s else 0
            cot_detalle = (f"\n   🦈 Lev Funds — Long:{longs:,} | Short:{shorts:,} | Ratio:{ratio_lf:.2f}"
                           f"\n   🏛️ Asset Mgr — Long:{am_l:,} | Short:{am_s:,} | Ratio:{ratio_am:.2f} | Neto:{am_neto:+,}"
                           f"\n   📅 Fecha corte:{cot_fecha}")
        if not gex_niveles["disponible"]: _gex_fallback()
        gex_lunes = ""
        if gex_niveles["disponible"]:
            fuente_gex = gex_niveles.get("fuente", "EST")
            gex_lunes  = (f"\n⚡ GEX ({fuente_gex}) lunes:\n"
                         f"   Flip: `{gex_niveles['gamma_flip']}` | "
                         f"Call: `{gex_niveles['call_wall']}` | Put: `{gex_niveles['put_wall']}`")
        resultado_macro  = buscar_contexto_macro()
        sesgo_macro      = resultado_macro.get("sesgo", "N/D") if resultado_macro else "N/D"
        noticias_semana  = resultado_macro.get("noticias", []) if resultado_macro else []
        noticias_str     = "\n".join(f"  • {n}" for n in noticias_semana[:3])
        # ── Liquidez Fed + aprendizaje ────────────────────────
        obtener_liquidez_fed()
        liq_fed_txt = ""
        if fed_liquidez_cache["disponible"]:
            liq_fed_txt = (f"\n💧 Liquidez Fed neta: `${fed_liquidez_cache['neta']:,.0f}B` "
                           f"(Δ4sem: `{fed_liquidez_cache['cambio_4w']:+,.0f}B`) — {fed_liquidez_cache['tendencia']}")
        wr_txt = ""
        try:
            wr_txt = texto_win_rates()
        except Exception as e:
            print(f"  [JOURNAL] Error win rates: {e}")
        emoji_cot = "🟢" if "ALCISTA" in cot_sesgo else ("🔴" if "BAJISTA" in cot_sesgo else "⚪")
        msg = (f"📊 *RESUMEN DOMINICAL — Semana {semana_actual}*\n{'─'*28}\n"
               f"*Smart Money — Leveraged Funds (COT {cot_fuente}):*\n"
               f"{emoji_cot} Sesgo: `{cot_sesgo}` | Neto: `{cot_neto:+,}` contratos"
               f"{cot_detalle}\n{'─'*28}\n"
               f"*Futuros S&P 500:*\n"
               f"💵 Precio: `{futuro_precio:.0f}` | Semana: `{futuro_cambio_semana:+.2f}%`"
               f"{gex_lunes}\n{'─'*28}\n"
               f"*Noticias clave semana:*\n{noticias_str}\n{'─'*28}\n"
               f"*Sesgo institucional:* {sesgo_macro}"
               f"{liq_fed_txt}{wr_txt}\n"
               f"📅 Mercado abre mañana 9:30 ET")
        if len(msg) > 4096: msg = msg[:4090] + "..."
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        resumen_dominical_enviado["semana"] = semana_actual
        # Validar COT estimado vs real e iniciar nueva semana
        try:
            if cot_estimado_cache["disponible"]:
                validar_cot_estimado_vs_real()  # valida Y llama iniciar_acumulacion_cot
            else:
                iniciar_acumulacion_cot()  # Primera vez — solo iniciar
        except Exception as e:
            print(f"  [COT_EST] Error validando/iniciando: {e}")
        print("  [DOMINICAL] ✅ Resumen enviado")
    except Exception as e:
        print(f"  [DOMINICAL] Error: {e}")

ultimo_alerta_overnight = {"hora": None}

# Estado para reporte COT estimado de los miércoles
cot_est_reporte_enviado = {"semana": None}

def enviar_reporte_cot_estimado_miercoles():
    """
    DESACTIVADA (1-jul): el COT Estimado viejo fue jubilado y reemplazado
    por el Índice de Tiburones, así que este reporte semanal de los
    miércoles quedó sin fundamento (mostraba un neto estimado en contratos
    que ya no se calcula). Se devuelve de inmediato para que NO envíe nada.
    Se conserva el código viejo abajo (inalcanzable) por si algún día se
    reactiva con el índice como fuente.
    """
    return
    # --- código viejo deshabilitado (dependía del COT estimado jubilado) ---
    ahora = hora_ny()

    # Solo miércoles (weekday=2)
    if ahora.weekday() != 2:
        return

    hora_et = ahora.hour * 60 + ahora.minute

    # Ventana 9:00-9:15 ET
    if not (9 * 60 <= hora_et <= 9 * 60 + 15):
        return

    semana_actual = ahora.isocalendar()[1]
    if cot_est_reporte_enviado["semana"] == semana_actual:
        return

    if not cot_estimado_cache["disponible"]:
        print("  [COT_EST_MIÉRC] Sin datos estimados disponibles")
        return

    print("  [COT_EST_MIÉRC] Preparando reporte semanal...")
    try:
        # Ventana del corte que se valida ESTE viernes:
        # el corte CFTC fue AYER martes → ventana = miércoles anterior → ayer
        hoy              = ahora.date()
        martes_fin       = hoy - timedelta(days=1)        # martes de corte (ayer)
        miercoles_inicio = martes_fin - timedelta(days=6)  # miércoles anterior

        neto_est    = cot_estimado_cache.get("neto_estimado", 0)
        cot_base    = cot_estimado_cache.get("cot_base", 0)
        cambio_est  = cot_estimado_cache.get("cambio_estimado", 0)
        sesgo_est   = cot_estimado_cache.get("sesgo", "NEUTRAL")
        confianza   = cot_estimado_cache.get("confianza", 0.0)
        dias_acc    = cot_senales_semana.get("dias_acumulados", 0)
        comps       = cot_estimado_cache.get("componentes", {})

        # Emoji según sesgo
        emoji_sesgo = "🟢" if "ALCISTA" in sesgo_est else ("🔴" if "BAJISTA" in sesgo_est else "⚪")

        # Componentes del estimado
        comp_sweep  = comps.get("sweep", 0)
        comp_dp     = comps.get("dark_pool", 0)
        comp_oi     = comps.get("rotacion", 0)
        comp_pc     = comps.get("pc_semanal", 0)

        # Nivel de confianza en texto
        if confianza >= 0.9:
            conf_txt = "🏆 ALTA (modelo validado)"
        elif confianza >= 0.5:
            conf_txt = "⚠️ MEDIA (calibrando)"
        else:
            conf_txt = "🔬 BAJA (experimental)"

        msg = (
            f"📊 *COT ESTIMADO — {miercoles_inicio.strftime('%d %b')} al {martes_fin.strftime('%d %b %Y')}*\n"
            f"{'─'*28}\n"
            f"{emoji_sesgo} *Sesgo estimado:* `{sesgo_est.replace('_',' ')}`\n"
            f"📈 *Neto estimado:* `{neto_est:+,}` contratos\n"
            f"📐 *Cambio vs semana ant:* `{cambio_est:+,}`\n"
            f"🎯 *Base COT real:* `{cot_base:+,}`\n"
            f"{'─'*28}\n"
            f"*Componentes del estimado:*\n"
            f"  • Sweep neto: `{comp_sweep:+,}`\n"
            f"  • Dark Pool: `{comp_dp:+,}`\n"
            f"  • CME OI: `{comp_oi:+,}`\n"
            f"  • Put/Call: `{comp_pc:+,}`\n"
            f"{'─'*28}\n"
            f"📅 Días acumulados: `{dias_acc}`\n"
            f"🔬 Confianza: {conf_txt}\n"
            f"{'─'*28}\n"
            f"⚡ *COT real CFTC llega el viernes*\n"
            f"Ventaja: 3 días antes que el mercado retail."
        )

        if len(msg) > 4096: msg = msg[:4090] + "..."
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        cot_est_reporte_enviado["semana"] = semana_actual
        print(f"  [COT_EST_MIÉRC] ✅ Reporte enviado — {sesgo_est} | Neto:{neto_est:+,} | Confianza:{confianza:.0%}")

    except Exception as e:
        print(f"  [COT_EST_MIÉRC] Error: {e}")

def monitorear_overnight():
    try:
        ahora = hora_ny()
        if (ultimo_alerta_overnight["hora"] and
            (ahora - ultimo_alerta_overnight["hora"]).total_seconds() < 1800): return
        es_data       = descargar_futuros(period="2d", interval="5m")
        if es_data.empty or len(es_data) < 2: return
        close_col     = es_data["Close"]
        if hasattr(close_col, "columns"): close_col = close_col.iloc[:, 0]
        close_col     = close_col.squeeze()
        precio_actual = float(close_col.iloc[-1])
        precio_cierre = float(close_col.iloc[-13]) if len(close_col) >= 13 else float(close_col.iloc[0])
        cambio_pct    = (precio_actual / precio_cierre - 1) * 100
        if abs(cambio_pct) < 0.5: return
        emoji     = "📈" if cambio_pct > 0 else "📉"
        tipo      = "ALCISTA" if cambio_pct > 0 else "BAJISTA"
        gex_nivel = gex_niveles.get("gamma_flip", "N/D")
        wall      = gex_niveles.get("call_wall" if cambio_pct > 0 else "put_wall", "N/D")
        msg = (f"⚠️ *ALERTA OVERNIGHT — US500 v3.9*\n{'─'*28}\n"
               f"{emoji} Futuros /ES moviéndose `{cambio_pct:+.2f}%`\n"
               f"💵 Precio futuro: `{precio_actual:.0f}`\n"
               f"🎯 Gap probable mañana: *{tipo}*\n"
               f"⚡ GEX Flip: `{gex_nivel}` | {'Call Wall' if cambio_pct > 0 else 'Put Wall'}: `{wall}`\n"
               f"⏰ {ahora.strftime('%H:%M ET')} — Mercado cerrado")
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        ultimo_alerta_overnight["hora"] = ahora
        print(f"  [OVERNIGHT] ⚠️ Alerta enviada — futuros {cambio_pct:+.2f}%")
    except Exception as e:
        print(f"  [OVERNIGHT] Error: {e}")

# ================================================================
# === ANÁLISIS CON CLAUDE ========================================
# ================================================================

def analizar_con_claude(resultado):
    score     = resultado["score"]
    detalle   = resultado["detalle"]
    pen_rsi   = resultado["penalizacion_rsi"]
    pen_tend  = resultado["penalizacion_tendencia"]
    direccion = "ALCISTA" if score > 0 else "BAJISTA"

    notas = []
    if pen_rsi  != 0: notas.append(f"RSI {'sobrecomprado' if pen_rsi<0 else 'sobrevendido'} ({detalle['rsi']}) ajusta {pen_rsi:+d}pts")
    if pen_tend != 0: notas.append(f"Filtro tendencia: precio {'bajo' if pen_tend<0 else 'sobre'} EMA20 ajusta {pen_tend:+d}pts")
    liq = detalle["liquidez"]
    if liq["alerta"]: notas.append(f"⚠️ LIQUIDEZ {liq['nivel']}")
    notas_str = " | ".join(notas)

    vix_r = detalle["vix_ratio"]
    move  = detalle["move_index"]
    dxy   = detalle["dxy"]
    gex   = detalle["gex"]
    dp    = detalle["dark_pool"]
    tend  = detalle["tendencia"]

    vix_str  = f"VIX/VIX3M:{vix_r.get('ratio','N/D')}" + (" [FAT]" if vix_r.get("fatiga") else "") if vix_r.get("disponible") else "VIX/VIX3M:N/D"
    move_str = f"MOVE:{move.get('nivel','N/D')} {move.get('señal','')}" if move.get("disponible") else "MOVE:N/D"
    dxy_str  = f"DXY:{dxy.get('nivel','N/D')} {dxy.get('señal','')}" if dxy.get("disponible") else "DXY:N/D"
    gex_str  = f"GEX({gex.get('fuente','?')}) Flip:{gex.get('gamma_flip')} Call:{gex.get('call_wall')} Put:{gex.get('put_wall')} | {gex.get('señal','')}" if gex.get("disponible") else "GEX:N/D"
    dp_str   = f"DarkPool({dp.get('fuente','?')}):{dp.get('ratio',0):.1%} {dp.get('interpretacion','')}" if dp.get("disponible") else "DarkPool:N/D"
    tend_str = f"EMA20:{tend.get('ema20','?')} {'SOBRE' if tend.get('sobre_ema') else 'BAJO'}"
    if tend.get("tendencia_bajista_fuerte"): tend_str += " ⚠️TENDENCIA BAJISTA FUERTE"
    elif tend.get("minimos_bajistas"): tend_str += " ⚠️MINIMOS BAJISTAS"

    macro_impacto  = contexto_macro.get("impacto", "NEUTRAL")
    macro_resumen  = contexto_macro.get("resumen", "")
    macro_noticias = " | ".join(contexto_macro.get("noticias", []))
    macro_sesgo    = contexto_macro.get("sesgo", "")
    macro_hora     = contexto_macro.get("ultima_actualizacion")
    macro_hora_str = macro_hora.strftime("%H:%M ET") if macro_hora else "?"

    cot_d  = detalle.get("cot", {})
    mcl_d  = detalle.get("mcclellan", {})
    vvix_d = detalle.get("vvix", {})
    pc_d   = detalle.get("put_call", {})
    rot_d  = detalle.get("rotacion", {})
    br_d   = detalle.get("breadth", {})
    fg_d   = detalle.get("fear_greed", {})

    cot_fuente = cot_d.get("fuente", "?")
    cot_str  = f"COT({cot_fuente}):{cot_d.get('sesgo','N/D')} Neto:{cot_d.get('neto',0):+,}" if cot_d.get("disponible") else "COT:N/D"
    mcl_str  = f"McC:{mcl_d.get('oscilador','N/D')} {mcl_d.get('señal','')}" if mcl_d.get("disponible") else "McC:N/D"
    vvix_str = f"VVIX:{vvix_d.get('nivel','N/D')} {vvix_d.get('señal','')}" if vvix_d.get("disponible") else "VVIX:N/D"
    pc_str   = f"PC:{pc_d.get('ratio','N/D')} {pc_d.get('señal','')}" if pc_d.get("disponible") else "PC:N/D"
    rot_str  = f"Rotacion:{rot_d.get('señal','N/D')}" if rot_d.get("disponible") else "Rotacion:N/D"
    br_str   = f"Breadth:{br_d.get('verdes',0)}/11 {br_d.get('señal','')}" if br_d.get("disponible") else "Breadth:N/D"
    fg_str   = f"FearGreed:{fg_d.get('valor','N/D')} {fg_d.get('etiqueta','')}" if fg_d.get("disponible") else "FG:N/D"

    g0_d = detalle.get("gex_0dte", {})
    vc_d = detalle.get("vol_control", {})
    hy_d = detalle.get("credito_hyg", {})
    vw_d = detalle.get("vwap", {})
    g0_str = f"GEX0DTE:{g0_d.get('señal','')}" if g0_d.get("disponible") else "GEX0DTE:N/D"
    vc_str = f"VolCtl:{vc_d.get('señal','')}"  if vc_d.get("disponible") else "VolCtl:N/D"
    hy_str = f"HYG:{hy_d.get('señal','')}"     if hy_d.get("disponible") else "HYG:N/D"
    vw_str = f"VWAP:{vw_d.get('señal','')}"    if vw_d.get("disponible") else "VWAP:N/D"

    prompt = f"""Analista cuantitativo US500 intradía v3.9. Score:{score}/10 {direccion}.{' NOTAS: '+notas_str if notas_str else ''}

MACRO ({macro_hora_str}): {macro_impacto} | {macro_noticias} | {macro_resumen} | Sesgo:{macro_sesgo}

TÉCNICO: Precio:{detalle['precio']} RSI:{detalle['rsi']} VIX:{detalle['vix_nivel']}
{vix_str} | {move_str} | {dxy_str} | {tend_str}
{gex_str}
{dp_str}

SEÑALES INSTITUCIONALES v3.9:
{cot_str} | {mcl_str} | {vvix_str}
{pc_str} | {rot_str} | {br_str} | {fg_str}

AVANZADO v4.0:
{g0_str} | {vc_str}
{hy_str} | {vw_str}

Rango:{detalle['posicion_rango']['posicion_pct']}% (max:{detalle['posicion_rango']['max_dia']} min:{detalle['posicion_rango']['min_dia']})

Responde en español en EXACTAMENTE 5 líneas cortas, sin asteriscos, sin títulos:
1. Probabilidad {direccion} 5-15min: XX% — razón principal en 5 palabras
2. Señal más fuerte: [nombre] — qué dice en 5 palabras
3. Macro vs institucional: alineados o contradicción en 5 palabras
4. Niveles: Flip:{gex.get('gamma_flip','N/D')} | Stop recomendado | Target recomendado
5. Acción: una frase corta y directa"""

    try:
        respuesta = claude_client.messages.create(
            model=MODELO_SEÑALES, max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        return respuesta.content[0].text
    except Exception as e:
        if "429" in str(e) or "rate_limit" in str(e):
            print("  [CLAUDE] Rate limit — reintentando en 15s...")
            time.sleep(15)
            try:
                respuesta = claude_client.messages.create(
                    model=MODELO_SEÑALES, max_tokens=1000,
                    messages=[{"role": "user", "content": prompt}]
                )
                return respuesta.content[0].text
            except Exception as e2: return f"[Error Claude: {e2}]"
        return f"[Error Claude: {e}]"

# ================================================================
# === ALERTAS TELEGRAM ===========================================
# ================================================================

# ── Alerta de contradicción institucional: ELIMINADA (5-jul) ──────
# Toda su lógica dependía del dark pool proxy (yfinance), que está roto
# (daba "MOMENTUM 20.0%" clavado y contaminaba el pre-market con falsas
# "CONTRADICCIONES"). El dark pool ya se neutralizó del score y de la
# señal, así que la alerta quedó sin fundamento y se retira por completo
# (función detectora, envío y llamada en el loop). Si algún día entra
# dark pool REAL (FINRA ATS o servicio pago), se reconstruye desde cero
# con esa fuente confiable.

# Flag de "primer ciclo del día" — dispara la acumulación diaria de
# señales para el COT estimado una sola vez por jornada.
ciclo_diario_cache = {"dia": None}

def barra_score(score):
    abs_s = abs(score)
    return f"[{'█'*abs_s}{'░'*(10-abs_s)}] {'+' if score>0 else ''}{score}/10"

def enviar_alerta_score(resultado, analisis_claude):
    score    = resultado["score"]
    detalle  = resultado["detalle"]
    comps    = resultado["componentes"]
    pen_rsi  = resultado["penalizacion_rsi"]
    pen_tend = resultado["penalizacion_tendencia"]
    ahora    = hora_ny().strftime("%H:%M ET")
    emoji_dir = "🟢" if score > 0 else "🔴"
    dir_texto = "ALCISTA" if score > 0 else "BAJISTA"

    senales_activas = [n.replace("_"," ").upper() for n, v in comps.items() if v != 0]
    senales_str     = "\n".join(f"  • {s}" for s in senales_activas) or "  • Ninguna"

    pen_lines = ""
    if pen_rsi  != 0: pen_lines += f"\n⚠️ RSI extremo ({detalle['rsi']}) — ajustado {pen_rsi:+d} pts"
    if pen_tend != 0:
        tend = detalle["tendencia"]
        if tend.get("tendencia_bajista_fuerte"):
            pen_lines += f"\n📐 TENDENCIA BAJISTA FUERTE — ajustado {pen_tend:+d} pts"
        elif tend.get("minimos_bajistas"):
            pen_lines += f"\n📐 Mínimos bajistas bajo EMA20 — ajustado {pen_tend:+d} pts"
        else:
            pen_lines += f"\n📐 Precio {'bajo' if pen_tend<0 else 'sobre'} EMA20 ({tend.get('ema20','?')}) — ajustado {pen_tend:+d} pts"

    vix_r    = detalle["vix_ratio"]
    vix_str  = f"\n📐 VIX/VIX3M: `{vix_r['ratio']}`" + (" ⚠️fat" if vix_r.get("fatiga") else "") if vix_r.get("disponible") else ""
    move     = detalle["move_index"]
    move_str = f"\n📈 MOVE: `{move['nivel']}` ({move['cambio_pct']:+.1f}%) — {move['señal']}" if move.get("disponible") else ""
    dxy      = detalle["dxy"]
    dxy_str  = f"\n💵 DXY: `{dxy['nivel']}` ({dxy['cambio_pct']:+.3f}%) — {dxy['señal']}" if dxy.get("disponible") else ""

    gex     = detalle["gex"]
    gex_str = ""
    if gex.get("disponible"):
        fuente_gex = gex.get("fuente", "EST")
        gex_str    = f"\n⚡ GEX({fuente_gex}): Flip:`{gex['gamma_flip']}` | Call:`{gex['call_wall']}` | Put:`{gex['put_wall']}`"

    dp     = detalle["dark_pool"]
    # Dark pool DESACTIVADO (24-jun): proxy roto, ya no se muestra ni cuenta
    # en el score. Se reactivará con dark pool REAL (servicio pago).
    dp_str = ""

    liq     = detalle["liquidez"]
    liq_str = f"\n🌊 Liquidez: `{liq['nivel']}`" + (" ⚠️" if liq["alerta"] else "")

    extras = []
    for nom, dd in [("0DTE", detalle.get("gex_0dte", {})),
                    ("VolCtl", detalle.get("vol_control", {})),
                    ("HYG", detalle.get("credito_hyg", {})),
                    ("VWAP", detalle.get("vwap", {}))]:
        if dd.get("disponible") and dd.get("score", 0) != 0:
            extras.append(f"{nom}:{dd['score']:+d}")
    inst2_str = ("\n⚙️ Avanzado: `" + " | ".join(extras) + "`") if extras else ""

    macro_impacto = contexto_macro.get("impacto", "NEUTRAL")
    macro_emoji   = {"ALCISTA_FUERTE":"🟢🟢","ALCISTA_MODERADO":"🟢","NEUTRAL":"⚪",
                     "BAJISTA_MODERADO":"🔴","BAJISTA_FUERTE":"🔴🔴"}.get(macro_impacto, "⚪")

    # Recalcular el Índice de Tiburones para mostrarlo CON los sweeps del
    # día ya cargados (resuelve el timing: en la apertura el índice salía
    # sin sweeps; en las señales del día ya los tiene).
    tiburones_str = ""
    try:
        calcular_indice_tiburones()
        tib = texto_indice_tiburones()
        if tib:
            tiburones_str = "\n" + tib
    except Exception:
        pass

    msg = (f"{emoji_dir} *SEÑAL {dir_texto} — US500*\n{'─'*28}\n"
           f"🕐 Hora: {ahora}\n💵 Precio: `{detalle['precio']:.2f}`\n"
           f"📊 Score: `{barra_score(score)}`\n"
           f"📉 RSI: `{detalle['rsi']}` | VIX: `{detalle['vix_nivel']}`"
           f"{vix_str}{move_str}{dxy_str}{gex_str}{dp_str}{liq_str}{inst2_str}\n"
           f"📍 Rango: `{detalle['posicion_rango']['posicion_pct']}%`\n"
           f"🌍 Macro: {macro_emoji} `{macro_impacto.replace('_',' ')}`{pen_lines}\n"
           f"{'─'*28}\n*Señales activas:*\n{senales_str}"
           f"{tiburones_str}")
    if len(msg) > 4096: msg = msg[:4090] + "..."
    try: bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
    except:
        try: bot.send_message(TELEGRAM_CHAT_ID, msg.replace("*","").replace("`","").replace("_",""))
        except Exception as e: print(f"  [ALERTA] Telegram error: {e}")

    analisis_msg = f"📋 *Análisis:*\n\n{analisis_claude}"
    if len(analisis_msg) > 4096: analisis_msg = analisis_msg[:4090] + "..."
    try: bot.send_message(TELEGRAM_CHAT_ID, analisis_msg, parse_mode="Markdown")
    except:
        try: bot.send_message(TELEGRAM_CHAT_ID, analisis_msg.replace("*","").replace("`",""))
        except Exception as e: print(f"  [ANALISIS] Telegram error: {e}")

# ================================================================
# === DETECTOR DE AGOTAMIENTO ====================================
# ================================================================

estado_agotamiento = {
    "activo": False, "direccion": None, "precio_entrada": None,
    "score_entrada": None, "historial_delta": [], "rsi_entrada": None, "alerta_enviada": False,
}

def activar_detector_agotamiento(resultado):
    estado_agotamiento.update({
        "activo": True, "direccion": "ALCISTA" if resultado["score"] > 0 else "BAJISTA",
        "precio_entrada": resultado["detalle"]["precio"],
        "score_entrada":  resultado["score"],
        "rsi_entrada":    resultado["detalle"]["rsi"],
        "historial_delta": [], "alerta_enviada": False,
    })
    print(f"  [AGOT] Detector activado — {estado_agotamiento['direccion']}")

def resetear_detector_agotamiento():
    estado_agotamiento.update({"activo": False, "direccion": None,
                                "alerta_enviada": False, "historial_delta": []})

def evaluar_agotamiento(resultado):
    if not estado_agotamiento["activo"] or estado_agotamiento["alerta_enviada"]: return False
    detalle    = resultado["detalle"]
    direccion  = estado_agotamiento["direccion"]
    condiciones = 0
    delta_actual = detalle["delta_volumen"]["ratio"]
    estado_agotamiento["historial_delta"].append(delta_actual)
    if len(estado_agotamiento["historial_delta"]) > 5: estado_agotamiento["historial_delta"].pop(0)
    if len(estado_agotamiento["historial_delta"]) >= 3:
        hist = estado_agotamiento["historial_delta"][-3:]
        if (direccion == "ALCISTA" and all(h < 0 for h in hist)) or \
           (direccion == "BAJISTA" and all(h > 0 for h in hist)):
            condiciones += 1
    rsi_actual = detalle["rsi"]; precio_act = detalle["precio"]
    precio_ent = estado_agotamiento["precio_entrada"]; rsi_ent = estado_agotamiento["rsi_entrada"]
    if   direccion == "ALCISTA" and precio_act > precio_ent and rsi_actual < rsi_ent - 5: condiciones += 1
    elif direccion == "BAJISTA" and precio_act < precio_ent and rsi_actual > rsi_ent + 5: condiciones += 1
    move = detalle["move_index"]
    if move.get("disponible"):
        if (direccion == "ALCISTA" and move["score"] < -1) or (direccion == "BAJISTA" and move["score"] > 1):
            condiciones += 1
    dxy = detalle["dxy"]
    if dxy.get("disponible"):
        if (direccion == "ALCISTA" and dxy["score"] < -1) or (direccion == "BAJISTA" and dxy["score"] > 1):
            condiciones += 1
    absorc = detalle["absorcion"]
    if (direccion == "ALCISTA" and absorc["tipo"] == "DISTRIBUCION") or \
       (direccion == "BAJISTA" and absorc["tipo"] == "ACUMULACION"):
        condiciones += 1
    print(f"  [AGOT] Condiciones: {condiciones}/{AGOTAMIENTO_CONDICIONES}")
    return condiciones >= AGOTAMIENTO_CONDICIONES

def enviar_alerta_agotamiento(resultado):
    detalle    = resultado["detalle"]
    direccion  = estado_agotamiento["direccion"]
    precio_ent = estado_agotamiento["precio_entrada"]
    precio_act = detalle["precio"]
    ganancia   = precio_act - precio_ent if direccion == "ALCISTA" else precio_ent - precio_act
    ahora      = hora_ny().strftime("%H:%M ET")
    msg = (f"⚠️ *AGOTAMIENTO {direccion} — US500*\n{'─'*28}\n"
           f"🕐 Hora: {ahora}\n💵 Precio actual: `{precio_act:.2f}`\n"
           f"📍 Entrada señal: `{precio_ent:.2f}`\n"
           f"{'✅' if ganancia > 0 else '⚠️'} Movimiento: `{ganancia:+.2f}` puntos\n{'─'*28}\n"
           f"💡 La tendencia {direccion} está perdiendo fuerza.\n"
           f"Considera tomar ganancias parciales o ajustar stop.")
    if len(msg) > 4096: msg = msg[:4090] + "..."
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        estado_agotamiento["alerta_enviada"] = True
    except:
        try:
            bot.send_message(TELEGRAM_CHAT_ID, msg.replace("*","").replace("`",""))
            estado_agotamiento["alerta_enviada"] = True
        except Exception as e: print(f"  [AGOT] Telegram error: {e}")

# ================================================================
# === COOLDOWN INTELIGENTE =======================================
# ================================================================

class EstadoCooldown:
    def __init__(self):
        self.ultima_alcista = None
        self.ultima_bajista = None

    def _snapshot(self, resultado):
        comps = resultado["componentes"]
        return {"score": resultado["score"], "precio": resultado["detalle"]["precio"],
                "senales_activas": frozenset(k for k, v in comps.items() if v != 0),
                "hora": hora_ny()}

    def _es_nuevo(self, snap_actual, ultima):
        if ultima is None: return True, "primera señal"
        mins = (hora_ny() - ultima["hora"]).total_seconds() / 60
        if mins < TIEMPO_MIN_ALERTAS: return False, f"solo {mins:.0f} min"
        if snap_actual["senales_activas"] != ultima["senales_activas"]:
            return True, "señales cambiaron"
        if ultima["precio"] > 0:
            mov = abs(snap_actual["precio"] - ultima["precio"]) / ultima["precio"]
            if mov >= UMBRAL_PRECIO_CAMBIO: return True, f"precio movió {mov*100:.2f}%"
        if abs(snap_actual["score"] - ultima["score"]) >= SALTO_SCORE_MINIMO:
            return True, f"score saltó {ultima['score']}→{snap_actual['score']}"
        return False, "mismo contexto"

    def debe_alertar_alcista(self, resultado):
        return self._es_nuevo(self._snapshot(resultado), self.ultima_alcista)

    def debe_alertar_bajista(self, resultado):
        return self._es_nuevo(self._snapshot(resultado), self.ultima_bajista)

    def registrar_alcista(self, resultado): self.ultima_alcista = self._snapshot(resultado)
    def registrar_bajista(self, resultado): self.ultima_bajista = self._snapshot(resultado)

# ================================================================
# === SISTEMA DE TRACKING DE POSICIONES ==========================
# ================================================================

posicion_activa = {
    "tipo": None, "precio": None, "activa": False,
    "alerta_distribucion_enviada": False,
}

def evaluar_distribucion_posicion(resultado):
    if not posicion_activa["activa"]: return False
    detalle    = resultado["detalle"]
    dp         = detalle.get("dark_pool", {})
    rsi_val    = detalle.get("rsi", 50)
    tend       = detalle.get("tendencia", {})
    dp_distrib = dp.get("interpretacion", "") in ["DISTRIBUCION EN DARK POOL", "DISTRIBUCION INSTITUCIONAL"]
    rsi_cayendo = rsi_val < 45
    bajo_ema    = not tend.get("sobre_ema", True)
    if posicion_activa["tipo"] == "long":
        return sum([dp_distrib, rsi_cayendo, bajo_ema]) >= 2
    elif posicion_activa["tipo"] == "short":
        dp_acum      = dp.get("interpretacion", "") in ["ACUMULACION INSTITUCIONAL OCULTA", "ACUMULACION INSTITUCIONAL"]
        rsi_subiendo = rsi_val > 55
        sobre_ema    = tend.get("sobre_ema", False)
        return sum([dp_acum, rsi_subiendo, sobre_ema]) >= 2
    return False

def enviar_alerta_distribucion(resultado):
    detalle  = resultado["detalle"]
    precio   = detalle.get("precio", 0)
    dp       = detalle.get("dark_pool", {})
    rsi_val  = detalle.get("rsi", 50)
    tipo     = posicion_activa["tipo"].upper()
    entrada  = posicion_activa["precio"]
    pnl      = (precio - entrada) if tipo == "LONG" else (entrada - precio)
    emoji    = "🟢" if tipo == "LONG" else "🔴"
    msg = (f"⚠️ *DISTRIBUCIÓN DETECTADA — POSICIÓN EN RIESGO*\n{'─'*28}\n"
           f"{emoji} Posición: `{tipo}` desde `{entrada}`\n"
           f"💵 Precio actual: `{precio}` | P&L: `{pnl:+.1f}pts`\n{'─'*28}\n"
           f"🏦 Dark Pool: `{dp.get('interpretacion','N/D')}`\n"
           f"📉 RSI: `{rsi_val}` — momentum deteriorándose\n"
           f"📊 EMA20: `{'por debajo' if tipo=='LONG' else 'por encima'}`\n{'─'*28}\n"
           f"⚠️ *Considera cerrar o ajustar tu stop.*")
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        print("  [POSICIÓN] ⚠️ Alerta distribución enviada")
    except Exception as e:
        print(f"  [POSICIÓN] Error: {e}")

@bot.message_handler(commands=["long"])
def cmd_long(message):
    try:
        partes = message.text.split()
        precio = float(partes[1]) if len(partes) > 1 else None
        if not precio:
            bot.reply_to(message, "Uso: /long 7575"); return
        posicion_activa.update({"tipo": "long", "precio": precio,
                                "activa": True, "alerta_distribucion_enviada": False})
        bot.reply_to(message, f"✅ Posición LONG registrada en {precio}\nTe avisaré si las condiciones se deterioran.")
        print(f"  [POSICIÓN] Long registrado en {precio}")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

@bot.message_handler(commands=["short"])
def cmd_short(message):
    try:
        partes = message.text.split()
        precio = float(partes[1]) if len(partes) > 1 else None
        if not precio:
            bot.reply_to(message, "Uso: /short 7575"); return
        posicion_activa.update({"tipo": "short", "precio": precio,
                                "activa": True, "alerta_distribucion_enviada": False})
        bot.reply_to(message, f"✅ Posición SHORT registrada en {precio}\nTe avisaré si las condiciones se deterioran.")
        print(f"  [POSICIÓN] Short registrado en {precio}")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

@bot.message_handler(commands=["cerrar"])
def cmd_cerrar(message):
    tipo   = posicion_activa.get("tipo", "N/D")
    precio = posicion_activa.get("precio", 0)
    posicion_activa.update({"tipo": None, "precio": None,
                            "activa": False, "alerta_distribucion_enviada": False})
    bot.reply_to(message, f"✅ Posición {tipo.upper() if tipo else ''} desde {precio} cerrada.\nMonitoreo desactivado.")
    print("  [POSICIÓN] Posición cerrada manualmente")

@bot.message_handler(commands=["posicion"])
def cmd_posicion(message):
    if not posicion_activa["activa"]:
        bot.reply_to(message, "No hay posición activa.\nUsa /long <precio> o /short <precio>")
    else:
        bot.reply_to(message, f"Posición activa: {posicion_activa['tipo'].upper()} desde {posicion_activa['precio']}")

# ================================================================
# === LOOP PRINCIPAL — PRODUCCIÓN 24/7 ===========================
# ================================================================

estado_mercado_enviado = False
contador_ciclos        = 0
cooldown               = EstadoCooldown()
cot_viernes_procesado  = {"dia": None}   # flag COT real del viernes

def iniciar_polling():
    """Polling con reconexión automática — se reinicia solo si falla."""
    intentos = 0
    while True:
        try:
            intentos += 1
            print(f"  [POLLING] Iniciando (intento #{intentos})...")
            bot.polling(none_stop=True, interval=2, timeout=20)
        except Exception as e:
            print(f"  [POLLING] Error: {e} — reconectando en 15s...")
            time.sleep(15)
        except KeyboardInterrupt:
            print("  [POLLING] Detenido manualmente")
            break

polling_thread = threading.Thread(target=iniciar_polling, daemon=True)
polling_thread.start()
# Servidor HTTP — sirve el weekly al dashboard sin pasar por Sheets
http_thread = threading.Thread(target=iniciar_servidor_http, daemon=True)
http_thread.start()
print("  [TELEGRAM] Comandos activos: /long /short /cerrar /posicion")

print("=" * 60)
print("   US500 MONITOR v3.9 — VISION INSTITUCIONAL COMPLETA")
print("   MEJORAS 31/05/2026: COT REAL + GEX REAL + DP GRANULAR")
print("=" * 60)
print("  COT: CFTC real | GEX: option_chain() | DP: bloques 5min")
print(f"  Umbral: ±{UMBRAL_SCORE}/10 | Min entre alertas: {TIEMPO_MIN_ALERTAS} min")
print("=" * 60)

print("  [INIT] Cargando COT Report REAL (CFTC)...")
obtener_cot_report()
print("  [INIT] Calculando McClellan Oscillator...")
calcular_mcclellan()
print("  [INIT] Obteniendo Put/Call Ratio...")
obtener_put_call_ratio()
print("  [INIT] Cargando journal de señales desde GitHub...")
cargar_journal_github()
print("  [INIT] Restaurando estado COT Estimado desde GitHub...")
cargar_estado_cot_github()
print("  [INIT] Restaurando trayectoria weekly...")
cargar_weekly_github()

while True:
    try:
        inicio_ciclo = time.time()
        ahora_ny     = hora_ny()
        abierto      = mercado_abierto()
        minutos      = minutos_desde_apertura()

        # ── Mercado cerrado ──────────────────────────────────
        if not abierto:
            if estado_mercado_enviado:
                spy_temp = descargar_datos()
                if spy_temp:
                    precio_final = float(spy_temp["close"]["^GSPC"].iloc[-1])
                    despedida = proximo_dia_habil_texto()
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"🔕 *MERCADO CERRADO — US500 v3.9*\n"
                            f"US500 final: `{precio_final:.2f}`\nNos vemos {despedida}. 🌙",
                            parse_mode="Markdown")
                    except:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"MERCADO CERRADO\nUS500 final: {precio_final:.2f}")
                estado_mercado_enviado = False
                resetear_detector_agotamiento()
                gex_niveles["disponible"] = False
                gex_niveles["ultima_actualizacion"] = None
                detector_rango["activo"] = False
                detector_rango["señales_suspendidas"] = False
                # Persistir journal de señales del día en GitHub
                guardar_journal_github()
                # Persistir estado COT (acumulación del día)
                guardar_estado_cot_github()

# ── Weekly direccional — también con mercado cerrado ──
            # El OI ya está publicado del cierre anterior, así que el
            # fin de semana ya se puede ver la foto de la semana que
            # arranca. No hace falta esperar a la apertura.
            try:
                calcular_weekly()
            except Exception as e:
                print(f"  [WEEKLY] Error (cerrado): {e}")            
            enviar_resumen_dominical()
            monitorear_overnight()
            # Pre-apertura — la ventana 8:45-9:29 ET cae con mercado CERRADO,
            # por eso debe ir aquí dentro (antes del continue), no después.
            enviar_pre_apertura()
            # Reporte COT Estimado — solo miércoles a las 9:00 ET
            enviar_reporte_cot_estimado_miercoles()
            # ── COT Real CFTC — viernes al cierre (4:00 PM ET = 2:00 PM HN) ──
            # CFTC LIBERA a las 3:30 PM ET pero el archivo descargable recién
            # aparece en el sitio web a las 6:30 PM ET. Intentar a las 8:30 PM ET
            # (= 6:30 PM HN) da 2h de colchón tras la publicación. Antes el bot
            # buscaba a las 3:30 PM ET y no encontraba nada (archivo aún no subido),
            # reintentando cada minuto → causaba el spam de mensajes COT.
            if ahora_ny.weekday() == 4:  # viernes
                hora_min_vie = ahora_ny.hour * 60 + ahora_ny.minute
                if (hora_min_vie >= 20 * 60 + 30 and   # 8:30 PM ET = 6:30 PM HN
                        cot_viernes_procesado["dia"] != ahora_ny.date() and
                        not es_festivo_hoy()):   # festivo federal → CFTC no publica
                    print("  [COT_VIERNES] 📥 Descargando COT real CFTC...")
                    if obtener_cot_report():
                        cot_viernes_procesado["dia"] = ahora_ny.date()
                        print("  [COT_VIERNES] ✅ COT real actualizado")
                        # El COT estimado viejo fue jubilado, así que ya NO se
                        # bifurca: siempre se guarda el estado y se ENVÍA el
                        # mensaje del COT real a Telegram. (Antes el mensaje
                        # estaba en un else que no se ejecutaba → descargaba
                        # pero no enviaba.)
                        guardar_estado_cot_github()
                        try:
                            sesgo_v = cot_cache.get("sesgo", "N/D")
                            neto_v  = cot_cache.get("neto_largo", 0) or 0
                            long_v  = cot_cache.get("longs", 0) or 0
                            short_v = cot_cache.get("shorts", 0) or 0
                            am_l    = cot_cache.get("am_long", 0) or 0
                            am_s    = cot_cache.get("am_short", 0) or 0
                            am_v    = cot_cache.get("neto_am", 0) or 0
                            fecha_v = cot_cache.get("fecha_reporte", "N/D")
                            emoji_v = "🟢" if "ALCISTA" in sesgo_v else ("🔴" if "BAJISTA" in sesgo_v else "⚪")
                            ratio_v  = (long_v / short_v) if short_v else 0
                            ratio_am = (am_l / am_s) if am_s else 0
                            bot.send_message(TELEGRAM_CHAT_ID,
                                f"📊 *COT REAL CFTC — VIERNES*\n"
                                f"📄 Contrato: `{cot_cache.get('contrato','N/D')}`\n"
                                f"────────────────────────────\n"
                                f"🦈 *Tiburones (Leveraged Funds):*\n"
                                f"{emoji_v} Sesgo: `{sesgo_v.replace('_',' ')}`\n"
                                f"📈 Neto: `{neto_v:+,}` contratos\n"
                                f"   Long: `{long_v:,}` | Short: `{short_v:,}` | Ratio: `{ratio_v:.2f}`\n"
                                f"────────────────────────────\n"
                                f"🏛️ *Asset Managers (institucional):*\n"
                                f"   Neto: `{am_v:+,}`\n"
                                f"   Long: `{am_l:,}` | Short: `{am_s:,}` | Ratio: `{ratio_am:.2f}`\n"
                                f"📅 Fecha corte: `{fecha_v}`\n"
                                f"────────────────────────────\n"
                                f"🦈 Posicionamiento institucional real (CFTC).",
                                parse_mode="Markdown")
                            print("  [COT_VIERNES] 📊 COT real enviado")
                        except Exception as e:
                            print(f"  [COT_VIERNES] Error enviando: {e}")
            elapsed = time.time() - inicio_ciclo
            time.sleep(max(0, 60 - elapsed))
            contador_ciclos += 1
            continue

        # ── Macro post-evento ─────────────────────────────────
        evento_reciente = detectar_evento_reciente()
        if evento_reciente:
            procesar_macro_post_evento(evento_reciente)
        elif necesita_actualizar_macro():
            actualizar_contexto_macro(enviar_telegram=True)

        # ── Descargar datos ───────────────────────────────────
        datos = descargar_datos()
        if datos is None:
            print(f"[{ahora_ny.strftime('%H:%M')}] ⚠️ Sin datos")
            elapsed = time.time() - inicio_ciclo
            time.sleep(max(0, 60 - elapsed))
            contador_ciclos += 1
            continue

        # Precio principal desde Tradier; yfinance solo como respaldo.
        spy_precio = precio_us500_tradier()
        if spy_precio is None:
            spy_precio = float(datos["close"]["^GSPC"].iloc[-1])
            print("  [PRECIO] ⚠️ Tradier no respondió — usando yfinance")
        vix_precio = float(datos["close"]["^VIX"].iloc[-1])

        # ── Apertura del mercado ──────────────────────────────
        if not estado_mercado_enviado:
            print("  [INIT] Obteniendo niveles GEX REAL...")
            obtener_gex()
            print("  [INIT] Obteniendo GEX 0DTE...")
            obtener_gex_0dte()
            print("  [INIT] Obteniendo Dark Pool granular...")
            obtener_dark_pool()
            print("  [INIT] Calculando Breadth sectores...")
            calcular_breadth_sectores()
            print("  [INIT] Actualizando McClellan...")
            calcular_mcclellan()
            print("  [INIT] Obteniendo Put/Call ratio...")
            obtener_put_call_ratio()

            macro_str = contexto_macro.get("impacto", "calculando...")

            # Cada bloque protegido independientemente
            gex_msg = ""
            try:
                if gex_niveles["disponible"]:
                    fuente_gex = str(gex_niveles.get("fuente", "?"))
                    flip  = str(gex_niveles.get("gamma_flip", "N/D"))
                    call  = str(gex_niveles.get("call_wall",  "N/D"))
                    put   = str(gex_niveles.get("put_wall",   "N/D"))
                    gex_msg = f"\n⚡ GEX({fuente_gex}): Flip:`{flip}` | Call:`{call}` | Put:`{put}`"
            except Exception as eg: print(f"  [APERTURA] gex_msg error: {eg}")

            breadth_msg = ""
            try:
                if breadth_cache["disponible"]:
                    breadth_msg = f"\n📊 Breadth: `{int(breadth_cache['verdes'])}/11` sectores alcistas"
            except Exception as eb: print(f"  [APERTURA] breadth_msg error: {eb}")

            cot_msg = ""
            try:
                if cot_cache["disponible"]:
                    sesgo_cot  = str(cot_cache.get("sesgo", "N/D"))
                    neto_cot   = int(cot_cache.get("neto_largo", 0))
                    fuente_cot = str(cot_cache.get("fuente", "?"))
                    emoji_cot  = "🟢" if "ALCISTA" in sesgo_cot else ("🔴" if "BAJISTA" in sesgo_cot else "⚪")
                    cot_msg    = f"\n{emoji_cot} COT({fuente_cot}): `{sesgo_cot}` ({neto_cot:+,})"
            except Exception as ec: print(f"  [APERTURA] cot_msg error: {ec}")

            dp_msg = ""
            try:
                if dark_pool_cache["disponible"]:
                    tend_dp   = str(dark_pool_cache.get("tendencia", "N/D"))
                    fuente_dp = str(dark_pool_cache.get("fuente", "?"))
                    dp_msg    = f"\n🏦 Dark Pool({fuente_dp}): `{tend_dp}`"
            except Exception as ed: print(f"  [APERTURA] dp_msg error: {ed}")

            try:
                msg_apertura = (f"🔔 *MERCADO ABIERTO — US500 v3.9*\n"
                               f"US500: `{spy_precio:.2f}` | VIX: `{vix_precio:.2f}`\n"
                               f"Macro: `{macro_str}`"
                               f"{gex_msg}{breadth_msg}{cot_msg}{dp_msg}\n"
                               f"Sistema v3.9 activo. Ciclo: 1 min.")
                bot.send_message(TELEGRAM_CHAT_ID, msg_apertura, parse_mode="Markdown")
                print("  [APERTURA] ✅ Mensaje completo enviado")
            except Exception as e:
                print(f"  [APERTURA] Error Markdown: {e}")
                try:
                    fallback = (f"MERCADO ABIERTO US500 v3.9\n"
                               f"US500: {spy_precio:.2f} | VIX: {vix_precio:.2f}\n"
                               f"Macro: {macro_str}\n"
                               f"GEX: {gex_msg.replace('`','').replace('*','').strip() if gex_msg else 'N/D'}")
                    bot.send_message(TELEGRAM_CHAT_ID, fallback)
                except: pass

            # ── Cheat-sheet del régimen de gamma del día ─────────
            try:
                gamma_pos, fuente_reg = _regimen_gamma(vix_precio)
                neto_0dte = gex_0dte_cache.get("neto") if gex_0dte_cache.get("disponible") else None
                neto_txt  = f"{neto_0dte:+,.0f}" if neto_0dte is not None else "N/D"
                cw = gex_niveles.get("call_wall", "N/D")
                pw = gex_niveles.get("put_wall", "N/D")
                if gamma_pos:
                    msg_regimen = (
                        f"🟢 *RÉGIMEN DEL DÍA: GAMMA POSITIVA*\n"
                        f"GEX 0DTE neto: `{neto_txt}` (fuente: {fuente_reg})\n"
                        f"────────────────────────────\n"
                        f"📌 Mercado *PEGAJOSO* — dealers amortiguan\n"
                        f"Esperar *RANGO*, movimientos contenidos.\n"
                        f"────────────────────────────\n"
                        f"🟢 Call Wall `{cw}` → *resistencia* (rebota abajo)\n"
                        f"🔴 Put Wall `{pw}` → *soporte* (rebota arriba)\n"
                        f"🎯 Plan: operar *rebotes* en los walls.\n"
                        f"El precio tiende a quedar atrapado entre ellos."
                    )
                else:
                    msg_regimen = (
                        f"🔴 *RÉGIMEN DEL DÍA: GAMMA NEGATIVA*\n"
                        f"GEX 0DTE neto: `{neto_txt}` (fuente: {fuente_reg})\n"
                        f"────────────────────────────\n"
                        f"📌 Mercado *RESBALOSO* — dealers amplifican\n"
                        f"Esperar *TENDENCIA*, movimientos explosivos.\n"
                        f"────────────────────────────\n"
                        f"🟢 Call Wall `{cw}` → si lo supera, *acelera al alza* 🚀\n"
                        f"🔴 Put Wall `{pw}` → si lo pierde, *acelera la caída* ⚠️\n"
                        f"🎯 Plan: operar *rupturas*, no rebotes.\n"
                        f"⚡ Cuidado: las caídas se retroalimentan."
                    )
                # Añadir OI en los muros si hay datos
                try:
                    msg_regimen += texto_oi_walls()
                except Exception:
                    pass
                # Añadir Índice de Tiburones (sesgo institucional por huellas)
                try:
                    calcular_indice_tiburones()
                    msg_regimen += texto_indice_tiburones()
                except Exception as e:
                    print(f"  [TIBURONES] Error en régimen: {e}")
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, msg_regimen, parse_mode="Markdown")
                except Exception:
                    bot.send_message(TELEGRAM_CHAT_ID,
                                     msg_regimen.replace("*", "").replace("`", ""))
                print(f"  [REGIMEN] ✅ Cheat-sheet enviado — gamma {'POSITIVA' if gamma_pos else 'NEGATIVA'} ({fuente_reg})")
            except Exception as e:
                print(f"  [REGIMEN] Error: {e}")

            estado_mercado_enviado = True

        # ── Recalcular GEX cada 30 minutos durante el día ────
        if gex_niveles["disponible"] and gex_niveles["ultima_actualizacion"]:
            mins_desde_gex = (ahora_ny - gex_niveles["ultima_actualizacion"]).total_seconds() / 60
            if mins_desde_gex >= 30:
                print(f"  [GEX] ♻️ Recalculando niveles ({mins_desde_gex:.0f} min desde última actualización)...")
                obtener_gex()

        # ── Recalcular GEX 0DTE cada 30 minutos ──────────────
        if TRADIER_TOKEN:
            if (not gex_0dte_cache["ultima_actualizacion"] or
                (ahora_ny - gex_0dte_cache["ultima_actualizacion"]).total_seconds() / 60 >= 30):
                obtener_gex_0dte()

        # ── Journal: etiquetar señales pendientes (30/60 min) ─
        verificar_senales_pendientes(spy_precio)

        # ── Ventana CHARM — viernes 14:30-16:00 ET ───────────
        if ahora_ny.weekday() == 4 and charm_alertado["dia"] != ahora_ny.date():
            if ahora_ny.hour * 60 + ahora_ny.minute >= 14 * 60 + 30:
                charm_alertado["dia"] = ahora_ny.date()
                try:
                    ctx_opex_ch = contexto_opex()
                    extra_ch = " — *OPEX HOY, flujo extra fuerte*" if ctx_opex_ch["es_opex_hoy"] else ""
                    bot.send_message(TELEGRAM_CHAT_ID,
                        f"🕞 *VENTANA CHARM — Viernes 14:30 ET*{extra_ch}\n"
                        f"El decay de opciones obliga a dealers a deshacer hedges.\n"
                        f"⚡ Suele favorecer la tendencia del día hasta el cierre.",
                        parse_mode="Markdown")
                    print("  [CHARM] 🕞 Alerta ventana charm enviada")
                except Exception as e:
                    print(f"  [CHARM] Error: {e}")

        # ── Recalcular Dark Pool cada 30 minutos durante el día ──
        if dark_pool_cache["disponible"] and dark_pool_cache["ultima_actualizacion"]:
            mins_desde_dp = (ahora_ny - dark_pool_cache["ultima_actualizacion"]).total_seconds() / 60
            if mins_desde_dp >= 30:
                print(f"  [DARK_POOL] ♻️ Recalculando ({mins_desde_dp:.0f} min desde última actualización)...")
                obtener_dark_pool()

        # ── Put/Call ratio semanal cada 30 minutos ────────────
        if TRADIER_TOKEN:
            if (not pc_semanal_cache["ultima_actualizacion"] or
                (ahora_ny - pc_semanal_cache["ultima_actualizacion"]).total_seconds() / 60 >= 30):
                obtener_pc_ratio_semanal()
# ── Weekly direccional — una vez al día ──────────────
        # El OI se actualiza una sola vez por jornada, así que
        # calcularlo más seguido devuelve lo mismo.
        try:
            calcular_weekly()
        except Exception as e:
            print(f"  [WEEKLY] Error en loop: {e}")
        # ── CME OI — actualizar cada 60 minutos ──────────────
        if (not cme_oi_cache["ultima_actualizacion"] or
            (ahora_ny - cme_oi_cache["ultima_actualizacion"]).total_seconds() / 60 >= 60):
            obtener_cme_oi()

        # ── Índice de Tiburones — recalcular cada 30 minutos ──
        if (not indice_tiburones_cache["ultima_actualizacion"] or
            (ahora_ny - indice_tiburones_cache["ultima_actualizacion"]).total_seconds() / 60 >= 30):
            try:
                calcular_indice_tiburones()
            except Exception as e:
                print(f"  [TIBURONES] Error recalculando: {e}")

        # ── Alertas proximidad GEX ───────────────────────────
        if gex_niveles["disponible"]:
            try:
                verificar_proximidad_gex(spy_precio, vix_precio)  # ^GSPC ya en escala US500
            except Exception as e:
                print(f"  [GEX_PROX] Error loop: {e}")

        # ── Detector squeeze de volatilidad ──────────────────
        try:
            detectar_squeeze_volatilidad(datos, spy_precio)
        except Exception as e:
            print(f"  [SQUEEZE] Error loop: {e}")

        # ── Options Sweep Detection cada 5 minutos ────────────
        if TRADIER_TOKEN and contador_ciclos % 5 == 0:
            sweep = detectar_options_sweep()
            if sweep:
                hoy = ahora_ny.date()
                # Resetear alerta si es nuevo día
                if sweep_cache["dia"] != hoy:
                    sweep_cache["alerta_enviada"] = False
                    sweep_cache["dia"] = hoy
                    sweep_cache["historial_balance"] = []  # limpiar historial diario

                # Registrar el balance neto en el historial CADA 5 min (no solo
                # al alertar) para medir aceleración/desaceleración con buena
                # resolución. El usuario lee la DERIVADA del balance: si viene
                # creciendo y luego desacelera, hay agotamiento aunque el signo
                # siga igual. Guardamos el balance acumulado del día en cada
                # lectura; solo registramos si cambió para no llenar de repetidos.
                balance_actual = sweep.get("prima_calls", 0) - sweep.get("prima_puts", 0)
                hist = sweep_cache.get("historial_balance", [])
                if not hist or hist[-1] != balance_actual:
                    hist.append(balance_actual)
                    if len(hist) > 8:   # mantener las últimas 8 lecturas
                        hist.pop(0)
                    sweep_cache["historial_balance"] = hist

                # ── Envío a la app de seguimiento (dashboard) ───────
                # Se manda CADA lectura de 5 min (no solo las que alertan),
                # para que la web tenga la trayectoria completa del balance.
                # No bloquea el loop: corre en un thread daemon con timeout
                # corto y try/except silencioso.
                try:
                    enviar_sweep_dashboard(sweep, balance_actual, ahora_ny, spy_precio)
                except Exception as e:
                    print(f"  [DASHBOARD] Error preparando envío: {e}")

                # ── Decisión de alerta (anti "perder el tren") ──────
                # Reglas para enviar alerta:
                #  1. Primera alerta del día, o cambió el tipo (signo)
                #  2. Pasaron >=30 min (ciclo normal)
                #  3. SALTAR el ciclo si el balance varió MUCHO desde la
                #     última alerta — el usuario no quiere esperar 30 min
                #     si el flujo cambió fuerte. Combina dos gatillos:
                #     (a) salto de >=$150M desde la última alerta, O
                #     (b) cruzó el umbral dominante de $500M (en cualquier
                #         dirección) — el momento en que el flujo se vuelve
                #         una pared de dinero que mueve el precio.
                ultimo = sweep_cache["ultimo_sweep"]
                mins_desde_sweep = (ahora_ny - ultimo).total_seconds() / 60 if ultimo else 9999
                bal_ult_alerta = sweep_cache.get("balance_ultima_alerta", 0.0)
                salto_balance  = abs(balance_actual - bal_ult_alerta)

                SALTO_FUERTE   = 150_000_000   # $150M de cambio = novedad
                UMBRAL_DOM     = 500_000_000   # cruce de $500M = dominante

                # ¿El balance cruzó el umbral dominante desde la última alerta?
                cruzo_dominante = (abs(balance_actual) >= UMBRAL_DOM and
                                   abs(bal_ult_alerta) < UMBRAL_DOM)

                # ¿Cambió el tipo (signo) respecto al último alertado?
                tipo_nuevo = (sweep_cache.get("tipo") is not None and
                              sweep["tipo"] != sweep_cache.get("tipo"))

                ciclo_normal   = (not sweep_cache["alerta_enviada"] or
                                  mins_desde_sweep >= 30)
                salto_relevante = (salto_balance >= SALTO_FUERTE or cruzo_dominante)

                if ciclo_normal or tipo_nuevo or salto_relevante:
                    # Marcar el motivo para el log (transparencia)
                    if salto_relevante and not ciclo_normal and not tipo_nuevo:
                        motivo = (f"salto ${salto_balance/1e6:.0f}M" if salto_balance >= SALTO_FUERTE
                                  else f"cruzó ${UMBRAL_DOM/1e6:.0f}M dominante")
                        print(f"  [SWEEP] ⚡ Alerta ADELANTADA (saltó ciclo 30min): {motivo}")
                    sweep_cache.update({
                        "ultimo_sweep":    ahora_ny,
                        "tipo":            sweep["tipo"],
                        "contratos_calls": sweep.get("contratos_calls", 0),
                        "contratos_puts":  sweep.get("contratos_puts", 0),
                        "prima_total":     sweep["balance_neto"],
                        "prima_calls_hoy": sweep.get("prima_calls", 0),
                        "prima_puts_hoy":  sweep.get("prima_puts", 0),
                        "strikes":         max(sweep.get("strikes_calls", 0), sweep.get("strikes_puts", 0)),
                        "alerta_enviada":  True,
                        "balance_ultima_alerta": balance_actual,
                    })
                    direccion = sweep["tipo"]
                    emoji = "🟢" if direccion == "ALCISTA" else ("🔴" if direccion == "BAJISTA" else "⚪")
                    
                    # Construir mensaje con ambos lados
                    calls_txt = ""
                    puts_txt  = ""
                    if sweep["hay_calls"]:
                        calls_txt = (f"🟢 CALLS: `{sweep['contratos_calls']:,}` contratos "
                                    f"({sweep['strikes_calls']} strikes) — `${sweep['prima_calls']:,.0f}`\n")
                    if sweep["hay_puts"]:
                        puts_txt  = (f"🔴 PUTS: `{sweep['contratos_puts']:,}` contratos "
                                    f"({sweep['strikes_puts']} strikes) — `${sweep['prima_puts']:,.0f}`\n")

                    # En días OPEX/triple witching el pinning a los walls
                    # neutraliza el efecto de los sweeps — advertir de no fiarse.
                    cierre_sweep = f"⚡ Movimiento {direccion} probable en 15-30 min."
                    try:
                        _ctx_op = contexto_opex()
                        if _ctx_op["es_opex_hoy"]:
                            _tw = "triple witching" if _ctx_op["es_triple"] else "OPEX"
                            cierre_sweep = (f"⚠️ HOY es {_tw} — el pinning a los walls suele "
                                            f"anular el efecto de los sweeps.\n"
                                            f"NO fiarse del balance para predecir dirección hoy "
                                            f"(aunque el monto sea alto).")
                    except Exception:
                        pass

                    # ── Control del balance (lo que el usuario quería ver) ──
                    # Muestra si el balance neto viene SUBIENDO, se FRENÓ, o
                    # cruzó el umbral DOMINANTE de $500M, comparando con las
                    # lecturas previas del historial.
                    control_txt = ""
                    hist_bal = sweep_cache.get("historial_balance", [])
                    bal_actual = sweep.get("prima_calls", 0) - sweep.get("prima_puts", 0)
                    abs_actual = abs(bal_actual)
                    if abs_actual >= 500_000_000:
                        control_txt = f"\n🚨 *DOMINANTE* — balance cruzó $500M, el precio suele seguir esta dirección"
                    elif len(hist_bal) >= 2:
                        prev = hist_bal[-2] if hist_bal[-1] == bal_actual else hist_bal[-1]
                        mismo_signo = (prev > 0) == (bal_actual > 0)
                        if mismo_signo:
                            if abs_actual > abs(prev) * 1.05:
                                control_txt = f"\n📈 Balance *ACELERANDO* (${abs(prev)/1e6:.0f}M → ${abs_actual/1e6:.0f}M) — el flujo gana fuerza"
                            elif abs_actual < abs(prev) * 0.95:
                                control_txt = f"\n⚠️ Balance *DESACELERANDO* (${abs(prev)/1e6:.0f}M → ${abs_actual/1e6:.0f}M) — posible agotamiento, el precio puede frenar"
                            else:
                                control_txt = f"\n➡️ Balance *ESTABLE* (~${abs_actual/1e6:.0f}M)"
                        else:
                            control_txt = f"\n🔄 Balance *CAMBIÓ DE LADO* (ahora {direccion})"

                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"{emoji} *SWEEP INSTITUCIONAL DETECTADO*\n"
                            f"────────────────────────────\n"
                            f"{calls_txt}{puts_txt}"
                            f"────────────────────────────\n"
                            f"📊 Balance neto: *{direccion}* `${sweep['balance_neto']:,.0f}`"
                            f"{control_txt}\n"
                            f"📅 Expiración: `{sweep['expiracion']}`\n"
                            f"{cierre_sweep}",
                            parse_mode="Markdown")
                        print(f"  [SWEEP] {emoji} Alerta {direccion} enviada — balance ${sweep['balance_neto']:,.0f}")
                    except Exception as e:
                        print(f"  [SWEEP] Error enviando alerta: {e}")

        # ── Calcular score ────────────────────────────────────
        resultado = calcular_score_total(datos, minutos)
        score     = resultado["score"]
        detalle   = resultado["detalle"]
        pen_rsi   = resultado["penalizacion_rsi"]
        pen_tend  = resultado["penalizacion_tendencia"]
        fatiga    = resultado.get("minutos_vix_fatiga", 0)
        move      = detalle["move_index"]
        dxy       = detalle["dxy"]
        liq       = detalle["liquidez"]
        tend      = detalle["tendencia"]
        gex       = detalle["gex"]
        dp        = detalle["dark_pool"]

        # ── Detector de rango ─────────────────────────────────
        gamma_flip_nivel = gex_niveles.get("gamma_flip") if gex_niveles["disponible"] else None
        rango_estado = evaluar_detector_rango(spy_precio, gamma_flip_nivel)

        # ── Log consola ───────────────────────────────────────
        comps_activos = {k: v for k, v in resultado["componentes"].items() if v != 0}
        tags = ""
        if pen_rsi  != 0: tags += f" [RSI:{pen_rsi:+d}]"
        if pen_tend != 0: tags += f" [TEND:{pen_tend:+d}]"
        if fatiga >= MINUTOS_VIX_RATIO_FATIGA: tags += f" [FAT:{fatiga}m]"
        if liq["alerta"]: tags += f" [LIQ:{liq['nivel'][:3]}]"
        if dxy.get("disponible"): tags += f" [DXY:{dxy.get('señal','?')[:5]}]"
        if gex.get("disponible"): tags += f" [GEX({gex.get('fuente','?')[:3]}):{gex.get('score',0):+d}]"
        if dp.get("disponible"):  tags += f" [DP({dp.get('fuente','?')[:3]}):{dp.get('tendencia','?')[:3]}]"
        if rango_estado["en_rango"]: tags += f" [RANGO:{rango_estado['minutos']:.0f}m]"
        tags += f" [{contexto_macro['impacto'][:3]}]"
        print(f"[{ahora_ny.strftime('%H:%M')}] {detalle['precio']:.2f} | Score:{score:+d}{tags} | "
              f"RSI:{detalle['rsi']} | VIX:{detalle['vix_nivel']} | EMA:{'↑' if tend.get('sobre_ema') else '↓'}"
              + (f" | {comps_activos}" if comps_activos else ""))

        # ── Detector de agotamiento ───────────────────────────
        if estado_agotamiento["activo"] and not estado_agotamiento["alerta_enviada"]:
            if evaluar_agotamiento(resultado):
                enviar_alerta_agotamiento(resultado)

        # ── Detector distribución en posición activa ──────────
        if posicion_activa["activa"] and not posicion_activa["alerta_distribucion_enviada"]:
            if evaluar_distribucion_posicion(resultado):
                enviar_alerta_distribucion(resultado)
                posicion_activa["alerta_distribucion_enviada"] = True

        # ── Acumulación diaria de señales para COT estimado ───
        # (Antes vivía dentro del bloque de "contradicción institucional",
        #  ya retirado. Se conserva con su propio flag de primer ciclo.)
        ahora_dia = ahora_ny.date()
        if ciclo_diario_cache["dia"] != ahora_dia:
            ciclo_diario_cache["dia"] = ahora_dia
            try:
                acumular_senales_cot()
            except Exception as e:
                print(f"  [COT_EST] Error acumulando: {e}")

        # ── Divergencia precio-Dark Pool ──────────────────────
        if datos:
            try:
                div_dp = detectar_divergencia_dark_pool(spy_precio, resultado)
                if div_dp:
                    emoji_div = "📉" if div_dp == "BAJISTA" else "📈"
                    dp_tend   = dark_pool_cache.get("tendencia", "N/D")
                    dir_precio = "subiendo" if div_dp == "BAJISTA" else "bajando"
                    dir_dp     = "distribuyendo" if div_dp == "BAJISTA" else "acumulando"
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"{emoji_div} *DIVERGENCIA PRECIO vs DARK POOL*\n"
                            f"Precio {dir_precio} pero institucionales {dir_dp}\n"
                            f"🏦 Dark Pool: `{dp_tend}`\n"
                            f"⚠️ Posible reversión {div_dp.lower()}.",
                            parse_mode="Markdown")
                        print(f"  [DIV_DP] {emoji_div} Alerta divergencia {div_dp} enviada")
                    except Exception as e:
                        print(f"  [DIV_DP] Error enviando: {e}")
            except: pass

        # ── Regla de apertura ─────────────────────────────────
        if minutos < 5:
            print(f"  → ⏸ Bloqueo apertura ({minutos:.0f} min < 5)")
            contador_ciclos += 1
            elapsed = time.time() - inicio_ciclo
            time.sleep(max(0, 60 - elapsed))
            continue
        elif minutos < 30:
            score = max(-10, min(10, score - 2 if score > 0 else score + 2))
            print(f"  → ⚠️ Apertura temprana ({minutos:.0f} min) — score ajustado a {score}")

        # ── Verificar penalización post-evento macro ─────────
        if penalizacion_macro_activa["activa"]:
            mins_pen = (ahora_ny - penalizacion_macro_activa["hora_inicio"]).total_seconds() / 60
            if mins_pen >= 45:
                penalizacion_macro_activa["activa"] = False
                print(f"  [POST-EVENTO] ✅ Penalización levantada ({mins_pen:.0f} min)")
            else:
                print(f"  → ⏸ Post-{penalizacion_macro_activa['tipo']} — señales bloqueadas ({mins_pen:.0f}/45 min)")
                elapsed = time.time() - inicio_ciclo
                time.sleep(max(0, 60 - elapsed))
                contador_ciclos += 1
                continue

        # ── Alertas principales ───────────────────────────────
        if rango_estado.get("suspender"):
            print(f"  → ⏸ Señales suspendidas por detector de rango ({rango_estado['minutos']:.0f} min)")
        elif score >= UMBRAL_SCORE and ENVIAR_SENALES_SCORE:
            ok, razon = cooldown.debe_alertar_alcista(resultado)
            if ok:
                print(f"  → 🟢 ALCISTA score={score} ({razon}). Consultando Claude...")
                analisis = analizar_con_claude(resultado)
                # ── Verificar si hay sweep reciente alineado ──
                sweep_reciente = (sweep_cache["ultimo_sweep"] and
                                  sweep_cache["tipo"] == "ALCISTA" and
                                  (ahora_ny - sweep_cache["ultimo_sweep"]).total_seconds() / 60 <= 30)
                if sweep_reciente:
                    _nota_pin = ""
                    try:
                        _c = contexto_opex()
                        if _c["es_opex_hoy"]:
                            _nota_pin = ("\n⚠️ Día de vencimiento (pinning) — la confirmación "
                                         "pierde fuerza, no fiarse del balance hoy.")
                    except Exception:
                        pass
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"🔥 *CONFIRMACIÓN INSTITUCIONAL ALCISTA*\n"
                            f"Score +{score}/10 + Sweep ALCISTA detectado\n"
                            f"📊 {sweep_cache['contratos_calls']:,} calls vs {sweep_cache['contratos_puts']:,} puts\n"
                            f"💰 Balance neto: `${sweep_cache['prima_total']:,.0f}`\n"
                            f"⚡ Señal de alta convicción institucional.{_nota_pin}",
                            parse_mode="Markdown")
                        print("  [SWEEP+SCORE] 🔥 Confirmación institucional alcista enviada")
                    except Exception as e:
                        print(f"  [SWEEP+SCORE] Error: {e}")
                enviar_alerta_score(resultado, analisis)
                cooldown.registrar_alcista(resultado)
                activar_detector_agotamiento(resultado)
                registrar_senal_journal(resultado)
                print(f"  → ✅ Enviado ({ahora_ny.strftime('%H:%M:%S')} ET)")
            else:
                print(f"  → ⏸ Alcista {score} bloqueado: {razon}")
        elif score <= -UMBRAL_SCORE and ENVIAR_SENALES_SCORE:
            ok, razon = cooldown.debe_alertar_bajista(resultado)
            if ok:
                print(f"  → 🔴 BAJISTA score={score} ({razon}). Consultando Claude...")
                analisis = analizar_con_claude(resultado)
                # ── Verificar si hay sweep reciente alineado ──
                sweep_reciente = (sweep_cache["ultimo_sweep"] and
                                  sweep_cache["tipo"] == "BAJISTA" and
                                  (ahora_ny - sweep_cache["ultimo_sweep"]).total_seconds() / 60 <= 30)
                if sweep_reciente:
                    _nota_pin = ""
                    try:
                        _c = contexto_opex()
                        if _c["es_opex_hoy"]:
                            _nota_pin = ("\n⚠️ Día de vencimiento (pinning) — la confirmación "
                                         "pierde fuerza, no fiarse del balance hoy.")
                    except Exception:
                        pass
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"🔥 *CONFIRMACIÓN INSTITUCIONAL BAJISTA*\n"
                            f"Score -{abs(score)}/10 + Sweep BAJISTA detectado\n"
                            f"📊 {sweep_cache['contratos_puts']:,} puts vs {sweep_cache['contratos_calls']:,} calls\n"
                            f"💰 Balance neto: `${sweep_cache['prima_total']:,.0f}`\n"
                            f"⚡ Señal de alta convicción institucional.{_nota_pin}",
                            parse_mode="Markdown")
                        print("  [SWEEP+SCORE] 🔥 Confirmación institucional bajista enviada")
                    except Exception as e:
                        print(f"  [SWEEP+SCORE] Error: {e}")
                enviar_alerta_score(resultado, analisis)
                cooldown.registrar_bajista(resultado)
                activar_detector_agotamiento(resultado)
                registrar_senal_journal(resultado)
                print(f"  → ✅ Enviado ({ahora_ny.strftime('%H:%M:%S')} ET)")
            else:
                print(f"  → ⏸ Bajista {score} bloqueado: {razon}")
        else:
            if estado_agotamiento["activo"] and estado_agotamiento["alerta_enviada"]:
                resetear_detector_agotamiento()

        contador_ciclos += 1
        elapsed = time.time() - inicio_ciclo
        time.sleep(max(0, 60 - elapsed))

    except KeyboardInterrupt:
        print("\n[Bot detenido manualmente]")
        break
    except Exception as e:
        print(f"[ERROR] {e}")
        time.sleep(60)
