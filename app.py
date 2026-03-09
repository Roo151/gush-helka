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

WFS_BASE       = "https://open.govmap.gov.il/geoserver/opendata/wfs"
GOVMAP_GEOCODE = "https://wms.govmap.gov.il/tachles/addresssearch.aspx"
IPLAN_BASE     = "https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/gvulot_retzef/MapServer"


# ── IPLAN uses older TLS ─────────────────────────────────────────────────────

class _LegacyTLSAdapter(HTTPAdapter):
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

def itm_to_wgs84(x, y):
    """Convert Israeli ITM (EPSG:2039) to WGS84 lon/lat (approximate)."""
    # ITM false easting/northing and scale
    a, f = 6378137.0, 1 / 298.257223563
    b = a * (1 - f)
    e2 = 1 - (b / a) ** 2
    e = math.sqrt(e2)

    k0 = 1.0000067
    lon0 = math.radians(35.2045169444444)
    lat0 = math.radians(31.7343936111111)
    E0, N0 = 219529.584, 626907.390

    X = x - E0
    Y = y - N0

    M0 = a * ((1 - e2/4 - 3*e2**2/64) * lat0
              - (3*e2/8 + 3*e2**2/32) * math.sin(2*lat0)
              + (15*e2**2/256) * math.sin(4*lat0))
    M = M0 + Y / k0
    mu = M / (a * (1 - e2/4 - 3*e2**2/64))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    lat1 = (mu + (3*e1/2 - 27*e1**3/32) * math.sin(2*mu)
            + (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu)
            + (151*e1**3/96) * math.sin(6*mu))
    N1 = a / math.sqrt(1 - e2 * math.sin(lat1)**2)
    T1 = math.tan(lat1)**2
    C1 = e2 / (1 - e2) * math.cos(lat1)**2
    R1 = a * (1 - e2) / (1 - e2 * math.sin(lat1)**2)**1.5
    D = X / (N1 * k0)

    lat = lat1 - (N1 * math.tan(lat1) / R1) * (
        D**2/2 - (5 + 3*T1 + 10*C1 - 4*C1**2 - 9*e2/(1-e2)) * D**4/24)
    lon = lon0 + (D - (1 + 2*T1 + C1) * D**3/6) / math.cos(lat1)

    return math.degrees(lon), math.degrees(lat)


def wgs84_to_mercator(lon, lat):
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    y = y * 20037508.34 / 180
    return x, y


def _centroid(geometry):
    try:
        coords = geometry["coordinates"]
        ring = coords[0][0] if geometry["type"] == "MultiPolygon" else coords[0]
        x = sum(c[0] for c in ring) / len(ring)
        y = sum(c[1] for c in ring) / len(ring)
        return x, y
    except Exception:
        return None


# ── WFS helpers ───────────────────────────────────────────────────────────────

