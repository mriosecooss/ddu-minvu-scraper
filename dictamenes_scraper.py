#!/usr/bin/env python3
"""
Descarga e indexa dictamenes de Contraloria desde un origen configurable
(bucket de almacenamiento estatico, publico y sin autenticacion), y genera
un .md por dictamen en el vault de Obsidian, carpeta "03 Dictamenes Contraloria".

Configuracion requerida via variables de entorno (sin valores por defecto,
a proposito, para no fijar un origen especifico en el codigo):
    SOURCE_BUCKET       nombre del bucket
    SOURCE_PREFIX       prefijo/carpeta dentro del bucket con los HTML

Uso:
    py dictamenes_scraper.py --scrape-only   # solo lista y cuenta
    py dictamenes_scraper.py                 # corrida incremental normal
    py dictamenes_scraper.py --force         # reprocesa todo
"""
import argparse
import json
import logging
import os
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state_dictamenes.json"
LOG_FILE = BASE_DIR / "dictamenes_scraper.log"
RAW_DIR = BASE_DIR / "dictamenes_raw"

VAULT_OUTPUT_DIR = Path(r"C:\Users\Miguel\Desktop\Vault Claude\03 Dictamenes Contraloria")

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "")
SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "")

BUCKET_API = f"https://storage.googleapis.com/storage/v1/b/{SOURCE_BUCKET}/o"
PUBLIC_BASE = f"https://storage.googleapis.com/{SOURCE_BUCKET}/"
PREFIX = SOURCE_PREFIX

HEADERS = {"User-Agent": "dictamenes-indexer/1.0 (research; contact: mriosecooss@gmail.com)"}
RATE_LIMIT = 0.3
MAX_RETRIES = 3

logger = logging.getLogger("dictamenes_scraper")
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
        params = {"prefix": PREFIX, "maxResults": "1000", "fields": "nextPageToken,items(name)"}
        if token:
            params["pageToken"] = token
        url = BUCKET_API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for it in data.get("items", []):
            if it["name"].endswith(".html"):
                names.append(it["name"])
        token = data.get("nextPageToken")
        if not token:
            break
    return sorted(names)


# ---------------------------------------------------------------------------
# 2. DESCARGA
# ---------------------------------------------------------------------------

def fetch(url: str) -> str:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_exc = exc
            logger.warning("Intento %d/%d fallo para %s: %s", attempt, MAX_RETRIES, url, exc)
            time.sleep(1.5 * attempt)
    raise last_exc


# ---------------------------------------------------------------------------
# 3. PARSING
# ---------------------------------------------------------------------------

APLICA_RE = re.compile(r'\b(\d{1,3}(?:\.\d{3})*|E\d{4,7})\s*,\s*de\s*(\d{4})\b')


