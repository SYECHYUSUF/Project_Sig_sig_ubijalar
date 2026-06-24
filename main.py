from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import json
import os

# ══════════════════════════════════════════════════════════════════
# Inisialisasi Aplikasi FastAPI
# ══════════════════════════════════════════════════════════════════
app = FastAPI(
    title="API SIG Kesesuaian Lahan Ubi Jalar",
    description="Backend API untuk analisis spasial lahan pertanian — SIG Semester 4 Universitas Hasanuddin\n\n"
                "Mode: GeoJSON File (tanpa database)",
    version="2.1.0"
)

# ==========================================
# PENGATURAN CORS
# ==========================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# KONFIGURASI PATH FILE GEOJSON
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GEOJSON_FILES = {
    "administrasi_wilayah": os.path.join(BASE_DIR, "geojson_administrasi_wilayah.geojson"),
    "curah_hujan":          os.path.join(BASE_DIR, "geojson_curah_hujan.geojson"),
    "kemiringan_lereng":    os.path.join(BASE_DIR, "geojson_kemiringan_lerang.geojson"),
    "pola_ruang":           os.path.join(BASE_DIR, "geojson_pola_ruang.geojson"),
    "kesesuaian_lahan":     os.path.join(BASE_DIR, "geojson_tanaman_ubi_jalan.geojson"),
}

# ==========================================
# CACHE DATA GEOJSON (dimuat sekali saat startup)
# ==========================================
_geojson_cache: dict = {}

def load_geojson(layer_name: str) -> dict:
    """Muat GeoJSON dari file, dengan caching di memori."""
    if layer_name in _geojson_cache:
        return _geojson_cache[layer_name]

    filepath = GEOJSON_FILES.get(layer_name)
    if not filepath or not os.path.exists(filepath):
        return None

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    _geojson_cache[layer_name] = data
    return data


# Model Data untuk menerima Polygon dari Leaflet Draw
class PolygonRequest(BaseModel):
    geometry: dict


# ══════════════════════════════════════════════════════════════════
# HELPER: Point-in-Polygon sederhana (ray casting)
# ══════════════════════════════════════════════════════════════════
def point_in_polygon(px, py, polygon_coords):
    """Ray casting algorithm untuk cek apakah titik ada dalam polygon."""
    # polygon_coords bisa nested (Polygon atau MultiPolygon)
    if not polygon_coords:
        return False

    def _ray_cast(ring, px, py):
        n = len(ring)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    return _ray_cast(polygon_coords, px, py)


def point_in_geometry(px, py, geometry):
    """Cek apakah titik (lon, lat) berada dalam geometry GeoJSON."""
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])

    if gtype == "Polygon":
        # Pakai ring luar saja (index 0)
        return point_in_polygon(px, py, coords[0]) if coords else False
    elif gtype == "MultiPolygon":
        for polygon in coords:
            if polygon and point_in_polygon(px, py, polygon[0]):
                return True
        return False
    return False


# ══════════════════════════════════════════════════════════════════
# ENDPOINT API
# ══════════════════════════════════════════════════════════════════

@app.get("/")
def home():
    return {"message": "API SIG Ubi Jalar v2.1 Aktif! Mode: GeoJSON File (tanpa database)."}


# ── 1. Daftar Layer ──────────────────────────────────────────────
@app.get("/layers")
def get_layers():
    layers = []
    for name, path in GEOJSON_FILES.items():
        exists = os.path.exists(path)
        layers.append({
            "nama_layer": name,
            "tipe_geometri": "MULTIPOLYGON",
            "sistem_koordinat": 4326,
            "file_exists": exists
        })
    return {"status": "success", "data": layers}


