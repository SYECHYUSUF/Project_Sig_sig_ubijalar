from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
import json

app = FastAPI(
    title="API SIG Kesesuaian Lahan Ubi Jalar",
    description="Backend API untuk analisis spasial lahan pertanian — SIG Semester 4 Universitas Hasanuddin",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_HOST = "localhost"
DB_NAME = "sig_ubijalar"
DB_USER = "postgres"
DB_PASS = ""

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS
        )
        return conn
    except Exception as e:
        print(f"Gagal koneksi ke database: {e}")
        return None

class PolygonRequest(BaseModel):
    geometry: dict

# ── Kolom yang sudah diverifikasi (semua lowercase) ──
# administrasi_wilayah : wadmkc, wadmkd
# curah_hujan          : ch
# kemiringan_lereng    : kl
# pola_ruang           : namobj
# kesesuaian_lahan     : suai_lahan, pembatas


# ── 1. Home ──────────────────────────────────────────────────────
@app.get("/")
def home():
    return {"message": "API SIG Ubi Jalar v2.0 Aktif! PostGIS terhubung."}


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
            FROM geometry_columns WHERE f_table_schema = 'public';
        """)
        return {"status": "success", "data": cur.fetchall()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close(); conn.close()


# ── 3. GeoJSON Layer ─────────────────────────────────────────────
@app.get("/layer/{nama_layer}/geojson")
def get_layer_geojson(nama_layer: str, bbox: str = None):
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")

    tabel_valid = ["administrasi_wilayah", "curah_hujan", "kemiringan_lereng",
                   "pola_ruang", "kesesuaian_lahan"]
    if nama_layer not in tabel_valid:
        raise HTTPException(status_code=404, detail="Layer tidak ditemukan")

    cur = conn.cursor()
    try:
        bbox_clause = ""
        if bbox:
            parts = bbox.split(",")
            if len(parts) == 4:
                minlon, minlat, maxlon, maxlat = parts
                bbox_clause = f"""
                WHERE wkb_geometry && ST_MakeEnvelope(
                    {float(minlon)}, {float(minlat)}, {float(maxlon)}, {float(maxlat)}, 4326
                )"""

        # Kolom biner kesesuaian_lahan yang tidak perlu ditampilkan
        KOLOM_BINER_KESESUAIAN = [
            'batuan_di_permukaan', 'c_organik', 'drainase', 'ktk_liat',
            'kedalaman_tanah', 'kemiringan_lereng', 'ph', 'salinitas', 'tekstur',
            'id', 'ogc_fid'
        ]

        if nama_layer == 'kesesuaian_lahan':
            # Hanya ambil kolom yang bermakna: suai_lahan, pembatas, luas
            exclude_expr = " - ".join(
                ["to_jsonb(t) - 'wkb_geometry'"] +
                [f"'{k}'" for k in KOLOM_BINER_KESESUAIAN]
            )
            query = f"""
            SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'features', COALESCE(jsonb_agg(feature), '[]'::jsonb)
            ) FROM (
                SELECT jsonb_build_object(
                    'type', 'Feature',
                    'id', ogc_fid,
                    'geometry', ST_AsGeoJSON(wkb_geometry)::jsonb,
                    'properties', {exclude_expr}
                ) AS feature
                FROM public.{nama_layer} AS t
                {bbox_clause}
            ) AS subquery;
            """
        else:
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
        cur.close(); conn.close()


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
        pt = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"

        # Kesesuaian Lahan
        cur.execute(f"""
            SELECT suai_lahan, pembatas FROM kesesuaian_lahan
            WHERE ST_Intersects(wkb_geometry, {pt}) LIMIT 1;
        """, (lon, lat))
        kesesuaian = cur.fetchone()

        # Curah Hujan — kolom lowercase: ch
        cur.execute(f"""
            SELECT ch AS curah_hujan FROM curah_hujan
            WHERE ST_Intersects(wkb_geometry, {pt}) LIMIT 1;
        """, (lon, lat))
        curah = cur.fetchone()

        # Kemiringan Lereng — kolom lowercase: kl
        cur.execute(f"""
            SELECT kl AS kemiringan_lereng FROM kemiringan_lereng
            WHERE ST_Intersects(wkb_geometry, {pt}) LIMIT 1;
        """, (lon, lat))
        kemiringan = cur.fetchone()

        # Pola Ruang — kolom lowercase: namobj
        cur.execute(f"""
            SELECT namobj AS pola_ruang FROM pola_ruang
            WHERE ST_Intersects(wkb_geometry, {pt}) LIMIT 1;
        """, (lon, lat))
        pola = cur.fetchone()

        # Administrasi Wilayah — kolom lowercase: wadmkc, wadmkd
        cur.execute(f"""
            SELECT wadmkc AS kecamatan, wadmkd AS desa FROM administrasi_wilayah
            WHERE ST_Intersects(wkb_geometry, {pt}) LIMIT 1;
        """, (lon, lat))
        admin = cur.fetchone()

        return {
            "status": "success",
            "data": {
                "kesesuaian_lahan":  dict(kesesuaian) if kesesuaian else None,
                "curah_hujan":       dict(curah)      if curah      else None,
                "kemiringan_lereng": dict(kemiringan) if kemiringan else None,
                "pola_ruang":        dict(pola)        if pola       else None,
                "administrasi":      dict(admin)        if admin      else None,
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close(); conn.close()


# ── 5. Analyze Area (POST) ───────────────────────────────────────
@app.post("/analyze")
def analyze_area(data: PolygonRequest):
    """
    Hitung luas area per kelas kesesuaian lahan dalam polygon
    yang digambar pengguna (dari Leaflet Draw).
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
        return {"status": "success", "hasil_analisis": cur.fetchall()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close(); conn.close()


# ── 6. Recommend — Analisis Spasial Wajib ────────────────────────
@app.get("/recommend")
def recommend_locations():
    """
    ANALISIS SPASIAL WAJIB (Tahap 4):
    Area rekomendasi perluasan lahan Ubi Jalar:
    - Kesesuaian: S1/S2/S3 (minimal Sesuai)
    - Curah hujan sedang-tinggi: ch IN ('2500-2600', '2600-2700', '2700-2800')
    - Kemiringan lereng < 15%: kl IN ('0-3%','3-8%','8-15%')
    - Pola Ruang mendukung pertanian: namobj IN ('Kawasan Ketahanan Pangan', 'Kawasan Hortikultura', 'Kawasan Perkebunan')

    Kolom terverifikasi lowercase: ch, kl, namobj
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Kita pakai pendekatan sequential join yang lebih aman
        # Daripada multi-JOIN sekaligus yang bisa timeout,
        # kita JOIN bertahap menggunakan ST_Intersects
        query = """
        SELECT
            k.suai_lahan,
            k.pembatas,
            p.namobj                                                        AS pola_ruang,
            c.ch                                                            AS curah_hujan,
            m.kl                                                            AS kemiringan,
            ROUND(
                (ST_Area(k.wkb_geometry::geography) / 10000.0)::numeric, 2
            )                                                               AS luas_ha,
            ST_AsGeoJSON(k.wkb_geometry)::jsonb                            AS geometry
        FROM kesesuaian_lahan k
        JOIN pola_ruang p
            ON ST_Intersects(k.wkb_geometry, p.wkb_geometry)
        JOIN curah_hujan c
            ON ST_Intersects(k.wkb_geometry, c.wkb_geometry)
        JOIN kemiringan_lereng m
            ON ST_Intersects(k.wkb_geometry, m.wkb_geometry)
        WHERE
            k.suai_lahan ILIKE 'S%'
            AND c.ch IN ('2500-2600', '2600-2700', '2700-2800')
            AND m.kl IN ('0-3%', '3-8%', '8-15%')
            AND p.namobj IN ('Kawasan Ketahanan Pangan', 'Kawasan Hortikultura', 'Kawasan Perkebunan')
        LIMIT 200;
        """
        cur.execute(query)
        rows = cur.fetchall()

        features = []
        for row in rows:
            geom = row.pop("geometry", None)
            if geom:
                features.append({
                    "type": "Feature",
                    "geometry": geom,
                    "properties": dict(row)
                })

        return {"type": "FeatureCollection", "features": features}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close(); conn.close()


# ── 7. Overlay Pola Ruang × Kesesuaian ──────────────────────────
@app.get("/overlay/kesesuaian-pola")
def overlay_kesesuaian_pola():
    """
    ANALISIS SPASIAL WAJIB (Tahap 4):
    Overlay pola_ruang × kesesuaian_lahan.
    Area SESUAI (S%) yang berada di luar zona pertanian.
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database terputus")
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
        SELECT
            p.namobj                                                          AS pola_ruang,
            k.suai_lahan,
            COUNT(*)                                                          AS jumlah_polygon,
            ROUND(SUM(
                ST_Area(ST_Intersection(k.wkb_geometry, p.wkb_geometry)::geography)
                / 10000.0
            )::numeric, 2)                                                    AS total_luas_ha
        FROM kesesuaian_lahan k
        JOIN pola_ruang p ON ST_Intersects(k.wkb_geometry, p.wkb_geometry)
        WHERE
            k.suai_lahan ILIKE 'S%'
            AND p.namobj NOT IN ('Kawasan Ketahanan Pangan', 'Kawasan Hortikultura', 'Kawasan Perkebunan')
        GROUP BY p.namobj, k.suai_lahan
        ORDER BY total_luas_ha DESC;
        """
        cur.execute(query)
        return {
            "status": "success",
            "keterangan": "Area kesesuaian S1/S2/S3 di luar zona pertanian",
            "data": cur.fetchall()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close(); conn.close()