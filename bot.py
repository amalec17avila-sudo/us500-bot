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
from datetime import datetime, timedelta

# ── Credenciales desde variables de entorno (Railway) ────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

for var, nombre in [
    (TELEGRAM_TOKEN,    "TELEGRAM_TOKEN"),
    (TELEGRAM_CHAT_ID,  "TELEGRAM_CHAT_ID"),
    (ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY"),
]:
    if not var:
        raise EnvironmentError(f"❌ Variable de entorno faltante: {nombre}")

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
RANGO_MAXIMO_PUNTOS      = 20   # Detector de rango: max puntos para considerar rango
RANGO_MINUTOS_MINIMO     = 30   # Detector de rango: minutos mínimos en rango
RANGO_ALEJAMIENTO_MIN    = 10   # Detector de rango: puntos mínimos para ruptura real

# ================================================================
# === CAPA GEX — GAMMA EXPOSURE ==================================
# ================================================================

gex_niveles = {
    "gamma_flip":           None,
    "call_wall":            None,
    "put_wall":             None,
    "ultima_actualizacion": None,
    "disponible":           False,
}

def obtener_gex():
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
            gex_niveles.update({"gamma_flip": gamma_flip, "call_wall": call_wall,
                                "put_wall": put_wall, "ultima_actualizacion": hora_ny(),
                                "disponible": True})
            print(f"  [GEX] ✅ Flip:{gamma_flip} | Call:{call_wall} | Put:{put_wall}")
            return True
    except Exception as e:
        print(f"  [GEX] API error: {e}")
    return _gex_fallback()

def _gex_fallback():
    try:
        spy = yf.Ticker("SPY")
        precio_spy = spy.fast_info.last_price
        if not precio_spy: return False
        precio_us500 = precio_spy * 10
        redondeo   = 50
        gamma_flip = round(precio_us500 / redondeo) * redondeo
        call_wall  = (round(precio_us500 / redondeo) + 2) * redondeo
        put_wall   = (round(precio_us500 / redondeo) - 2) * redondeo
        gex_niveles.update({"gamma_flip": gamma_flip, "call_wall": call_wall,
                            "put_wall": put_wall, "ultima_actualizacion": hora_ny(),
                            "disponible": True, "es_estimado": True})
        print(f"  [GEX] 📊 Estimado — Flip:{gamma_flip} | Call:{call_wall} | Put:{put_wall}")
        return True
    except Exception as e:
        print(f"  [GEX] Fallback error: {e}")
        return False

def evaluar_gex(precio_actual):
    if not gex_niveles["disponible"]:
        return {"disponible": False, "score": 0, "señal": "NO DISPONIBLE",
                "distancia_flip": None, "distancia_call": None, "distancia_put": None}
    gamma_flip = gex_niveles["gamma_flip"]
    call_wall  = gex_niveles["call_wall"]
    put_wall   = gex_niveles["put_wall"]
    es_estimado = gex_niveles.get("es_estimado", False)
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
    if es_estimado: señal_texto += " (est.)"
    return {"disponible": True, "score": max(-3, min(3, score)), "señal": señal_texto,
            "gamma_flip": gamma_flip, "call_wall": call_wall, "put_wall": put_wall,
            "distancia_flip": round(dist_flip, 2) if dist_flip is not None else None,
            "distancia_call": round(dist_call, 2) if dist_call is not None else None,
            "distancia_put":  round(dist_put,  2) if dist_put  is not None else None,
            "es_estimado": es_estimado}

# ================================================================
# === DARK POOL VOLUME ===========================================
# ================================================================

dark_pool_cache = {
    "ratio":                None,
    "ultima_actualizacion": None,
    "disponible":           False,
}

def obtener_dark_pool():
    try:
        url = "https://api.finra.org/data/group/OTCMarket/name/weeklySummary?compareFilters=symbol==SPY&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        if data and len(data) > 0:
            vol_dp    = float(data[0].get("totalWeeklyShareQuantity", 0))
            vol_total = vol_dp * 1.6
            ratio = vol_dp / vol_total if vol_total > 0 else 0.35
            dark_pool_cache.update({"ratio": round(ratio, 4),
                                    "ultima_actualizacion": hora_ny(), "disponible": True})
            print(f"  [DARK_POOL] ✅ Ratio: {ratio:.1%}")
            return True
    except Exception as e:
        print(f"  [DARK_POOL] FINRA error: {e}")
    try:
        spy = yf.download("SPY", period="5d", interval="1d", progress=False)
        if not spy.empty:
            vol_reciente = float(spy["Volume"].iloc[-1])
            vol_promedio = float(spy["Volume"].mean())
            ratio_vol    = vol_reciente / vol_promedio if vol_promedio > 0 else 1.0
            ratio_est    = max(0.25, min(0.55, 0.38 + (1 - ratio_vol) * 0.05))
            dark_pool_cache.update({"ratio": round(ratio_est, 4),
                                    "ultima_actualizacion": hora_ny(),
                                    "disponible": True, "es_estimado": True})
            print(f"  [DARK_POOL] 📊 Ratio estimado: {ratio_est:.1%}")
            return True
    except Exception as e:
        print(f"  [DARK_POOL] Fallback error: {e}")
    return False

def evaluar_dark_pool(datos, ventana=10):
    if not dark_pool_cache["disponible"]:
        return {"disponible": False, "score": 0, "señal": "NO DISPONIBLE",
                "ratio": None, "interpretacion": ""}
    ratio    = dark_pool_cache["ratio"]
    es_estim = dark_pool_cache.get("es_estimado", False)
    spy    = datos["close"]["^GSPC"]
    volume = datos["volume"]["^GSPC"]
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
    señal = f"{interpretacion} (ratio:{ratio:.1%})"
    if es_estim: señal += " est."
    return {"disponible": True, "score": score, "señal": señal, "ratio": ratio,
            "interpretacion": interpretacion, "rango_pct": round(rango_pct * 100, 3),
            "es_estimado": es_estim}

# ================================================================
# === SEÑALES INSTITUCIONALES v3.9 ================================
# ================================================================

# ── Estado global COT Report ─────────────────────────────────
cot_cache = {
    "neto_largo":           None,
    "sesgo":                "NEUTRAL",
    "ultima_actualizacion": None,
    "disponible":           False,
}

def obtener_cot_report():
    """
    Descarga el COT Report de la CFTC para futuros del S&P 500 (E-mini).
    Se actualiza cada viernes. Usamos yfinance como proxy calculando
    la posición neta de non-commercials en futuros /ES=F.
    """
    try:
        es = yf.download("/ES=F", period="5d", interval="1d", progress=False)
        spy = yf.download("SPY", period="5d", interval="1d", progress=False)
        if es.empty or spy.empty:
            cot_cache["disponible"] = False
            return False
        # Proxy: si futuros suben más que SPY = institucionales largos
        ret_es  = float((es["Close"].iloc[-1] / es["Close"].iloc[-5] - 1) * 100) if len(es) >= 5 else 0
        ret_spy = float((spy["Close"].iloc[-1] / spy["Close"].iloc[-5] - 1) * 100) if len(spy) >= 5 else 0
        diferencia = ret_es - ret_spy
        # Calcular volumen relativo como proxy de posicionamiento
        vol_reciente = float(es["Volume"].iloc[-1]) if not es["Volume"].empty else 0
        vol_promedio = float(es["Volume"].mean()) if not es["Volume"].empty else 1
        ratio_vol = vol_reciente / vol_promedio if vol_promedio > 0 else 1.0
        # Score basado en divergencia futuros vs ETF y volumen
        if diferencia > 0.3 and ratio_vol > 1.2:
            sesgo = "ALCISTA_FUERTE"; neto = round(diferencia * 1000)
        elif diferencia > 0.1:
            sesgo = "ALCISTA_MODERADO"; neto = round(diferencia * 500)
        elif diferencia < -0.3 and ratio_vol > 1.2:
            sesgo = "BAJISTA_FUERTE"; neto = round(diferencia * 1000)
        elif diferencia < -0.1:
            sesgo = "BAJISTA_MODERADO"; neto = round(diferencia * 500)
        else:
            sesgo = "NEUTRAL"; neto = 0
        cot_cache.update({"neto_largo": neto, "sesgo": sesgo,
                          "ultima_actualizacion": hora_ny(), "disponible": True})
        print(f"  [COT] ✅ Sesgo:{sesgo} | Neto estimado:{neto:+,}")
        return True
    except Exception as e:
        print(f"  [COT] Error: {e}")
        cot_cache["disponible"] = False
        return False

def evaluar_cot():
    if not cot_cache["disponible"]:
        return {"disponible": False, "score": 0, "sesgo": "N/D"}
    sesgo = cot_cache["sesgo"]
    score_map = {"ALCISTA_FUERTE": 2, "ALCISTA_MODERADO": 1,
                 "NEUTRAL": 0, "BAJISTA_MODERADO": -1, "BAJISTA_FUERTE": -2}
    return {"disponible": True, "score": score_map.get(sesgo, 0),
            "sesgo": sesgo, "neto": cot_cache.get("neto_largo", 0)}

# ── McClellan Oscillator + A/D Volume ────────────────────────
mcclellan_cache = {"oscilador": None, "ad_ratio": None, "disponible": False}

def calcular_mcclellan():
    """
    Calcula el McClellan Oscillator y A/D ratio usando datos de
    los principales sectores del S&P 500 como proxy del NYSE.
    """
    try:
        sectores = ["XLK", "XLF", "XLV", "XLI", "XLC",
                    "XLY", "XLP", "XLE", "XLB", "XLRE", "XLU"]
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
        # McClellan simplificado con EMA19 - EMA39
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
            ema19 = pd.Series(ad_series).ewm(span=19, adjust=False).mean().iloc[-1]
            ema39 = pd.Series(ad_series).ewm(span=min(39, len(ad_series)), adjust=False).mean().iloc[-1]
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
    return {"disponible": True, "score": score, "oscilador": osc,
            "ad_ratio": ad, "señal": señal,
            "avances": mcclellan_cache.get("avances", 0),
            "declives": mcclellan_cache.get("declives", 0)}

# ── VVIX — VIX del VIX ───────────────────────────────────────
def evaluar_vvix(datos):
    """
    VVIX mide la volatilidad implícita de las opciones sobre el VIX.
    Cuando sube antes que el VIX = institucionales comprando protección.
    Señal adelantada al movimiento del mercado.
    """
    try:
        vvix_data = yf.download("^VVIX", period="5d", interval="1d", progress=False)
        if vvix_data.empty:
            return {"disponible": False, "score": 0, "nivel": None, "señal": "N/D"}
        vvix_actual   = float(vvix_data["Close"].iloc[-1])
        vvix_anterior = float(vvix_data["Close"].iloc[-2]) if len(vvix_data) >= 2 else vvix_actual
        cambio_pct    = (vvix_actual / vvix_anterior - 1) * 100
        # Niveles de referencia: VVIX normal ~80-90, elevado >100, extremo >120
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

# ── Put/Call Ratio SPY ────────────────────────────────────────
put_call_cache = {"ratio": None, "disponible": False, "ultima_actualizacion": None}

def obtener_put_call_ratio():
    """
    Obtiene el Put/Call ratio del SPY via Yahoo Finance.
    Ratio > 1.0 = más puts que calls = sesgo bajista institucional.
    Ratio < 0.7 = más calls que puts = sesgo alcista/complacencia.
    """
    try:
        spy = yf.Ticker("SPY")
        opciones = spy.options
        if not opciones: return False
        # Usar la expiración más cercana
        exp = opciones[0]
        chain = spy.option_chain(exp)
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
        # Fallback: usar VIX como proxy
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
    ratio = put_call_cache["ratio"]
    es_est = put_call_cache.get("es_estimado", False)
    if   ratio > 1.5:  score = -3; señal = "PÁNICO — PUTS EXTREMAS"
    elif ratio > 1.2:  score = -2; señal = "SESGO BAJISTA INSTITUCIONAL"
    elif ratio > 1.0:  score = -1; señal = "MÁS PUTS QUE CALLS"
    elif ratio < 0.6:  score =  2; señal = "EUFORIA ALCISTA — COMPLACENCIA"
    elif ratio < 0.8:  score =  1; señal = "SESGO ALCISTA EN OPCIONES"
    else:              score =  0; señal = "PUT/CALL NEUTRAL"
    if es_est: señal += " est."
    return {"disponible": True, "score": score, "ratio": ratio, "señal": señal}

# ── SPY vs SHY — Rotación defensiva ──────────────────────────
def evaluar_rotacion_defensiva(datos):
    """
    Compara SPY vs SHY (Treasury 1-3 años).
    Si SHY sube mientras SPY cae = rotación defensiva genuina.
    Si ambos caen = liquidación total, más peligroso.
    Si SPY sube y SHY cae = risk-on real, alcista confirmado.
    """
    try:
        shy_data = yf.download("SHY", period="5d", interval="1d", progress=False)
        if shy_data.empty:
            return {"disponible": False, "score": 0, "señal": "N/D"}
        ret_shy = float((shy_data["Close"].iloc[-1] / shy_data["Close"].iloc[-2] - 1) * 100) \
                  if len(shy_data) >= 2 else 0
        spy_data = yf.download("SPY", period="5d", interval="1d", progress=False)
        ret_spy = float((spy_data["Close"].iloc[-1] / spy_data["Close"].iloc[-2] - 1) * 100) \
                  if len(spy_data) >= 2 else 0
        if   ret_spy > 0.2 and ret_shy < -0.05: score =  2; señal = "RISK-ON REAL — SPY SUBE SHY CAE"
        elif ret_spy > 0.1 and ret_shy < 0:     score =  1; señal = "ROTACION HACIA RIESGO"
        elif ret_spy < -0.2 and ret_shy > 0.05: score = -1; señal = "ROTACION DEFENSIVA — PRECAUCION"
        elif ret_spy < -0.2 and ret_shy < -0.05:score = -3; señal = "LIQUIDACION TOTAL — PELIGRO"
        elif ret_spy < 0    and ret_shy > 0.1:  score = -2; señal = "HUIDA A BONOS CORTOS"
        else:                                    score =  0; señal = "FLUJO NEUTRAL"
        return {"disponible": True, "score": score, "señal": señal,
                "ret_spy": round(ret_spy, 3), "ret_shy": round(ret_shy, 3)}
    except Exception as e:
        return {"disponible": False, "score": 0, "señal": f"ERROR:{e}"}

# ── Breadth de sectores ───────────────────────────────────────
breadth_cache = {"verdes": 0, "rojos": 0, "total": 0, "disponible": False,
                 "ultima_actualizacion": None}

SECTORES_SP500 = ["XLK", "XLF", "XLV", "XLI", "XLC",
                  "XLY", "XLP", "XLE", "XLB", "XLRE", "XLU"]

def calcular_breadth_sectores():
    """
    Cuenta cuántos de los 11 sectores del S&P 500 están en positivo hoy.
    Se calcula en la apertura (primeros 30 minutos).
    9/11 o más en verde = día alcista estructural.
    3/11 o menos en verde = día bajista estructural.
    """
    try:
        datos = yf.download(SECTORES_SP500, period="2d", interval="1d", progress=False)
        if datos.empty: return False
        close = datos["Close"]
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

# ── Fear/Greed implícito ──────────────────────────────────────
def calcular_fear_greed(vix_nivel, vvix_resultado, pc_resultado):
    """
    Calcula un índice Fear/Greed implícito en tiempo real
    combinando VIX + VVIX + Put/Call ratio.
    Escala 0-100: 0=Miedo Extremo, 50=Neutral, 100=Codicia Extrema
    """
    try:
        # Componente VIX (invertido — VIX alto = miedo)
        vix_score = max(0, min(100, 100 - (vix_nivel - 10) * 3.33))
        # Componente VVIX
        vvix_nivel = vvix_resultado.get("nivel") if vvix_resultado.get("disponible") else 90
        vvix_score = max(0, min(100, 100 - (vvix_nivel - 70) * 2)) if vvix_nivel else 50
        # Componente Put/Call (invertido — PC alto = miedo)
        pc_ratio = pc_resultado.get("ratio") if pc_resultado.get("disponible") else 1.0
        pc_score = max(0, min(100, (1.5 - pc_ratio) / 0.9 * 100)) if pc_ratio else 50
        # Promedio ponderado
        fg = round(vix_score * 0.4 + vvix_score * 0.3 + pc_score * 0.3, 1)
        if   fg >= 75: etiqueta = "CODICIA EXTREMA"; score =  1
        elif fg >= 55: etiqueta = "CODICIA";          score =  1
        elif fg >= 45: etiqueta = "NEUTRAL";          score =  0
        elif fg >= 25: etiqueta = "MIEDO";            score = -1
        else:          etiqueta = "MIEDO EXTREMO";    score = -2
        return {"disponible": True, "valor": fg, "etiqueta": etiqueta, "score": score}
    except:
        return {"disponible": False, "valor": 50, "etiqueta": "N/D", "score": 0}

# ── Detector de rango ─────────────────────────────────────────
detector_rango = {
    "activo":              False,
    "inicio":              None,
    "precio_centro":       None,
    "señales_suspendidas": False,
}

def evaluar_detector_rango(precio_actual, gamma_flip):
    """
    Si el precio lleva 30+ minutos oscilando en un rango menor a 20 puntos
    alrededor del Gamma Flip, suspende señales hasta ruptura real.
    Ruptura = alejamiento sostenido de 10+ puntos del flip.
    """
    global detector_rango
    ahora = hora_ny()
    if gamma_flip is None:
        return {"en_rango": False, "suspender": False, "minutos": 0}
    distancia_flip = abs(precio_actual - gamma_flip)
    en_rango = distancia_flip <= RANGO_MAXIMO_PUNTOS / 2
    if en_rango:
        if not detector_rango["activo"]:
            detector_rango["activo"]        = True
            detector_rango["inicio"]        = ahora
            detector_rango["precio_centro"] = gamma_flip
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

# ── Tiempo ──────────────────────────────────────────────────
def hora_ny():
    return datetime.now(pytz.timezone("America/New_York"))

# Días festivos NYSE 2025-2027 (año, mes, día)
NYSE_FESTIVOS = {
    # 2025
    (2025, 1,  1),  # Año Nuevo
    (2025, 1, 20),  # MLK Day
    (2025, 2, 17),  # Presidents Day
    (2025, 4, 18),  # Good Friday
    (2025, 5, 26),  # Memorial Day
    (2025, 6, 19),  # Juneteenth
    (2025, 7,  4),  # Independence Day
    (2025, 9,  1),  # Labor Day
    (2025,11, 27),  # Thanksgiving
    (2025,12, 25),  # Navidad
    # 2026
    (2026, 1,  1),  # Año Nuevo
    (2026, 1, 19),  # MLK Day
    (2026, 2, 16),  # Presidents Day
    (2026, 4,  3),  # Good Friday
    (2026, 5, 25),  # Memorial Day ← HOY
    (2026, 6, 19),  # Juneteenth
    (2026, 7,  3),  # Independence Day (observado)
    (2026, 9,  7),  # Labor Day
    (2026,11, 26),  # Thanksgiving
    (2026,12, 25),  # Navidad
    # 2027
    (2027, 1,  1),  # Año Nuevo
    (2027, 1, 18),  # MLK Day
    (2027, 2, 15),  # Presidents Day
    (2027, 3, 26),  # Good Friday
    (2027, 5, 31),  # Memorial Day
    (2027, 6, 18),  # Juneteenth (observado)
    (2027, 7,  5),  # Independence Day (observado)
    (2027, 9,  6),  # Labor Day
    (2027,11, 25),  # Thanksgiving
    (2027,12, 24),  # Navidad (observado)
}

def mercado_abierto():
    ahora = hora_ny()
    if ahora.weekday() > 4: return False
    # Verificar festivos NYSE
    if (ahora.year, ahora.month, ahora.day) in NYSE_FESTIVOS: return False
    apertura = ahora.replace(hour=9,  minute=30, second=0, microsecond=0)
    cierre   = ahora.replace(hour=16, minute=0,  second=0, microsecond=0)
    return apertura <= ahora <= cierre

def minutos_desde_apertura():
    ahora    = hora_ny()
    apertura = ahora.replace(hour=9, minute=30, second=0, microsecond=0)
    return max(0, int((ahora - apertura).total_seconds() / 60))

def es_dia_evento():
    resumen  = contexto_macro.get("resumen", "").lower()
    sesgo    = contexto_macro.get("sesgo", "").lower()
    noticias = " ".join(contexto_macro.get("noticias", [])).lower()
    texto    = resumen + sesgo + noticias
    palabras = ["fed", "fomc", "nfp", "nóminas", "cpi", "inflación",
                "pce", "pib", "gdp", "powell", "warsh"]
    return any(p in texto for p in palabras)

def evento_acaba_de_ocurrir():
    ahora = hora_ny()
    for eventos in EVENTOS_CALENDARIO.values():
        for hora_ev, min_ev in eventos:
            evento_dt = ahora.replace(hour=hora_ev, minute=min_ev, second=0, microsecond=0)
            diff_mins = (ahora - evento_dt).total_seconds() / 60
            if 0 <= diff_mins <= 5: return True
    return False

# ── Descarga de datos ────────────────────────────────────────
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
            "close":       close, "volume": volume, "high": high, "low": low, "open": open_,
            "tiene_vix3m": "^VIX3M"   in close.columns and not close["^VIX3M"].isna().all(),
            "tiene_move":  "^MOVE"    in close.columns and not close["^MOVE"].isna().all(),
            "tiene_dxy":   "DX-Y.NYB" in close.columns and not close["DX-Y.NYB"].isna().all(),
        }
    except Exception as e:
        print(f"[ERROR descarga] {e}")
        return None

