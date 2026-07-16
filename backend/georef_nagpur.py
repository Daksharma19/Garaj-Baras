# Garaj Baras - georef_nagpur.py
#
# Nagpur radar crop: 520 x 520 px. Raw GIF is the standard 880x720 MAXZ frame,
# but the map panel sits at the BOTTOM-LEFT (same layout as Jaipur/Paradip/
# Patiala), so radar_nagpur.py applies its own CROP_BOX = (0, 200, 520, 720).
#
# Model: exact azimuthal-equidistant (AEQD) projection centered on the radar,
# derived from the image itself — NOT a polynomial fit to town markers (see the
# Lucknow/Patiala georef notes and ARCHITECTURE.md §8). Measured off a live
# frame:
#   * The radar disc (max range) is inscribed in the 520 px panel about the
#     station crosshair at crop (260, 259). Its edge radius (~257 px) over the
#     250 km max range gives scale 1.028 px/km — identical rendering to the
#     other 250 km bottom-left panels (Jaipur 257 px, Paradip likewise).
#   * The NGP crosshair marks the radar site; the derived center is
#     cross-validated by projecting surrounding towns (Betul, Wardha, Seoni,
#     Chandrapur, Akola, Yavatmal, Bhandara, Gadchiroli, Jabalpur) against
#     their IMD basemap labels — all land within label-placement noise, and a
#     least-squares fit over them pins the center at ~(21.15 N, 79.05 E).
#     (Nagpur's graticule is unlabeled, unlike Patiala's, so the surrounding
#     real-city fit is the anchor; IMD's basemap dots are only a loose check —
#     they are drawn sloppily, per the Lucknow note.)
#
# 250 km radar. Forward/inverse are closed-form spherical geodesics (identical
# form to georef_lucknow.py / georef_patiala.py).

import math

IMAGE_WIDTH  = 520
IMAGE_HEIGHT = 520

CENTER_LAT = 21.15     # Nagpur radar station (Sonegaon), city-fit render position
CENTER_LON = 79.05

CENTER_PX = 260.0      # ring/crosshair center, x (px)
CENTER_PY = 259.0      # ring/crosshair center, y (px)
PX_PER_KM = 1.028      # disc scale (250 km edge at 257 px)

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
    """Convert WGS84 lat/lon to Nagpur radar image pixel (x, y)."""
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
        ("Nagpur",     21.1458, 79.0882),
        ("Wardha",     20.7453, 78.6022),
        ("Bhandara",   21.1700, 79.6500),
        ("Chandrapur", 19.9500, 79.2970),
        ("Betul",      21.9010, 77.9010),
        ("Seoni",      22.0850, 79.5430),
        ("Akola",      20.7000, 77.0080),
        ("Yavatmal",   20.3930, 78.1330),
        ("Gadchiroli", 20.1810, 80.0030),
        ("Jabalpur",   23.1680, 79.9860),
    ]
    print("Nagpur AEQD georef check (pixel -> latlon -> pixel round trip):")
    for name, la, lo in towns:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        err_km = math.hypot((la2 - la) * 111.32,
                            (lo2 - lo) * 111.32 * math.cos(math.radians(la)))
        inb = is_within_radar(la, lo)
        print(f"  {name:<11}: ({la:.4f},{lo:.4f}) -> pix({px:3d},{py:3d}) "
              f"-> ({la2:.4f},{lo2:.4f})  rt_err={err_km:.2f} km  in={inb}")
