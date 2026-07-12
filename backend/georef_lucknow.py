# Garaj Baras - georef_lucknow.py
#
# Lucknow radar crop: 392 x 392 px, cropped from the raw 704x594 GIF at
# CROP_BOX (0, 176, 392, 568) — see radar_lucknow.py.
#
# Model: exact azimuthal-equidistant (AEQD) projection centered on the radar,
# derived from the range rings in the image itself — NOT a polynomial fit to
# town markers. The 50/100/150/200/250 km rings are concentric circles
# (axis ratio 0.999, residual < 1 px), so echoes are plotted in true
# range/azimuth space:
#
#   ring center = (196.547, 195.576) px,  scale = 0.78693 px/km
#   (joint least-squares over all 5 rings x 4 frames; stable to 0.01 px)
#
# History: this module previously used a quadratic fit to 7 ground control
# points measured from IMD's basemap town dots. Those dots are drawn sloppily
# (Kannauj's is ~20 km off), and the flexible quadratic bent to absorb the
# errors, then extrapolated badly east of the easternmost GCP (Jaunpur,
# 82.68E): an echo over Gorakhpur (83.37E, 241 km out) was displayed ~8 km
# south of the city. Echoes follow the rings, not the basemap dots, so the
# ring-derived AEQD model is ground truth for echo placement.

import math

IMAGE_WIDTH  = 392
IMAGE_HEIGHT = 392

CENTER_LAT = 26.8467   # Lucknow radar station
CENTER_LON = 80.9462

CENTER_PX = 196.547    # ring center, x (px)
CENTER_PY = 195.576    # ring center, y (px)
PX_PER_KM = 0.78693    # ring scale (250 km ring at 196.7 px)

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
    """Convert WGS84 lat/lon to Lucknow radar image pixel (x, y)."""
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
    # Round-trip and reference checks. Towns listed with true coordinates;
    # expected pixels are from this AEQD model (IMD's basemap dots may sit a
    # few px away — they are drawn sloppily and are NOT ground truth).
    towns = [
        ("Lucknow",   26.8467, 80.9462),
        ("Gonda",     27.1320, 81.9607),
        ("Bareilly",  28.3670, 79.4304),
        ("Prayagraj", 25.4358, 81.8463),
        ("Jaunpur",   25.7464, 82.6836),
        ("Banda",     25.4804, 80.3377),
        ("Kannauj",   27.0535, 79.9207),
        ("Gorakhpur", 26.7606, 83.3732),
        ("Basti",     26.8140, 82.7630),
    ]
    print("Lucknow AEQD georef check (pixel -> latlon -> pixel round trip):")
    for name, la, lo in towns:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        err_km = math.hypot((la2 - la) * 111.32,
                            (lo2 - lo) * 111.32 * math.cos(math.radians(la)))
        print(f"  {name:<10}: ({la:.4f},{lo:.4f}) -> pix({px:3d},{py:3d}) "
              f"-> ({la2:.4f},{lo2:.4f})  rt_err={err_km:.2f} km")