def query_wfs(params):
    try:
        resp = requests.get(WFS_BASE, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("WFS query failed: %s", exc)
        return None


def get_municipality_by_coords(x, y):
    """
    Spatial query on muni_il — returns the correct municipality for any point.
    Much more reliable than the LOCALITY_I code stored in the parcel layer.
    """
    params = {
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": "opendata:muni_il", "outputFormat": "application/json",
        "CQL_FILTER": f"INTERSECTS(the_geom,SRID=3857;POINT({x} {y}))",
        "maxFeatures": 1,
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


# ── IPLAN ArcGIS ──────────────────────────────────────────────────────────────

def get_planning_zone(x, y, in_sr=3857):
    """Layer 3 = מרחבי תכנון → returns (mt_heb, mt_eng, sug_mt)."""
    url = f"{IPLAN_BASE}/3/query"
    params = {
        "geometry":       f"{x},{y}",
        "geometryType":   "esriGeometryPoint",
        "spatialRel":     "esriSpatialRelIntersects",
        "inSR":           in_sr,
        "outFields":      "MT_Heb,MT_Eng,CodeMT,Sug_MT",
        "returnGeometry": "false",
        "f":              "json",
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
        logger.warning("IPLAN query failed: %s", exc)
    return None, None, None


# ── result builder ────────────────────────────────────────────────────────────

def build_result(feature):
    props    = feature["properties"]
    geometry = feature.get("geometry")
    centroid = _centroid(geometry) if geometry else None

    # Spatial municipality query (accurate — not dependent on LOCALITY_I code)
    muni = get_municipality_by_coords(*centroid) if centroid else None

    # IPLAN planning zone
    mt_heb = mt_eng = sug_mt = None
    if centroid:
        mt_heb, mt_eng, sug_mt = get_planning_zone(centroid[0], centroid[1])

    # Fallback committee name from muni
    if not mt_heb and muni:
        mt_heb = muni["name"]

    return {
        "gush":               props.get("GUSH_NUM"),
        "helka":              props.get("PARCEL"),
        # שם רשות מחושב מ-muni_il בשאילתה גיאוגרפית
        "locality":           muni["name"]        if muni else (props.get("LOCALITY_N") or "").strip(),
        "county":             (props.get("COUNTY_NAM") or "").strip(),
        "region":             (props.get("REGION_NAM") or "").strip(),
        "status":             (props.get("STATUS_TEX") or "").strip(),
        "area":               props.get("LEGAL_AREA"),
        # מרחב תכנון מ-IPLAN
        "planning_zone":      mt_heb,
        "planning_zone_eng":  mt_eng,
        "planning_zone_type": sug_mt,
        # רשות מקומית מ-muni_il
        "municipality":       muni["name"]        if muni else "",
        "municipality_type":  muni["type"]        if muni else "",
        "district":           muni["district"]    if muni else "",
        "subdistrict":        muni["subdistrict"] if muni else "",
    }


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/lookup-parcel")
def lookup_parcel():
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


@app.route("/api/geocode")
def geocode():
    """
    GET /api/geocode?street=<street+number>&city=<city>
    Returns up to 5 address candidates from Nominatim.
    """
    street = request.args.get("street", "").strip()
    city   = request.args.get("city",   "").strip()

    if not street and not city:
        return jsonify({"error": "יש להזין רחוב או עיר"}), 400

    query = " ".join(filter(None, [street, city]))

    try:
        resp = requests.get(
            GOVMAP_GEOCODE,
            params={"keys": query, "lang": 0, "type": 1},
            headers={"Referer": "https://www.govmap.gov.il/"},
            timeout=12,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as exc:
        return jsonify({"error": f"שגיאה בחיפוש הכתובת: {exc}"}), 502

    if not results:
        return jsonify({"error": f"לא נמצאה כתובת: {query}"}), 404

    candidates = []
    for r in results[:5]:
        try:
            itm_x = float(r.get("X") or r.get("x") or 0)
            itm_y = float(r.get("Y") or r.get("y") or 0)
            if not itm_x or not itm_y:
                continue
            lon_r, lat_r = itm_to_wgs84(itm_x, itm_y)
        except Exception:
            continue
        label = r.get("ResultLable") or r.get("ResultLabel") or query
        candidates.append({
            "label":        label,
            "display_name": label,
            "lat":          lat_r,
            "lon":          lon_r,
        })

    return jsonify({"candidates": candidates})


@app.route("/api/lookup-address")
def lookup_address():
    """
    GET /api/lookup-address?lat=<lat>&lon=<lon>&display=<text>
    Finds the parcel at the given coordinates.
    """
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "קואורדינטות לא תקינות"}), 400

    display_address = request.args.get("display", "")

    x, y = wgs84_to_mercator(lon, lat)
    params = {
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": "opendata:PARCEL_ALL", "outputFormat": "application/json",
        "CQL_FILTER": f"INTERSECTS(the_geom,SRID=3857;POINT({x} {y}))",
        "maxFeatures": 1,
    }
    data = query_wfs(params)
    if data is None:
        return jsonify({"error": "שגיאה בחיפוש חלקה לפי מיקום."}), 502

    features = data.get("features") or []
    if not features:
        return jsonify({
            "error": "הכתובת אותרה אך לא נמצאה חלקה רשומה. נסי כתובת ספציפית יותר.",
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
