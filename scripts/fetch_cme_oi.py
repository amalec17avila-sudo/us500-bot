"""
fetch_cme_oi.py - Descarga OI diario de ES desde CME Group
Corre en GitHub Actions (no tiene restricciones de red)
"""

import json, re, urllib.request, urllib.error
from datetime import datetime, date
from pathlib import Path
import io

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "es_oi.json"
OUTPUT_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Referer": "https://www.cmegroup.com/market-data/volume-open-interest/equity-volume.html",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
}

def fetch_pdf_text():
    url = ("https://www.cmegroup.com/daily_bulletin/current/"
           "Section01C_Summary_Volume_And_Open_Interest_Equity_Index_Futures_And_Options.pdf")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            pdf_bytes = resp.read()
        print(f"✅ PDF: {len(pdf_bytes):,} bytes")

        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            return text
        except ImportError:
            # Fallback: try pypdf
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                return text
            except Exception as e:
                print(f"❌ PDF parse error: {e}")
                return None
    except Exception as e:
        print(f"❌ PDF download error: {e}")
        return None

def parse_es_oi(text):
    if not text:
        return None, None

    print(f"  Texto total: {len(text)} chars")

    # Encontrar sección E-MINI S&P 500 FUTURES (no MICRO, no OPTIONS)
    # Buscar específicamente la línea de futuros
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if 'E-MINI S&P 500 FUTURES' in line and 'MICRO' not in line and 'OPTION' not in line:
            print(f"  Línea encontrada [{i}]: '{line}'")
            # Extraer todos los números de la línea
            numbers = re.findall(r'\d+', line.replace(',', ''))
            print(f"  Números: {numbers}")
            
            # También revisar línea siguiente por si el dato está partido
            if i + 1 < len(lines):
                next_line = lines[i+1]
                print(f"  Línea siguiente [{i+1}]: '{next_line}'")
                # Buscar signo + o - en línea o siguiente
                combined = line + ' ' + next_line
                
                # Patrón: buscar el signo de cambio
                match_cambio = re.search(r'([+-])\s*(\d+)', combined)
                
                if len(numbers) >= 3 and match_cambio:
                    # El OI actual suele ser el 3er o 4to número grande
                    # Filtrar números pequeños (< 1000)
                    big_numbers = [int(n) for n in numbers if int(n) > 1000]
                    print(f"  Números grandes: {big_numbers}")
                    
                    if len(big_numbers) >= 2:
                        oi_actual = big_numbers[1]  # Segundo número grande = OI actual
                        signo = match_cambio.group(1)
                        cambio = int(match_cambio.group(2))
                        if signo == '-':
                            cambio = -cambio
                        print(f"  ✅ OI: {oi_actual:,} | Cambio: {cambio:+,}")
                        return oi_actual, cambio
            break

    # Si no encontró con el método anterior, buscar con regex más amplio
    # Intentar con el texto completo
    pattern = r'E-MINI S&P 500 FUTURES[^\n]*?(\d[\d,]+)\s+(\d[\d,]+)\s+([+-]?\s*\d[\d,]*)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        try:
            oi_actual = int(match.group(2).replace(',', ''))
            cambio_str = match.group(3).replace(' ', '').replace(',', '')
            cambio = int(cambio_str)
            print(f"  ✅ Regex amplio: OI={oi_actual:,} | Cambio={cambio:+,}")
            return oi_actual, cambio
        except: pass

    print("  ⚠️ No se pudo extraer OI")
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
        "oi_actual": oi_actual,
        "cambio_diario": cambio,
        "cambio_semanal": cambio_semana,
        "fecha": hoy,
        "ultima_actualizacion": datetime.utcnow().isoformat(),
        "historial": historial,
    }
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(resultado, f, indent=2)
    print(f"✅ Guardado: OI={oi_actual:,} | Diario={cambio:+,} | Semanal={cambio_semana:+,}")

def main():
    print(f"=== CME ES OI Fetcher — {date.today()} ===")
    text = fetch_pdf_text()
    oi, cambio = parse_es_oi(text)

    if oi and oi > 0:
        guardar_datos(oi, cambio or 0)
        print("=== Completado ✅ ===")
    else:
        print("❌ No se pudo obtener OI — guardando error")
        # Crear archivo con error para que git add no falle
        hoy = date.today().isoformat()
        datos = cargar_historial()
        with open(OUTPUT_FILE, 'w') as f:
            json.dump({
                "oi_actual": 0, "cambio_diario": 0, "cambio_semanal": 0,
                "fecha": hoy, "error": "PDF no parseado",
                "historial": datos.get("historial", [])
            }, f, indent=2)

if __name__ == "__main__":
    main()
