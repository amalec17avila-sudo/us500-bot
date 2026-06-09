"""
fetch_cme_oi.py
---------------
Descarga el Open Interest diario de E-Mini S&P 500 (ES) desde CME Group.
Guarda el resultado en data/es_oi.json para que el bot lo lea.
"""

import json
import re
import urllib.request
from datetime import datetime, date
from pathlib import Path

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "es_oi.json"
OUTPUT_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,*/*",
    "Referer": "https://www.cmegroup.com/",
}

def fetch_cme_pdf():
    """Descarga y parsea el PDF del boletín diario de CME."""
    try:
        import pdfplumber, io

        url = ("https://www.cmegroup.com/daily_bulletin/current/"
               "Section01C_Summary_Volume_And_Open_Interest_Equity_Index_Futures_And_Options.pdf")

        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            pdf_bytes = resp.read()

        print(f"✅ PDF descargado: {len(pdf_bytes):,} bytes")

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = ""
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"

        # Formato real del PDF:
        # E-MINI S&P 500 FUTURES  OI_52W  GLOBEX_VOL  OI_ACTUAL  OI_PREV  +/- CAMBIO
        # Ejemplo: E-MINI S&P 500 FUTURES 2384037 17459 2401496 2189212 + 18223 ...
        pattern = r'E-MINI S&P 500 FUTURES\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([+-])\s*(\d+)'
        match = re.search(pattern, text)

        if match:
            groups    = match.groups()
            oi_actual = int(groups[2])
            signo     = groups[4]
            cambio    = int(groups[5])
            if signo == '-':
                cambio = -cambio
            oi_prev   = oi_actual - cambio

            print(f"✅ ES OI: {oi_actual:,} | Cambio: {cambio:+,}")
            return oi_actual, cambio, oi_prev

        # Intentar patrón alternativo más flexible
        pattern2 = r'E-MINI S&P 500 FUTURES\s+\d+\s+\d+\s+(\d+)\s+\d+\s+([+-]\s*\d+)'
        match2 = re.search(pattern2, text)
        if match2:
            oi_actual  = int(match2.group(1))
            cambio_str = match2.group(2).replace(' ', '')
            cambio     = int(cambio_str)
            oi_prev    = oi_actual - cambio
            print(f"✅ ES OI (alt): {oi_actual:,} | Cambio: {cambio:+,}")
            return oi_actual, cambio, oi_prev

        # Mostrar fragmento para debug
        idx = text.find("E-MINI S&P 500 FUTURES")
        if idx >= 0:
            fragmento = text[idx:idx+200].replace('\n', ' ')
            print(f"⚠️ Patrón no coincide. Fragmento: {fragmento}")
        else:
            print("⚠️ 'E-MINI S&P 500 FUTURES' no encontrado en el texto")

        return None, None, None

    except Exception as e:
        print(f"❌ Error PDF: {e}")
        import traceback; traceback.print_exc()
        return None, None, None


def cargar_historial():
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"historial": []}


def guardar_datos(oi_actual, cambio, oi_prev):
    hoy     = date.today().isoformat()
    datos   = cargar_historial()
    historial = [h for h in datos.get("historial", []) if h.get("fecha") != hoy]
    historial.append({"fecha": hoy, "oi": oi_actual, "cambio": cambio, "oi_prev": oi_prev})
    historial = sorted(historial, key=lambda x: x["fecha"])[-30:]

    # Cambio semanal (últimos 5 días hábiles)
    cambio_semana = oi_actual - historial[-5]["oi"] if len(historial) >= 5 else cambio

    resultado = {
        "oi_actual":            oi_actual,
        "cambio_diario":        cambio,
        "cambio_semanal":       cambio_semana,
        "fecha":                hoy,
        "ultima_actualizacion": datetime.utcnow().isoformat(),
        "historial":            historial,
    }

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(resultado, f, indent=2)

    print(f"✅ Guardado: OI={oi_actual:,} | Diario={cambio:+,} | Semanal={cambio_semana:+,}")
    return resultado


def main():
    print(f"=== CME ES OI Fetcher — {date.today()} ===")
    oi, cambio, oi_prev = fetch_cme_pdf()

    if oi is None:
        print("❌ No se pudo obtener OI")
        # Crear archivo vacío para que git add no falle
        if not OUTPUT_FILE.exists():
            with open(OUTPUT_FILE, 'w') as f:
                json.dump({"oi_actual": 0, "cambio_diario": 0, "cambio_semanal": 0,
                           "fecha": date.today().isoformat(), "historial": [],
                           "error": "PDF no parseado"}, f, indent=2)
        return

    guardar_datos(oi, cambio or 0, oi_prev or 0)
    print("=== Completado ===")


if __name__ == "__main__":
    main()
