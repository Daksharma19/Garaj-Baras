# Garaj Baras - georef_patiala.py
#
# Patiala radar crop: 520 x 520 px. Raw GIF is the standard 880x720 MAXZ frame,
# but the map panel sits at the BOTTOM-LEFT (same layout as Jaipur/Paradip), so
# radar_patiala.py applies its own CROP_BOX = (0, 200, 520, 720).
#
# Model: exact azimuthal-equidistant (AEQD) projection centered on the radar,
# derived from the image itself — NOT a polynomial fit to town markers (see the
# Lucknow georef note and ARCHITECTURE.md §8). Two independent measurements off
# a live frame agree:
#   * Range rings: the 100/200/300 km rings are concentric circles about the
#     station crosshair at crop (260, 259); their radii (87 / 173 / 260 px)
#     give scale 0.867 px/km.
#   * Graticule: IMD's lat/lon grid lines land exactly where this AEQD model
#     predicts — 30/31/32 N at py 293/197/101, and 75/76/77/78 E at
#     px 138/223/305/389 — fixing the crosshair's WGS84 position at
#     (30.354 N, 76.454 E). (This is IMD's internally-consistent render position
#     for the antenna, which is what places echoes correctly; it differs by a
#     few km from nominal "Patiala city" coordinates, and the graticule wins.)
#
# 300 km radar (same range as Delhi). Forward/inverse are closed-form spherical
# geodesics (identical form to georef_lucknow.py).

import math

IMAGE_WIDTH  = 520
IMAGE_HEIGHT = 520

CENTER_LAT = 30.354    # Patiala radar station (graticule-derived render position)
CENTER_LON = 76.454

CENTER_PX = 260.0      # ring/crosshair center, x (px)
CENTER_PY = 259.0      # ring/crosshair center, y (px)
PX_PER_KM = 0.867      # ring scale (300 km ring at 260 px)

EARTH_RADIUS_KM = 6371.0

_CLAT = math.radians(CENTER_LAT)
_CLON = math.radians(CENTER_LON)


def _latlon_to_pixel_float(lat, lon):
    la2 = math.radians(lat)
    dlon = math.radians(lon) - _CLON
    cosc = math.sin(_CLAT) * math.sin(la2) + math.cos(_CLAT) * math.cos(la2) * math.cos(dlon)
    d = math.acos(max(-1.0, min(1.0, cosc))) * EARTH_RADIUS_KM
    theta = math.atan2(
        math.sin(dlon) * math.cos(la2),
        math.cos(_CLAT) * math.sin(la2) - math.sin(_CLAT) * math.cos(la2) * math.cos(dlon),
    )
    px = CENTER_PX + PX_PER_KM * d * math.sin(theta)
    py = CENTER_PY - PX_PER_KM * d * math.cos(theta)
    return px, py


def latlon_to_pixel(lat, lon):
    """Convert WGS84 lat/lon to Patiala radar image pixel (x, y)."""
    px, py = _latlon_to_pixel_float(lat, lon)
    return (int(round(px)), int(round(py)))


def pixel_to_latlon(px, py):
    """Exact inverse: pixel -> lat/lon via the spherical direct geodesic."""
    dx = px - CENTER_PX
    dy = CENTER_PY - py
    d = math.hypot(dx, dy) / PX_PER_KM
    if d < 1e-9:
        return (round(CENTER_LAT, 4), round(CENTER_LON, 4))
    theta = math.atan2(dx, dy)          # azimuth from north, clockwise
    delta = d / EARTH_RADIUS_KM
    sin_lat = (math.sin(_CLAT) * math.cos(delta)
               + math.cos(_CLAT) * math.sin(delta) * math.cos(theta))
    lat = math.asin(max(-1.0, min(1.0, sin_lat)))
    lon = _CLON + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(_CLAT),
        math.cos(delta) - math.sin(_CLAT) * sin_lat,
    )
    return (round(math.degrees(lat), 4), round(math.degrees(lon), 4))


def is_within_radar(lat, lon):
    px, py = latlon_to_pixel(lat, lon)
    return 0 <= px < IMAGE_WIDTH and 0 <= py < IMAGE_HEIGHT


if __name__ == "__main__":
    # Round-trip and reference checks. Expected pixels are from this AEQD model;
    # IMD's basemap dots may sit a few px away (drawn sloppily, not ground truth).
    towns = [
        ("Patiala",     30.3376, 76.3869),
        ("Chandigarh",  30.7333, 76.7794),
        ("Ludhiana",    30.9010, 75.8573),
        ("Ambala",      30.3782, 76.7767),
        ("Karnal",      29.6857, 76.9905),
        ("Bathinda",    30.2110, 74.9455),
        ("Hisar",       29.1492, 75.7217),
        ("Delhi",       28.6139, 77.2090),
    ]
    print("Patiala AEQD georef check (pixel -> latlon -> pixel round trip):")
    for name, la, lo in towns:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        err_km = math.hypot((la2 - la) * 111.32,
                            (lo2 - lo) * 111.32 * math.cos(math.radians(la)))
        inb = is_within_radar(la, lo)
        print(f"  {name:<11}: ({la:.4f},{lo:.4f}) -> pix({px:3d},{py:3d}) "
              f"-> ({la2:.4f},{lo2:.4f})  rt_err={err_km:.2f} km  in={inb}")