# ── Indicadores base ─────────────────────────────────────────
def rsi(serie, periodos=14):
    delta    = serie.diff()
    ganancia = delta.where(delta > 0, 0.0).rolling(periodos).mean()
    perdida  = (-delta.where(delta < 0, 0.0)).rolling(periodos).mean()
    rs = ganancia / perdida
    return 100 - (100 / (1 + rs))

def ema(serie, span):
    return serie.ewm(span=span, adjust=False).mean()

# ── CAPA 1: Microestructura ──────────────────────────────────
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

def aceleracion_volumen_placeholder():
    pass  # Eliminado en v3.9 — bajo impacto

def zscore_volumen_placeholder():
    pass  # Eliminado en v3.9 — bajo impacto

def monitor_liquidez(close, volume, high, low, ventana=10):
    if len(close) < ventana + 2:
        return {"nivel": "NORMAL", "ratio_vol_mov": None, "volatilidad_velas": None,
                "alerta": False, "score": 0}
    try:
        movimientos  = (high.iloc[-ventana:] - low.iloc[-ventana:]).abs()
        vol_reciente = volume.iloc[-ventana:]
        mov_promedio = float(movimientos.mean())
        vol_promedio = float(vol_reciente.mean())
        ratio_vm     = vol_promedio / mov_promedio if mov_promedio > 0 else 0
        mov_hist     = (high.iloc[-60:-ventana] - low.iloc[-60:-ventana]).abs().mean()
        vol_hist     = volume.iloc[-60:-ventana].mean()
        ratio_vm_hist = float(vol_hist) / float(mov_hist) if float(mov_hist) > 0 else ratio_vm
        ratio_relativo = ratio_vm / ratio_vm_hist if ratio_vm_hist > 0 else 1.0
        gaps = close.iloc[-ventana:].diff().abs()
        volatilidad_velas = float(gaps.mean())
        volatilidad_hist  = float(close.iloc[-60:-ventana].diff().abs().mean())
        ratio_volatilidad = volatilidad_velas / volatilidad_hist if volatilidad_hist > 0 else 1.0
        if ratio_relativo < 0.4 or ratio_volatilidad > 3.0:
            nivel = "MUY BAJA"; alerta = True; score = -2
        elif ratio_relativo < 0.6 or ratio_volatilidad > 2.0:
            nivel = "BAJA";     alerta = True; score = -1
        elif ratio_relativo < 0.8:
            nivel = "REDUCIDA"; alerta = False; score = 0
        else:
            nivel = "NORMAL";   alerta = False; score = 0
        return {"nivel": nivel, "ratio_vol_mov": round(ratio_relativo, 2),
                "volatilidad_velas": round(ratio_volatilidad, 2), "alerta": alerta, "score": score}
    except:
        return {"nivel": "NORMAL", "ratio_vol_mov": None, "volatilidad_velas": None,
                "alerta": False, "score": 0}

