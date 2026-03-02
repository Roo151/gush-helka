import math
import logging
import ssl

import urllib3
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, jsonify, render_template, request

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

WFS_BASE    = "https://open.govmap.gov.il/geoserver/opendata/wfs"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
# IPLAN ArcGIS – gvulot_retzef layers:
#   Layer 2 = ועדים מקומיים   (Vaad_Heb)
#   Layer 3 = מרחבי תכנון     (MT_Heb)  ← used as planning committee
IPLAN_BASE  = "https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/gvulot_retzef/MapServer"


# ── IPLAN uses older TLS — requires a permissive SSL adapter ─────────────────

class _LegacyTLSAdapter(HTTPAdapter):
    """Allow connections to servers with older TLS configurations."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


_iplan_session = requests.Session()
_iplan_session.mount("https://", _LegacyTLSAdapter())


# ── coordinate helpers ────────────────────────────────────────────────────────

def wgs84_to_mercator(lon, lat):
    """WGS84 → Web Mercator (EPSG:3857)."""
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    y = y * 20037508.34 / 180
    return x, y


def _centroid(geometry):
    """Return (x, y) centroid of a GeoJSON Polygon or MultiPolygon."""
    try:
        coords = geometry["coordinates"]
        ring = coords[0][0] if geometry["type"] == "MultiPolygon" else coords[0]
        x = sum(c[0] for c in ring) / len(ring)
        y = sum(c[1] for c in ring) / len(ring)
        return x, y
    except Exception:
        return None


# ── WFS (GovMap open data) ───────────────────────────────────────────────────

def query_wfs(params):
    """GET request to GovMap WFS; returns parsed JSON or None."""
    try:
        resp = requests.get(WFS_BASE, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("WFS query failed: %s", exc)
        return None


def get_municipality_info(locality_id):
    """Return {name, type, district, subdistrict} from muni_il WFS layer."""
    if not locality_id:
        return None
    params = {
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": "opendata:muni_il", "outputFormat": "application/json",
        "CQL_FILTER": f"CR_LAMAS='{locality_id}'", "maxFeatures": 1,
    }
    data = query_wfs(params)
    if data and data.get("features"):
        p = data["features"][0]["properties"]
        return {
            "name":        (p.get("Muni_Heb")   or "").strip(),
            "type":        (p.get("Sug_Muni")   or "").strip(),
            "district":    (p.get("Machoz")     or "").strip(),
            "subdistrict": (p.get("FIRST_Nafa") or "").strip(),
        }
    return None


# ── IPLAN ArcGIS (מרחב תכנון) ────────────────────────────────────────────────

def get_planning_zone(x, y, in_sr=3857):
    """
    Spatial query against IPLAN Layer 3 (מרחבי תכנון).
    Returns (mt_heb, mt_eng, sug_mt) or (None, None, None) on failure.
    """
    url = f"{IPLAN_BASE}/3/query"
    params = {
        "geometry":     f"{x},{y}",
        "geometryType": "esriGeometryPoint",
        "spatialRel":   "esriSpatialRelIntersects",
        "inSR":         in_sr,
        "outFields":    "MT_Heb,MT_Eng,CodeMT,Sug_MT,Machoz",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = _iplan_session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        features = resp.json().get("features") or []
        if features:
            a = features[0]["attributes"]
            return (
                (a.get("MT_Heb") or "").strip(),
                (a.get("MT_Eng") or "").strip(),
                (a.get("Sug_MT") or "").strip(),
            )
    except Exception as exc:
        logger.warning("IPLAN planning-zone query failed: %s", exc)
    return None, None, None


# ── result builder ────────────────────────────────────────────────────────────

def build_result(feature):
    """
    Build the full response dict from a WFS PARCEL_ALL feature.
    Queries IPLAN for the real מרחב תכנון name; falls back to muni_il.
    """
    props    = feature["properties"]
    geometry = feature.get("geometry")

    locality_id   = props.get("LOCALITY_I")
    locality_name = (props.get("LOCALITY_N") or "").strip()

    # ── muni_il for settlement type / district ──────────────────────────────
    muni = get_municipality_info(locality_id)

    # ── IPLAN מרחב תכנון ────────────────────────────────────────────────────
    mt_heb = mt_eng = sug_mt = None
    if geometry:
        centroid = _centroid(geometry)
        if centroid:
            mt_heb, mt_eng, sug_mt = get_planning_zone(centroid[0], centroid[1], in_sr=3857)

    # Fallback: derive from muni name if IPLAN unavailable
    if not mt_heb:
        muni_name = muni["name"] if muni else locality_name
        mt_heb = f"ועדה מקומית לתכנון ובניה {muni_name}" if muni_name else None

    return {
        "gush":              props.get("GUSH_NUM"),
        "helka":             props.get("PARCEL"),
        "locality":          locality_name,
        "county":            (props.get("COUNTY_NAM") or "").strip(),
        "region":            (props.get("REGION_NAM") or "").strip(),
        "status":            (props.get("STATUS_TEX") or "").strip(),
        "area":              props.get("LEGAL_AREA"),
        # מרחב תכנון (= ועדה מקומית)
        "planning_zone":     mt_heb,
        "planning_zone_eng": mt_eng,
        "planning_zone_type": sug_mt,
        # רשות מקומית from muni_il
        "municipality":      muni["name"]        if muni else "",
        "municipality_type": muni["type"]        if muni else "",
        "district":          muni["district"]    if muni else "",
        "subdistrict":       muni["subdistrict"] if muni else "",
    }


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/lookup-parcel")
def lookup_parcel():
    """GET /api/lookup-parcel?gush=<n>&helka=<n>"""
    gush  = request.args.get("gush",  "").strip()
    helka = request.args.get("helka", "").strip()

    if not gush or not helka:
        return jsonify({"error": "חסר מספר גוש או חלקה"}), 400
    try:
        gush_int, helka_int = int(gush), int(helka)
    except ValueError:
        return jsonify({"error": "מספר גוש וחלקה חייבים להיות מספרים שלמים"}), 400

    params = {
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": "opendata:PARCEL_ALL", "outputFormat": "application/json",
        "CQL_FILTER": f"GUSH_NUM={gush_int} AND PARCEL={helka_int}",
        "maxFeatures": 1,
    }
    data = query_wfs(params)
    if data is None:
        return jsonify({"error": "שגיאה בגישה לשרת הנתונים. נסי שוב."}), 502

    features = data.get("features") or []
    if not features:
        return jsonify({"error": f"לא נמצאה חלקה עבור גוש {gush} חלקה {helka}"}), 404

    return jsonify(build_result(features[0]))


@app.route("/api/lookup-address")
def lookup_address():
    """GET /api/lookup-address?address=<text>"""
    address = request.args.get("address", "").strip()
    if not address:
        return jsonify({"error": "יש להזין כתובת"}), 400

    # 1. Geocode
    try:
        geo_resp = requests.get(
            NOMINATIM_BASE,
            params={"q": f"{address}, ישראל", "format": "json",
                    "limit": 1, "accept-language": "he,en", "countrycodes": "il"},
            headers={"User-Agent": "GushHelkaApp/1.0 (open-source-israel-parcel-lookup)"},
            timeout=12,
        )
        geo_resp.raise_for_status()
        geo_results = geo_resp.json()
    except Exception as exc:
        return jsonify({"error": f"שגיאה בחיפוש הכתובת: {exc}"}), 502

    if not geo_results:
        return jsonify({"error": f"לא נמצאה כתובת: {address}"}), 404

    lat = float(geo_results[0]["lat"])
    lon = float(geo_results[0]["lon"])
    display_address = geo_results[0].get("display_name", "")

    # 2. Find parcel via spatial WFS query
    x, y = wgs84_to_mercator(lon, lat)
    params = {
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": "opendata:PARCEL_ALL", "outputFormat": "application/json",
        "CQL_FILTER": f"INTERSECTS(the_geom,POINT({x} {y}))",
        "maxFeatures": 1,
    }
    data = query_wfs(params)
    if data is None:
        return jsonify({"error": "שגיאה בחיפוש חלקה לפי מיקום. נסי שוב."}), 502

    features = data.get("features") or []
    if not features:
        return jsonify({
            "error": "הכתובת אותרה אך לא נמצאה חלקה רשומה. נסי להיות יותר ספציפית.",
            "geocoded_address": display_address,
        }), 404

    result = build_result(features[0])
    result["geocoded_address"] = display_address
    return jsonify(result)


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
