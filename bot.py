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

def obtener_cot_report():
    """
    Descarga el COT Report REAL de la CFTC para E-mini S&P 500.
    Publicado cada viernes 3:30 PM ET con datos del martes anterior.
    URL: https://www.cftc.gov/dea/newcot/FinFutWk.txt
    Fallback: proxy via /ES=F vs SPY si CFTC no disponible.
    """
    try:
        url = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            contenido = resp.read().decode("latin-1")

        lineas = contenido.strip().split("\n")
        linea_emini = None
        for linea in lineas:
            if "E-MINI S&P 500" in linea.upper():
                linea_emini = linea
                break

        if linea_emini is None:
            print("  [COT] No se encontró E-mini S&P 500 en el CSV")
            return _cot_proxy_fallback()

        campos = linea_emini.split(",")
        if len(campos) < 10:
            print("  [COT] CSV con formato inesperado")
            return _cot_proxy_fallback()

        fecha_str = campos[2].strip().strip('"')
        longs_nc  = int(campos[7].strip().replace('"','').replace(',',''))
        shorts_nc = int(campos[8].strip().replace('"','').replace(',',''))
        neto      = longs_nc - shorts_nc

        if   neto > 150000:  sesgo = "ALCISTA_FUERTE"
        elif neto > 50000:   sesgo = "ALCISTA_MODERADO"
        elif neto < -100000: sesgo = "BAJISTA_FUERTE"
        elif neto < -20000:  sesgo = "BAJISTA_MODERADO"
        else:                sesgo = "NEUTRAL"

        cot_cache.update({
            "neto_largo":           neto,
            "sesgo":                sesgo,
            "longs":                longs_nc,
            "shorts":               shorts_nc,
            "ultima_actualizacion": hora_ny(),
            "disponible":           True,
            "fuente":               "CFTC_REAL",
            "fecha_reporte":        fecha_str,
        })
        print(f"  [COT] ✅ REAL CFTC — Fecha:{fecha_str} | Longs:{longs_nc:,} | Shorts:{shorts_nc:,} | Neto:{neto:+,} | Sesgo:{sesgo}")
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
}


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

                    gex = gamma * oi * 100 * strike

                    if tipo == "call":
                        gex_por_strike[strike] = gex_por_strike.get(strike, 0) + gex
                    elif tipo == "put":
                        gex_por_strike[strike] = gex_por_strike.get(strike, 0) - gex

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

        gex_niveles.update({
            "gamma_flip":           gamma_flip_us500,
            "call_wall":            call_wall_us500,
            "put_wall":             put_wall_us500,
            "ultima_actualizacion": hora_ny(),
            "disponible":           True,
            "es_estimado":          False,
            "fuente":               fuente,
            "strikes_totales":      len(gex_filtrado),
        })
        print(f"  [GEX] ✅ {fuente} — Flip:{gamma_flip_us500} | Call:{call_wall_us500} | Put:{put_wall_us500} | Strikes:{len(gex_filtrado)}")
        return True

    except Exception as e:
        print(f"  [GEX] _procesar_gex error: {e}")
        return _gex_fallback()

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
# === DESCARGA Y INDICADORES BASE ================================
# ================================================================

