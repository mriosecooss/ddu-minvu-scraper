#!/usr/bin/env python3
"""
Scraper e indexador de Circulares DDU del MINVU para un vault de Obsidian.

Uso:
    py ddu_scraper.py --scrape-only          # solo escanea y cuenta, no descarga nada
    py ddu_scraper.py                        # corrida normal (incremental)
    py ddu_scraper.py --force                # reprocesa todo, ignorando state.json
    py ddu_scraper.py --limit 20             # limita a las primeras N DDU (pruebas)

Requiere: requests, pypdf, pdf2image, pytesseract, Pillow (ver requirements.txt).
Para el fallback OCR se necesitan además los binarios de Tesseract y Poppler
instalados en el sistema (ver README.md).
"""

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from pdf2image import convert_from_path
    import pytesseract
except ImportError:
    convert_from_path = None
    pytesseract = None


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "ddu_raw"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "ddu_scraper.log"

# Carpeta 02-Normativa del vault de Obsidian (destino final de los .md)
VAULT_OUTPUT_DIR = Path(r"C:\Users\Miguel\Desktop\Vault Claude\02 Normativa")

SOURCES = [
    ("generales_por_numero", "https://www.minvu.gob.cl/elementos-tecnicos/circulares-generales-ddu-por-numero/"),
    ("generales_por_materia", "https://www.minvu.gob.cl/elementos-tecnicos/circulares-generales-ddu-por-materia/"),
    ("especificas_por_numero", "https://www.minvu.gob.cl/elementos-tecnicos/circulares-especificas-ddu-por-numero/"),
    ("especificas_por_materia", "https://www.minvu.gob.cl/elementos-tecnicos/circulares-especificas-ddu-por-materia/"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ddu-indexer/1.0"}

RATE_LIMIT_MIN = 1.0
RATE_LIMIT_MAX = 2.0
MAX_RETRIES = 3
MIN_TEXT_CHARS_PER_PAGE = 120  # bajo este umbral se asume PDF escaneado -> OCR

# Poppler/Tesseract instalados vía winget en este equipo. Se pueden sobrescribir
# con variables de entorno POPPLER_PATH / TESSERACT_CMD / TESSDATA_DIR si migras
# el script a otra máquina.
_DEFAULT_POPPLER_PATH = (
    r"C:\Users\Miguel\AppData\Local\Microsoft\WinGet\Packages"
    r"\oschwartz10612.Poppler_Microsoft.Winget.Source_8wekyb3d8bbwe\poppler-25.07.0\Library\bin"
)
_DEFAULT_TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
_DEFAULT_TESSDATA_DIR = str(BASE_DIR / "tessdata")  # contiene spa.traineddata (sin permisos admin)

POPPLER_PATH = os.environ.get("POPPLER_PATH", _DEFAULT_POPPLER_PATH)
TESSERACT_CMD = os.environ.get("TESSERACT_CMD", _DEFAULT_TESSERACT_CMD)
TESSDATA_DIR = os.environ.get("TESSDATA_DIR", _DEFAULT_TESSDATA_DIR)

if TESSERACT_CMD and pytesseract and Path(TESSERACT_CMD).exists():
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
if not Path(POPPLER_PATH).exists():
    POPPLER_PATH = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("ddu_scraper")
logger.setLevel(logging.DEBUG)

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("%(message)s"))

logger.addHandler(_fh)
logger.addHandler(_ch)


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------

@dataclass
class DDUEntry:
    numero: str
    titulo: str
    pdf_url: str
    fuentes: list = field(default_factory=list)

    # Se completan en fases posteriores
    fecha: str = ""
    circular_ord: str = ""
    articulos_citados: list = field(default_factory=list)
    leyes_citadas: list = field(default_factory=list)
    relaciones: list = field(default_factory=list)
    estado: str = "vigente"
    pdf_path: str = ""
    texto: str = ""
    ocr_used: bool = False


# ---------------------------------------------------------------------------
# 1. SCRAPER
# ---------------------------------------------------------------------------

LINK_RE = re.compile(
    r'<a\s+href="([^"]+\.pdf)"[^>]*class="link_pdf"[^>]*title="([^"]*)"',
    re.IGNORECASE | re.DOTALL,
)
NEXT_PAGE_RE = re.compile(
    r'<a[^>]+class="[^"]*next[^"]*"[^>]+href="([^"]+)"', re.IGNORECASE
)


def fetch(url, session, max_retries=MAX_RETRIES):
    """GET con reintentos y backoff simple."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Intento %d/%d fallido para %s: %s", attempt, max_retries, url, exc)
            time.sleep(2 * attempt)
    logger.error("No se pudo obtener %s tras %d intentos: %s", url, max_retries, last_exc)
    return None


def extract_numero(title: str, url: str = "") -> str | None:
    """Normaliza el número de una DDU a partir del título o, si falla, del nombre de archivo.

    IMPORTANTE: los patrones se anclan al INICIO del título (con re.match, no re.search).
    Muchos títulos de DDU generales mencionan más adelante otra circular relacionada
    (ej. "DDU 430 ... Deja sin efecto ... DDU-ESPECÍFICA 29/2007") — si se buscara la
    referencia ESP en cualquier parte del texto, se confundiría el número propio del
    documento con el de la circular citada.

    Si el título es explícitamente un PDF de índice ("Índice Circulares... hasta la
    DDU NNN"), se rechaza de inmediato sin probar el fallback de nombre de archivo:
    algunos índices están alojados bajo una URL con nombre de archivo idéntico al de
    un circular individual real (ej. "DDU-422.pdf"), y el fallback los capturaría
    igual si no se corta acá primero.
    """
    if re.match(r'\s*[IÍ]ndice\b', html.unescape(title or ""), re.IGNORECASE):
        return None

    for candidate in (title, os.path.basename(url)):
        if not candidate:
            continue
        t = html.unescape(candidate)

        # DDU específica: "DDU-ESP 001-07", "DDU-ESPECÍFICA N°16/2008", "DDU ESP 006-08"
        m = re.match(
            r'\s*DDU[\s\-]?ESP(?:EC[IÍ]FICA)?\.?\s*N?[°ºo]?\.?\s*[:\-]?\s*(\d{1,4})\s*[\/\-]\s*(\d{2,4})',
            t, re.IGNORECASE,
        )
        if m:
            return f"DDU-ESP-{m.group(1).zfill(3)}-{m.group(2)}"

        m = re.match(
            r'\s*DDU[\s\-]?ESP(?:EC[IÍ]FICA)?\.?\s*N?[°ºo]?\.?\s*[:\-]?\s*(\d{1,4})',
            t, re.IGNORECASE,
        )
        if m:
            return f"DDU-ESP-{m.group(1).zfill(3)}"

        # DDU general: "DDU 546", "DDU-149" (con espacio o guion)
        m = re.match(r'\s*DDU[\s\-]+(\d{1,4})\b', t, re.IGNORECASE)
        if m:
            return f"DDU-{m.group(1)}"

    return None


def parse_listing_page(html_text: str) -> list[tuple[str, str]]:
    """Devuelve lista de (pdf_url, title) encontrados en una página de listado."""
    return LINK_RE.findall(html_text)


def scrape_source(label: str, start_url: str, session: requests.Session) -> list[tuple[str, str]]:
    """Recorre la paginación (si existe) de una fuente y devuelve todos los (url, title)."""
    results = []
    url = start_url
    seen_urls = set()
    page_num = 1
    while url and url not in seen_urls and page_num <= 50:
        seen_urls.add(url)
        logger.info("[%s] descargando página %d: %s", label, page_num, url)
        resp = fetch(url, session)
        if resp is None:
            break
        found = parse_listing_page(resp.text)
        logger.info("[%s] página %d: %d enlaces PDF encontrados", label, page_num, len(found))
        results.extend(found)

        next_match = NEXT_PAGE_RE.search(resp.text)
        url = urljoin(start_url, next_match.group(1)) if next_match else None
        page_num += 1
        if url:
            time.sleep(random.uniform(RATE_LIMIT_MIN, RATE_LIMIT_MAX))
    return results


def run_scraper(session: requests.Session) -> dict[str, DDUEntry]:
    """Ejecuta el paso 1 completo: scrapea las 4 fuentes y deduplica por número."""
    entries: dict[str, DDUEntry] = {}
    skipped_non_ddu = 0

    for label, url in SOURCES:
        raw = scrape_source(label, url, session)
        for pdf_url, title in raw:
            title_clean = html.unescape(title).strip()
            numero = extract_numero(title_clean, pdf_url)
            if numero is None:
                skipped_non_ddu += 1
                logger.debug("[%s] omitido (no es DDU numerada): %s", label, title_clean[:100])
                continue
            if numero in entries:
                if label not in entries[numero].fuentes:
                    entries[numero].fuentes.append(label)
                continue
            entries[numero] = DDUEntry(
                numero=numero,
                titulo=title_clean,
                pdf_url=pdf_url,
                fuentes=[label],
            )

    logger.info("Enlaces omitidos (índices u otros no-DDU): %d", skipped_non_ddu)
    return entries


# ---------------------------------------------------------------------------
# 2. DOWNLOADER
# ---------------------------------------------------------------------------

def normalize_filename(numero: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-]", "_", numero) + ".pdf"


def download_pdf(entry: DDUEntry, session: requests.Session) -> bool:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_DIR / normalize_filename(entry.numero)
    entry.pdf_path = str(dest)

    if dest.exists() and dest.stat().st_size > 0:
        logger.debug("%s ya descargado, se omite.", entry.numero)
        return True

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(entry.pdf_url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            content = resp.content
            if len(content) < 100 or not content.startswith(b"%PDF"):
                raise ValueError(f"Contenido no parece un PDF válido ({len(content)} bytes)")
            dest.write_bytes(content)
            logger.info("Descargado %s (%d KB)", entry.numero, len(content) // 1024)
            time.sleep(random.uniform(RATE_LIMIT_MIN, RATE_LIMIT_MAX))
            return True
        except Exception as exc:
            logger.warning(
                "Intento %d/%d fallido descargando %s (%s): %s",
                attempt, MAX_RETRIES, entry.numero, entry.pdf_url, exc,
            )
            time.sleep(2 * attempt)

    logger.error("FALLO descarga definitiva: %s (%s)", entry.numero, entry.pdf_url)
    return False


# ---------------------------------------------------------------------------
# 3. EXTRACCIÓN DE TEXTO (pypdf + OCR fallback)
# ---------------------------------------------------------------------------

def extract_text_pypdf(pdf_path: Path) -> tuple[str, int]:
    if PdfReader is None:
        raise RuntimeError("pypdf no está instalado")
    reader = PdfReader(str(pdf_path))
    pages_text = []
    for page in reader.pages:
        try:
            pages_text.append(page.extract_text() or "")
        except Exception as exc:
            logger.warning("Error extrayendo página en %s: %s", pdf_path.name, exc)
            pages_text.append("")
    return "\n".join(pages_text), len(reader.pages)


def extract_text_ocr(pdf_path: Path) -> str:
    if convert_from_path is None or pytesseract is None:
        raise RuntimeError("pdf2image/pytesseract no están instalados")
    kwargs = {"poppler_path": POPPLER_PATH} if POPPLER_PATH else {}
    images = convert_from_path(str(pdf_path), dpi=200, **kwargs)
    tess_config = f"--tessdata-dir {TESSDATA_DIR}" if Path(TESSDATA_DIR).exists() else ""
    texts = []
    for i, img in enumerate(images, start=1):
        try:
            texts.append(pytesseract.image_to_string(img, lang="spa", config=tess_config))
        except Exception as exc:
            logger.warning("OCR falló en página %d de %s (%s), reintentando en inglés", i, pdf_path.name, exc)
            try:
                texts.append(pytesseract.image_to_string(img))
            except Exception as exc2:
                logger.error("OCR definitivamente falló en página %d de %s: %s", i, pdf_path.name, exc2)
                texts.append("")
    return "\n".join(texts)


def extract_text(entry: DDUEntry) -> bool:
    pdf_path = Path(entry.pdf_path)
    if not pdf_path.exists():
        logger.error("No existe PDF para extraer texto: %s", entry.numero)
        return False

    try:
        text, n_pages = extract_text_pypdf(pdf_path)
    except Exception as exc:
        logger.error("pypdf falló en %s: %s", entry.numero, exc)
        text, n_pages = "", 1

    avg_chars = len(text.strip()) / max(n_pages, 1)
    if avg_chars < MIN_TEXT_CHARS_PER_PAGE:
        logger.info(
            "%s: texto insuficiente (%.0f chars/pág), probando OCR...", entry.numero, avg_chars
        )
        try:
            ocr_text = extract_text_ocr(pdf_path)
            if len(ocr_text.strip()) > len(text.strip()):
                text = ocr_text
                entry.ocr_used = True
        except Exception as exc:
            logger.error("OCR no disponible/falló para %s: %s", entry.numero, exc)

    entry.texto = text
    if not text.strip():
        logger.warning("%s: no se pudo extraer texto (ni con OCR).", entry.numero)
        return False
    return True


# ---------------------------------------------------------------------------
# 4. PARSING NORMATIVO
# ---------------------------------------------------------------------------

ARTICULO_RE = re.compile(
    r'\bart(?:[íi]culo[s]?|\.)\s*(\d{1,3}(?:\.\d{1,3}){1,3}|\d{1,3}\s*(?:bis|ter)?)\b',
    re.IGNORECASE,
)
LEY_RE = re.compile(r'\bLey\s*N[°ºo\.]*\s*(\d{1,2}\.\d{3})\b', re.IGNORECASE)

RELACION_VERBOS = {
    "complementa": "complementa",
    "modifica": "modifica",
    "reemplaza": "reemplaza",
    "deja sin efecto": "deja_sin_efecto",
    "deroga": "deja_sin_efecto",
    "ajusta": "ajusta",
}
# Ventana de texto tras el verbo donde se busca la(s) DDU referenciada(s)
RELACION_TARGET_RE = re.compile(
    r'DDU[\s\-]?(ESP(?:EC[IÍ]FICA)?)?\.?\s*N?[°ºo]?\.?\s*(\d{1,4}(?:[\-\/]\d{1,4})?)',
    re.IGNORECASE,
)


def parse_articulos(text: str) -> list[str]:
    found = set()
    for m in ARTICULO_RE.finditer(text):
        art = m.group(1).strip().rstrip(".")
        if "." in art:  # solo artículos tipo OGUC (x.x.x); se descartan sueltos ambiguos como "el artículo 5"
            found.add(art)
        elif re.match(r'^\d{1,3}$', art):
            found.add(art)
    return sorted(found, key=lambda s: [int(p) if p.isdigit() else p for p in s.split(".")])


def parse_leyes(text: str) -> list[str]:
    found = {f"Ley N°{m.group(1)}" for m in LEY_RE.finditer(text)}
    return sorted(found)


def parse_relaciones(text: str, self_numero: str) -> list[dict]:
    relaciones = []
    lower = text.lower()
    for verbo, tipo in RELACION_VERBOS.items():
        start = 0
        while True:
            idx = lower.find(verbo, start)
            if idx == -1:
                break
            window = text[idx: idx + 250]
            for tm in RELACION_TARGET_RE.finditer(window):
                esp = tm.group(1)
                num = tm.group(2)
                target = f"DDU-ESP-{num.zfill(3)}" if esp else f"DDU-{num}"
                if target != self_numero:
                    relaciones.append({"tipo": tipo, "ddu": target})
            start = idx + len(verbo)
    # dedup preservando orden
    seen = set()
    unique = []
    for r in relaciones:
        key = (r["tipo"], r["ddu"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


CIRCULAR_ORD_PROPIA_RE = re.compile(
    r'CIRCULAR\s+ORD(?:INARIA)?\.?\s*N[°ºo\.]*\s*_?(\d+)'
    r'|ORD\.?\s*CIRCULAR\s*N[°ºo\.]*\s*_?(\d+)',
    re.IGNORECASE,
)
FECHA_RE = re.compile(
    r'(\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóú]+\s+de\s+\d{4})', re.IGNORECASE
)


def parse_circular_ord_propia(text: str) -> tuple[str, str]:
    """Heurística: busca en los primeros ~3000 caracteres el N° de Circular Ord. propio y su fecha."""
    head = text[:3000]
    m = CIRCULAR_ORD_PROPIA_RE.search(head)
    circular_ord = f"N°{m.group(1) or m.group(2)}" if m else ""
    fm = FECHA_RE.search(head)
    fecha = fm.group(1) if fm else ""
    return circular_ord, fecha


def parse_normativo(entry: DDUEntry) -> None:
    text = entry.texto or ""
    entry.articulos_citados = parse_articulos(text)
    entry.leyes_citadas = parse_leyes(text)
    entry.relaciones = parse_relaciones(text, entry.numero) or parse_relaciones(entry.titulo, entry.numero)
    circular_ord, fecha = parse_circular_ord_propia(text)
    entry.circular_ord = circular_ord
    entry.fecha = fecha


def compute_estados(entries: dict[str, DDUEntry]) -> None:
    """Segunda pasada: si alguna DDU dice 'deja_sin_efecto'/'reemplaza' a otra, esa otra queda derogada."""
    derogadas = set()
    modificadas = set()
    for entry in entries.values():
        for rel in entry.relaciones:
            if rel["tipo"] in ("deja_sin_efecto", "reemplaza"):
                derogadas.add(rel["ddu"])
            elif rel["tipo"] == "modifica":
                modificadas.add(rel["ddu"])

    for numero, entry in entries.items():
        if numero in derogadas:
            entry.estado = "derogada"
        elif numero in modificadas:
            entry.estado = "modificada"
        else:
            entry.estado = "vigente"


# ---------------------------------------------------------------------------
# 5. OUTPUT MARKDOWN
# ---------------------------------------------------------------------------

def yaml_list(items: list[str]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def yaml_relaciones(relaciones: list[dict]) -> str:
    if not relaciones:
        return "[]"
    parts = [f'{{tipo: {r["tipo"]}, ddu: "{r["ddu"]}"}}' for r in relaciones]
    return "[" + ", ".join(parts) + "]"


def write_markdown(entry: DDUEntry, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = normalize_filename(entry.numero).replace(".pdf", ".md")
    dest = output_dir / filename

    frontmatter = (
        "---\n"
        f'numero: "{entry.numero}"\n'
        f'titulo: "{entry.titulo.replace(chr(34), chr(39))}"\n'
        f'fecha: "{entry.fecha}"\n'
        f'circular_ord: "{entry.circular_ord}"\n'
        f"articulos_citados: {yaml_list(entry.articulos_citados)}\n"
        f"leyes_citadas: {yaml_list(entry.leyes_citadas)}\n"
        f"relaciones: {yaml_relaciones(entry.relaciones)}\n"
        f"estado: {entry.estado}\n"
        f'pdf_original: "{entry.pdf_url}"\n'
        "---\n\n"
    )
    body = entry.texto or "(No se pudo extraer texto de este PDF; revisar manualmente.)"
    dest.write_text(frontmatter + body, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# 6. STATE INCREMENTAL
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("state.json corrupto, se reinicia.")
    return {"processed": {}}


def save_state(state: dict) -> None:
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scraper e indexador DDU MINVU -> Obsidian")
    parser.add_argument("--scrape-only", action="store_true", help="Solo escanea y cuenta, no descarga ni procesa")
    parser.add_argument("--force", action="store_true", help="Ignora state.json y reprocesa todo")
    parser.add_argument("--limit", type=int, default=None, help="Limita el número de DDU a procesar (pruebas)")
    parser.add_argument("--vault-dir", type=str, default=None, help="Sobrescribe la carpeta de salida .md")
    args = parser.parse_args()

    output_dir = Path(args.vault_dir) if args.vault_dir else VAULT_OUTPUT_DIR

    session = requests.Session()

    logger.info("=== Paso 1: escaneando las 4 fuentes de circulares DDU ===")
    entries = run_scraper(session)
    logger.info("Total DDU únicas encontradas: %d", len(entries))

    if args.scrape_only:
        print(f"\nTOTAL DDU encontradas (deduplicadas): {len(entries)}")
        by_prefix = {}
        for e in entries:
            prefix = "DDU-ESP" if e.startswith("DDU-ESP") else "DDU"
            by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
        for k, v in by_prefix.items():
            print(f"  {k}: {v}")
        return

    state = load_state()
    processed = state.get("processed", {})

    numeros = sorted(entries.keys())
    if args.limit:
        numeros = numeros[: args.limit]

    pending = [
        n for n in numeros
        if args.force or processed.get(n, {}).get("status") != "ok"
    ]
    logger.info("Pendientes de procesar (incremental): %d de %d", len(pending), len(numeros))

    stats = {"total_encontradas": len(entries), "descargadas_ok": 0, "fallidas": 0, "ocr_usadas": 0, "ya_procesadas": len(numeros) - len(pending)}

    to_process = {n: entries[n] for n in pending}

    # Entradas OK acumuladas en esta corrida, para poder recalcular relaciones/estado
    # con el contexto visto hasta el momento en cada iteración (no solo al final).
    ok_entries: dict[str, DDUEntry] = {}

    for i, (numero, entry) in enumerate(to_process.items(), start=1):
        logger.info("--- Procesando %s ---", numero)
        try:
            ok = download_pdf(entry, session)
            if not ok:
                stats["fallidas"] += 1
                processed[numero] = {"status": "download_failed", "pdf_url": entry.pdf_url}
                save_state(state)
                continue

            if not extract_text(entry):
                stats["fallidas"] += 1
                processed[numero] = {"status": "text_extraction_failed", "pdf_url": entry.pdf_url}
                save_state(state)
                continue

            if entry.ocr_used:
                stats["ocr_usadas"] += 1

            parse_normativo(entry)
            stats["descargadas_ok"] += 1

            # Escritura inmediata: si el proceso se corta, no se pierde el trabajo ya hecho.
            # compute_estados solo ve lo procesado hasta ahora en esta corrida (limitación
            # conocida, documentada en el README).
            ok_entries[numero] = entry
            compute_estados(ok_entries)
            dest = write_markdown(entry, output_dir)
            processed[numero] = {
                "status": "ok",
                "pdf_url": entry.pdf_url,
                "md_path": str(dest),
                "ocr_used": entry.ocr_used,
            }
            logger.info("Escrito %s", dest)
        except Exception as exc:
            logger.exception("Error inesperado procesando %s: %s", numero, exc)
            stats["fallidas"] += 1
            processed[numero] = {"status": "error", "detail": str(exc)}

        state["processed"] = processed
        if i % 5 == 0 or i == len(to_process):
            save_state(state)

    state["processed"] = processed
    save_state(state)

    print("\n=== RESUMEN ===")
    print(f"Total DDU encontradas:      {stats['total_encontradas']}")
    print(f"Ya procesadas (incremental): {stats['ya_procesadas']}")
    print(f"Descargadas/procesadas OK:  {stats['descargadas_ok']}")
    print(f"Fallidas:                   {stats['fallidas']}")
    print(f"Requirieron OCR:            {stats['ocr_usadas']}")
    print(f"\nMarkdown generado en: {output_dir}")
    print(f"Log detallado en: {LOG_FILE}")


if __name__ == "__main__":
    main()
