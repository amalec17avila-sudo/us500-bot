"""
fetch_cme_oi.py - Descarga OI diario de ES desde CME Group
Corre en GitHub Actions (sin restricciones de red)

Formato real del PDF (según logs):
'ES E-MINI S&P 500 FUTURES 1842824 15867 1858691 2186454 - 2758 959819 2169180'

Números en orden:
- 1842824 → OI 52 semanas
- 15867   → Volumen Globex (pequeño, < 100K a veces)
- 1858691 → OI ACTUAL ← este queremos
- 2186454 → OI previo
- 2758    → cambio (con signo + o -)
"""

import json, re, urllib.request
from datetime import datetime, date
from pathlib import Path
import io

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "es_oi.json"
OUTPUT_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Referer": "https://www.cmegroup.com/",
}

def fetch_pdf_text():
    url = ("https://www.cmegroup.com/daily_bulletin/current/"
           "Section01C_Summary_Volume_And_Open_Interest_Equity_Index_Futures_And_Options.pdf")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            pdf_bytes = resp.read()
        print(f"✅ PDF: {len(pdf_bytes):,} bytes")
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        return text
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def parse_es_oi(text):
    if not text:
        return None, None

    lines = text.split('\n')
    for i, line in enumerate(lines):
        if 'E-MINI S&P 500 FUTURES' in line and 'MICRO' not in line and 'OPTION' not in line:
            print(f"  Línea [{i}]: '{line.strip()}'")

            combined = line
            if i + 1 < len(lines):
                combined = line + ' ' + lines[i+1]

            # Extraer todos los números
            all_numbers = [int(n.replace(',','')) for n in re.findall(r'[\d,]+', combined)]
            print(f"  Todos los números: {all_numbers}")

            # OI siempre > 500,000 (millones de contratos)
            # Formato: OI_52W  GLOBEX_VOL  OI_ACTUAL  OI_PREV  CAMBIO
            # OI_52W y OI_ACTUAL son los únicos > 500K normalmente
            oi_candidates = [n for n in all_numbers if n > 500_000]
            print(f"  Candidatos OI (>500K): {oi_candidates}")

            if len(oi_candidates) >= 2:
                # El segundo número > 500K es el OI actual
                oi_actual = oi_candidates[1]
            elif len(oi_candidates) == 1:
                oi_actual = oi_candidates[0]
            else:
                print("  ⚠️ No se encontraron candidatos OI")
                continue

            # Buscar el cambio (+/- número pequeño < 100K)
            cambio_match = re.search(r'([+-])\s*(\d{1,6})\b', combined)
            cambio = 0
            if cambio_match:
                signo  = cambio_match.group(1)
                cambio = int(cambio_match.group(2))
                if signo == '-':
                    cambio = -cambio

            print(f"  ✅ OI: {oi_actual:,} | Cambio: {cambio:+,}")
            return oi_actual, cambio

    print("  ⚠️ Línea E-MINI S&P 500 FUTURES no encontrada")
    return None, None

def cargar_historial():
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f)
        except: pass
    return {"historial": []}

def guardar_datos(oi_actual, cambio):
    hoy = date.today().isoformat()
    datos = cargar_historial()
    historial = [h for h in datos.get("historial", []) if h.get("fecha") != hoy]
    historial.append({"fecha": hoy, "oi": oi_actual, "cambio": cambio})
    historial = sorted(historial, key=lambda x: x["fecha"])[-30:]
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
                "fecha": date.today().isoformat(), "error": f"OI inválido: {oi}",
                "historial": datos.get("historial", [])
            }, f, indent=2)

if __name__ == "__main__":
    main()
