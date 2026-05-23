import unicodedata
from datetime import date, timedelta

import time
from requests.exceptions import ConnectionError, Timeout, SSLError, HTTPError

import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Playas Asturias - AEMET", layout="wide")
st.title("🌊 Predicción por horas en playas de Asturias para Alicia ")

import os

try:
    AEMET_API_KEY = st.secrets.get("AEMET_API_KEY")
except Exception:
    AEMET_API_KEY = None

if not AEMET_API_KEY:
    AEMET_API_KEY = os.getenv("AEMET_API_KEY")

AEMET_BASE = "https://opendata.aemet.es/opendata"

# 3 playas + municipio asociado (predicción horaria por municipio)
BEACHES = [
    {"name": "San Lorenzo (Gijón)", "municipio_nombre": "Gijón", "lat": 43.5405, "lon": -5.65487},
    {"name": "Rodiles (Villaviciosa)", "municipio_nombre": "Villaviciosa", "lat": 43.532527, "lon": -5.38244},
    {"name": "Torimbia (Llanes)", "municipio_nombre": "Llanes", "lat": 43.4424125, "lon": -4.8550563888889},
    {"name": "Aguilar (Muros de Nalón)", "municipio_nombre": "Muros de Nalón", "lat": 43.5558, "lon": -6.1173},
    {"name": "La Concha de Artedo (Cudillero)", "municipio_nombre": "Cudillero", "lat": 43.562699, "lon": -6.185861},
]

# ---------- utilidades ----------

WEBCAMS = {
    "San Lorenzo (Gijón)": "https://www.webcamsdeasturias.com/asturias/centro/gijon/gijon/la-escalerona-playa-de-san-lorenzo-hd/148/",
    "Aguilar (Muros de Nalón)": "https://www.webcamsdeasturias.com/asturias/bajo-nalon/muros-del-nalon/aguilar/playa-de-aguilar/122/",
    "La Concha de Artedo (Cudillero)": "https://www.webcamsdeasturias.com/asturias/comarca-vaqueira/cudillero/cudillero/playa-de-la-concha-de-artedo/151/",
    "Rodiles (Villaviciosa)": "https://www.webcamsdeasturias.com/asturias/comarca-de-la-sidra/villaviciosa/rodiles/rodiles-surf-hd/120/",
}

def norm_txt(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    return s


def pick_icon(desc: str) -> str:
    d = norm_txt(desc)
    if "tormenta" in d:
        return "⛈️"
    if any(k in d for k in ["lluv", "chubasc", "precipit"]):
        return "🌧️"
    if any(k in d for k in ["nieve", "nev"]):
        return "❄️"
    if any(k in d for k in ["niebla", "bruma"]):
        return "🌫️"
    if any(k in d for k in ["despejado", "soleado"]):
        return "☀️"
    if any(k in d for k in ["nub", "cubierto", "nubes"]):
        return "☁️"
    return "🌤️"

def classify_oleaje_marin(wave):
    try:
        w = float(wave)
    except Exception:
        return ""

    if w < 0.5:
        return "🌊 Muy tranquilo"
    elif w < 1:
        return "🌊 Tranquilo"
    elif w < 1.5:
        return "🌊 Algo movido"
    elif w < 2:
        return "🌊 Movido"
    else:
        return "🌊 Muy fuerte"


def classify_viento(v_kmh):
    """
    Clasifica el viento en 4 rangos sencillos.
    """
    try:
        v = float(v_kmh)
    except Exception:
        return ""

    if v < 5:
        return "🍃 Sin viento"
    elif v < 15:
        return "🌬️ Viento suave"
    elif v < 30:
        return "💨 Viento moderado"
    else:
        return "🌪️ Viento fuerte"


def classify_lluvia(prob):
    """
    Convierte la probabilidad de lluvia en rangos de texto.
    Si viene vacío o no se puede leer, lo tratamos como 0.
    """
    try:
        p = float(prob)
    except Exception:
        p = 0

    if p <= 0:
        return "Sin lluvia"
    elif p < 30:
        return "🌦️ Puede llover un poco"
    elif p < 70:
        return "🌧️ Casi seguro que llueve"
    else:
        return "⛈️ Llueve seguro"


def map_periodo_list(items):
    """
    Convierte listas tipo:
      [{"periodo":"00","descripcion":"Despejado"}, {"periodo":"01","descripcion":"Nuboso"}]
    o:
      [{"periodo":"00","value": 18}]
    en un dict: {"00": valor, "01": valor}
    """
    out = {}

    if not isinstance(items, list):
        return out

    for it in items:
        if not isinstance(it, dict):
            continue

        per = str(it.get("periodo") or it.get("hora") or "")
        if "-" in per:
            per = per.split("-")[0]
        per = per.zfill(2)

        val = it.get("value")
        if val is None:
            val = it.get("descripcion") or it.get("desc") or it.get("estado") or ""

        out[per] = val

    return out


def extract_viento_map(bloque):
    """
    Intenta sacar velocidad de viento por hora del bloque de predicción.
    Según cómo venga el JSON, puede variar un poco la estructura.
    """
    viento = bloque.get("vientoAndRachaMax") or bloque.get("viento") or {}

    # Caso 1: viene como dict con "dato"
    if isinstance(viento, dict):
        if "dato" in viento and isinstance(viento["dato"], list):
            return map_periodo_list(viento["dato"])

        if "velocidad" in viento and isinstance(viento["velocidad"], list):
            return map_periodo_list(viento["velocidad"])

    # Caso 2: viene directamente como lista
    if isinstance(viento, list):
        return map_periodo_list(viento)

    return {}

@st.cache_data(ttl=3600)
def get_marine_data(lat: float, lon: float):
    """
    Open-Meteo Marine API:
    - wave_height (m)
    - wave_period (s)
    - wave_direction (°)
    """
    url = "https://marine-api.open-meteo.com/v1/marine"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wave_height,wave_period,wave_direction",
        "forecast_days": 4,
        "timezone": "auto",
    }

    r = requests.get(url, params=params, timeout=30)
    if r.status_code == 400:
        raise RuntimeError(f"Open-Meteo respondió 400: {r.text}")
    r.raise_for_status()
    return r.json()


