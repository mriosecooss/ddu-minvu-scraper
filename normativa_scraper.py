#!/usr/bin/env python3
"""
Descarga PDFs de normativa de construccion/urbanismo complementaria desde un
origen configurable (bucket de almacenamiento estatico, publico y sin
autenticacion), extrae texto (pypdf + OCR fallback) y genera un .md por
documento en el vault de Obsidian, carpeta "04 Normativa Vinculada/<categoria>/".

Configuracion requerida via variables de entorno (sin valores por defecto,
a proposito, para no fijar un origen especifico en el codigo):
    SOURCE_BUCKET       nombre del bucket
    SOURCE_ROOT_PREFIX  prefijo/carpeta raiz dentro del bucket

Uso:
    py normativa_scraper.py --scrape-only
    py normativa_scraper.py
    py normativa_scraper.py --force
"""
import argparse
import json
import logging
import os
import time
import urllib.request
import urllib.parse
from pathlib import Path

from ddu_scraper import extract_text_pypdf, extract_text_ocr, MIN_TEXT_CHARS_PER_PAGE

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state_normativa.json"
LOG_FILE = BASE_DIR / "normativa_scraper.log"
RAW_DIR = BASE_DIR / "normativa_raw"

VAULT_OUTPUT_DIR = Path(r"C:\Users\Miguel\Desktop\Vault Claude\04 Normativa Vinculada")

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
ROOT_PREFIX = os.environ.get("SOURCE_ROOT_PREFIX", "")

BUCKET_API = f"https://storage.googleapis.com/storage/v1/b/{SOURCE_BUCKET}/o"
PUBLIC_BASE = f"https://storage.googleapis.com/{SOURCE_BUCKET}/"

# Carpetas incluidas dentro de ROOT_PREFIX. Nota: si el origen tiene una
# carpeta "PRC" (mayuscula, plana) ademas de "prc" (organizada por sub-carpetas),
# verificar duplicados por tamanio de archivo antes de incluir ambas.
INCLUDED_SUBFOLDERS = [
    "normativa-vinculada",
    "prc",
    "FUNs",
    "normativa-local",
    "LGUC",
    "OGUC",
    "Copropiedad",
    "PRMS",
    "PRS",
]

HEADERS = {"User-Agent": "normativa-indexer/1.0 (research; contact: mriosecooss@gmail.com)"}
RATE_LIMIT = 0.3
MAX_RETRIES = 3

logger = logging.getLogger("normativa_scraper")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
logger.addHandler(_ch)


# ---------------------------------------------------------------------------
# 1. LISTADO
# ---------------------------------------------------------------------------

def list_objects() -> list[str]:
    names = []
    token = None
    while True:
        params = {"prefix": ROOT_PREFIX, "maxResults": "1000", "fields": "nextPageToken,items(name)"}
        if token:
            params["pageToken"] = token
        url = BUCKET_API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for it in data.get("items", []):
            name = it["name"]
            rest = name[len(ROOT_PREFIX):]
            top = rest.split("/", 1)[0]
            if top in INCLUDED_SUBFOLDERS and name.lower().endswith(".pdf"):
                names.append(name)
        token = data.get("nextPageToken")
        if not token:
            break
    return sorted(names)


# ---------------------------------------------------------------------------
# 2. DESCARGA
# ---------------------------------------------------------------------------

def download(url: str, dest: Path) -> None:
    last_exc = None
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
            return
        except Exception as exc:
            last_exc = exc
            logger.warning("Intento %d/%d fallo para %s: %s", attempt, MAX_RETRIES, url, exc)
            time.sleep(1.5 * attempt)
    raise last_exc


# ---------------------------------------------------------------------------
# 3. OUTPUT MARKDOWN
# ---------------------------------------------------------------------------

def yaml_str(s: str) -> str:
    return '"' + (s or "").replace('"', "'") + '"'


def write_markdown(categoria: str, nombre_archivo: str, fuente_url: str, texto: str, ocr_used: bool) -> Path:
    rel = Path(nombre_archivo)
    out_dir = VAULT_OUTPUT_DIR / categoria / rel.parent if rel.parent != Path(".") else VAULT_OUTPUT_DIR / categoria
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (rel.stem + ".md")

    frontmatter = (
        "---\n"
        f"categoria: {yaml_str(categoria)}\n"
        f'titulo: {yaml_str(Path(nombre_archivo).stem)}\n'
        f'nombre_archivo: {yaml_str(nombre_archivo)}\n'
        f'fuente_url: {yaml_str(fuente_url)}\n'
        f"ocr_used: {str(ocr_used).lower()}\n"
        "---\n\n"
    )
    body = texto or "(No se pudo extraer texto de este PDF; revisar manualmente.)"
    dest.write_text(frontmatter + body, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# 4. STATE
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("state_normativa.json corrupto, se reinicia.")
    return {"processed": {}}


def save_state(state: dict) -> None:
    state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scrape-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logger.info("=== Listando normativa vinculada ===")
    object_names = list_objects()
    logger.info("Total documentos encontrados: %d", len(object_names))

    if args.scrape_only:
        from collections import Counter
        c = Counter(n[len(ROOT_PREFIX):].split("/", 1)[0] for n in object_names)
        print(f"\nTOTAL documentos encontrados: {len(object_names)}")
        for k, v in sorted(c.items(), key=lambda kv: -kv[1]):
            print(f"  {k}: {v}")
        return

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    processed = state.get("processed", {})

    if args.limit:
        object_names = object_names[: args.limit]

    pending = [n for n in object_names if args.force or processed.get(n, {}).get("status") != "ok"]
    logger.info("Pendientes de procesar: %d de %d", len(pending), len(object_names))

    stats = {"total": len(object_names), "ok": 0, "fallidas": 0, "ocr_usadas": 0, "ya_ok": len(object_names) - len(pending)}

    for i, name in enumerate(pending, 1):
        url = PUBLIC_BASE + urllib.parse.quote(name)
        rest = name[len(ROOT_PREFIX):]
        categoria = rest.split("/", 1)[0]
        nombre_archivo = rest.split("/", 1)[1] if "/" in rest else rest
        logger.info("[%d/%d] %s", i, len(pending), name)

        try:
            pdf_path = RAW_DIR / nombre_archivo
            download(url, pdf_path)

            text, n_pages = extract_text_pypdf(pdf_path)
            ocr_used = False
            avg_chars = len(text.strip()) / max(n_pages, 1)
            if avg_chars < MIN_TEXT_CHARS_PER_PAGE:
                logger.info("%s: texto insuficiente (%.0f chars/pag), probando OCR...", nombre_archivo, avg_chars)
                try:
                    ocr_text = extract_text_ocr(pdf_path)
                    if len(ocr_text.strip()) > len(text.strip()):
                        text = ocr_text
                        ocr_used = True
                except Exception as exc:
                    logger.error("OCR fallo para %s: %s", nombre_archivo, exc)

            dest = write_markdown(categoria, nombre_archivo, url, text, ocr_used)
            processed[name] = {
                "status": "ok",
                "categoria": categoria,
                "md_path": str(dest),
                "url": url,
                "ocr_used": ocr_used,
            }
            stats["ok"] += 1
            if ocr_used:
                stats["ocr_usadas"] += 1
        except Exception as exc:
            logger.error("FALLO en %s: %s", name, exc)
            processed[name] = {"status": "error", "error": str(exc), "url": url}
            stats["fallidas"] += 1

        state["processed"] = processed
        save_state(state)
        time.sleep(RATE_LIMIT)

    logger.info("=== RESUMEN === %s", json.dumps(stats, ensure_ascii=False))
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
