"""
fetch_cme_oi.py - Descarga OI diario de ES desde CME Group
"""

import json, re, urllib.request, urllib.error, ssl
from datetime import datetime, date
from pathlib import Path
import io, time, random

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "es_oi.json"
OUTPUT_FILE.parent.mkdir(exist_ok=True)

def fetch_pdf_text():
    """Intenta múltiples métodos para descargar el PDF del CME."""
    
    url = ("https://www.cmegroup.com/daily_bulletin/current/"
           "Section01C_Summary_Volume_And_Open_Interest_Equity_Index_Futures_And_Options.pdf")
    
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    
    for ua in user_agents:
        try:
            ctx = ssl.create_default_context()
            
            headers = {
                "User-Agent": ua,
                "Accept": "application/pdf,text/html,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Referer": "https://www.cmegroup.com/market-data/volume-open-interest/equity-volume.html",
            }
            
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                pdf_bytes = resp.read()
            
            print(f"✅ PDF descargado: {len(pdf_bytes):,} bytes")
            
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            return text
            
        except urllib.error.HTTPError as e:
            print(f"⚠️ HTTP {e.code} con UA: {ua[:50]}...")
            time.sleep(2)
        except Exception as e:
            print(f"⚠️ Error: {e}")
            time.sleep(2)
    
    # Fallback: intentar URL alternativa del CME
    try:
        alt_url = "https://www.cmegroup.com/CmeWS/mvc/Volume/V2/FUTURES/ES"
        req = urllib.request.Request(alt_url, headers={
            "User-Agent": user_agents[0],
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            import json as json_mod
            data = json_mod.loads(resp.read().decode())
            print(f"✅ API JSON: {data}")
            if "openInterest" in str(data):
                return str(data)
    except Exception as e:
        print(f"⚠️ API fallback: {e}")
    
    return None

def parse_es_oi(text):
    if not text:
        return None, None

    lines = text.split('\n')
    for i, line in enumerate(lines):
        if 'E-MINI S&P 500 FUTURES' in line and 'MICRO' not in line and 'OPTION' not in line:
            print(f"  Línea [{i}]: '{line.strip()}'")
            combined = line + (' ' + lines[i+1] if i + 1 < len(lines) else '')
            
            all_numbers = [int(n.replace(',','')) for n in re.findall(r'[\d,]+', combined)]
            oi_candidates = [n for n in all_numbers if n > 500_000]
            print(f"  Candidatos OI (>500K): {oi_candidates}")
            
            if len(oi_candidates) >= 2:
                oi_actual = oi_candidates[1]
                cambio_match = re.search(r'([+-])\s*(\d{1,6})\b', combined)
                cambio = 0
                if cambio_match:
                    signo = cambio_match.group(1)
                    cambio = int(cambio_match.group(2))
                    if signo == '-': cambio = -cambio
                print(f"  ✅ OI: {oi_actual:,} | Cambio: {cambio:+,}")
                return oi_actual, cambio
    
    return None, None

def cargar_historial():
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f)
        except: pass
    return {"historial": []}

def calcular_cambio_semanal_cot(historial, oi_actual):
    """
    Calcula el cambio semanal respetando la ventana del COT de la CFTC:
    miércoles → martes de la semana siguiente.
    
    Busca el miércoles más reciente en el historial como punto de partida.
    Si no hay miércoles disponible (primeros días), usa el dato más antiguo disponible.
    """
    if not historial:
        return 0

    # Buscar el miércoles más reciente ANTERIOR al día actual
    # weekday(): lunes=0, martes=1, miércoles=2, jueves=3, viernes=4
    # Excluimos el día actual para no usar el miércoles de hoy como base de hoy mismo
    hoy_str = date.today().isoformat()
    miercoles_entries = [
        h for h in historial
        if datetime.strptime(h["fecha"], "%Y-%m-%d").weekday() == 2  # miércoles
        and h["fecha"] < hoy_str  # estrictamente anterior a hoy
    ]

    if miercoles_entries:
        # Tomar el miércoles más reciente como base
        miercoles_base = miercoles_entries[-1]
        cambio = oi_actual - miercoles_base["oi"]
        print(f"  [COT_VENTANA] Base miércoles {miercoles_base['fecha']}: OI={miercoles_base['oi']:,} → Cambio={cambio:+,}")
        return cambio
    else:
        # Sin miércoles en historial — usar el dato más antiguo disponible
        oi_base = historial[0]["oi"]
        cambio = oi_actual - oi_base
        print(f"  [COT_VENTANA] Sin miércoles — usando base {historial[0]['fecha']}: OI={oi_base:,} → Cambio={cambio:+,}")
        return cambio

def guardar_datos(oi_actual, cambio):
    hoy = date.today().isoformat()
    datos = cargar_historial()
    historial = [h for h in datos.get("historial", []) if h.get("fecha") != hoy]
    historial.append({"fecha": hoy, "oi": oi_actual, "cambio": cambio})
    historial = sorted(historial, key=lambda x: x["fecha"])[-30:]

    # Cambio semanal respetando ventana miércoles→martes del COT CFTC
    cambio_semana = calcular_cambio_semanal_cot(historial, oi_actual)

    resultado = {
        "oi_actual": oi_actual, "cambio_diario": cambio,
        "cambio_semanal": cambio_semana, "fecha": hoy,
        "ultima_actualizacion": datetime.utcnow().isoformat(),
        "historial": historial,
    }
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(resultado, f, indent=2)
    print(f"✅ Guardado: OI={oi_actual:,} | Diario={cambio:+,} | Semanal(miérc-base)={cambio_semana:+,}")

def main():
    print(f"=== CME ES OI Fetcher — {date.today()} ===")
    text = fetch_pdf_text()
    oi, cambio = parse_es_oi(text)

    if oi and oi > 1_000_000:
        guardar_datos(oi, cambio or 0)
        print("=== Completado ✅ ===")
    else:
        print(f"❌ OI inválido ({oi})")
        datos = cargar_historial()
        with open(OUTPUT_FILE, 'w') as f:
            json.dump({
                "oi_actual": 0, "cambio_diario": 0, "cambio_semanal": 0,
                "fecha": date.today().isoformat(), "error": "CME bloqueó acceso",
                "historial": datos.get("historial", [])
            }, f, indent=2)

if __name__ == "__main__":
    main()
