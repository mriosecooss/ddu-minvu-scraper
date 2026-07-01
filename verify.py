#!/usr/bin/env python3
"""Verificación exhaustiva de calidad post-procesamiento (no solo conteos)."""
import json
import re
from pathlib import Path

VAULT = Path(r"C:\Users\Miguel\Desktop\Vault Claude\02 Normativa")
RAW = Path(r"C:\Users\Miguel\ddu-minvu-scraper\ddu_raw")
STATE = Path(r"C:\Users\Miguel\ddu-minvu-scraper\state.json")
LOG = Path(r"C:\Users\Miguel\ddu-minvu-scraper\ddu_scraper.log")

state = json.loads(STATE.read_text(encoding="utf-8"))
processed = state["processed"]

print(f"=== 1. Totales ===")
print(f"DDU en state.json: {len(processed)}")
pdfs = list(RAW.glob("*.pdf"))
print(f"PDFs descargados:  {len(pdfs)}")
mds = list(VAULT.glob("DDU*.md"))
print(f"Markdown en vault: {len(mds)}")

by_status = {}
for v in processed.values():
    by_status.setdefault(v.get("status"), []).append(v)
print(f"\n=== 2. Desglose por status ===")
for k, v in sorted(by_status.items(), key=lambda x: -len(x[1])):
    print(f"  {k}: {len(v)}")

ok_numeros = {k for k, v in processed.items() if v.get("status") == "ok"}
md_stems = {p.stem for p in mds}
missing_md = ok_numeros - md_stems
extra_md = md_stems - ok_numeros
print(f"\n=== 3. Consistencia state.json <-> archivos .md ===")
print(f"OK en state pero SIN archivo .md: {len(missing_md)} {list(missing_md)[:10]}")
print(f"Archivos .md SIN registro 'ok' en state: {len(extra_md)} {list(extra_md)[:10]}")

print(f"\n=== 4. Validación de contenido de TODOS los .md marcados 'ok' ===")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n\n(.*)$", re.DOTALL)
empty_body = []
tiny_body = []
bad_yaml = []
placeholder_body = []
garbage_ocr = []
ok_checked = 0

def looks_like_garbage(text: str) -> bool:
    sample = text[:2000]
    if not sample.strip():
        return False
    alpha = sum(c.isalpha() for c in sample)
    return alpha / max(len(sample), 1) < 0.35  # muy poco texto alfabético = ruido OCR

for numero in sorted(ok_numeros):
    p = VAULT / f"{numero}.md"
    if not p.exists():
        continue
    ok_checked += 1
    raw = p.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(raw)
    if not m:
        bad_yaml.append(numero)
        continue
    fm, body = m.group(1), m.group(2)
    required = ["numero:", "titulo:", "estado:", "pdf_original:"]
    if not all(r in fm for r in required):
        bad_yaml.append(numero)
    body_stripped = body.strip()
    if not body_stripped:
        empty_body.append(numero)
    elif "No se pudo extraer texto" in body_stripped:
        placeholder_body.append(numero)
    elif len(body_stripped) < 80:
        tiny_body.append(numero)
    elif looks_like_garbage(body_stripped):
        garbage_ocr.append(numero)

print(f"Archivos 'ok' verificados: {ok_checked}")
print(f"  YAML frontmatter inválido/incompleto: {len(bad_yaml)} {bad_yaml[:10]}")
print(f"  Cuerpo completamente vacío:            {len(empty_body)} {empty_body[:10]}")
print(f"  Placeholder 'no se pudo extraer':       {len(placeholder_body)} {placeholder_body[:10]}")
print(f"  Cuerpo sospechosamente corto (<80c):    {len(tiny_body)} {tiny_body[:10]}")
print(f"  Posible ruido OCR (muy poco alfabético): {len(garbage_ocr)} {garbage_ocr[:10]}")

print(f"\n=== 5. Causas reales de las NO-ok ===")
not_ok = {k: v for k, v in processed.items() if v.get("status") != "ok"}
for numero, v in list(not_ok.items())[:100]:
    status = v.get("status")
    print(f"  {numero}: {status}")

# Buscar la última línea de log relevante para cada fallida (causa real)
if not_ok:
    log_text = LOG.read_text(encoding="utf-8", errors="replace")
    print(f"\n=== 6. Última línea de log por cada NO-ok (causa) ===")
    for numero in not_ok:
        matches = [l for l in log_text.splitlines() if numero in l]
        cause = matches[-1] if matches else "(sin log)"
        print(f"  {numero}: {cause[-160:]}")

print(f"\n=== RESUMEN FINAL ===")
print(f"Total: {len(processed)} | OK verificados sanos: {ok_checked - len(bad_yaml) - len(empty_body) - len(placeholder_body)} | Problemas de calidad: {len(bad_yaml)+len(empty_body)+len(placeholder_body)+len(tiny_body)+len(garbage_ocr)} | No-ok: {len(not_ok)}")