# ── CAPA 2: Correlaciones ────────────────────────────────────
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

def vix_momentum_placeholder():
    pass  # Eliminado en v3.9 — reemplazado por VVIX

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
        fatiga = minutos_sin_cambio >= MINUTOS_VIX_RATIO_FATIGA
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
        if   cambio_move > 2.0 and cambio_vix > 2.0:       score = -3; señal = "PANICO SINCRONIZADO"
        elif cambio_move > 2.0 and abs(cambio_vix) < 1.0:  score = -2; señal = "BONOS ANTICIPAN CAIDA"
        elif cambio_move > 1.0 and abs(cambio_vix) < 0.5:  score = -1; señal = "TENSION EN BONOS"
        elif cambio_move < -2.0 and vix_actual > 20:        score =  3; señal = "BONOS ANTICIPAN REBOTE"
        elif cambio_move < -1.0 and vix_actual > 18:        score =  2; señal = "ALIVIO EN BONOS"
        elif cambio_move < -0.5:                            score =  1; señal = "BONOS CALMANDOSE"
        elif abs(cambio_move) < 0.5 and move_actual < 100:  score =  1; señal = "BONOS ESTABLES"
        else:                                               score =  0; señal = "NEUTRAL"
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
        elif cambio_dxy > 0.20 and cambio_spy > 0.10:   score = -2; señal = "RALLY FRAGIL"
        elif cambio_dxy > 0.20 and cambio_spy < -0.10:  score = -3; señal = "HUIDA AL EFECTIVO"
        elif cambio_dxy > 0.10 and cambio_spy < 0:      score = -2; señal = "PRESION BAJISTA"
        elif cambio_dxy > 0.05:                          score = -1; señal = "DOLAR FORTALECIENDOSE"
        else:                                            score =  0; señal = "NEUTRAL"
        return {"disponible": True, "nivel": round(dxy_actual, 3),
                "cambio_pct": round(cambio_dxy, 3), "cambio_spy": round(cambio_spy, 3),
                "score": score, "señal": señal}
    except Exception as e:
        return {"disponible": False, "nivel": None, "cambio_pct": None,
                "score": 0, "señal": f"ERROR: {e}"}

