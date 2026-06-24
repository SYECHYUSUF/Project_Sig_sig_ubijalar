from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import json
import os

# Inisialisasi Aplikasi FastAPI
app = FastAPI(
    title="API SIG Kesesuaian Lahan Ubi Jalar",
    description="Backend API untuk analisis spasial lahan pertanian — SIG Semester 4 Universitas Hasanuddin\n\n"
                "Mode: GeoJSON File (tanpa database)",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# KONFIGURASI DATABASE
# ==========================================
DB_HOST = "localhost"
DB_NAME = "sig_ubijalar"
DB_USER = "postgres"
DB_PASS = ""

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS
        )
        return conn
    except Exception as e:
        print(f"Gagal koneksi ke database: {e}")
        return None

# Model Data untuk menerima Polygon dari Leaflet Draw
class PolygonRequest(BaseModel):
    geometry: dict

# ==========================================
# ENDPOINT API
# ==========================================

@app.get("/")
def home():
    return {"message": "API SIG Ubi Jalar v2.1 Aktif! Mode: GeoJSON File (tanpa database)."}


# ── 2. Daftar Layer ──────────────────────────────────────────────
@app.get("/layers")
def get_layers():
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")

    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT f_table_name AS nama_layer, type AS tipe_geometri, srid AS sistem_koordinat
            FROM geometry_columns
            WHERE f_table_schema = 'public';
        """)
        layers = cur.fetchall()
        return {"status": "success", "data": layers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ── 3. GeoJSON Layer ─────────────────────────────────────────────
@app.get("/layer/{nama_layer}/geojson")
def get_layer_geojson(nama_layer: str, bbox: str = None):
    """
    Ambil data layer dalam format GeoJSON.
    Optional: bbox=minLon,minLat,maxLon,maxLat untuk filter spasial.
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")

    tabel_valid = [
        "administrasi_wilayah", "curah_hujan", "kemiringan_lereng",
        "pola_ruang", "kesesuaian_lahan"
    ]
    if nama_layer not in tabel_valid:
        raise HTTPException(status_code=404, detail="Layer tidak ditemukan")

    cur = conn.cursor()
    try:
        # Filter bbox opsional
        bbox_clause = ""
        if bbox:
            parts = bbox.split(",")
            if len(parts) == 4:
                minlon, minlat, maxlon, maxlat = parts
                bbox_clause = f"""
                WHERE wkb_geometry && ST_MakeEnvelope(
                    {minlon}, {minlat}, {maxlon}, {maxlat}, 4326
                )"""

        query = f"""
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(feature), '[]'::jsonb)
        ) FROM (
            SELECT jsonb_build_object(
                'type', 'Feature',
                'id', ogc_fid,
                'geometry', ST_AsGeoJSON(wkb_geometry)::jsonb,
                'properties', to_jsonb(t) - 'wkb_geometry'
            ) AS feature
            FROM public.{nama_layer} AS t
            {bbox_clause}
        ) AS subquery;
        """
        cur.execute(query)
        result = cur.fetchone()[0]

        if not result or not result.get('features'):
            return {"type": "FeatureCollection", "features": []}

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ── 4. Suitability Point Check (join 5 tabel) ────────────────────
@app.get("/suitability")
def check_suitability(lat: float, lon: float):
    """
    Cek semua informasi spasial pada koordinat klik:
    kelas kesesuaian, curah hujan, kemiringan lereng,
    pola ruang RTRW, dan nama wilayah administrasi.
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")

    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        point_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"

        # ── Kesesuaian Lahan ──
        cur.execute(f"""
            SELECT suai_lahan, pembatas
            FROM kesesuaian_lahan
            WHERE ST_Intersects(wkb_geometry, {point_sql})
            LIMIT 1;
        """, (lon, lat))
        kesesuaian = cur.fetchone()

        # ── Curah Hujan ──
        cur.execute(f"""
            SELECT "CH" AS curah_hujan
            FROM curah_hujan
            WHERE ST_Intersects(wkb_geometry, {point_sql})
            LIMIT 1;
        """, (lon, lat))
        curah = cur.fetchone()

        # ── Kemiringan Lereng ──
        cur.execute(f"""
            SELECT "KL" AS kemiringan_lereng
            FROM kemiringan_lereng
            WHERE ST_Intersects(wkb_geometry, {point_sql})
            LIMIT 1;
        """, (lon, lat))
        kemiringan = cur.fetchone()

        # ── Pola Ruang ──
        cur.execute(f"""
            SELECT "NAMOBJ" AS pola_ruang
            FROM pola_ruang
            WHERE ST_Intersects(wkb_geometry, {point_sql})
            LIMIT 1;
        """, (lon, lat))
        pola = cur.fetchone()

        # ── Administrasi Wilayah ──
        cur.execute(f"""
            SELECT "WADMKC" AS kecamatan, "WADMKD" AS desa
            FROM administrasi_wilayah
            WHERE ST_Intersects(wkb_geometry, {point_sql})
            LIMIT 1;
        """, (lon, lat))
        admin = cur.fetchone()

        # Gabungkan semua hasil
        hasil = {
            "kesesuaian_lahan": dict(kesesuaian) if kesesuaian else None,
            "curah_hujan":      dict(curah)      if curah      else None,
            "kemiringan_lereng": dict(kemiringan) if kemiringan else None,
            "pola_ruang":       dict(pola)        if pola       else None,
            "administrasi":     dict(admin)        if admin      else None,
        }

        return {"status": "success", "data": hasil}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ── 5. Analyze Area (POST) ───────────────────────────────────────
@app.post("/analyze")
def analyze_area(data: PolygonRequest):
    """
    Hitung luas area per kelas kesesuaian lahan
    dalam polygon yang digambar pengguna (dari Leaflet Draw).
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")

    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        geom_json = json.dumps(data.geometry)

        query = """
        WITH user_geom AS (
            SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) AS geom
        )
        SELECT
            k.suai_lahan,
            SUM(ST_Area(ST_Intersection(k.wkb_geometry, u.geom)::geography)) AS luas_meter_persegi
        FROM kesesuaian_lahan k, user_geom u
        WHERE ST_Intersects(k.wkb_geometry, u.geom)
        GROUP BY k.suai_lahan
        ORDER BY k.suai_lahan;
        """
        cur.execute(query, (geom_json,))
        result = cur.fetchall()

        return {"status": "success", "hasil_analisis": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ── 6. Recommend — Analisis Spasial Wajib ────────────────────────
@app.get("/recommend")
def recommend_locations():
    """
    ANALISIS SPASIAL WAJIB (Tahap 4):
    Mencari area rekomendasi perluasan lahan Ubi Jalar berdasarkan:
    - Kesesuaian lahan minimal "Sesuai" (S1/S2/S3)
    - Curah hujan sedang-tinggi (>= 2400 mm/thn, yaitu bukan kelas terendah)
    - Kemiringan lereng landai (kelas 0-3%, 3-8%, atau 8-15%)
    - Pola Ruang mendukung pertanian (bukan kawasan lindung/permukiman)

    Mengembalikan GeoJSON area rekomendasi + luas per area.
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")

    cur = conn.cursor()
    try:
        query = """
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(feature), '[]'::jsonb)
        )
        FROM (
            SELECT jsonb_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(
                    ST_Intersection(k.wkb_geometry, p.wkb_geometry)
                )::jsonb,
                'properties', jsonb_build_object(
                    'suai_lahan',   k.suai_lahan,
                    'pembatas',     k.pembatas,
                    'pola_ruang',   p."NAMOBJ",
                    'curah_hujan',  c."CH",
                    'kemiringan',   m."KL",
                    'luas_ha',      ROUND(
                        (ST_Area(
                            ST_Intersection(
                                ST_Intersection(k.wkb_geometry, p.wkb_geometry),
                                ST_Intersection(c.wkb_geometry, m.wkb_geometry)
                            )::geography
                        ) / 10000.0)::numeric, 2
                    ),
                    'kategori', 'Rekomendasi Lahan Ubi Jalar'
                )
            ) AS feature
            FROM kesesuaian_lahan k
            JOIN pola_ruang p
                ON ST_Intersects(k.wkb_geometry, p.wkb_geometry)
                AND p."NAMOBJ" NOT ILIKE '%lindung%'
                AND p."NAMOBJ" NOT ILIKE '%permukiman%'
                AND p."NAMOBJ" NOT ILIKE '%industri%'
            JOIN curah_hujan c
                ON ST_Intersects(k.wkb_geometry, c.wkb_geometry)
                AND c."CH" NOT IN ('2300-2400')
            JOIN kemiringan_lereng m
                ON ST_Intersects(k.wkb_geometry, m.wkb_geometry)
                AND m."KL" IN ('0-3%', '3-8%', '8-15%')
            WHERE k.suai_lahan ILIKE 'S%'
              AND ST_IsValid(k.wkb_geometry)
              AND ST_IsValid(p.wkb_geometry)
        ) AS subquery
        WHERE feature IS NOT NULL;
        """
        cur.execute(query)
        result = cur.fetchone()

        if not result or not result[0]:
            return {"type": "FeatureCollection", "features": []}

        return result[0]

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ── 6. Overlay Pola Ruang × Kesesuaian (Analisis Wajib) ─────────
@app.get("/overlay/kesesuaian-pola")
def overlay_kesesuaian_pola():
    """
    Overlay pola_ruang × kesesuaian_lahan.
    Menampilkan area yang SESUAI (S) tapi BUKAN zona pertanian
    sebagai area potensi konversi / temuan analisis.
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")

    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
        SELECT
            p."NAMOBJ"                            AS pola_ruang,
            k.suai_lahan,
            COUNT(*)                              AS jumlah_polygon,
            ROUND(SUM(ST_Area(
                ST_Intersection(k.wkb_geometry, p.wkb_geometry)::geography
            ) / 10000.0)::numeric, 2)             AS total_luas_ha
        FROM kesesuaian_lahan k
        JOIN pola_ruang p
            ON ST_Intersects(k.wkb_geometry, p.wkb_geometry)
        WHERE k.suai_lahan ILIKE 'S%'
          AND p."NAMOBJ" NOT ILIKE '%pertanian%'
        GROUP BY p."NAMOBJ", k.suai_lahan
        ORDER BY total_luas_ha DESC;
        """
        cur.execute(query)
        result = cur.fetchall()
        return {
            "status": "success",
            "keterangan": "Area kesesuaian S1/S2/S3 yang berada di luar zona pertanian",
            "data": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()