def sign(x):
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def find_prev_turn(values, idx):
    """
    Busca el último cambio de tendencia antes del índice actual.
    """
    if idx <= 1:
        return 0

    current_dir = sign(values[idx] - values[idx - 1])
    if current_dir == 0:
        return max(0, idx - 1)

    for j in range(idx - 1, 0, -1):
        d = sign(values[j] - values[j - 1])
        if d != 0 and d != current_dir:
            return j

    return 0


def find_next_turn(values, idx):
    """
    Busca el siguiente cambio de tendencia después del índice actual.
    """
    if idx >= len(values) - 2:
        return len(values) - 1

    current_dir = sign(values[idx + 1] - values[idx])
    if current_dir == 0:
        return min(len(values) - 1, idx + 1)

    for j in range(idx + 1, len(values) - 1):
        d = sign(values[j + 1] - values[j])
        if d != 0 and d != current_dir:
            return j

    return len(values) - 1


def describe_tide_state(sea_levels, idx):
    """
    Devuelve:
    - dirección de marea (subiendo/bajando)
    - porcentaje de la fase actual
    """
    if idx <= 0 or idx >= len(sea_levels) - 1:
        return "", ""

    current = sea_levels[idx]
    prev_val = sea_levels[idx - 1]
    next_val = sea_levels[idx + 1]

    # Dirección de la marea
    if next_val > current:
        estado = "⬆️ Subiendo"
    elif next_val < current:
        estado = "⬇️ Bajando"
    else:
        estado = "⏸️ Estable"

    prev_turn = find_prev_turn(sea_levels, idx)
    next_turn = find_next_turn(sea_levels, idx)

    a = sea_levels[prev_turn]
    b = sea_levels[next_turn]

    # Evitar división por cero
    if a == b:
        return estado, ""

    # Si sube: desde mínimo anterior a máximo siguiente
    if estado.startswith("⬆️"):
        progreso = (current - a) / (b - a) * 100 if (b - a) != 0 else 0
        progreso = max(0, min(100, progreso))
        fase = f"{progreso:.0f}% de la subida"
        return estado, fase

    # Si baja: desde máximo anterior a mínimo siguiente
    if estado.startswith("⬇️"):
        progreso = (a - current) / (a - b) * 100 if (a - b) != 0 else 0
        progreso = max(0, min(100, progreso))
        fase = f"{progreso:.0f}% de la bajada"
        return estado, fase

    return estado, ""