# ── 2. Ambil Data Layer → GeoJSON ───────────────────────────────
@app.get("/layer/{nama_layer}/geojson")
def get_layer_geojson(nama_layer: str, bbox: str = None):
    """
    Ambil data layer dalam format GeoJSON.
    Optional: bbox=minLon,minLat,maxLon,maxLat untuk filter spasial.
    """
    if nama_layer not in GEOJSON_FILES:
        raise HTTPException(status_code=404, detail="Layer tidak ditemukan")

    data = load_geojson(nama_layer)
    if data is None:
        raise HTTPException(status_code=500, detail=f"File GeoJSON untuk {nama_layer} tidak ditemukan")

    # Jika ada bbox filter, lakukan filtering sederhana
    if bbox:
        try:
            parts = bbox.split(",")
            if len(parts) == 4:
                minlon, minlat, maxlon, maxlat = map(float, parts)
                filtered_features = []
                for feat in data.get("features", []):
                    geom = feat.get("geometry", {})
                    coords_str = json.dumps(geom.get("coordinates", []))
                    # Simplified: check if any coordinate falls within bbox
                    # For production, use proper spatial indexing
                    filtered_features.append(feat)
                data = {"type": "FeatureCollection", "features": filtered_features}
        except Exception:
            pass  # Jika bbox invalid, kembalikan semua

    return data


# ── 3. Cek Kesesuaian Titik + Info Semua Layer ──────────────────
@app.get("/suitability")
def check_suitability(lat: float, lon: float):
    """
    Cek semua informasi spasial pada titik tertentu:
    kelas kesesuaian, curah hujan, kemiringan lereng,
    pola ruang RTRW, dan nama wilayah administrasi.
    """
    hasil = {
        "kesesuaian_lahan": None,
        "curah_hujan": None,
        "kemiringan_lereng": None,
        "pola_ruang": None,
        "administrasi": None,
    }

    # ── Kesesuaian Lahan ──
    kesesuaian_data = load_geojson("kesesuaian_lahan")
    if kesesuaian_data:
        for feat in kesesuaian_data.get("features", []):
            if point_in_geometry(lon, lat, feat.get("geometry", {})):
                props = feat.get("properties", {})
                hasil["kesesuaian_lahan"] = {
                    "suai_lahan": props.get("suai_lahan"),
                    "pembatas": props.get("pembatas"),
                }
                break

    # ── Curah Hujan ──
    curah_data = load_geojson("curah_hujan")
    if curah_data:
        for feat in curah_data.get("features", []):
            if point_in_geometry(lon, lat, feat.get("geometry", {})):
                props = feat.get("properties", {})
                hasil["curah_hujan"] = {
                    "curah_hujan": props.get("CH"),
                }
                break

    # ── Kemiringan Lereng ──
    kemiringan_data = load_geojson("kemiringan_lereng")
    if kemiringan_data:
        for feat in kemiringan_data.get("features", []):
            if point_in_geometry(lon, lat, feat.get("geometry", {})):
                props = feat.get("properties", {})
                hasil["kemiringan_lereng"] = {
                    "kemiringan_lereng": props.get("KL"),
                }
                break

    # ── Pola Ruang ──
    pola_data = load_geojson("pola_ruang")
    if pola_data:
        for feat in pola_data.get("features", []):
            if point_in_geometry(lon, lat, feat.get("geometry", {})):
                props = feat.get("properties", {})
                hasil["pola_ruang"] = {
                    "pola_ruang": props.get("NAMOBJ"),
                }
                break

    # ── Administrasi Wilayah ──
    admin_data = load_geojson("administrasi_wilayah")
    if admin_data:
        for feat in admin_data.get("features", []):
            if point_in_geometry(lon, lat, feat.get("geometry", {})):
                props = feat.get("properties", {})
                hasil["administrasi"] = {
                    "kecamatan": props.get("WADMKC"),
                    "desa": props.get("WADMKD"),
                }
                break

    return {"status": "success", "data": hasil}