def parse_dictamen_html(html_text: str, object_name: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")

    h1 = soup.find("h1")
    numero_full = h1.get_text(strip=True) if h1 else ""
    m = re.search(r'N[°ºo]?\s*([A-Za-z]?\d+/\d{4})', numero_full)
    numero = m.group(1) if m else object_name.rsplit("/", 1)[-1].replace(".html", "")

    sub = soup.select_one("header.doc-head .sub")
    fecha = caracter = identificador = ""
    if sub:
        sub_text = sub.get_text(" ", strip=True)
        fm = re.search(r'Fecha\s+([\d\-]+)', sub_text)
        fecha = fm.group(1) if fm else ""
        cm = re.search(r'Car[aá]cter\s+(\S+)', sub_text)
        caracter = cm.group(1) if cm else ""
        im = re.search(r'Identificador\s+(\S+)', sub_text)
        identificador = im.group(1) if im else ""

    materia = ""
    info = {}
    fuentes_legales = ""
    flags = {}
    body_text = ""
    fuente_url = ""

    for section in soup.find_all("section", class_="card"):
        h2 = section.find("h2")
        titulo_sec = h2.get_text(strip=True) if h2 else ""
        if "materia" in section.get("class", []):
            p = section.find("p")
            materia = p.get_text(" ", strip=True) if p else ""
        elif titulo_sec == "Información del dictamen":
            for p in section.find_all("p"):
                strong = p.find("strong")
                if not strong:
                    continue
                label = strong.get_text(strip=True).rstrip(":")
                value = p.get_text(" ", strip=True)[len(strong.get_text(strip=True)):].strip()
                info[label] = value
        elif titulo_sec == "Fuentes legales":
            p = section.find("p")
            fuentes_legales = p.get_text(" ", strip=True) if p else ""
        elif titulo_sec == "Carácter procesal":
            for span in section.select(".flag"):
                txt = span.get_text(strip=True)
                if ":" in txt:
                    k, v = txt.split(":", 1)
                    flags[k.strip()] = v.strip().upper() == "SI"
        elif titulo_sec == "Documento":
            body_div = section.find("div", class_="body-content")
            if body_div:
                paras = [p.get_text(" ", strip=True) for p in body_div.find_all("p")]
                body_text = "\n\n".join(p for p in paras if p)
                if not body_text.strip():
                    body_text = body_div.get_text("\n", strip=True)

    footer_a = soup.select_one("footer a")
    if footer_a and footer_a.get("href"):
        fuente_url = footer_a["href"]

    aplica_dictamenes = sorted(
        {f"{num}/{anio}" for num, anio in APLICA_RE.findall(body_text)},
        key=lambda s: (s.split("/")[1], s.split("/")[0]),
    )

    return {
        "numero": numero,
        "fecha": fecha,
        "caracter": caracter,
        "identificador": identificador,
        "materia": materia,
        "criterio": info.get("Criterio", ""),
        "destinatarios": info.get("Destinatarios", ""),
        "origen": info.get("Origen", ""),
        "abogados": info.get("Abogados", ""),
        "descriptores": [d.strip() for d in info.get("Descriptores", "").split(",") if d.strip()],
        "fuentes_legales": [f.strip() for f in re.split(r',\s*(?=[a-zA-Z]+\s|\bDTO\b|\bPOL\b)', fuentes_legales) if f.strip()],
        "caracter_procesal": flags,
        "aplica_dictamenes": aplica_dictamenes,
        "fuente_url": fuente_url or object_name,
        "texto": body_text,
    }


# ---------------------------------------------------------------------------
# 4. OUTPUT MARKDOWN
# ---------------------------------------------------------------------------

def yaml_str(s: str) -> str:
    return '"' + (s or "").replace('"', "'") + '"'


def yaml_list(items: list[str]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(yaml_str(i) for i in items) + "]"


def yaml_flags(flags: dict) -> str:
    if not flags:
        return "{}"
    parts = [f"{k.lower().replace(' ', '_').replace('.', '')}: {str(v).lower()}" for k, v in flags.items()]
    return "{" + ", ".join(parts) + "}"


def write_markdown(data: dict) -> Path:
    VAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_numero = data["numero"].replace("/", "-")
    dest = VAULT_OUTPUT_DIR / f"Dictamen-{safe_numero}.md"

    frontmatter = (
        "---\n"
        f'numero: {yaml_str(data["numero"])}\n'
        f'fecha: {yaml_str(data["fecha"])}\n'
        f'caracter: {yaml_str(data["caracter"])}\n'
        f'identificador: {yaml_str(data["identificador"])}\n'
        f'criterio: {yaml_str(data["criterio"])}\n'
        f'destinatarios: {yaml_str(data["destinatarios"])}\n'
        f'origen: {yaml_str(data["origen"])}\n'
        f"descriptores: {yaml_list(data['descriptores'])}\n"
        f"fuentes_legales: {yaml_list(data['fuentes_legales'])}\n"
        f"caracter_procesal: {yaml_flags(data['caracter_procesal'])}\n"
        f"aplica_dictamenes: {yaml_list(data['aplica_dictamenes'])}\n"
        f'fuente_url: {yaml_str(data["fuente_url"])}\n'
        "---\n\n"
        f"## Materia\n\n{data['materia']}\n\n"
        f"## Texto\n\n"
    )
    body = data["texto"] or "(No se pudo extraer texto de este dictamen; revisar manualmente.)"
    dest.write_text(frontmatter + body, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# 5. STATE
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("state_dictamenes.json corrupto, se reinicia.")
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

    logger.info("=== Listando dictamenes en %s ===", PREFIX)
    object_names = list_objects()
    logger.info("Total dictamenes encontrados: %d", len(object_names))

    if args.scrape_only:
        print(f"\nTOTAL dictamenes encontrados: {len(object_names)}")
        return

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    processed = state.get("processed", {})

    if args.limit:
        object_names = object_names[: args.limit]

    pending = [n for n in object_names if args.force or processed.get(n, {}).get("status") != "ok"]
    logger.info("Pendientes de procesar: %d de %d", len(pending), len(object_names))

    stats = {"total": len(object_names), "ok": 0, "fallidas": 0, "ya_ok": len(object_names) - len(pending)}

    for i, name in enumerate(pending, 1):
        url = PUBLIC_BASE + urllib.parse.quote(name)
        logger.info("[%d/%d] %s", i, len(pending), name)
        try:
            html_text = fetch(url)
            (RAW_DIR / name.rsplit("/", 1)[-1]).write_text(html_text, encoding="utf-8")
            data = parse_dictamen_html(html_text, name)
            dest = write_markdown(data)
            processed[name] = {
                "status": "ok",
                "numero": data["numero"],
                "md_path": str(dest),
                "url": url,
            }
            stats["ok"] += 1
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