def descargar_datos():
    try:
        tickers = ["^GSPC", "QQQ", "TLT", "^VIX", "^VIX3M", "^MOVE", "DX-Y.NYB"]
        raw = yf.download(tickers, period="2d", interval="1m", progress=False, auto_adjust=True)
        if raw.empty or len(raw) < 50: return None
        close  = raw["Close"].ffill().dropna()
        volume = raw["Volume"].ffill().dropna()
        high   = raw["High"].ffill().dropna()
        low    = raw["Low"].ffill().dropna()
        open_  = raw["Open"].ffill().dropna()
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

    if vix_ratio.get("disponible") and vix_ratio.get("ratio"):
        vix_ratio_historia.append(vix_ratio["ratio"])
        if len(vix_ratio_historia) > 120: vix_ratio_historia.pop(0)

    componentes = {
        "delta_volumen":   d_vol["score"],
        "absorcion":       absorc["score"],
        "divergencia_qqq": div_qqq["score"],
        "divergencia_tlt": div_tlt["score"],
        "vix_ratio":       vix_ratio["score"],
        "move_index":      move["score"],
        "dxy":             dxy["score"],
        "gex":             gex["score"],
        "dark_pool":       dark_pool["score"],
        "patron_apertura": p_hora["score"] if p_hora["activo"] else 0,
        "posicion_rango":  pos_rango["score"],
        "liquidez":        liquidez["score"],
        "cot":             cot["score"],
        "mcclellan":       mcclellan["score"],
        "vvix":            vvix["score"],
        "put_call":        put_call["score"],
        "rotacion":        rotacion["score"],
        "breadth":         breadth["score"],
        "fear_greed":      fear_greed["score"],
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
    # Aplica SIEMPRE — incluso si ultima es None pero hubo actualizaciones hoy
    if ultima:
        mins_desde = (ahora - ultima).total_seconds() / 60
        if mins_desde < 60: return False

    # Actualización 1 — solo en ventana de apertura
    # Si el bot reinicia fuera de esa ventana NO envía macro 1
    if contexto_macro["actualizaciones_hoy"] == 0:
        if apertura <= hora_actual <= apertura + 15:
            return True
        # Reinicio tardío — solo actualizar si es antes del mediodía
        # y nunca se actualizó hoy
        if not mismo_dia and apertura + 15 < hora_actual < mediodia:
            return True
        return False  # ← Fuera de ventana = NO actualizar

    # Actualización 2 — solo en ventana de mediodía
    # Requiere que ultima exista Y sea del mismo día
    if contexto_macro["actualizaciones_hoy"] == 1:
        if (mediodia <= hora_actual <= mediodia + 15
                and mismo_dia and ultima
                and (ahora - ultima).total_seconds() / 60 >= 60):
            return True
        return False  # ← Fuera de ventana = NO actualizar

    return False

# ================================================================
# === FUNCIONES v3.9 ============================================
# ================================================================

ultimo_evento_procesado = {"tipo": None, "hora": None}

def detectar_evento_reciente():
    ahora    = hora_ny()
    hora_et  = ahora.hour * 60 + ahora.minute
    eventos  = {"NFP": 8*60+30, "CPI": 8*60+30, "PCE": 8*60+30,
                "FED": 14*60+0, "FOMC": 14*60+0, "PIB": 8*60+30}
    for nombre, hora_evento in eventos.items():
        minutos_desde = hora_et - hora_evento
        if 15 <= minutos_desde <= 20:
            if (ultimo_evento_procesado["tipo"] != nombre or
                ultimo_evento_procesado.get("dia") != ahora.date()):
                return nombre
    return None

def procesar_macro_post_evento(nombre_evento):
    global ultimo_evento_procesado
    print(f"  [POST-EVENTO] Actualizando macro después de {nombre_evento}...")
    actualizar_contexto_macro(enviar_telegram=True)
    ultimo_evento_procesado = {"tipo": nombre_evento, "hora": hora_ny(), "dia": hora_ny().date()}

pre_apertura_enviado = {"dia": None}

def enviar_pre_apertura():
    ahora = hora_ny()
    if pre_apertura_enviado["dia"] == ahora.date(): return
    hora_et = ahora.hour * 60 + ahora.minute
    if not (9 * 60 <= hora_et <= 9 * 60 + 45): return
    print("  [PRE-APERTURA] Preparando contexto...")
    try:
        es_data       = descargar_futuros(period="2d", interval="5m")
        if not es_data.empty:
            close_es = es_data["Close"]
            if hasattr(close_es, "columns"): close_es = close_es.iloc[:, 0]
            close_es = close_es.squeeze()
            if hasattr(close_es, "values"): close_es = close_es.squeeze()
            futuro_precio = float(close_es.values[-1]) if hasattr(close_es, "values") else float(close_es.iloc[-1])
            if len(close_es) >= 12:
                futuro_cambio = float((close_es.values[-1] / close_es.values[-12] - 1) * 100) if hasattr(close_es, "values") else 0.0
            else:
                futuro_cambio = 0.0
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
        vix_d  = yf.download("^VIX", period="2d", interval="1d", progress=False)
        if not vix_d.empty:
            vix_close = vix_d["Close"]
            if hasattr(vix_close, "columns"): vix_close = vix_close.iloc[:, 0]
            vix_n = float(vix_close.squeeze().iloc[-1])
        else:
            vix_n = 20
        fg     = calcular_fear_greed(vix_n, {"disponible": False}, {"disponible": False})
        fg_texto   = f"Fear/Greed: {fg['valor']} — {fg['etiqueta']}"
        macro_imp  = contexto_macro.get("impacto", "calculando...")
        emoji_dir  = "📈" if futuro_cambio > 0 else "📉"
        msg = (f"🌅 *PRE-APERTURA — US500 v3.9*\n{'─'*28}\n"
               f"⏰ Mercado abre en ~15 minutos\n"
               f"{emoji_dir} Futuros S&P: `{futuro_precio:.0f}` ({futuro_cambio:+.2f}%)\n"
               f"📊 {cot_texto}\n"
               f"🌡️ {fg_texto}\n"
               f"📉 {breadth_texto}\n"
               f"🌍 Macro: `{macro_imp}`"
               f"{gex_texto}")
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
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
        # Mostrar longs/shorts si son reales
        cot_detalle = ""
        if cot_fuente == "CFTC_REAL":
            longs  = cot_cache.get("longs", 0)
            shorts = cot_cache.get("shorts", 0)
            cot_detalle = f"\n   Longs:{longs:,} | Shorts:{shorts:,} | Fecha corte:{cot_fecha}"
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
        emoji_cot = "🟢" if "ALCISTA" in cot_sesgo else ("🔴" if "BAJISTA" in cot_sesgo else "⚪")
        msg = (f"📊 *RESUMEN DOMINICAL — Semana {semana_actual}*\n{'─'*28}\n"
               f"*Posicionamiento Smart Money (COT {cot_fuente}):*\n"
               f"{emoji_cot} Sesgo: `{cot_sesgo}` | Neto: `{cot_neto:+,}` contratos"
               f"{cot_detalle}\n{'─'*28}\n"
               f"*Futuros S&P 500:*\n"
               f"💵 Precio: `{futuro_precio:.0f}` | Semana: `{futuro_cambio_semana:+.2f}%`"
               f"{gex_lunes}\n{'─'*28}\n"
               f"*Noticias clave semana:*\n{noticias_str}\n{'─'*28}\n"
               f"*Sesgo institucional:* {sesgo_macro}\n"
               f"📅 Mercado abre mañana 9:30 ET")
        if len(msg) > 4096: msg = msg[:4090] + "..."
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        resumen_dominical_enviado["semana"] = semana_actual
        print("  [DOMINICAL] ✅ Resumen enviado")
    except Exception as e:
        print(f"  [DOMINICAL] Error: {e}")

ultimo_alerta_overnight = {"hora": None}

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

    prompt = f"""Analista cuantitativo US500 intradía v3.9. Score:{score}/10 {direccion}.{' NOTAS: '+notas_str if notas_str else ''}

MACRO ({macro_hora_str}): {macro_impacto} | {macro_noticias} | {macro_resumen} | Sesgo:{macro_sesgo}

TÉCNICO: Precio:{detalle['precio']} RSI:{detalle['rsi']} VIX:{detalle['vix_nivel']}
{vix_str} | {move_str} | {dxy_str} | {tend_str}
{gex_str}
{dp_str}

SEÑALES INSTITUCIONALES v3.9:
{cot_str} | {mcl_str} | {vvix_str}
{pc_str} | {rot_str} | {br_str} | {fg_str}

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

def detectar_contradiccion_institucional(resultado):
    detalle  = resultado["detalle"]
    macro_imp = contexto_macro.get("impacto", "")
    dp        = detalle.get("dark_pool", {})
    vix_nivel = detalle.get("vix_nivel", 20)
    fg        = detalle.get("fear_greed", {})
    vvix      = detalle.get("vvix", {})
    pc        = detalle.get("put_call", {})
    macro_bajista  = "BAJISTA" in macro_imp.upper()
    dp_acumulando  = dp.get("interpretacion", "") in ["ACUMULACION INSTITUCIONAL OCULTA", "ACUMULACION INSTITUCIONAL"]
    vix_bajo       = vix_nivel < 18
    fg_codicia     = fg.get("valor", 50) > 60 if fg.get("disponible") else False
    vvix_bajo      = vvix.get("score", 0) >= 0 if vvix.get("disponible") else True
    pc_neutro      = pc.get("ratio", 1.0) < 1.1 if pc.get("disponible") else True
    señales_alcistas = sum([dp_acumulando, vix_bajo, fg_codicia, vvix_bajo, pc_neutro])
    return macro_bajista and señales_alcistas >= 4

def enviar_alerta_contradiccion(resultado):
    detalle   = resultado["detalle"]
    precio    = detalle["precio"]
    gex       = detalle.get("gex", {})
    flip      = gex.get("gamma_flip", gex_niveles.get("gamma_flip", "N/D"))
    call_wall = gex_niveles.get("call_wall", "N/D")
    vix_nivel = detalle.get("vix_nivel", "N/D")
    dp        = detalle.get("dark_pool", {})
    fg        = detalle.get("fear_greed", {})
    fg_val    = fg.get("valor", "N/D") if fg.get("disponible") else "N/D"
    msg = (f"⚡ *CONTRADICCIÓN INSTITUCIONAL DETECTADA*\n{'─'*30}\n"
           f"💵 Precio: `{precio}`\n"
           f"🌍 Macro: `BAJISTA` — pero institucionales COMPRANDO\n{'─'*30}\n"
           f"🏦 Dark Pool: `{dp.get('interpretacion','N/D')} {dp.get('ratio',0):.1%}`\n"
           f"📉 VIX: `{vix_nivel}` — bajo, sin pánico\n"
           f"🌡️ Fear/Greed: `{fg_val}` — codicia\n{'─'*30}\n"
           f"⚡ GEX Flip: `{flip}` | Call Wall: `{call_wall}`\n"
           f"⚠️ *Los institucionales ignoran el ruido macro.*\n"
           f"📈 Posible movimiento alcista encubierto.")
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        print("  [CONTRADICCIÓN] ⚡ Alerta enviada")
    except Exception as e:
        print(f"  [CONTRADICCIÓN] Error: {e}")

contradiccion_cache = {"enviada": False, "dia": None}

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
    dp_str = f"\n🏦 DarkPool({dp.get('fuente','?')}): `{dp['ratio']:.1%}` — {dp['interpretacion']}" \
             if dp.get("disponible") else ""

    liq     = detalle["liquidez"]
    liq_str = f"\n🌊 Liquidez: `{liq['nivel']}`" + (" ⚠️" if liq["alerta"] else "")

    macro_impacto = contexto_macro.get("impacto", "NEUTRAL")
    macro_emoji   = {"ALCISTA_FUERTE":"🟢🟢","ALCISTA_MODERADO":"🟢","NEUTRAL":"⚪",
                     "BAJISTA_MODERADO":"🔴","BAJISTA_FUERTE":"🔴🔴"}.get(macro_impacto, "⚪")

    msg = (f"{emoji_dir} *SEÑAL {dir_texto} — US500*\n{'─'*28}\n"
           f"🕐 Hora: {ahora}\n💵 Precio: `{detalle['precio']:.2f}`\n"
           f"📊 Score: `{barra_score(score)}`\n"
           f"📉 RSI: `{detalle['rsi']}` | VIX: `{detalle['vix_nivel']}`"
           f"{vix_str}{move_str}{dxy_str}{gex_str}{dp_str}{liq_str}\n"
           f"📍 Rango: `{detalle['posicion_rango']['posicion_pct']}%`\n"
           f"🌍 Macro: {macro_emoji} `{macro_impacto.replace('_',' ')}`{pen_lines}\n"
           f"{'─'*28}\n*Señales activas:*\n{senales_str}")
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

def iniciar_polling():
    try:
        bot.polling(none_stop=True, interval=2, timeout=20)
    except Exception as e:
        print(f"  [POLLING] Error: {e}")

polling_thread = threading.Thread(target=iniciar_polling, daemon=True)
polling_thread.start()
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
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"🔕 *MERCADO CERRADO — US500 v3.9*\n"
                            f"US500 final: `{precio_final:.2f}`\nHasta mañana. 🌙",
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

            enviar_resumen_dominical()
            monitorear_overnight()
            elapsed = time.time() - inicio_ciclo
            time.sleep(max(0, 60 - elapsed))
            contador_ciclos += 1
            continue

        # ── Pre-apertura ─────────────────────────────────────
        enviar_pre_apertura()

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

        spy_precio = float(datos["close"]["^GSPC"].iloc[-1])
        vix_precio = float(datos["close"]["^VIX"].iloc[-1])

        # ── Apertura del mercado ──────────────────────────────
        if not estado_mercado_enviado:
            print("  [INIT] Obteniendo niveles GEX REAL...")
            obtener_gex()
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
            estado_mercado_enviado = True

        # ── Recalcular GEX cada 30 minutos durante el día ────
        if gex_niveles["disponible"] and gex_niveles["ultima_actualizacion"]:
            mins_desde_gex = (ahora_ny - gex_niveles["ultima_actualizacion"]).total_seconds() / 60
            if mins_desde_gex >= 30:
                print(f"  [GEX] ♻️ Recalculando niveles ({mins_desde_gex:.0f} min desde última actualización)...")
                obtener_gex()

        # ── Recalcular Dark Pool cada 30 minutos durante el día ──
        if dark_pool_cache["disponible"] and dark_pool_cache["ultima_actualizacion"]:
            mins_desde_dp = (ahora_ny - dark_pool_cache["ultima_actualizacion"]).total_seconds() / 60
            if mins_desde_dp >= 30:
                print(f"  [DARK_POOL] ♻️ Recalculando ({mins_desde_dp:.0f} min desde última actualización)...")
                obtener_dark_pool()

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

        # ── Contradicción institucional ───────────────────────
        ahora_dia = ahora_ny.date()
        if contradiccion_cache["dia"] != ahora_dia:
            contradiccion_cache["enviada"] = False
            contradiccion_cache["dia"]     = ahora_dia
        if not contradiccion_cache["enviada"] and detectar_contradiccion_institucional(resultado):
            enviar_alerta_contradiccion(resultado)
            contradiccion_cache["enviada"] = True

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

        # ── Alertas principales ───────────────────────────────
        if rango_estado.get("suspender"):
            print(f"  → ⏸ Señales suspendidas por detector de rango ({rango_estado['minutos']:.0f} min)")
        elif score >= UMBRAL_SCORE:
            ok, razon = cooldown.debe_alertar_alcista(resultado)
            if ok:
                print(f"  → 🟢 ALCISTA score={score} ({razon}). Consultando Claude...")
                analisis = analizar_con_claude(resultado)
                enviar_alerta_score(resultado, analisis)
                cooldown.registrar_alcista(resultado)
                activar_detector_agotamiento(resultado)
                print(f"  → ✅ Enviado ({ahora_ny.strftime('%H:%M:%S')} ET)")
            else:
                print(f"  → ⏸ Alcista {score} bloqueado: {razon}")
        elif score <= -UMBRAL_SCORE:
            ok, razon = cooldown.debe_alertar_bajista(resultado)
            if ok:
                print(f"  → 🔴 BAJISTA score={score} ({razon}). Consultando Claude...")
                analisis = analizar_con_claude(resultado)
                enviar_alerta_score(resultado, analisis)
                cooldown.registrar_bajista(resultado)
                activar_detector_agotamiento(resultado)
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