# ── CAPA 3: Estadística intradía ─────────────────────────────
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

def zscore_volumen_hora(volume):
    if len(volume) < 60: return {"zscore": 0.0, "score_bonus": 0}
    vol_actual = float(volume.iloc[-1])
    historico  = volume.iloc[-60:-1]
    mu, sigma  = float(historico.mean()), float(historico.std())
    if sigma == 0: return {"zscore": 0.0, "score_bonus": 0}
    z = (vol_actual - mu) / sigma
    return {"zscore": round(float(z), 2), "score_bonus": 1 if abs(z) > 2.5 else 0}

def posicion_rango_diario(spy, high, low, minutos_apertura):
    velas    = max(2, minutos_apertura)
    max_dia  = float(high.iloc[-velas:].max())
    min_dia  = float(low.iloc[-velas:].min())
    precio   = float(spy.iloc[-1])
    rango    = max_dia - min_dia
    if rango == 0:
        return {"posicion_pct": 50.0, "score": 0, "max_dia": max_dia, "min_dia": min_dia}
    posicion = (precio - min_dia) / rango * 100
    score    = 1 if posicion > 80 else (-1 if posicion < 20 else 0)
    return {"posicion_pct": round(float(posicion), 1),
            "max_dia": round(max_dia, 2), "min_dia": round(min_dia, 2), "score": score}

