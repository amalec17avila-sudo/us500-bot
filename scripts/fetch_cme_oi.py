"""
fetch_cme_oi.py
---------------
Descarga el Open Interest diario de E-Mini S&P 500 (ES) desde CME Group.
Guarda el resultado en data/es_oi.json para que el bot lo lea.

Fuentes en orden de prioridad:
1. CME Daily Bulletin PDF (Section 01C)
2. CME web scraping como fallback
"""

import json
import os
import re
import urllib.request
from datetime import datetime, date
from pathlib import Path

# ── Configuración ────────────────────────────────────────────
OUTPUT_FILE = Path(__file__).parent.parent / "data" / "es_oi.json"
OUTPUT_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/pdf,text/html,*/*",
    "Referer": "https://www.cmegroup.com/",
}

def fetch_cme_pdf():
    """Descarga y parsea el PDF del boletín diario de CME."""
    try:
        import pdfplumber
        import io

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

        # Buscar línea de E-MINI S&P 500 FUTURES (no Micro, no Options)
        # Formato: OI_PREV  NOMBRE  OI_ACTUAL  +/- CAMBIO  ...  ES
        patterns = [
            r'(\d+)\s+E-MINI S&P 500 FUTURES\s+(\d+)\s+([+-]\s*\d+)',
            r'E-MINI S&P 500 FUTURES\s+(\d+)\s+([+-]\s*\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                groups = match.groups()
                if len(groups) == 3:
                    oi_prev   = int(groups[0].replace(',', ''))
                    oi_actual = int(groups[1].replace(',', ''))
                    cambio    = int(groups[2].replace(' ', '').replace(',', ''))
                elif len(groups) == 2:
                    oi_actual = int(groups[0].replace(',', ''))
                    cambio    = int(groups[1].replace(' ', '').replace(',', ''))
                    oi_prev   = oi_actual - cambio
                else:
                    continue
                    
                print(f"✅ ES OI encontrado: {oi_actual:,} (cambio: {cambio:+,})")
                return oi_actual, cambio, oi_prev

        print("⚠️ Patrón ES no encontrado en PDF")
        print(f"  Fragmento relevante: {text[text.find('E-MINI S&P'):text.find('E-MINI S&P')+200] if 'E-MINI S&P' in text else 'NO ENCONTRADO'}")
        return None, None, None

    except Exception as e:
        print(f"❌ Error PDF: {e}")
        return None, None, None


def fetch_cme_web():
    """Fallback: scraping de la página web de CME."""
    try:
        url = "https://www.cmegroup.com/market-data/browse-data/equity-volume.html"
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode('utf-8', errors='replace')

        # Buscar OI de ES en el HTML
        pattern = r'E-Mini S&P 500[^<]*</td>[^<]*<td[^>]*>[\d,]+</td>[^<]*<td[^>]*>([\d,]+)</td>'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            oi = int(match.group(1).replace(',', ''))
            print(f"✅ ES OI web: {oi:,}")
            return oi, None, None

        print("⚠️ Patrón ES no encontrado en web")
        return None, None, None

    except Exception as e:
        print(f"❌ Error web: {e}")
        return None, None, None


def cargar_historial():
    """Carga el historial existente."""
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"historial": [], "ultima_actualizacion": None}


def guardar_datos(oi_actual, cambio, oi_prev):
    """Guarda el OI en el archivo JSON."""
    hoy   = date.today().isoformat()
    datos = cargar_historial()
    
    # Evitar duplicados del mismo día
    historial = datos.get("historial", [])
    historial = [h for h in historial if h.get("fecha") != hoy]
    
    entrada = {
        "fecha":    hoy,
        "oi":       oi_actual,
        "cambio":   cambio,
        "oi_prev":  oi_prev,
    }
    historial.append(entrada)
    
    # Mantener últimos 30 días
    historial = sorted(historial, key=lambda x: x["fecha"])[-30:]
    
    # Calcular tendencia semanal (últimos 5 días)
    if len(historial) >= 5:
        oi_hace_5 = historial[-5]["oi"]
        cambio_semana = oi_actual - oi_hace_5
    else:
        cambio_semana = 0

    datos_finales = {
        "oi_actual":         oi_actual,
        "cambio_diario":     cambio,
        "cambio_semanal":    cambio_semana,
        "fecha":             hoy,
        "ultima_actualizacion": datetime.utcnow().isoformat(),
        "historial":         historial,
    }

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(datos_finales, f, indent=2)

    print(f"✅ Guardado en {OUTPUT_FILE}")
    print(f"   OI: {oi_actual:,} | Cambio diario: {cambio:+,} | Cambio semanal: {cambio_semana:+,}")
    return datos_finales


def main():
    print(f"=== CME ES OI Fetcher — {date.today()} ===")

    # Intentar PDF primero
    oi, cambio, oi_prev = fetch_cme_pdf()

    # Fallback a web si falla
    if oi is None:
        print("Intentando fallback web...")
        oi, cambio, oi_prev = fetch_cme_web()

    if oi is None:
        print("❌ No se pudo obtener OI — cargando último valor conocido")
        datos = cargar_historial()
        if datos.get("historial"):
            ultimo = datos["historial"][-1]
            print(f"  Último valor: {ultimo['oi']:,} ({ultimo['fecha']})")
        return

    guardar_datos(oi, cambio or 0, oi_prev or 0)
    print("=== Completado ===")


if __name__ == "__main__":
    main()