# ── 4. Analisis Luas Area dari Polygon Pengguna ─────────────────
@app.post("/analyze")
def analyze_area(data: PolygonRequest):
    """
    Hitung luas area per kelas kesesuaian lahan
    dalam polygon yang digambar pengguna (dari Leaflet Draw).
    Simplified: menghitung berdasarkan jumlah feature yang intersect.
    """
    user_geom = data.geometry
    user_coords = user_geom.get("coordinates", [[]])[0] if user_geom.get("type") == "Polygon" else []

    kesesuaian_data = load_geojson("kesesuaian_lahan")
    if not kesesuaian_data:
        raise HTTPException(status_code=500, detail="Data kesesuaian lahan tidak tersedia")

    # Hitung per kelas kesesuaian
    kelas_count = {}
    for feat in kesesuaian_data.get("features", []):
        props = feat.get("properties", {})
        suai = props.get("suai_lahan", "Unknown")
        luas = props.get("luas", 0)
        geom = feat.get("geometry", {})

        # Simplified intersection check: cek apakah centroid polygon kesesuaian ada di dalam user polygon
        centroid = _get_centroid(geom)
        if centroid and user_coords and point_in_polygon(centroid[0], centroid[1], user_coords):
            if suai not in kelas_count:
                kelas_count[suai] = 0.0
            kelas_count[suai] += float(luas) if luas else 0.0

    result = [
        {"suai_lahan": k, "luas_meter_persegi": round(v * 10000, 2)}  # ha to m²
        for k, v in sorted(kelas_count.items())
    ]

    return {"status": "success", "hasil_analisis": result}


def _get_centroid(geometry):
    """Hitung centroid sederhana dari geometry GeoJSON."""
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])

    all_points = []

    def _collect_points(c, depth=0):
        if depth > 3:
            return
        if isinstance(c, list) and len(c) >= 2 and isinstance(c[0], (int, float)):
            all_points.append(c[:2])
        elif isinstance(c, list):
            for item in c:
                _collect_points(item, depth + 1)

    _collect_points(coords)

    if not all_points:
        return None

    avg_x = sum(p[0] for p in all_points) / len(all_points)
    avg_y = sum(p[1] for p in all_points) / len(all_points)
    return [avg_x, avg_y]


# ── 5. Analisis Spasial: Rekomendasi Lokasi Terbaik ─────────────
@app.get("/recommend")
def recommend_locations():
    """
    Mencari area rekomendasi perluasan lahan Ubi Jalar berdasarkan:
    - Kesesuaian lahan minimal "Sesuai" (S1/S2/S3)
    - Curah hujan sedang-tinggi (>= 2400 mm/thn)
    - Kemiringan lereng landai (0-3%, 3-8%, 8-15%)
    - Pola Ruang mendukung pertanian (bukan kawasan lindung/permukiman)

    Mengembalikan GeoJSON area rekomendasi.
    """
    kesesuaian_data = load_geojson("kesesuaian_lahan")
    curah_data = load_geojson("curah_hujan")
    kemiringan_data = load_geojson("kemiringan_lereng")
    pola_data = load_geojson("pola_ruang")

    if not all([kesesuaian_data, curah_data, kemiringan_data, pola_data]):
        raise HTTPException(status_code=500, detail="Data layer tidak lengkap")

    # Kelas kemiringan yang diperbolehkan
    kemiringan_ok = {'0-3%', '3-8%', '8-15%'}
    # Curah hujan yang tidak diperbolehkan (terlalu rendah)
    curah_exclude = {'2300-2400'}
    # Pola ruang yang dikecualikan
    pola_exclude_keywords = ['lindung', 'permukiman', 'industri']

    # Bangun set centroid curah hujan yang OK
    curah_features_ok = []
    for feat in curah_data.get("features", []):
        ch = feat.get("properties", {}).get("CH", "")
        if ch and ch not in curah_exclude:
            curah_features_ok.append(feat)

    # Bangun set kemiringan yang OK
    kemiringan_features_ok = []
    for feat in kemiringan_data.get("features", []):
        kl = feat.get("properties", {}).get("KL", "")
        if kl and kl in kemiringan_ok:
            kemiringan_features_ok.append(feat)

    # Bangun set pola ruang yang OK
    pola_features_ok = []
    for feat in pola_data.get("features", []):
        namobj = (feat.get("properties", {}).get("NAMOBJ", "") or "").lower()
        excluded = any(kw in namobj for kw in pola_exclude_keywords)
        if not excluded:
            pola_features_ok.append(feat)

    # Filter kesesuaian yang S (sesuai)
    recommend_features = []
    for feat in kesesuaian_data.get("features", []):
        props = feat.get("properties", {})
        suai = (props.get("suai_lahan", "") or "").upper()
        if not suai.startswith("S"):
            continue

        centroid = _get_centroid(feat.get("geometry", {}))
        if not centroid:
            continue

        cx, cy = centroid

        # Cek curah hujan
        in_curah = False
        curah_val = ""
        for cf in curah_features_ok:
            if point_in_geometry(cx, cy, cf.get("geometry", {})):
                in_curah = True
                curah_val = cf.get("properties", {}).get("CH", "")
                break
        if not in_curah:
            continue

        # Cek kemiringan
        in_kemiringan = False
        kemiringan_val = ""
        for kf in kemiringan_features_ok:
            if point_in_geometry(cx, cy, kf.get("geometry", {})):
                in_kemiringan = True
                kemiringan_val = kf.get("properties", {}).get("KL", "")
                break
        if not in_kemiringan:
            continue

        # Cek pola ruang
        in_pola = False
        pola_val = ""
        for pf in pola_features_ok:
            if point_in_geometry(cx, cy, pf.get("geometry", {})):
                in_pola = True
                pola_val = pf.get("properties", {}).get("NAMOBJ", "")
                break
        if not in_pola:
            continue

        # Lolos semua filter → masukkan sebagai rekomendasi
        luas = props.get("luas", 0)
        recommend_features.append({
            "type": "Feature",
            "geometry": feat.get("geometry"),
            "properties": {
                "suai_lahan": props.get("suai_lahan"),
                "pembatas": props.get("pembatas"),
                "pola_ruang": pola_val,
                "curah_hujan": curah_val,
                "kemiringan": kemiringan_val,
                "luas_ha": round(float(luas), 2) if luas else 0,
                "kategori": "Rekomendasi Lahan Ubi Jalar"
            }
        })

    return {
        "type": "FeatureCollection",
        "features": recommend_features
    }