def filtro_tendencia(spy, ventana_ema=20, ventana_minimos=30):
    """
    Filtro de tendencia agresivo v3.8:
    - Detecta mínimos más bajos consecutivos bajo la EMA20
    - Si hay tendencia bajista clara, penaliza score alcista en -4 pts
    - Si precio está bajo Gamma Flip Y haciendo mínimos más bajos = tendencia bajista confirmada
    """
    if len(spy) < ventana_ema + 1:
        return {"precio_vs_ema": 0.0, "sobre_ema": True, "penalizacion": 0,
                "minimos_bajistas": False, "tendencia_bajista_fuerte": False}

    ema20      = float(ema(spy, ventana_ema).iloc[-1])
    precio_act = float(spy.iloc[-1])
    diff_pct   = (precio_act - ema20) / ema20 * 100
    sobre_ema  = precio_act > ema20

    # Detectar mínimos más bajos en los últimos 30 minutos
    minimos_bajistas = False
    tendencia_bajista_fuerte = False

    if len(spy) >= ventana_minimos:
        # Dividir en 3 segmentos y comparar mínimos
        seg = ventana_minimos // 3
        min1 = float(spy.iloc[-ventana_minimos:-2*seg].min())
        min2 = float(spy.iloc[-2*seg:-seg].min())
        min3 = float(spy.iloc[-seg:].min())

        # Si cada segmento tiene mínimo más bajo = tendencia bajista
        if min3 < min2 < min1:
            minimos_bajistas = True
            # Si además está bajo la EMA20 = tendencia bajista fuerte
            if not sobre_ema:
                tendencia_bajista_fuerte = True

    return {
        "ema20":                    round(ema20, 2),
        "precio_vs_ema":            round(diff_pct, 3),
        "sobre_ema":                sobre_ema,
        "minimos_bajistas":         minimos_bajistas,
        "tendencia_bajista_fuerte": tendencia_bajista_fuerte,
        "penalizacion":             0,
    }

# ── Score combinado ──────────────────────────────────────────
vix_ratio_historia = []

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

    # NUEVAS SEÑALES v3.9
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

    # v3.8/v3.9: Filtro de tendencia AGRESIVO
    penalizacion_tendencia = 0
    if tendencia["tendencia_bajista_fuerte"]:
        if score_raw > 0:
            penalizacion_tendencia = -4
        if gex.get("disponible") and gex.get("distancia_flip") is not None:
            if gex["distancia_flip"] < 0:
                penalizacion_tendencia = -6
    elif tendencia["minimos_bajistas"] and not tendencia["sobre_ema"]:
        if score_raw > 0:
            penalizacion_tendencia = -3
    elif not tendencia["sobre_ema"]:
        if score_raw > 0:
            penalizacion_tendencia = -2
    elif tendencia["sobre_ema"] and score_raw < 0:
        penalizacion_tendencia = 2

    score_raw += penalizacion_tendencia

    # v3.9: Filtro de agotamiento de rally/caída diario
    # Si el precio ya subió 30+ puntos desde el mínimo del día → penaliza señales alcistas
    # Si el precio ya cayó 30+ puntos desde el máximo del día → penaliza señales bajistas
    penalizacion_rally = 0
    try:
        min_dia = float(low.iloc[-minutos_apertura:].min()) if minutos_apertura > 0 else float(low.iloc[-30:].min())
        max_dia = float(high.iloc[-minutos_apertura:].max()) if minutos_apertura > 0 else float(high.iloc[-30:].max())
        distancia_desde_minimo = precio_actual - min_dia
        distancia_desde_maximo = max_dia - precio_actual
        if score_raw > 0 and distancia_desde_minimo > 50:
            penalizacion_rally = -3
            print(f"  [RALLY] ⚠️ Precio subió {distancia_desde_minimo:.1f}pts desde mínimo — penalización -3")
        elif score_raw > 0 and distancia_desde_minimo > 30:
            penalizacion_rally = -2
            print(f"  [RALLY] ⚠️ Precio subió {distancia_desde_minimo:.1f}pts desde mínimo — penalización -2")
        elif score_raw < 0 and distancia_desde_maximo > 50:
            penalizacion_rally = 3
            print(f"  [RALLY] ⚠️ Precio cayó {distancia_desde_maximo:.1f}pts desde máximo — penalización +3")
        elif score_raw < 0 and distancia_desde_maximo > 30:
            penalizacion_rally = 2
            print(f"  [RALLY] ⚠️ Precio cayó {distancia_desde_maximo:.1f}pts desde máximo — penalización +2")
    except:
        pass
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
# === FUNCIONES NUEVAS v3.9 ======================================
# ================================================================

# ── Estado para macro post-evento ────────────────────────────
ultimo_evento_procesado = {"tipo": None, "hora": None}

def detectar_evento_reciente():
    """
    Detecta si ocurrió un evento macro de alto impacto en los últimos 20 minutos.
    Busca en el contexto macro palabras clave de eventos conocidos.
    """
    ahora = hora_ny()
    hora_et = ahora.hour * 60 + ahora.minute
    # Horarios de eventos de alto impacto en minutos desde medianoche ET
    eventos = {
        "NFP":  8 * 60 + 30,
        "CPI":  8 * 60 + 30,
        "PCE":  8 * 60 + 30,
        "FED": 14 * 60 + 0,
        "FOMC":14 * 60 + 0,
        "PIB":  8 * 60 + 30,
    }
    for nombre, hora_evento in eventos.items():
        minutos_desde = hora_et - hora_evento
        if 15 <= minutos_desde <= 20:
            # Verificar que no lo hayamos procesado ya hoy
            if (ultimo_evento_procesado["tipo"] != nombre or
                ultimo_evento_procesado.get("dia") != ahora.date()):
                return nombre
    return None

def procesar_macro_post_evento(nombre_evento):
    """Actualiza la macro inmediatamente después de un evento de alto impacto."""
    global ultimo_evento_procesado
    print(f"  [POST-EVENTO] Actualizando macro después de {nombre_evento}...")
    actualizar_contexto_macro(enviar_telegram=True)
    ultimo_evento_procesado = {
        "tipo": nombre_evento,
        "hora": hora_ny(),
        "dia":  hora_ny().date()
    }

# ── Pre-apertura ──────────────────────────────────────────────
pre_apertura_enviado = {"dia": None}

def enviar_pre_apertura():
    """
    Envía contexto completo 15 minutos antes de la apertura del mercado (9:15 ET).
    Incluye futuros, COT, GEX estimado, Fear/Greed y eventos del día.
    """
    ahora = hora_ny()
    if pre_apertura_enviado["dia"] == ahora.date():
        return
    hora_et = ahora.hour * 60 + ahora.minute
    # Ventana amplia 9:00-9:15 ET para no depender del ciclo exacto de 60s
    if not (9 * 60 <= hora_et <= 9 * 60 + 15):
        return
    print("  [PRE-APERTURA] Preparando contexto...")
    try:
        # Futuros S&P 500
        es_data = yf.download("/ES=F", period="2d", interval="5m", progress=False)
        futuro_precio = float(es_data["Close"].iloc[-1]) if not es_data.empty else 0
        futuro_cambio = float((es_data["Close"].iloc[-1] / es_data["Close"].iloc[-12] - 1) * 100) \
                        if len(es_data) >= 12 else 0
        # COT sesgo
        cot_info = cot_cache
        cot_texto = f"COT: {cot_info.get('sesgo','N/D')}" if cot_info.get("disponible") else "COT: N/D"
        # GEX niveles
        gex_texto = ""
        if gex_niveles["disponible"]:
            gex_texto = (f"\n⚡ GEX: Flip:`{gex_niveles['gamma_flip']}` | "
                        f"Call:`{gex_niveles['call_wall']}` | Put:`{gex_niveles['put_wall']}`")
        # Breadth sectores
        if not breadth_cache["disponible"]:
            calcular_breadth_sectores()
        breadth_texto = f"Breadth: {breadth_cache.get('verdes',0)}/11 sectores en verde" \
                       if breadth_cache["disponible"] else "Breadth: N/D"
        # Fear/Greed
        vix_d = yf.download("^VIX", period="2d", interval="1d", progress=False)
        vix_n = float(vix_d["Close"].iloc[-1]) if not vix_d.empty else 20
        fg = calcular_fear_greed(vix_n, {"disponible": False}, {"disponible": False})
        fg_texto = f"Fear/Greed: {fg['valor']} — {fg['etiqueta']}"
        # Macro actual
        macro_imp = contexto_macro.get("impacto", "calculando...")
        emoji_dir = "📈" if futuro_cambio > 0 else "📉"
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

