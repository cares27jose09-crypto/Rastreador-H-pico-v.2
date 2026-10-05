"""
hipica.py - Descarga y lectura de programas de carreras de caballos de Chile
y buscador por caballo, criador (haras), jinete, preparador y stud.

Fuentes:
  - Valparaíso Sporting : páginas HTML por carrera (campo "Haras" = criador).
  - Club Hípico Santiago: PDF del volante (se adjunta).
  - Hipódromo Chile     : PDF del volante (se adjunta).

Todas las funciones devuelven filas con las mismas columnas (ver COLUMNAS).
"""
from __future__ import annotations

import datetime as dt
import io
import re
import subprocess
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from bs4 import BeautifulSoup

COLUMNAS = [
    "fecha", "hipodromo", "reunion", "carrera", "hora", "prueba", "distancia",
    "mandil", "caballo", "jinete", "preparador", "stud", "criador", "padres",
]

HIP_SPORTING = "Valparaíso Sporting"
HIP_CHS = "Club Hípico de Santiago"
HIP_HCH = "Hipódromo Chile"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = 25

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


# --------------------------------------------------------------------------
# Utilidades de texto
# --------------------------------------------------------------------------
def quitar_tildes(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


# Palabras que no distinguen a un criador ("Haras X", "H. X", "Criadero X").
_GENERICAS = {"haras", "hras", "h", "hs", "criadero", "criaderos", "stud", "sta", "st"}


def norm(s: str, quitar_genericas: bool = False) -> str:
    """Minúsculas, sin tildes ni signos. 'Sta.' -> 'santa'."""
    s = quitar_tildes(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9ñ]+", " ", s)
    toks = []
    for t in s.split():
        if t in ("sta",):
            t = "santa"
        if quitar_genericas and t in _GENERICAS:
            continue
        toks.append(t)
    return " ".join(toks)


def limpiar_persona(s: str) -> str:
    """Quita estadísticas pegadas ('1c 14v') y puntos sobrantes de un nombre."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    s = re.sub(r"(?:\s+\d+(?:c|ch|m|v)\b)+\s*$", "", s)   # 21c 26ch 39v
    s = re.sub(r"\.\.+", ".", s)
    if re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{3,}\.$", s):         # 'Sergio Salazar.' -> sin punto
        s = s[:-1]
    return s.strip()


def espaciar_iniciales(s: str) -> str:
    """'L.P.Silva' -> 'L. P. Silva'."""
    return re.sub(r"(?<=\.)(?=[A-Za-zÁÉÍÓÚÑ])", " ", s).strip()


def titulo(s: str) -> str:
    """'BLACK KINGLY' -> 'Black Kingly' (respeta '(ARG)')."""
    s = s.strip().lower().title()
    return re.sub(r"\((\w+)\)", lambda m: "(" + m.group(1).upper() + ")", s)


def fila(**kw) -> dict:
    d = {c: "" for c in COLUMNAS}
    d.update(kw)
    return d


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
_sesion = requests.Session()
_sesion.headers.update(HEADERS)


def http_get(url: str) -> requests.Response:
    return _sesion.get(url, timeout=TIMEOUT)


# --------------------------------------------------------------------------
# PDF -> texto
# --------------------------------------------------------------------------
def pdf_paginas_layout(pdf_bytes: bytes) -> list[str]:
    """Texto por página conservando el diseño (pdftotext -layout)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        try:
            out = subprocess.run(
                ["pdftotext", "-layout", f.name, "-"],
                capture_output=True, check=True, timeout=180,
            ).stdout.decode("utf-8", "replace")
            return out.split("\f")
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    # Respaldo si no existe poppler: más lento y menos fiel
    import pdfplumber
    paginas = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            paginas.append(p.extract_text(layout=True) or "")
    return paginas


def _fecha_es(texto: str):
    """Primera fecha tipo '3 de Octubre de 2026' o '5 de octubre del año 2026'."""
    m = re.search(r"(\d{1,2})\s+de\s+([A-Za-zñÑáéíóú]+)\s+(?:de|del año)\s+(?:año\s+)?(\d{4})", texto)
    if not m:
        return None
    mes = MESES.get(quitar_tildes(m.group(2)).lower())
    if not mes:
        return None
    try:
        return dt.date(int(m.group(3)), mes, int(m.group(1)))
    except ValueError:
        return None


def corregir_horas(filas: list[dict]) -> list[dict]:
    """Si una carrera figura con una hora muy posterior a la siguiente (p.ej. 22:30 antes de 11:20),
    es un error de impresión de 12 h: se corrige restando 12 horas."""
    por_carrera = {}
    for f in filas:
        if f["carrera"] and f["hora"] and f["carrera"] not in por_carrera:
            por_carrera[f["carrera"]] = f["hora"]
    orden = sorted(por_carrera, key=lambda c: int(c))
    def minutos(h):
        a, b = h.split(":")
        return int(a) * 60 + int(b)
    nuevas = dict(por_carrera)
    for i, c in enumerate(orden[:-1]):
        sig = minutos(nuevas[orden[i + 1]]) if orden[i + 1] in nuevas else None
        cur = minutos(por_carrera[c])
        # comparar contra la mediana de las horas siguientes
        siguientes = [minutos(por_carrera[x]) for x in orden[i + 1:i + 4]]
        if siguientes and cur - min(siguientes) > 6 * 60:
            h, m = divmod(cur - 12 * 60, 60)
            nuevas[c] = f"{h:02d}:{m:02d}"
    for f in filas:
        if f["carrera"] in nuevas:
            f["hora"] = nuevas[f["carrera"]]
    return filas


# ==========================================================================
# HIPÓDROMO CHILE  (volante PDF)
# ==========================================================================
_RE_HCH_HORA = re.compile(r"(\d{1,2}:\d{2})\s+hrs\.\s+NOM\.\s+PREMIO:\s*(.*?)\s*$")
_RE_HCH_CARRERA = re.compile(r"^\s*(\d{1,2})ª")
_RE_HCH_CABALLO = re.compile(
    r"^\s{2,}(\d{1,2})\s{2,}([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ0-9'’.\-&() ]*?)\s{2,}(?:\d|\()"
)
_RE_HCH_JINETE = re.compile(r"^\s*\([A-Z]\.[A-Z]\.\)\s+(?:\(\s*\d+\)\s+)?\d+\s+[\d,]+\s+\d+\s+(.+)$")
_RE_HCH_STUD = re.compile(r"^\s*<<\s*(.+?)\s*>>")
_RE_PAREN_FINAL = re.compile(r"\(\s*([^()]*?)\s*\)\s*$")


def parse_hch(pdf_bytes: bytes) -> list[dict]:
    paginas = pdf_paginas_layout(pdf_bytes)
    texto = "\n".join(paginas)
    lineas = texto.split("\n")
    fecha = _fecha_es(texto[:3000]) or _fecha_es(texto)
    m = re.search(r"REUNION\s+N[ºo°]?\s*(\d+)", texto[:3000], re.I)
    reunion = m.group(1) if m else ""

    # Posiciones de encabezados de carrera y de caballos
    carrera = hora = prueba = ""
    i_hdr = []
    for i, l in enumerate(lineas):
        if _RE_HCH_HORA.search(l):
            i_hdr.append(i)
    # Mapa línea -> contexto de carrera
    ctx = {}
    for i in i_hdr:
        mh = _RE_HCH_HORA.search(lineas[i])
        num = ""
        for j in range(i, min(i + 8, len(lineas))):
            mc = _RE_HCH_CARRERA.match(lineas[j])
            if mc:
                num = mc.group(1)
                break
        ctx[i] = (num, mh.group(1), mh.group(2).strip())

    filas = []
    n = len(lineas)
    inicio_caballo = []
    for i, l in enumerate(lineas):
        if _RE_HCH_CABALLO.match(l):
            inicio_caballo.append(i)
    inicio_caballo_set = set(inicio_caballo)
    hdrs_ordenados = sorted(i_hdr)

    for idx, i in enumerate(inicio_caballo):
        # carrera vigente = último encabezado antes de esta línea
        previos = [h for h in hdrs_ordenados if h < i]
        if not previos:
            continue
        carrera, hora, prueba = ctx[previos[-1]]
        mm = _RE_HCH_CABALLO.match(lineas[i])
        mandil, nombre = mm.group(1), mm.group(2).strip()
        limite = inicio_caballo[idx + 1] if idx + 1 < len(inicio_caballo) else n
        # no cruzar al siguiente encabezado de carrera
        sig_hdr = [h for h in hdrs_ordenados if h > i]
        if sig_hdr:
            limite = min(limite, sig_hdr[0])

        jinete = prep = padres = criador = stud = ""
        for j in range(i + 1, min(i + 6, limite)):
            mj = _RE_HCH_JINETE.match(lineas[j])
            if mj:
                partes = [p for p in re.split(r"\s{2,}", mj.group(1).strip()) if p]
                if partes:
                    jinete = espaciar_iniciales(partes[0])
                    ultimo = partes[-1]
                    if len(partes) > 1 and re.search(r"[A-Za-z]{2}", ultimo) and not re.match(r"^\(?\d", ultimo):
                        prep = ultimo
                # línea de padres/haras: la siguiente con paréntesis final
                for k in range(j + 1, min(j + 4, limite)):
                    mp = _RE_PAREN_FINAL.search(lineas[k])
                    if mp and (" por " in lineas[k] or " y " in lineas[k]):
                        criador = mp.group(1).strip()
                        padres = lineas[k][: mp.start()].strip()
                        break
                break
        for j in range(i + 1, limite):
            ms = _RE_HCH_STUD.match(lineas[j])
            if ms:
                stud = titulo(ms.group(1))
                break
        filas.append(fila(
            fecha=fecha.isoformat() if fecha else "", hipodromo=HIP_HCH, reunion=reunion,
            carrera=carrera, hora=hora, prueba=prueba, mandil=mandil,
            caballo=titulo(nombre), jinete=jinete, preparador=prep, stud=stud,
            criador=criador, padres=padres,
        ))
    return corregir_horas(filas)


# ==========================================================================
# TELETRAK.CL — portada con el link directo al volante de la semana de cada hipódromo,
# sin JavaScript (los otros tres hipódromos lo descubren a través de aquí).
# ==========================================================================
TELETRAK_HOME = "https://teletrak.cl/"
_RE_TELETRAK_ENTRY = re.compile(
    r'!\[(?:Icono|Location)\]\([^)]*\)\s+([^\n]+?)\s*\n\n'
    r'(?:\[Descargar programa\]\((\S+?)\s+"Descargar programa"\)|Ver detalles)'
)


def teletrak_semana(hoy: dt.date) -> list[dict]:
    """Lee la portada de teletrak.cl (server-renderizada, sin JavaScript), que lista desde
    hoy un día por fila con el link directo al volante de cada hipódromo cuando ya está
    publicado. Devuelve [{fecha, hipodromo, url}]."""
    r = http_get(TELETRAK_HOME)
    r.raise_for_status()
    salida = []
    for i, m in enumerate(_RE_TELETRAK_ENTRY.finditer(r.text)):
        nombre, url = m.group(1).strip(), m.group(2)
        if url:
            fecha = fecha_de_url(url) or (hoy + dt.timedelta(days=i))
            salida.append(dict(fecha=fecha, hipodromo=nombre, url=url))
    return salida


def _teletrak_entrada(semana: list[dict], contiene: str) -> dict | None:
    return next((e for e in semana if contiene.lower() in e["hipodromo"].lower()), None)


def cargar_hch_automatico(hoy: dt.date | None = None) -> tuple[list[dict], str]:
    """Descarga y lee el volante de la próxima reunión de Hipódromo Chile (vía teletrak.cl),
    sin que el usuario tenga que adjuntar nada."""
    try:
        semana = teletrak_semana(hoy or dt.date.today())
    except requests.RequestException as e:
        return [], f"Hipódromo Chile: no se pudo conectar a teletrak.cl ({type(e).__name__})."
    ent = _teletrak_entrada(semana, "Hipódromo Chile")
    if not ent:
        return [], "Hipódromo Chile: no encontré su próximo volante en teletrak.cl."
    try:
        r = http_get(ent["url"])
        r.raise_for_status()
    except requests.RequestException as e:
        return [], f"Hipódromo Chile: no se pudo descargar el volante ({type(e).__name__})."
    try:
        return parse_hch(r.content), ""
    except Exception as e:  # noqa: BLE001
        return [], f"Hipódromo Chile: no se pudo leer el volante descargado ({e})."


def cargar_sporting_automatico(hoy: dt.date | None = None) -> tuple[list[dict], str]:
    """Detecta la fecha de la próxima reunión de Valparaíso Sporting (vía teletrak.cl) y
    lee sus carreras, sin que el usuario tenga que pegar ningún link."""
    try:
        semana = teletrak_semana(hoy or dt.date.today())
    except requests.RequestException as e:
        return [], f"Valparaíso Sporting: no se pudo conectar a teletrak.cl ({type(e).__name__})."
    ent = _teletrak_entrada(semana, "Sporting")
    if not ent:
        return [], "Valparaíso Sporting: no encontré su próxima reunión en teletrak.cl."
    return cargar_sporting(ent["fecha"])


# ==========================================================================
# CLUB HÍPICO DE SANTIAGO  (volante PDF)
# ==========================================================================
def _lineas_columna(pagina, x0, x1) -> list[str]:
    recorte = pagina.crop((x0, 0, x1, pagina.height))
    txt = recorte.extract_text(x_tolerance=1.5) or ""
    return [l.strip() for l in txt.split("\n") if l.strip()]


_RE_CHS_REFS = re.compile(r"(\d{1,2})ª\s+(\d{1,2})")


def _leer_indice(lineas: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Índice 'NOMBRE  14ª 3  15ª 5' -> {nombre: [(carrera, mandil)]}.
    El nombre puede venir solo en una línea y las referencias en la siguiente;
    las referencias pueden continuar en varias líneas (y pasar a la otra columna)."""
    res: dict[str, list[tuple[str, str]]] = {}
    actual = None
    for l in lineas:
        if l.startswith(("CHS Reunión", "Indice de", "Criador", "Preparador", "Jinete", "Carrera N")):
            continue
        refs = _RE_CHS_REFS.findall(l)
        primera = _RE_CHS_REFS.search(l)
        nombre = l[: primera.start()].strip() if primera else l.strip()
        if refs:
            if nombre:
                actual = nombre
                res.setdefault(actual, [])
            if actual is not None:
                res[actual].extend(refs)
        else:
            # línea sólo con nombre (o con la continuación de un nombre largo)
            if actual is not None and not res.get(actual):
                nuevo_nombre = f"{actual} {nombre}"
                res[nuevo_nombre] = res.pop(actual)
                actual = nuevo_nombre
            else:
                actual = nombre
                res.setdefault(actual, [])
    return res


def parse_chs(pdf_bytes: bytes) -> list[dict]:
    import pdfplumber

    paginas = pdf_paginas_layout(pdf_bytes)
    texto = "\n".join(paginas)
    fecha = _fecha_es(texto[:2000]) or _fecha_es(texto)
    m = re.search(r"Reuni[oó]n\s+(\d+)", texto[:2000])
    reunion = m.group(1) if m else ""

    idx_listado, idx_prep, idx_crit, idx_programa = [], [], [], []
    for i, p in enumerate(paginas):
        cab = "\n".join(p.split("\n")[:4])
        if "Ejemplares por Carrera" in cab:
            idx_listado.append(i)
        if "Indice de Preparadores" in cab:
            idx_prep.append(i)
        if "Indice de Criadores" in cab:
            idx_crit.append(i)
        if "Programa de Hoy" in cab:
            idx_programa.append(i)

    # ---- Programa de hoy: prueba, distancia, condición -------------------
    info_carrera: dict[str, dict] = {}
    for i in idx_programa:
        for l in paginas[i].split("\n"):
            mm = re.match(
                r"^\s*(\d+)ª\s+(\d{1,2}:\d{2})\s+(.+?)\s+(\d{3,4})m\s+(.*?)\s+\$[\d.]+\s*$", l)
            if mm:
                info_carrera[mm.group(1)] = dict(
                    hora=mm.group(2), prueba=titulo(mm.group(3)),
                    distancia=mm.group(4) + " m", cond=re.sub(r"\s{2,}", " ", mm.group(5)))

    horses: dict[tuple[str, str], dict] = {}
    criadores: dict[str, list] = {}
    preparadores: dict[str, list] = {}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        mitad = pdf.pages[0].width / 2 - 2

        def col(pn, lado):
            p = pdf.pages[pn]
            return _lineas_columna(p, 0, mitad) if lado == 0 else _lineas_columna(p, mitad, p.width)

        # ---- Listado de ejemplares por carrera (dos columnas) ------------
        flujo = []
        for pn in idx_listado:
            flujo += col(pn, 0) + col(pn, 1)
        carrera_act = ""
        for l in flujo:
            mc = re.match(r"^(\d{1,2})ª\s+(\d{1,2}:\d{2})\b", l)
            if mc:
                carrera_act = mc.group(1)
                continue
            mh = re.match(r"^(\d{1,2})\s+(.+?)\s+G\d\s+(\d{2,3})\s+(.+)$", l)
            if mh and carrera_act:
                horses[(carrera_act, mh.group(1))] = dict(
                    caballo=titulo(mh.group(2)), jinete=titulo(mh.group(4)))

        # ---- Índice de preparadores (columna izquierda) ------------------
        flujo = []
        for pn in idx_prep:
            flujo += col(pn, 0)
        preparadores = _leer_indice(flujo)

        # ---- Índice de criadores (izquierda y luego derecha) -------------
        flujo = []
        for pn in idx_crit:
            flujo += col(pn, 0) + col(pn, 1)
        criadores = _leer_indice(flujo)

    prep_de, crit_de = {}, {}
    for nombre, refs in preparadores.items():
        nom = re.sub(r"^\(\w+\)\s*", "", nombre)          # '(AP) JOSE ARAYA B.'
        for r in refs:
            prep_de[r] = titulo(nom)
    for nombre, refs in criadores.items():
        for r in refs:
            crit_de[r] = titulo(nombre)

    # ---- Studs desde las fichas: 'NOMBRE (h, c, 5)  JIN: ...  STUD: «X»' ---
    stud_de: dict[str, str] = {}
    nombre_ficha = None
    for l in texto.split("\n"):
        mf = re.match(r"^\s{6,}(.+?)\s+(?:\([A-Za-z ]+\)\s+)?\(([a-z]),\s*[a-z]+,\s*\d+\)\s+JIN:", l)
        if mf:
            nombre_ficha = norm(re.sub(r"\(.*?\)", "", mf.group(1)))
        ms = re.search(r"STUD:\s*[«\"]\s*(.+?)\s*[»\"]", l)
        if ms and nombre_ficha:
            stud_de.setdefault(nombre_ficha, titulo(ms.group(1)))

    filas = []
    for (car, mandil), h in sorted(horses.items(), key=lambda kv: (int(kv[0][0]), int(kv[0][1]))):
        info = info_carrera.get(car, {})
        filas.append(fila(
            fecha=fecha.isoformat() if fecha else "", hipodromo=HIP_CHS, reunion=reunion,
            carrera=car, hora=info.get("hora", ""), prueba=info.get("prueba", ""),
            distancia=info.get("distancia", ""), mandil=mandil, caballo=h["caballo"],
            jinete=h["jinete"], preparador=prep_de.get((car, mandil), ""),
            stud=stud_de.get(norm(re.sub(r"\(.*?\)", "", h["caballo"])), ""), criador=crit_de.get((car, mandil), ""),
        ))
    return filas


# ==========================================================================
# CLUB HÍPICO DE SANTIAGO  (volante PDF)
# ==========================================================================
CHS_VOLANTE_PAGINA = "https://www.clubhipico.cl/carreras/volante/"
_RE_CHS_PDF = re.compile(r"https://static\.clubhipico\.cl/archivos/volantes/\d{2}-\d{2}-\d{4}\.pdf")


def chs_pdf_proxima_reunion() -> tuple[str, str]:
    """Lee la página del volante del Club Hípico (sin necesitar JavaScript: el link real
    del PDF de la próxima reunión viene incluido en el HTML) y devuelve (url_pdf, aviso)."""
    try:
        r = http_get(CHS_VOLANTE_PAGINA)
    except requests.RequestException as e:
        return "", f"Club Hípico: no se pudo conectar a su sitio ({type(e).__name__})."
    if r.status_code != 200:
        return "", f"Club Hípico: su sitio respondió HTTP {r.status_code}."
    m = _RE_CHS_PDF.search(r.text)
    if not m:
        return "", "Club Hípico: no encontré el link del volante en su página (puede que no haya reunión próxima)."
    return m.group(0), ""


def cargar_chs_automatico() -> tuple[list[dict], str]:
    """Descarga y lee el volante de la próxima reunión del Club Hípico, sin que el usuario
    tenga que adjuntar nada."""
    url, aviso = chs_pdf_proxima_reunion()
    if not url:
        return [], aviso
    try:
        r = http_get(url)
        r.raise_for_status()
    except requests.RequestException as e:
        return [], f"Club Hípico: no se pudo descargar el volante ({type(e).__name__})."
    try:
        return parse_chs(r.content), ""
    except Exception as e:  # noqa: BLE001
        return [], f"Club Hípico: no se pudo leer el volante descargado ({e})."


# ==========================================================================
# VALPARAÍSO SPORTING  (HTML)
# ==========================================================================
SPORTING_BASE = "https://www.sporting.cl/hipica/front/es"


def sporting_reunion_url(fecha: dt.date) -> str:
    return f"{SPORTING_BASE}/reunion/{fecha:%Y-%m-%d}.html"


def sporting_programa_url(fecha: dt.date, n: int) -> str:
    return f"{SPORTING_BASE}/programa/{fecha:%Y-%m-%d}/{n:02d}.html"


def sporting_carreras_de_reunion(html: str, fecha: dt.date) -> tuple[list[int], str]:
    """Números de carrera enlazados en la página de la reunión y nº de reunión."""
    ds = fecha.strftime("%Y-%m-%d")
    nums = sorted({int(n) for n in re.findall(rf"/programa/{ds}/(\d{{2}})\.html", html)})
    texto = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    m = re.search(r"N[ºo°]\s*(\d+)\s*-\s*[A-Za-zÁÉÍÓÚáéíóú]+\s+\d+", texto)
    return nums, (m.group(1) if m else "")


_RE_SP_FICHA = re.compile(
    r"Edad:\s*(?P<edad>.+?)\s+Stud:\s*(?P<stud>.+?)\s+Padres:\s*(?P<padres>.+?)\s+"
    r"Haras:\s*(?P<haras>.+?)\s+Preparador:\s*(?P<prep>.+?)\s+Jinete:\s*(?P<jinete>.+?)\s+"
    r"Peso:"
)


def parse_sporting_carrera(html: str, fecha: dt.date, reunion: str = "") -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()

    # cada ejemplar es un enlace a /ejemplares/<id>.html
    for a in soup.find_all("a", href=re.compile(r"/ejemplares/\d+\.html")):
        nombre = a.get_text(" ", strip=True)
        a.replace_with(f" ¤¤{nombre}¤¤ ")
    texto = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    mc = re.search(
        r"(\d+)ª\s*Carrera\s*-\s*Premio\s*[\"“«]?\s*(.+?)\s*[\"”»]?\s+(\d{1,2}:\d{2})\s*hrs", texto)
    carrera, prueba, hora = (mc.group(1), mc.group(2), mc.group(3)) if mc else ("", "", "")
    md = re.search(r"Distancia\s*/\s*Tipo Pista:\s*([\d.]+)\s*mts", texto)
    distancia = (md.group(1) + " m") if md else ""

    partes = re.split(r"¤¤(.+?)¤¤", texto)
    # partes = [antes, nombre1, seg1, nombre2, seg2, ...]
    filas = []
    previo = partes[0] if partes else ""
    for k in range(1, len(partes) - 1, 2):
        nombre, seg = partes[k], partes[k + 1]
        mf = _RE_SP_FICHA.search(seg)
        if not mf:
            previo = seg
            continue
        mm = re.search(r"(\d{1,2})\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+(?:\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+)?\s*$", previo[-80:])
        mandil = mm.group(1) if mm else str((k + 1) // 2)
        filas.append(fila(
            fecha=fecha.isoformat(), hipodromo=HIP_SPORTING, reunion=reunion,
            carrera=carrera, hora=hora, prueba=prueba, distancia=distancia,
            mandil=mandil, caballo=nombre.strip(),
            jinete=limpiar_persona(mf.group("jinete")),
            preparador=limpiar_persona(mf.group("prep")),
            stud=mf.group("stud").strip(), criador=mf.group("haras").strip(),
            padres=mf.group("padres").strip(),
        ))
        previo = seg
    return filas


def fecha_de_url(url: str):
    """Fecha (AAAA-MM-DD) contenida en un link de Sporting."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", url or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def cargar_sporting_url(url: str) -> tuple[list[dict], str]:
    """Recibe el link de la reunión (o de cualquiera de sus carreras) y devuelve todos los
    ejemplares de la reunión. Las páginas de cada carrera se buscan automáticamente."""
    url = (url or "").strip()
    if "sporting.cl" not in url:
        return [], f"«{url[:60]}» no parece un link de Valparaíso Sporting."
    fecha = fecha_de_url(url)
    if not fecha:
        return [], ("Al link le falta la fecha. Debe verse como "
                    "https://www.sporting.cl/hipica/front/es/reunion/2026-10-07.html")
    return cargar_sporting(fecha)


def cargar_sporting(fecha: dt.date) -> tuple[list[dict], str]:
    """Todos los ejemplares de la reunión de Sporting de esa fecha."""
    try:
        r = http_get(sporting_reunion_url(fecha))
    except requests.RequestException as e:
        return [], f"Sporting {fecha:%d-%m-%Y}: no se pudo conectar ({type(e).__name__})."
    if r.status_code == 404:
        return [], f"Sporting {fecha:%d-%m-%Y}: no existe una reunión con ese link (¿aún no se publica?)."
    if r.status_code != 200:
        return [], f"Sporting {fecha:%d-%m-%Y}: el sitio respondió HTTP {r.status_code}."
    nums, reunion = sporting_carreras_de_reunion(r.text, fecha)
    sondeo = not nums
    if sondeo:                       # la página no lista las carreras: probar 1..20
        nums = list(range(1, 21))

    def una(n):
        rr = http_get(sporting_programa_url(fecha, n))
        if rr.status_code == 404 and sondeo:
            return []
        rr.raise_for_status()
        return parse_sporting_carrera(rr.text, fecha, reunion)

    filas, aviso = [], ""
    with ThreadPoolExecutor(max_workers=5) as ex:
        for n, res in zip(nums, ex.map(lambda x: _seguro(una, x), nums)):
            if isinstance(res, Exception):
                aviso += f"Sporting {fecha:%d-%m} carrera {n}: {res}. "
            else:
                if not res and not sondeo:
                    aviso += f"Sporting {fecha:%d-%m} carrera {n}: no se leyeron ejemplares. "
                filas += res
    if not filas and not aviso:
        aviso = f"Sporting {fecha:%d-%m-%Y}: la reunión todavía no tiene carreras publicadas."
    return filas, aviso


def _seguro(fn, x):
    try:
        return fn(x)
    except Exception as e:  # noqa: BLE001
        return e


# ==========================================================================
# Volantes PDF: lectura con detección automática del hipódromo
# ==========================================================================
def leer_volante(pdf_bytes: bytes, esperado: str) -> tuple[list[dict], str]:
    """esperado: 'hch' o 'chs'. Si el PDF resulta ser del otro hipódromo, se lee igual y se avisa."""
    lectores = {"hch": (parse_hch, HIP_HCH), "chs": (parse_chs, HIP_CHS)}
    otro = "chs" if esperado == "hch" else "hch"
    try:
        filas = lectores[esperado][0](pdf_bytes)
    except Exception:  # noqa: BLE001
        filas = []
    if filas:
        return filas, ""
    try:
        filas = lectores[otro][0](pdf_bytes)
    except Exception:  # noqa: BLE001
        filas = []
    if filas:
        return filas, (f"Ese PDF era de {lectores[otro][1]}, no de {lectores[esperado][1]}; "
                       "lo usé igual con el hipódromo correcto.")
    return [], ("No pude leer caballos en ese PDF. Revisa que sea el volante oficial con el programa "
                "completo (el de Hipódromo Chile o el del Club Hípico).")


# ==========================================================================
# Búsqueda
# ==========================================================================
CAMPOS_BUSQUEDA = {
    "Todo": ["caballo", "criador", "jinete", "preparador", "stud"],
    "Caballo": ["caballo"],
    "Criador (haras)": ["criador"],
    "Jinete": ["jinete"],
    "Preparador": ["preparador"],
    "Stud": ["stud"],
}


def _coincide(valor: str, tokens: list[str], exacta: bool, campo: str) -> bool:
    if not valor:
        return False
    generica = campo == "criador"
    v = norm(valor, quitar_genericas=generica)
    if exacta:
        return v == " ".join(tokens)
    palabras = v.split()
    return all(any(p.startswith(t) for p in palabras) for t in tokens)


def buscar(df: pd.DataFrame, consulta: str, campo: str = "Todo", exacta: bool = False) -> pd.DataFrame:
    """Filtra df. Cada palabra de la consulta debe coincidir (por inicio de palabra)
    dentro de un mismo campo. Agrega la columna 'coincide_en'."""
    if df.empty or not consulta.strip():
        return df.iloc[0:0].assign(coincide_en="")
    campos = CAMPOS_BUSQUEDA[campo]
    tokens_gen = norm(consulta, quitar_genericas=True).split()
    tokens_raw = norm(consulta).split()
    filas, motivos = [], []
    for idx, r in df.iterrows():
        hallado = []
        for c in campos:
            toks = tokens_gen if (c == "criador" and tokens_gen) else tokens_raw
            if toks and _coincide(r[c], toks, exacta, c):
                hallado.append(c)
        if hallado:
            filas.append(idx)
            motivos.append(", ".join(hallado))
    out = df.loc[filas].copy()
    out["coincide_en"] = motivos
    return out


def sugerencias(df: pd.DataFrame, consulta: str, campo: str = "Todo", n: int = 5) -> list[str]:
    """Nombres parecidos cuando no hay resultados."""
    from rapidfuzz import fuzz, process

    valores = set()
    for c in CAMPOS_BUSQUEDA[campo]:
        valores |= {v for v in df[c].unique() if v}
    if not valores:
        return []
    q = norm(consulta, quitar_genericas=True)
    mapa = {v: norm(v, quitar_genericas=True) for v in valores}
    res = process.extract(q, mapa, scorer=fuzz.WRatio, limit=n)
    return [k for (_, score, k) in res if score >= 70]


def a_dataframe(filas: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(filas, columns=COLUMNAS)
    if not df.empty:
        df["_c"] = pd.to_numeric(df["carrera"], errors="coerce")
        df["_m"] = pd.to_numeric(df["mandil"], errors="coerce")
        df = df.sort_values(["fecha", "hipodromo", "_c", "_m"]).drop(columns=["_c", "_m"])
        df = df.reset_index(drop=True)
    return df


# ==========================================================================
# INTERFAZ WEB (Streamlit)
# ==========================================================================
import datetime as dt

import streamlit as st

st.set_page_config(page_title="Buscador hípico de Chile", page_icon="🐎", layout="wide")

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def fmt_fecha(iso_o_fecha) -> str:
    if not iso_o_fecha:
        return ""
    d = iso_o_fecha if isinstance(iso_o_fecha, dt.date) else dt.date.fromisoformat(iso_o_fecha)
    return f"{DIAS[d.weekday()]} {d:%d-%m-%Y}"


# --------------------------------------------------------------------------
# Lectura con caché
# --------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def leer_hch_automatico():
    return cargar_hch_automatico()


@st.cache_data(ttl=900, show_spinner=False)
def leer_chs_automatico():
    return cargar_chs_automatico()


@st.cache_data(ttl=900, show_spinner=False)
def leer_sporting_automatico():
    return cargar_sporting_automatico()


@st.cache_data(ttl=3600, show_spinner=False)
def leer_pdf(contenido: bytes, esperado: str):
    return leer_volante(contenido, esperado)


@st.cache_data(ttl=900, show_spinner=False)
def leer_sporting_link(url: str):
    return cargar_sporting_url(url)


# --------------------------------------------------------------------------
# Encabezado
# --------------------------------------------------------------------------
st.title("🐎 Buscador hípico de Chile")
st.caption("Busca por caballo, criadero (haras), jinete, preparador o stud. La app entra sola a "
           "Hipódromo Chile, Club Hípico de Santiago y Valparaíso Sporting y trae sus próximas reuniones.")

filas, avisos = [], []
with st.spinner("Revisando las próximas reuniones…"):
    for nombre, fn in ((HIP_HCH, leer_hch_automatico), (HIP_CHS, leer_chs_automatico),
                       (HIP_SPORTING, leer_sporting_automatico)):
        r, a = fn()
        filas += r
        if a:
            avisos.append(a)

df = a_dataframe(filas)
if not df.empty:
    df = df.drop_duplicates(subset=["fecha", "hipodromo", "carrera", "mandil", "caballo"]).reset_index(drop=True)

reuniones = (
    df.groupby(["fecha", "hipodromo"]).agg(carreras=("carrera", "nunique"), caballos=("caballo", "count")).reset_index()
    if not df.empty else df)

cargadas = set(reuniones.hipodromo) if not df.empty else set()
c1, c2, c3 = st.columns(3)
for col, nombre in zip((c1, c2, c3), (HIP_HCH, HIP_CHS, HIP_SPORTING)):
    with col:
        if nombre in cargadas:
            f = reuniones.loc[reuniones.hipodromo == nombre, "fecha"].iloc[0]
            st.success(f"**{nombre}**\n\n{fmt_fecha(f)} ✓")
        else:
            st.warning(f"**{nombre}**\n\nNo se pudo cargar sola")

if avisos:
    with st.expander(f"⚠️ Avisos ({len(avisos)})"):
        for a in avisos:
            st.write(a)

with st.expander("¿Buscas otra fecha o un hipódromo no se cargó? Agrégalo aquí"):
    m1, m2 = st.columns(2)
    with m1:
        pdfs_extra = st.file_uploader(
            "Volante PDF (Hipódromo Chile o Club Hípico)", type="pdf", accept_multiple_files=True,
            help="La app detecta sola a cuál de los dos pertenece.")
    with m2:
        link_extra = st.text_input(
            "Link de una reunión de Valparaíso Sporting",
            placeholder="https://www.sporting.cl/hipica/front/es/reunion/2026-10-07.html")
    for archivo in pdfs_extra or []:
        r, a = leer_pdf(archivo.getvalue(), "hch")
        filas += r
        if a and "no pude leer" in a.lower():
            avisos.append(f"{archivo.name}: {a}")
    if link_extra.strip():
        r, a = leer_sporting_link(link_extra.strip())
        filas += r
        if a:
            avisos.append(a)

df = a_dataframe(filas)
if not df.empty:
    df = df.drop_duplicates(subset=["fecha", "hipodromo", "carrera", "mandil", "caballo"]).reset_index(drop=True)

if df.empty:
    st.error("No logré cargar ninguna reunión automáticamente. Prueba agregando un volante o link manualmente arriba.")
    st.stop()

reuniones = (
    df.groupby(["fecha", "hipodromo"]).agg(carreras=("carrera", "nunique"), caballos=("caballo", "count")).reset_index())
with st.expander(f"Reuniones cargadas ({len(reuniones)})"):
    st.dataframe(
        reuniones.assign(fecha=reuniones.fecha.map(fmt_fecha)).rename(columns={
            "fecha": "Fecha", "hipodromo": "Hipódromo", "carreras": "Carreras", "caballos": "Caballos"}),
        hide_index=True, width="stretch")

# --------------------------------------------------------------------------
# Buscar
# --------------------------------------------------------------------------
st.subheader("Busca")
b1, b2, b3 = st.columns([3, 1.4, 1.2])
consulta = b1.text_input("¿Qué buscas?", placeholder="Ej.: Haras Santa Mónica, un caballo, J. Herrera, Sagardia…")
campo = b2.selectbox("Buscar en", list(CAMPOS_BUSQUEDA), index=0)
exacta = b3.checkbox("Nombre exacto", help="Solo nombres idénticos (sin contar \'Haras\', \'H.\' ni tildes).")

if not consulta.strip():
    st.caption("Escribe arriba lo que quieres buscar. Por ejemplo un criadero: **Haras Santa Mónica**.")
else:
    res = buscar(df, consulta, campo, exacta)
    if res.empty:
        st.error(f"No encontré «{consulta}» en las {len(reuniones)} reunión(es) cargadas.")
        sug = sugerencias(df, consulta, campo)
        if sug:
            st.write("¿Quisiste decir…?")
            for s in sug:
                st.write("• " + s)
    else:
        n_reu = res.groupby(["fecha", "hipodromo"]).ngroups
        st.success(f"**{len(res)}** caballo(s) en **{n_reu}** reunión(es) para «{consulta}».")

        for c in CAMPOS_BUSQUEDA[campo]:
            vals = res.loc[res.coincide_en.str.contains(c), c]
            distintos = {}
            for v in vals:
                distintos.setdefault(norm(v, quitar_genericas=(c == "criador")), []).append(v)
            if len(distintos) > 1:
                nombres = ", ".join(f"{v[0]} ({len(v)})" for v in distintos.values())
                st.warning(f"La búsqueda coincidió con varios nombres en **{c}**: {nombres}. "
                           "Si buscas uno solo, escribe el nombre completo y marca «Nombre exacto».")

        tabla = res.assign(fecha=res.fecha.map(fmt_fecha)).rename(columns={
            "fecha": "Fecha", "hipodromo": "Hipódromo", "reunion": "Reunión", "carrera": "Carrera",
            "hora": "Hora", "prueba": "Prueba", "distancia": "Distancia", "mandil": "Nº",
            "caballo": "Caballo", "jinete": "Jinete", "preparador": "Preparador",
            "stud": "Stud", "criador": "Criador (haras)", "padres": "Padres",
            "coincide_en": "Coincide en"})
        orden = ["Fecha", "Hipódromo", "Carrera", "Hora", "Prueba", "Distancia", "Nº", "Caballo",
                 "Jinete", "Preparador", "Stud", "Criador (haras)", "Coincide en"]
        st.dataframe(tabla[orden], hide_index=True, width="stretch")
        st.download_button("Descargar resultados (CSV)",
                           tabla[orden].to_csv(index=False).encode("utf-8-sig"),
                           file_name="resultados_hipica.csv", mime="text/csv")

st.divider()
st.caption(
    "Datos tomados de los programas oficiales de cada hipódromo. Los retiros de última hora no "
    "aparecen en ellos: confírmalos en el sitio del hipódromo antes de decidir. "
    "Herramienta independiente, sin relación con los hipódromos."
)
