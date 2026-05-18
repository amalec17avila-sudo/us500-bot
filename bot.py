"""
================================================================
  US500 MARKET MONITOR BOT v3.8 — RAILWAY PRODUCTION
  Autor: Amalec (revisado y mejorado por Claude)

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
MODELO_MACRO   = "claude-opus-4-5"

# ── Configuración ────────────────────────────────────────────
UMBRAL_SCORE             = 7
TIEMPO_MIN_ALERTAS       = 15
UMBRAL_PRECIO_CAMBIO     = 0.003
SALTO_SCORE_MINIMO       = 2
MINUTOS_VIX_RATIO_FATIGA = 45
AGOTAMIENTO_CONDICIONES  = 3

EVENTOS_CALENDARIO = {
    "FED":  [(14, 0)],
    "NFP":  [(8, 30)],
    "CPI":  [(8, 30)],
    "PCE":  [(8, 30)],
    "PIB":  [(8, 30)],
}

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

# ── Tiempo ──────────────────────────────────────────────────
def hora_ny():
    return datetime.now(pytz.timezone("America/New_York"))

def mercado_abierto():
    ahora = hora_ny()
    if ahora.weekday() > 4: return False
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

def aceleracion_volumen(volume):
    if len(volume) < 20: return {"aceleracion": 0.0, "score": 0}
    vol_rec  = volume.iloc[-3:].mean()
    vol_base = volume.iloc[-15:-3].mean()
    acel     = vol_rec / vol_base if vol_base > 0 else 1.0
    score    = 2 if acel > 2.5 else (1 if acel > 1.8 else 0)
    return {"aceleracion": round(float(acel), 2), "score": score}

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

def vix_momentum(vix, ventana=10):
    if len(vix) < ventana + 1:
        return {"cambio_pct": 0.0, "nivel": float(vix.iloc[-1]), "score": 0}
    nivel_actual   = float(vix.iloc[-1])
    nivel_anterior = float(vix.iloc[-ventana])
    cambio_pct     = (nivel_actual / nivel_anterior - 1) * 100
    if   cambio_pct >  3.0:                        score = -3
    elif cambio_pct >  1.5:                        score = -2
    elif cambio_pct >  0.8:                        score = -1
    elif cambio_pct < -3.0 and nivel_actual > 20:  score =  3
    elif cambio_pct < -1.5 and nivel_actual > 18:  score =  2
    elif cambio_pct < -0.8:                        score =  1
    else:                                           score =  0
    return {"nivel": round(nivel_actual, 2), "cambio_pct": round(cambio_pct, 2), "score": score}

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

    d_vol      = delta_volumen(spy, open_, volume)
    absorc     = absorcion_silenciosa(spy, volume)
    acel       = aceleracion_volumen(volume)
    div_qqq    = divergencia_spy_qqq(spy, qqq)
    div_tlt    = divergencia_spy_tlt(spy, tlt)
    v_mom      = vix_momentum(vix)
    vix_ratio  = ratio_vix_vix3m(datos, minutos_vix_fatiga)
    move       = move_index(datos)
    dxy        = dxy_señal(datos)
    p_hora     = patron_primera_media_hora(spy, minutos_apertura)
    z_vol      = zscore_volumen_hora(volume)
    pos_rango  = posicion_rango_diario(spy, high, low, minutos_apertura)
    tendencia  = filtro_tendencia(spy)
    liquidez   = monitor_liquidez(spy, volume, high, low)
    gex        = evaluar_gex(float(spy.iloc[-1]))
    dark_pool  = evaluar_dark_pool(datos)
    val_rsi    = float(rsi(spy).iloc[-1])

    if vix_ratio.get("disponible") and vix_ratio.get("ratio"):
        vix_ratio_historia.append(vix_ratio["ratio"])
        if len(vix_ratio_historia) > 120: vix_ratio_historia.pop(0)

    componentes = {
        "delta_volumen":   d_vol["score"],
        "absorcion":       absorc["score"],
        "aceleracion_vol": acel["score"] if d_vol["score"] != 0 else 0,
        "divergencia_qqq": div_qqq["score"],
        "divergencia_tlt": div_tlt["score"],
        "vix_momentum":    v_mom["score"],
        "vix_ratio":       vix_ratio["score"],
        "move_index":      move["score"],
        "dxy":             dxy["score"],
        "gex":             gex["score"],
        "dark_pool":       dark_pool["score"],
        "patron_apertura": p_hora["score"] if p_hora["activo"] else 0,
        "posicion_rango":  pos_rango["score"],
        "liquidez":        liquidez["score"],
    }

    score_raw = sum(componentes.values())
    if abs(score_raw) >= 3:
        score_raw += z_vol["score_bonus"] * (1 if score_raw > 0 else -1)

    penalizacion_rsi = 0
    if score_raw > 0 and val_rsi > 75:   penalizacion_rsi = -2
    elif score_raw < 0 and val_rsi < 25: penalizacion_rsi =  2
    score_raw += penalizacion_rsi

    # v3.8: Filtro de tendencia AGRESIVO
    penalizacion_tendencia = 0
    if tendencia["tendencia_bajista_fuerte"]:
        # Tendencia bajista fuerte (mínimos más bajos + bajo EMA20)
        if score_raw > 0:
            penalizacion_tendencia = -4  # Penalización fuerte
        # Forzar score bajista si también está bajo el Gamma Flip
        if gex.get("disponible") and gex.get("distancia_flip") is not None:
            if gex["distancia_flip"] < 0:  # bajo el Gamma Flip
                penalizacion_tendencia = -6  # Penalización muy fuerte
    elif tendencia["minimos_bajistas"] and not tendencia["sobre_ema"]:
        # Mínimos bajistas + bajo EMA20 (sin confirmar fuerte aún)
        if score_raw > 0:
            penalizacion_tendencia = -3
    elif not tendencia["sobre_ema"]:
        # Solo bajo EMA20
        if score_raw > 0:
            penalizacion_tendencia = -2
    elif tendencia["sobre_ema"] and score_raw < 0:
        # Sobre EMA20 pero score bajista
        penalizacion_tendencia = 2

    score_raw += penalizacion_tendencia
    score_final = max(-10, min(10, score_raw))

    return {
        "score": score_final, "penalizacion_rsi": penalizacion_rsi,
        "penalizacion_tendencia": penalizacion_tendencia,
        "componentes": componentes, "minutos_vix_fatiga": minutos_vix_fatiga,
        "detalle": {
            "delta_volumen": d_vol, "absorcion": absorc, "aceleracion": acel,
            "div_qqq": div_qqq, "div_tlt": div_tlt, "vix_momentum": v_mom,
            "vix_ratio": vix_ratio, "move_index": move, "dxy": dxy,
            "gex": gex, "dark_pool": dark_pool, "tendencia": tendencia,
            "liquidez": liquidez, "patron_apertura": p_hora, "zscore_vol": z_vol,
            "posicion_rango": pos_rango, "rsi": round(val_rsi, 1),
            "precio": round(float(spy.iloc[-1]), 2), "vix_nivel": round(float(vix.iloc[-1]), 2),
        }
    }

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
    ahora     = hora_ny()
    ultima    = contexto_macro["ultima_actualizacion"]
    mins_desde = (ahora - ultima).total_seconds() / 60
    if mins_desde < 30: return False
    if evento_acaba_de_ocurrir() and mins_desde >= 5:
        print("  [MACRO] Evento detectado — actualizando inmediatamente"); return True
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

    prompt = f"""Analista cuantitativo US500 intradía. Score:{score}/10 {direccion}.{' NOTAS: '+notas_str if notas_str else ''}