def get_json_with_retries(url, params=None, timeout=30, tries=4):
    """
    Hace una petición GET con reintentos y backoff.
    Sirve tanto para AEMET como para la API marina.
    """
    last_err = None

    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)

            # Si hay rate limit, espera y reintenta
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** i))
                time.sleep(wait)
                continue

            r.raise_for_status()
            return r.json()

        except (ConnectionError, Timeout, SSLError) as e:
            last_err = e
            time.sleep(2 ** i)

        except HTTPError as e:
            status = e.response.status_code if e.response is not None else None

            if status in (429, 500, 502, 503, 504):
                last_err = e
                time.sleep(2 ** i)
            else:
                raise

    raise RuntimeError(f"Fallo de conexión tras {tries} intentos. Último error: {last_err}")


# ---------- AEMET OpenData: patrón 2 pasos (meta -> datos) ----------

import time

def aemet_two_step_json(api_path: str):
    """
    AEMET OpenData responde en 2 pasos:
    1) endpoint meta -> JSON con 'datos'
    2) GET a esa URL 'datos' -> JSON real
    """
    if not AEMET_API_KEY or AEMET_API_KEY == "PEGA_AQUI_TU_API_KEY":
        raise RuntimeError("Pega tu API key en la variable AEMET_API_KEY del código.")

    url_meta = f"{AEMET_BASE}/api{api_path}"

    # Reintentos en la llamada META
    for intento in range(5):
        r1 = requests.get(url_meta, params={"api_key": AEMET_API_KEY}, timeout=25)

        if r1.status_code == 429:
            espera = 2 ** intento
            time.sleep(espera)
            continue

        r1.raise_for_status()
        meta = r1.json()
        break
    else:
        raise RuntimeError("AEMET devolvió 429 demasiadas veces en la llamada meta.")

    datos_url = meta.get("datos")
    if not datos_url:
        raise RuntimeError(f"No llegó 'datos'. Respuesta meta: {meta}")

    # Reintentos en la llamada DATOS
    for intento in range(5):
        r2 = requests.get(datos_url, timeout=60)

        if r2.status_code == 429:
            espera = 2 ** intento
            time.sleep(espera)
            continue

        r2.raise_for_status()
        return r2.json()

    raise RuntimeError("AEMET devolvió 429 demasiadas veces en la llamada de datos.")

# ---------- Maestro municipios: obtener ID ----------

@st.cache_data(ttl=86400)
def get_maestro_municipios():
    return aemet_two_step_json("/maestro/municipios")


def normalize_municipio_id(mid) -> str:
    s = str(mid).strip().lower()

    # Si viene con prefijo "id", lo quitamos
    if s.startswith("id"):
        s = s[2:]

    # Dejamos solo dígitos
    s = "".join(ch for ch in s if ch.isdigit())

    # Rellenamos con ceros a la izquierda si hiciera falta
    if len(s) < 5:
        s = s.zfill(5)

    return s


def find_municipio_id_by_name(nombre_municipio: str):
    target = norm_txt(nombre_municipio)
    data = get_maestro_municipios()

    candidates = []
    for row in data:
        if not isinstance(row, dict):
            continue

        nom = row.get("nombre") or row.get("nombreMunicipio") or row.get("nm") or ""
        mid = (
            row.get("id")
            or row.get("idMunicipio")
            or row.get("codigo")
            or row.get("codigoINE")
            or None
        )

        if not mid:
            continue

        nom_n = norm_txt(nom)

        if nom_n == target:
            return normalize_municipio_id(mid)

        if target in nom_n:
            candidates.append((len(nom_n), normalize_municipio_id(mid), nom))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    return None


# ---------- Predicción horaria por municipio ----------

@st.cache_data(ttl=3600)
def get_pred_municipio_horaria(municipio_id: str):
    return aemet_two_step_json(f"/prediccion/especifica/municipio/horaria/{municipio_id}")

def extract_day_block(pred_json, target_date_iso: str):
    """
    Intenta localizar el bloque del día YYYY-MM-DD dentro de la predicción.
    """
    root = pred_json[0] if isinstance(pred_json, list) and pred_json else pred_json
    if not isinstance(root, dict):
        return None

    pred = root.get("prediccion")
    dias = None

    if isinstance(pred, dict):
        dias = pred.get("dia") or pred.get("dias")
    elif isinstance(pred, list):
        dias = pred
    else:
        dias = root.get("dia")

    if not isinstance(dias, list):
        return None

    for d in dias:
        if not isinstance(d, dict):
            continue
        fecha = (d.get("fecha") or d.get("fechaPrediccion") or "")[:10]
        if fecha == target_date_iso:
            return d

    return None

WEEKDAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

