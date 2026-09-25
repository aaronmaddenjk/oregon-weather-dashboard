"""
Active wildfire perimeters for the Map tab, from NIFC's WFIGS "Current Interagency Fire
Perimeters" (the same national feed Google Maps and InciWeb draw from).

"Current" keeps a fire until it is formally declared out, which can be months after it
stopped burning, so this keeps only fires that are actually active:
  - wildfires (not prescribed burns or complex roll-ups)
  - under 100% contained
  - at least MIN_ACRES (tiny initial-attack fires are dots, not perimeters)
  - perimeter mapped within MAX_AGE_DAYS
No evacuation zones - the feed doesn't carry them.

active_perimeters(bounds) -> GeoJSON FeatureCollection (polygons, simplified to ~100 m) with
properties name, acres, pct (None if unreported), updated ("Sep 24"), lon/lat (a label point),
or None if the feed is unavailable.
"""
from datetime import datetime, timedelta, timezone

import requests

URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
       "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")
MIN_ACRES = 10
MAX_AGE_DAYS = 14


def _label_point(geom):
    # centre of the largest ring's bounding box: good enough to hang a dot and a name on
    rings = geom["coordinates"] if geom["type"] == "Polygon" else [p[0] for p in geom["coordinates"]]
    ring = max(rings, key=len)
    xs, ys = [c[0] for c in ring], [c[1] for c in ring]
    return round((min(xs) + max(xs)) / 2, 4), round((min(ys) + max(ys)) / 2, 4)


def active_perimeters(bounds):
    since = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    try:
        r = requests.get(URL, params={
            "where": (f"attr_IncidentTypeCategory='WF' AND poly_GISAcres>={MIN_ACRES} "
                      "AND (attr_PercentContained IS NULL OR attr_PercentContained<100) "
                      "AND attr_FireOutDateTime IS NULL"),
            "geometry": ",".join(str(b) for b in bounds), "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outSR": 4326,
            "outFields": "poly_IncidentName,poly_GISAcres,attr_PercentContained,poly_DateCurrent",
            "maxAllowableOffset": 0.001, "geometryPrecision": 4, "f": "geojson"}, timeout=60)
        feats = r.json()["features"]
    except Exception as exc:
        print(f"  WARNING: fire perimeters unavailable ({exc})")
        return None
    out = []
    for f in feats:
        p, g = f.get("properties") or {}, f.get("geometry")
        upd = p.get("poly_DateCurrent")
        if not g or not upd or datetime.fromtimestamp(upd / 1000, timezone.utc) < since:
            continue   # an old perimeter nobody has re-mapped: not an active fire
        lon, lat = _label_point(g)
        name = (p.get("poly_IncidentName") or "Unnamed").strip().title()
        out.append({"type": "Feature", "geometry": g, "properties": {
            "name": name if name.lower().endswith("fire") else name + " Fire",
            "acres": round(p.get("poly_GISAcres") or 0), "pct": p.get("attr_PercentContained"),
            "updated": datetime.fromtimestamp(upd / 1000, timezone.utc).strftime("%b %d").replace(" 0", " "),
            "lon": lon, "lat": lat}})
    out.sort(key=lambda f: -f["properties"]["acres"])
    return {"type": "FeatureCollection", "features": out}