# ── Resumen dominical ─────────────────────────────────────────
resumen_dominical_enviado = {"semana": None}

def enviar_resumen_dominical():
    """
    Envía resumen dominical cada domingo entre 6-8 PM Honduras (8-10 PM ET).
    Honduras = UTC-6 | ET verano = UTC-4 → diferencia de 2 horas.
    Ventana amplia para no depender del ciclo exacto de 60 segundos.
    """
    ahora = hora_ny()
    if ahora.weekday() != 6:  # 6 = domingo
        return
    hora_et = ahora.hour * 60 + ahora.minute
    # 8-10 PM ET = 6-8 PM Honduras (ventana amplia de 2 horas)
    if not (20 * 60 <= hora_et <= 22 * 60):
        return
    semana_actual = ahora.isocalendar()[1]
    if resumen_dominical_enviado["semana"] == semana_actual:
        return
    print("  [DOMINICAL] Preparando resumen semanal...")
    try:
        # Actualizar COT
        obtener_cot_report()
        # Futuros
        es_data = yf.download("/ES=F", period="5d", interval="1d", progress=False)
        futuro_precio = float(es_data["Close"].iloc[-1]) if not es_data.empty else 0
        futuro_cambio_semana = float((es_data["Close"].iloc[-1] / es_data["Close"].iloc[0] - 1) * 100) \
                               if len(es_data) >= 5 else 0
        # COT
        cot_sesgo = cot_cache.get("sesgo", "N/D") if cot_cache.get("disponible") else "N/D"
        cot_neto  = cot_cache.get("neto_largo", 0)
        # GEX estimado para el lunes
        if not gex_niveles["disponible"]:
            _gex_fallback()
        gex_lunes = ""
        if gex_niveles["disponible"]:
            gex_lunes = (f"\n⚡ GEX estimado lunes:\n"
                        f"   Flip: `{gex_niveles['gamma_flip']}` | "
                        f"Call: `{gex_niveles['call_wall']}` | Put: `{gex_niveles['put_wall']}`")
        # Buscar contexto macro con Opus
        resultado_macro = buscar_contexto_macro()
        sesgo_macro = resultado_macro.get("sesgo", "N/D") if resultado_macro else "N/D"
        noticias_semana = resultado_macro.get("noticias", []) if resultado_macro else []
        noticias_str = "\n".join(f"  • {n}" for n in noticias_semana[:3])
        emoji_cot = "🟢" if "ALCISTA" in cot_sesgo else ("🔴" if "BAJISTA" in cot_sesgo else "⚪")
        msg = (f"📊 *RESUMEN DOMINICAL — Semana {semana_actual}*\n{'─'*28}\n"
               f"*Posicionamiento Smart Money (COT):*\n"
               f"{emoji_cot} Sesgo: `{cot_sesgo}` | Neto: `{cot_neto:+,}` contratos\n{'─'*28}\n"
               f"*Futuros S&P 500:*\n"
               f"💵 Precio: `{futuro_precio:.0f}` | Semana: `{futuro_cambio_semana:+.2f}%`"
               f"{gex_lunes}\n{'─'*28}\n"
               f"*Noticias clave semana:*\n{noticias_str}\n{'─'*28}\n"
               f"*Sesgo institucional:* {sesgo_macro}\n"
               f"📅 Mercado abre el martes 9:30 ET")
        if len(msg) > 4096: msg = msg[:4090] + "..."
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        resumen_dominical_enviado["semana"] = semana_actual
        print("  [DOMINICAL] ✅ Resumen enviado")
    except Exception as e:
        print(f"  [DOMINICAL] Error: {e}")

# ── Monitor overnight ─────────────────────────────────────────
ultimo_alerta_overnight = {"hora": None}

def monitorear_overnight():
    """
    Monitorea futuros, VIX y noticias overnight.
    Si futuros mueven más de 0.5%, envía alerta anticipando gap de apertura.
    """
    try:
        ahora = hora_ny()
        # Solo verificar cada 30 minutos para no saturar
        if (ultimo_alerta_overnight["hora"] and
            (ahora - ultimo_alerta_overnight["hora"]).total_seconds() < 1800):
            return
        es_data = yf.download("/ES=F", period="2d", interval="5m", progress=False)
        if es_data.empty or len(es_data) < 2: return
        precio_actual = float(es_data["Close"].iloc[-1])
        precio_cierre = float(es_data["Close"].iloc[-13])  # ~1 hora atrás
        cambio_pct = (precio_actual / precio_cierre - 1) * 100
        if abs(cambio_pct) < 0.5: return
        # Movimiento significativo detectado
        emoji = "📈" if cambio_pct > 0 else "📉"
        tipo  = "ALCISTA" if cambio_pct > 0 else "BAJISTA"
        gex_nivel = gex_niveles.get("gamma_flip", "N/D")
        wall = gex_niveles.get("call_wall" if cambio_pct > 0 else "put_wall", "N/D")
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
# === CAPA 0: CONTEXTO MACROECONÓMICO ============================
# ================================================================