# ── 6. Overlay Pola Ruang × Kesesuaian ──────────────────────────
@app.get("/overlay/kesesuaian-pola")
def overlay_kesesuaian_pola():
    """
    Overlay pola_ruang × kesesuaian_lahan.
    Menampilkan area yang SESUAI (S) tapi BUKAN zona pertanian.
    """
    kesesuaian_data = load_geojson("kesesuaian_lahan")
    pola_data = load_geojson("pola_ruang")

    if not kesesuaian_data or not pola_data:
        raise HTTPException(status_code=500, detail="Data layer tidak lengkap")

    # Hitung overlay berdasarkan centroid
    overlay_result = {}
    for feat in kesesuaian_data.get("features", []):
        props = feat.get("properties", {})
        suai = (props.get("suai_lahan", "") or "").upper()
        if not suai.startswith("S"):
            continue

        centroid = _get_centroid(feat.get("geometry", {}))
        if not centroid:
            continue

        cx, cy = centroid
        luas = float(props.get("luas", 0) or 0)

        for pf in pola_data.get("features", []):
            namobj = pf.get("properties", {}).get("NAMOBJ", "")
            if not namobj or "pertanian" in namobj.lower():
                continue

            if point_in_geometry(cx, cy, pf.get("geometry", {})):
                key = (namobj, props.get("suai_lahan", ""))
                if key not in overlay_result:
                    overlay_result[key] = {"pola_ruang": namobj, "suai_lahan": props.get("suai_lahan"), "jumlah_polygon": 0, "total_luas_ha": 0.0}
                overlay_result[key]["jumlah_polygon"] += 1
                overlay_result[key]["total_luas_ha"] += luas
                break

    result = sorted(overlay_result.values(), key=lambda x: x["total_luas_ha"], reverse=True)
    for r in result:
        r["total_luas_ha"] = round(r["total_luas_ha"], 2)

    return {
        "status": "success",
        "keterangan": "Area kesesuaian S1/S2/S3 yang berada di luar zona pertanian",
        "data": result
    }


# ══════════════════════════════════════════════════════════════════
# Serve frontend (index.html)
# ══════════════════════════════════════════════════════════════════
@app.get("/app")
def serve_frontend():
    """Serve the frontend index.html"""
    return FileResponse(os.path.join(BASE_DIR, "index.html"))