def extract_available_days(pred_json):
    """
    Saca las fechas disponibles dentro de prediccion.dia[]
    """
    root = pred_json[0] if isinstance(pred_json, list) and pred_json else pred_json
    if not isinstance(root, dict):
        return []

    pred = root.get("prediccion")
    dias = []

    if isinstance(pred, dict):
        dias = pred.get("dia") or []
    elif isinstance(pred, list):
        dias = pred

    out = []
    for d in dias:
        if not isinstance(d, dict):
            continue
        fecha = (d.get("fecha") or d.get("fechaPrediccion") or "")[:10]
        if fecha:
            out.append(fecha)

    return out


def format_day_label(fecha_iso):
    """
    Convierte una fecha ISO (YYYY-MM-DD) en:
    Hoy / Mañana / Lunes / Martes ...
    """
    d = date.fromisoformat(fecha_iso)
    today = date.today()

    if d == today:
        return "Hoy"
    elif d == today + timedelta(days=1):
        return "Mañana"
    else:
        return WEEKDAYS_ES[d.weekday()]

# ---------- UI ----------

playa = st.selectbox("Elige playa", [b["name"] for b in BEACHES])
b = next(x for x in BEACHES if x["name"] == playa)

if webcam_url := WEBCAMS.get(playa):
    st.link_button("📷 Ver webcam en directo", webcam_url)

st.subheader("🕒 Predicción por horas")

try:
    municipio_id = find_municipio_id_by_name(b["municipio_nombre"])

    if not municipio_id:
        st.error(f"No encontré el ID del municipio: {b['municipio_nombre']}")
        st.stop()

    pred = get_pred_municipio_horaria(municipio_id)
    available_days = extract_available_days(pred)
    available_days = available_days[:4]

    if not available_days:
        st.warning("No hay días disponibles en la predicción.")
        st.stop()

    # Mostrar selector de día (etiqueta amigable + fecha ISO)
    day_labels = [f"{format_day_label(d)} — {d}" for d in available_days]
    selected_label = st.selectbox("Elige día", day_labels, index=0)
    selected_day = selected_label.split(" — ")[1]

    marine = get_marine_data(b["lat"], b["lon"])
    marine_hourly = marine.get("hourly", {})

    marine_times = marine_hourly.get("time", [])
    wave_height = marine_hourly.get("wave_height", [])
    wave_period = marine_hourly.get("wave_period", [])
    sea_level = marine_hourly.get("sea_level_height", [])


    # Usamos el día seleccionado por el usuario
    bloque = extract_day_block(pred, selected_day)

    marine_map = {}

    for i, t in enumerate(marine_times):
        # t viene tipo "2026-05-22T08:00"
        if t.startswith(selected_day):
            hora = t[11:13]  # "08"
            estado_marea, fase_marea = describe_tide_state(sea_level, i)

            marine_map[hora] = {
                "wave_height": wave_height[i] if i < len(wave_height) else "",
                "wave_period": wave_period[i] if i < len(wave_period) else "",
                "sea_level": sea_level[i] if i < len(sea_level) else "",
                "estado_marea": estado_marea,
                "fase_marea": fase_marea,
            }


    if not bloque:
        st.warning(f"No encontré el bloque de predicción para {selected_label}.")
        st.info("Te muestro el JSON para que me pegues la estructura y lo ajusto en 1 minuto:")
        st.json(pred)
        st.stop()

    # Estos campos suelen estar presentes; si alguno falta, quedará vacío.
    estado_map = map_periodo_list(bloque.get("estadoCielo"))
    probprec_map = map_periodo_list(bloque.get("probPrecipitacion"))

    temp_raw = bloque.get("temperatura", {})
    temp_list = temp_raw.get("dato") if isinstance(temp_raw, dict) else temp_raw
    temp_map = map_periodo_list(temp_list)

    viento_map = extract_viento_map(bloque)

    rows = []
    for h in [f"{i:02d}" for i in range(9, 22)]:
        desc = str(estado_map.get(h, ""))
        viento = viento_map.get(h, "")

        marine_row = marine_map.get(h, {})

        rows.append({
            "Hora": f"{h}:00",
            "Tiempo": pick_icon(desc),
            "Temp": temp_map.get(h, ""),
            "Lluvia": classify_lluvia(probprec_map.get(h, 0)),
            "Viento": classify_viento(viento),
            "Oleaje": classify_oleaje_marin(marine_row.get("wave_height", "")),
    
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

except Exception as e:
    st.exception(e)