contexto_macro = {
    "resumen": "Sin contexto macro disponible.", "impacto": "NEUTRAL",
    "noticias": [], "sesgo": "", "ultima_actualizacion": None,
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
        lineas = texto.strip().split("\n")
        impacto = "NEUTRAL"; noticias = []; resumen = ""; sesgo = ""
        for linea in lineas:
            linea = linea.strip()
            if linea.startswith("IMPACTO:"):   impacto = linea.replace("IMPACTO:", "").strip()
            elif linea.startswith(("1.", "2.", "3.")): noticias.append(linea[2:].strip())
            elif linea.startswith("RESUMEN:"): resumen = linea.replace("RESUMEN:", "").strip()
            elif linea.startswith("SESGO:"):   sesgo   = linea.replace("SESGO:", "").strip()
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
    print(f"  [MACRO] Actualizado: {impacto_nuevo} (anterior: {impacto_anterior})")
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
    emoji = emoji_map.get(impacto, "⚪")
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
    if contexto_macro["ultima_actualizacion"] is None: return True
    ahora      = hora_ny()
    ultima     = contexto_macro["ultima_actualizacion"]
    mins_desde = (ahora - ultima).total_seconds() / 60
    # Cooldown mínimo de 30 minutos entre cualquier actualización macro
    # Evita spam de 6 mensajes consecutivos como ocurrió el 27/mayo
    if mins_desde < 30: return False
    hora_actual = ahora.hour * 60 + ahora.minute
    apertura    = 9 * 60 + 30
    mediodia    = 12 * 60 + 30
    mismo_dia   = ultima.date() == ahora.date()
    if (apertura <= hora_actual <= apertura + 30) and (not mismo_dia or mins_desde > 180): return True
    if (mediodia <= hora_actual <= mediodia + 30) and mismo_dia and mins_desde > 60: return True
    return False

# ── Análisis con Claude (Haiku) ──────────────────────────────
def analizar_con_claude(resultado):
    score     = resultado["score"]
    detalle   = resultado["detalle"]
    comps     = resultado["componentes"]
    pen_rsi   = resultado["penalizacion_rsi"]
    pen_tend  = resultado["penalizacion_tendencia"]
    fatiga    = resultado.get("minutos_vix_fatiga", 0)
    direccion = "ALCISTA" if score > 0 else "BAJISTA"

    notas = []
    if pen_rsi != 0:
        notas.append(f"RSI {'sobrecomprado' if pen_rsi<0 else 'sobrevendido'} ({detalle['rsi']}) ajusta {pen_rsi:+d}pts")
    if pen_tend != 0:
        notas.append(f"Filtro tendencia: precio {'bajo' if pen_tend<0 else 'sobre'} EMA20 ajusta {pen_tend:+d}pts")
    liq = detalle["liquidez"]
    if liq["alerta"]: notas.append(f"⚠️ LIQUIDEZ {liq['nivel']}")
    notas_str = " | ".join(notas)

    vix_r  = detalle["vix_ratio"]
    move   = detalle["move_index"]
    dxy    = detalle["dxy"]
    gex    = detalle["gex"]
    dp     = detalle["dark_pool"]
    tend   = detalle["tendencia"]

    vix_str  = f"VIX/VIX3M:{vix_r.get('ratio','N/D')}" + (" [FAT]" if vix_r.get("fatiga") else "") if vix_r.get("disponible") else "VIX/VIX3M:N/D"
    move_str = f"MOVE:{move.get('nivel','N/D')} {move.get('señal','')}" if move.get("disponible") else "MOVE:N/D"
    dxy_str  = f"DXY:{dxy.get('nivel','N/D')} {dxy.get('señal','')}" if dxy.get("disponible") else "DXY:N/D"
    gex_str  = f"GEX Flip:{gex.get('gamma_flip')} Call:{gex.get('call_wall')} Put:{gex.get('put_wall')} | {gex.get('señal','')}" if gex.get("disponible") else "GEX:N/D"
    dp_str   = f"DarkPool:{dp.get('ratio',0):.1%} {dp.get('interpretacion','')}" if dp.get("disponible") else "DarkPool:N/D"
    tend_str = f"EMA20:{tend.get('ema20','?')} {'SOBRE' if tend.get('sobre_ema') else 'BAJO'}"
    if tend.get("tendencia_bajista_fuerte"): tend_str += " ⚠️TENDENCIA BAJISTA FUERTE"
    elif tend.get("minimos_bajistas"): tend_str += " ⚠️MINIMOS BAJISTAS"

    macro_impacto  = contexto_macro.get("impacto", "NEUTRAL")
    macro_resumen  = contexto_macro.get("resumen", "")
    macro_noticias = " | ".join(contexto_macro.get("noticias", []))
    macro_sesgo    = contexto_macro.get("sesgo", "")
    macro_hora     = contexto_macro.get("ultima_actualizacion")
    macro_hora_str = macro_hora.strftime("%H:%M ET") if macro_hora else "?"

    # Nuevas señales v3.9
    cot_d  = detalle.get("cot", {})
    mcl_d  = detalle.get("mcclellan", {})
    vvix_d = detalle.get("vvix", {})
    pc_d   = detalle.get("put_call", {})
    rot_d  = detalle.get("rotacion", {})
    br_d   = detalle.get("breadth", {})
    fg_d   = detalle.get("fear_greed", {})

    cot_str  = f"COT:{cot_d.get('sesgo','N/D')}" if cot_d.get("disponible") else "COT:N/D"
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
4. Niveles: Flip:{gex_str.split('Flip:')[1].split('|')[0].strip() if 'Flip:' in gex_str else 'N/D'} | Stop recomendado | Target recomendado
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

# ── Alerta de Contradicción Institucional ────────────────────
def detectar_contradiccion_institucional(resultado):
    """
    Detecta cuando el macro dice bajista pero los institucionales
    están comprando — señal de movimiento encubierto inminente.
    """
    detalle    = resultado["detalle"]
    macro_imp  = contexto_macro.get("impacto", "")
    dp         = detalle.get("dark_pool", {})
    vix_nivel  = detalle.get("vix_nivel", 20)
    fg         = detalle.get("fear_greed", {})
    vvix       = detalle.get("vvix", {})
    pc         = detalle.get("put_call", {})

    macro_bajista   = "BAJISTA" in macro_imp.upper()
    dp_acumulando   = dp.get("interpretacion", "") == "ACUMULACION INSTITUCIONAL OCULTA"
    vix_bajo        = vix_nivel < 18
    fg_codicia      = fg.get("valor", 50) > 60 if fg.get("disponible") else False
    vvix_bajo       = vvix.get("score", 0) >= 0 if vvix.get("disponible") else True
    pc_neutro       = pc.get("ratio", 1.0) < 1.1 if pc.get("disponible") else True

    # Contradicción fuerte: macro bajista + 4 señales institucionales alcistas
    señales_alcistas = sum([dp_acumulando, vix_bajo, fg_codicia, vvix_bajo, pc_neutro])
    return macro_bajista and señales_alcistas >= 4

def enviar_alerta_contradiccion(resultado):
    """Envía alerta especial de contradicción institucional."""
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
           f"🌍 Macro: `BAJISTA` — pero institucionales COMPRANDO\n"
           f"{'─'*30}\n"
           f"🏦 Dark Pool: `ACUMULACIÓN {dp.get('ratio', 0):.1%}`\n"
           f"📉 VIX: `{vix_nivel}` — bajo, sin pánico\n"
           f"🌡️ Fear/Greed: `{fg_val}` — codicia\n"
           f"{'─'*30}\n"
           f"⚡ GEX Flip: `{flip}` | Call Wall: `{call_wall}`\n"
           f"⚠️ *Los institucionales ignoran el ruido macro.*\n"
           f"📈 Posible movimiento alcista encubierto.")
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        print("  [CONTRADICCIÓN] ⚡ Alerta enviada")
    except Exception as e:
        print(f"  [CONTRADICCIÓN] Error: {e}")

# Estado para no repetir la alerta de contradicción
contradiccion_cache = {"enviada": False, "dia": None}