MACRO ({macro_hora_str}): {macro_impacto} | {macro_noticias} | {macro_resumen} | Sesgo:{macro_sesgo}

TÉCNICO: Precio:{detalle['precio']} RSI:{detalle['rsi']} VIX:{detalle['vix_nivel']}
{vix_str} | {move_str} | {dxy_str} | {tend_str}
{gex_str}
{dp_str}
Rango:{detalle['posicion_rango']['posicion_pct']}% (max:{detalle['posicion_rango']['max_dia']} min:{detalle['posicion_rango']['min_dia']})
Señales: DeltaVol:{comps['delta_volumen']}({detalle['delta_volumen']['ratio']}) Absorc:{comps['absorcion']} QQQ:{comps['divergencia_qqq']} TLT:{comps['divergencia_tlt']} VIX_mom:{comps['vix_momentum']} DXY:{comps['dxy']} MOVE:{comps['move_index']} GEX:{comps['gex']} DP:{comps['dark_pool']} Rango:{comps['posicion_rango']}

Responde en español, 5 párrafos cortos, sin asteriscos:
1. Probabilidad {direccion} 5-15min (%) ajustada por macro, GEX y Dark Pool
2. Señales más fuertes — especialmente GEX (niveles de opciones) y Dark Pool (flujo institucional)
3. Alineación o contradicción macro vs técnico
4. Niveles clave: usa los niveles GEX (Gamma Flip, Call Wall, Put Wall) como soportes/resistencias
5. Acción inmediata considerando los niveles GEX"""

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
print("   US500 MONITOR v3.8 — FILTRO TENDENCIA AGRESIVO")
print("=" * 60)
print("  GEX + Dark Pool + Macro + DXY + MOVE + Liquidez")
print("  NUEVO v3.8: Filtro tendencia agresivo — señales bajistas")
print("  cuando precio hace minimos mas bajos bajo Gamma Flip")
print(f"  Umbral: ±{UMBRAL_SCORE}/10 | Min entre alertas: {TIEMPO_MIN_ALERTAS} min")
print("=" * 60)

while True:
    try:
        inicio_ciclo = time.time()
        ahora_ny     = hora_ny()
        abierto      = mercado_abierto()
        minutos      = minutos_desde_apertura()

        # Mercado cerrado
        if not abierto:
            if estado_mercado_enviado:
                spy_temp = descargar_datos()
                if spy_temp:
                    precio_final = float(spy_temp["close"]["^GSPC"].iloc[-1])
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"🔕 *MERCADO CERRADO*\nUS500 final: `{precio_final:.2f}`\nHasta mañana. 🌙",
                            parse_mode="Markdown")
                    except:
                        bot.send_message(TELEGRAM_CHAT_ID,
                            f"MERCADO CERRADO\nUS500 final: {precio_final:.2f}\nHasta mañana.")
                estado_mercado_enviado = False
                resetear_detector_agotamiento()
                # Resetear GEX para nueva sesión
                gex_niveles["disponible"] = False
                gex_niveles["ultima_actualizacion"] = None

            elapsed = time.time() - inicio_ciclo
            time.sleep(max(0, 60 - elapsed))
            contador_ciclos += 1
            continue

        # Actualizar macro si corresponde
        if necesita_actualizar_macro():
            actualizar_contexto_macro(enviar_telegram=True)

        # Descargar datos
        datos = descargar_datos()
        if datos is None:
            print(f"[{ahora_ny.strftime('%H:%M')}] ⚠️ Sin datos")
            elapsed = time.time() - inicio_ciclo
            time.sleep(max(0, 60 - elapsed))
            contador_ciclos += 1
            continue

        spy_precio = float(datos["close"]["^GSPC"].iloc[-1])
        vix_precio = float(datos["close"]["^VIX"].iloc[-1])

        # Mensaje de apertura + GEX + Dark Pool
        if not estado_mercado_enviado:
            print("  [INIT] Obteniendo niveles GEX...")
            obtener_gex()
            print("  [INIT] Obteniendo datos Dark Pool...")
            obtener_dark_pool()

            macro_str = contexto_macro.get("impacto", "calculando...")
            gex_msg   = ""
            if gex_niveles["disponible"]:
                est = " (est.)" if gex_niveles.get("es_estimado") else ""
                gex_msg = (f"\n⚡ GEX{est}: Flip:`{gex_niveles['gamma_flip']}` | "
                           f"Call:`{gex_niveles['call_wall']}` | Put:`{gex_niveles['put_wall']}`")
            try:
                bot.send_message(TELEGRAM_CHAT_ID,
                    f"🔔 *MERCADO ABIERTO — US500 v3.7*\n"
                    f"US500: `{spy_precio:.2f}` | VIX: `{vix_precio:.2f}`\n"
                    f"Macro: `{macro_str}`{gex_msg}\nSistema v3.7 activo. Ciclo: 1 min.",
                    parse_mode="Markdown")
            except:
                bot.send_message(TELEGRAM_CHAT_ID,
                    f"MERCADO ABIERTO US500 v3.7\nUS500: {spy_precio:.2f} | VIX: {vix_precio:.2f}\n"
                    f"Macro: {macro_str}\nSistema v3.7 activo.")
            estado_mercado_enviado = True

        # Calcular score
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

        # Log consola
        comps_activos = {k: v for k, v in resultado["componentes"].items() if v != 0}
        tags = ""
        if pen_rsi  != 0: tags += f" [RSI:{pen_rsi:+d}]"
        if pen_tend != 0: tags += f" [TEND:{pen_tend:+d}]"
        if fatiga >= MINUTOS_VIX_RATIO_FATIGA: tags += f" [FAT:{fatiga}m]"
        if liq["alerta"]: tags += f" [LIQ:{liq['nivel'][:3]}]"
        if dxy.get("disponible"): tags += f" [DXY:{dxy.get('señal','?')[:5]}]"
        if gex.get("disponible"): tags += f" [GEX:{gex.get('score',0):+d}]"
        if dp.get("disponible"):  tags += f" [DP:{dp.get('ratio',0):.0%}]"
        tags += f" [{contexto_macro['impacto'][:3]}]"
        print(f"[{ahora_ny.strftime('%H:%M')}] {detalle['precio']:.2f} | Score:{score:+d}{tags} | "
              f"RSI:{detalle['rsi']} | VIX:{detalle['vix_nivel']} | EMA:{'↑' if tend.get('sobre_ema') else '↓'}"
              + (f" | {comps_activos}" if comps_activos else ""))

        # Detector de agotamiento
        if estado_agotamiento["activo"] and not estado_agotamiento["alerta_enviada"]:
            if evaluar_agotamiento(resultado):
                enviar_alerta_agotamiento(resultado)

        # Alertas principales
        if score >= UMBRAL_SCORE:
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
