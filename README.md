# DDU MINVU Scraper -> Obsidian

Descarga, indexa y convierte a Markdown todas las Circulares DDU (Generales y
Específicas) del MINVU, para consulta en un vault de Obsidian.

Además incluye dos scripts complementarios que descargan e indexan, en el
mismo formato Markdown + frontmatter, dictámenes de Contraloría y normativa
de construcción/urbanismo adicional (LGUC, OGUC, PRC, formularios, etc.).

## Resultado de la exploración inicial (paso 1, `--scrape-only`)

- **544 DDU únicas** encontradas y deduplicadas (347 Generales + 197 Específicas).
- Las 4 páginas de MINVU **no usan paginación AJAX real**: cada listado es HTML
  estático con un buscador client-side. El script igual maneja paginación tipo
  WordPress (`rel=next` / `.next`) por si el sitio cambia en el futuro.

## Instalación

```
py -m pip install -r requirements.txt
```

### OCR (fallback para PDFs escaneados)

El fallback OCR requiere dos binarios externos que **no se instalan con pip**:

1. **Tesseract OCR** (Windows): https://github.com/UB-Mannheim/tesseract/wiki
   - Instala el paquete de idioma español ("spa") durante la instalación.
   - Anota la ruta del ejecutable, típicamente:
     `C:\Program Files\Tesseract-OCR\tesseract.exe`
2. **Poppler** (Windows): https://github.com/oschwartz10612/poppler-windows/releases
   - Descomprime y anota la ruta de la carpeta `Library\bin`.

Configura las rutas como variables de entorno antes de correr el script:

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
$env:POPPLER_PATH = "C:\poppler-24.08.0\Library\bin"
py ddu_scraper.py
```

Si no configuras estas variables, el script sigue funcionando para todos los
PDFs con texto nativo (la gran mayoría) y solo registra en el log los casos
que hubieran requerido OCR y no pudo procesar.

## Uso

```powershell
# Paso 1 solamente: escanea y cuenta, no descarga nada
py ddu_scraper.py --scrape-only

# Corrida completa (descarga + OCR + parsing + Markdown), incremental
py ddu_scraper.py

# Reprocesar todo desde cero, ignorando state.json
py ddu_scraper.py --force

# Probar con pocas DDU antes de la corrida completa
py ddu_scraper.py --limit 15
```

Los `.md` se escriben directamente en:
`C:\Users\Miguel\Desktop\Vault Claude\02 Normativa`

(configurable con `--vault-dir "otra\ruta"` o editando `VAULT_OUTPUT_DIR` en
`ddu_scraper.py`).

## Archivos generados

- `ddu_raw/{numero}.pdf` — PDFs originales descargados.
- `state.json` — registro incremental de DDU ya procesadas (corridas futuras
  solo bajan/procesan las nuevas, salvo `--force`).
- `ddu_scraper.log` — log detallado de descargas, OCR y errores.
- `02 Normativa/{numero}.md` — un archivo por DDU con frontmatter YAML:

```yaml
---
numero: "DDU-546"
titulo: "DDU 546. Aplicación del artículo 5.1.2..."
fecha: ""
circular_ord: ""
articulos_citados: ["5.1.2"]
leyes_citadas: []
relaciones: [{tipo: complementa, ddu: "DDU-542"}]
estado: vigente
pdf_original: "https://www.minvu.gob.cl/wp-content/uploads/..."
---

(texto completo extraído del PDF)
```

## Limitaciones conocidas (heurísticas basadas en regex)

- `articulos_citados` y `leyes_citadas` se extraen con regex sobre el texto
  completo; pueden generar falsos positivos/negativos en redacciones atípicas.
- `relaciones` (complementa/modifica/deja sin efecto/reemplaza) se detectan
  buscando el verbo seguido de una referencia "DDU NNN" en una ventana de
  ~250 caracteres. Los formatos de MINVU no son 100% consistentes, así que
  para uso legal/normativo formal se recomienda **verificar manualmente**
  las relaciones y el `estado` antes de tomarlas como definitivas.
- `circular_ord` (el N° de Circular Ord. propio de cada DDU, no el de las
  circulares que referencia) se busca en los primeros ~3000 caracteres del
  texto extraído; si el PDF no sigue el formato estándar, puede quedar vacío.
- El campo `estado` se calcula en una segunda pasada sobre **todas las DDU
  procesadas en la misma corrida**. Si corres el script de forma incremental
  (por partes), una DDU antigua no se re-evalúa automáticamente cuando una
  DDU nueva la deroga en una corrida posterior — usa `--force` periódicamente
  para recalcular todos los estados con el corpus completo.

## Scripts complementarios

Ambos scripts leen el origen de los datos desde variables de entorno (no
hay un origen fijado en el código); hay que configurarlas antes de correr:

```powershell
$env:SOURCE_BUCKET = "..."
$env:SOURCE_PREFIX = "..."       # solo dictamenes_scraper.py
$env:SOURCE_ROOT_PREFIX = "..."  # solo normativa_scraper.py
```

### `dictamenes_scraper.py` — Dictámenes de Contraloría

Genera un `.md` por dictamen con frontmatter: `numero, fecha, caracter,
identificador, materia, criterio, destinatarios, origen, descriptores,
fuentes_legales, caracter_procesal, aplica_dictamenes, fuente_url` + texto
completo.

```powershell
py dictamenes_scraper.py --scrape-only   # solo lista y cuenta
py dictamenes_scraper.py                 # corrida incremental
py dictamenes_scraper.py --force         # reprocesa todo
```

Salida: `03 Dictamenes Contraloria/Dictamen-{numero}.md`.
Estado incremental: `state_dictamenes.json`.

### `normativa_scraper.py` — Normativa complementaria

Descarga PDFs de normativa de construcción/urbanismo (LGUC, OGUC, PRC,
PRMS, PRS, Copropiedad, Formularios Únicos Nacionales, normativa local y
vinculada), extrae texto (pypdf + fallback OCR, reutiliza la configuración
de Tesseract/Poppler de `ddu_scraper.py`) y genera un `.md` por documento
con frontmatter: `categoria, titulo, nombre_archivo, fuente_url, ocr_used`.

```powershell
py normativa_scraper.py --scrape-only
py normativa_scraper.py
py normativa_scraper.py --force
```

Salida: `04 Normativa Vinculada/{categoria}/[{subcarpeta}/]{titulo}.md`.
Estado incremental: `state_normativa.json`.