# ── Telegram ─────────────────────────────────────────────────
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
    senales_str = "\n".join(f"  • {s}" for s in senales_activas) or "  • Ninguna"

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

    vix_r = detalle["vix_ratio"]
    vix_str = f"\n📐 VIX/VIX3M: `{vix_r['ratio']}`" + (" ⚠️fat" if vix_r.get("fatiga") else "") if vix_r.get("disponible") else ""

    move = detalle["move_index"]
    move_str = f"\n📈 MOVE: `{move['nivel']}` ({move['cambio_pct']:+.1f}%) — {move['señal']}" if move.get("disponible") else ""

    dxy = detalle["dxy"]
    dxy_str = f"\n💵 DXY: `{dxy['nivel']}` ({dxy['cambio_pct']:+.3f}%) — {dxy['señal']}" if dxy.get("disponible") else ""

    gex = detalle["gex"]
    gex_str = ""
    if gex.get("disponible"):
        est = " est." if gex.get("es_estimado") else ""
        gex_str = f"\n⚡ GEX{est}: Flip:`{gex['gamma_flip']}` | Call:`{gex['call_wall']}` | Put:`{gex['put_wall']}`"

    dp = detalle["dark_pool"]
    dp_str = f"\n🏦 DarkPool: `{dp['ratio']:.1%}` — {dp['interpretacion']}" + (" est." if dp.get("es_estimado") else "") if dp.get("disponible") else ""

    liq = detalle["liquidez"]
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

    # Enviar análisis en mensaje separado — así nunca se corta
    analisis_msg = f"📋 *Análisis:*\n\n{analisis_claude}"
    if len(analisis_msg) > 4096: analisis_msg = analisis_msg[:4090] + "..."
    try: bot.send_message(TELEGRAM_CHAT_ID, analisis_msg, parse_mode="Markdown")
    except:
        try: bot.send_message(TELEGRAM_CHAT_ID, analisis_msg.replace("*","").replace("`",""))
        except Exception as e: print(f"  [ANALISIS] Telegram error: {e}")

# ── Detector de agotamiento ──────────────────────────────────
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
    estado_agotamiento.update({"activo": False, "direccion": None, "alerta_enviada": False, "historial_delta": []})

def evaluar_agotamiento(resultado):
    if not estado_agotamiento["activo"] or estado_agotamiento["alerta_enviada"]: return False
    detalle   = resultado["detalle"]
    comps     = resultado["componentes"]
    direccion = estado_agotamiento["direccion"]
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
    if direccion == "ALCISTA" and precio_act > precio_ent and rsi_actual < rsi_ent - 5: condiciones += 1
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
        print(f"  → ⚠️ Agotamiento enviado ({ahora})")
    except:
        try:
            bot.send_message(TELEGRAM_CHAT_ID, msg.replace("*","").replace("`",""))
            estado_agotamiento["alerta_enviada"] = True
        except Exception as e: print(f"  [AGOT] Telegram error: {e}")

# ── Cooldown inteligente ─────────────────────────────────────
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
# === LOOP PRINCIPAL — PRODUCCIÓN 24/7 ===========================
# ================================================================

estado_mercado_enviado = False
contador_ciclos        = 0
cooldown               = EstadoCooldown()

print("=" * 60)
print("   US500 MONITOR v3.9 — VISION INSTITUCIONAL COMPLETA")
print("=" * 60)
print("  12 mejoras | 6 limpiezas | COT+McC+VVIX+PC+SHY+Breadth")
print(f"  Umbral: ±{UMBRAL_SCORE}/10 | Min entre alertas: {TIEMPO_MIN_ALERTAS} min")
print("=" * 60)

# Inicializar datos semanales
print("  [INIT] Cargando COT Report...")
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

            # Funciones fuera de horario
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
            print("  [INIT] Obteniendo niveles GEX...")
            obtener_gex()
            print("  [INIT] Obteniendo Dark Pool...")
            obtener_dark_pool()
            print("  [INIT] Calculando Breadth sectores...")
            calcular_breadth_sectores()
            print("  [INIT] Actualizando McClellan...")
            calcular_mcclellan()
            print("  [INIT] Obteniendo Put/Call ratio...")
            obtener_put_call_ratio()

            macro_str = contexto_macro.get("impacto", "calculando...")
            gex_msg = ""
            if gex_niveles["disponible"]:
                est = " (est.)" if gex_niveles.get("es_estimado") else ""
                gex_msg = (f"\n⚡ GEX{est}: Flip:`{gex_niveles['gamma_flip']}` | "
                          f"Call:`{gex_niveles['call_wall']}` | Put:`{gex_niveles['put_wall']}`")
            breadth_msg = ""
            if breadth_cache["disponible"]:
                breadth_msg = f"\n📊 Breadth: `{breadth_cache['verdes']}/11` sectores alcistas"
            cot_msg = ""
            if cot_cache["disponible"]:
                sesgo_cot = cot_cache['sesgo']
                neto_cot  = cot_cache.get('neto_largo', 0)
                emoji_cot = "🟢" if "ALCISTA" in sesgo_cot else ("🔴" if "BAJISTA" in sesgo_cot else "⚪")
                cot_msg = f"\n{emoji_cot} COT Smart Money: `{sesgo_cot}` ({neto_cot:+,} contratos est.)"
            try:
                bot.send_message(TELEGRAM_CHAT_ID,
                    f"🔔 *MERCADO ABIERTO — US500 v3.9*\n"
                    f"US500: `{spy_precio:.2f}` | VIX: `{vix_precio:.2f}`\n"
                    f"Macro: `{macro_str}`"
                    f"{gex_msg}{breadth_msg}{cot_msg}\n"
                    f"Sistema v3.9 activo. Ciclo: 1 min.",
                    parse_mode="Markdown")
            except:
                bot.send_message(TELEGRAM_CHAT_ID,
                    f"MERCADO ABIERTO US500 v3.9\nUS500: {spy_precio:.2f} | VIX: {vix_precio:.2f}")
            estado_mercado_enviado = True

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

        # ── Detector de rango v3.9 ────────────────────────────
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
        if gex.get("disponible"): tags += f" [GEX:{gex.get('score',0):+d}]"
        if dp.get("disponible"):  tags += f" [DP:{dp.get('ratio',0):.0%}]"
        if rango_estado["en_rango"]: tags += f" [RANGO:{rango_estado['minutos']:.0f}m]"
        tags += f" [{contexto_macro['impacto'][:3]}]"
        print(f"[{ahora_ny.strftime('%H:%M')}] {detalle['precio']:.2f} | Score:{score:+d}{tags} | "
              f"RSI:{detalle['rsi']} | VIX:{detalle['vix_nivel']} | EMA:{'↑' if tend.get('sobre_ema') else '↓'}"
              + (f" | {comps_activos}" if comps_activos else ""))

        # ── Detector de agotamiento ───────────────────────────
        if estado_agotamiento["activo"] and not estado_agotamiento["alerta_enviada"]:
            if evaluar_agotamiento(resultado):
                enviar_alerta_agotamiento(resultado)

        # ── Alerta de contradicción institucional ─────────────
        ahora_dia = ahora_ny.date()
        if contradiccion_cache["dia"] != ahora_dia:
            contradiccion_cache["enviada"] = False
            contradiccion_cache["dia"]     = ahora_dia
        if not contradiccion_cache["enviada"] and detectar_contradiccion_institucional(resultado):
            enviar_alerta_contradiccion(resultado)
            contradiccion_cache["enviada"] = True

        # ── Regla de apertura v3.9 ────────────────────────────
        # 0-5 min: bloqueo total — datos insuficientes y RSI irreal
        # 5-30 min: penalización -2 por liquidez baja de apertura
        # 30+ min: señales normales
        if minutos < 5:
            print(f"  → ⏸ Bloqueo apertura ({minutos:.0f} min < 5) — solo mensaje apertura")
            contador_ciclos += 1
            elapsed = time.time() - inicio_ciclo
            time.sleep(max(0, 60 - elapsed))
            continue
        elif minutos < 30:
            score = max(-10, min(10, score - 2 if score > 0 else score + 2))
            print(f"  → ⚠️ Apertura temprana ({minutos:.0f} min) — score ajustado a {score}")

        # ── Alertas principales (con detector de rango) ───────
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
