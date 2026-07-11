# Garaj Baras - georef_jaipur.py
#
# Jaipur radar crop: 520 x 520 px. Raw GIF is the standard 880x720 MAXZ frame,
# but the map panel sits at the BOTTOM-LEFT (not Delhi's offset), so
# radar_jaipur.py applies its own CROP_BOX = (0, 200, 520, 720).
#
# Jaipur is a 250 km radar (Delhi is 300 km): the coverage disc measured off a
# live frame is centered at crop (259.5, 259.5) with radius 257 px, giving
# 250/257 = 0.9728 km/px. GCP pixels were generated from an azimuthal
# equidistant projection around the disc center (= Jaipur/Sanganer airport,
# where the JPR plane marker sits) and validated visually against the frame's
# own airport markers (IGI, AGRA, KTA, UDP, KSG) — all landed on the symbols.
#
# Quadratic georef model (identical form to georef.py / georef_bhopal.py):
#   px = c0 + c1*lat + c2*lon + c3*lat*lon + c4*lat^2 + c5*lon^2
#   py = d0 + d1*lat + d2*lon + d3*lat*lon + d4*lat^2 + d5*lon^2

IMAGE_WIDTH  = 520
IMAGE_HEIGHT = 520

CENTER_LAT = 26.8242
CENTER_LON = 75.8122

CPX = (
    -9244.918472982725,
       68.15494682576617,
      124.6351719131573,
       -0.9021763539653598,
        0.0043651876089484485,
        0.010241542547584642,
)

CPY = (
     1022.4587864010665,
     -114.35719138474384,
       60.78153318023668,
        0.0004893431474551955,
        0.00019847135726936498,
       -0.4009645089252835,
)


def _features(lat, lon):
    return (1.0, lat, lon, lat * lon, lat * lat, lon * lon)


def _dot(coeffs, feat):
    return sum(c * f for c, f in zip(coeffs, feat))


def latlon_to_pixel(lat, lon):
    """Convert WGS84 lat/lon to Jaipur radar image pixel (x, y)."""
    feat = _features(lat, lon)
    px   = _dot(CPX, feat)
    py   = _dot(CPY, feat)
    return (int(round(px)), int(round(py)))


def _latlon_to_pixel_float(lat, lon):
    feat = _features(lat, lon)
    return _dot(CPX, feat), _dot(CPY, feat)


def pixel_to_latlon(px, py):
    """Inverse of latlon_to_pixel via Newton-Raphson."""
    lat = float(CENTER_LAT)
    lon = float(CENTER_LON)
    h   = 1e-5
    for _ in range(40):
        pxe, pye = _latlon_to_pixel_float(lat, lon)
        ex, ey   = pxe - px, pye - py
        if ex * ex + ey * ey < 0.04:
            break
        pxe_la, pye_la = _latlon_to_pixel_float(lat + h, lon)
        pxe_lo, pye_lo = _latlon_to_pixel_float(lat, lon + h)
        dpx_dlat = (pxe_la - pxe) / h
        dpx_dlon = (pxe_lo - pxe) / h
        dpy_dlat = (pye_la - pye) / h
        dpy_dlon = (pye_lo - pye) / h
        det = dpx_dlat * dpy_dlon - dpx_dlon * dpy_dlat
        if abs(det) < 1e-14:
            break
        lat -= (dpy_dlon * ex - dpx_dlon * ey) / det
        lon -= (-dpy_dlat * ex + dpx_dlat * ey) / det
    return (round(lat, 4), round(lon, 4))


def is_within_radar(lat, lon):
    px, py = latlon_to_pixel(lat, lon)
    return 0 <= px < IMAGE_WIDTH and 0 <= py < IMAGE_HEIGHT


if __name__ == "__main__":
    gcps = [
        ("Jaipur",         26.8242, 75.8122, 260, 260),
        ("Delhi IGI",      28.5562, 77.1000, 389,  61),
        ("Agra",           27.1767, 78.0081, 483, 217),
        ("Kota",           25.2138, 75.8648, 265, 444),
        ("Udaipur",        24.5854, 73.7125,  41, 514),
        ("Ajmer",          26.4499, 74.6399, 140, 302),
        ("Sikar",          27.6094, 75.1399, 191, 170),
        ("Alwar",          27.5530, 76.6346, 343, 176),
        ("Sawai Madhopur", 25.9928, 76.3597, 316, 354),
        ("Bhilwara",       25.3407, 74.6313, 138, 429),
    ]
    print("Jaipur GCP check:")
    for name, la, lo, ex, ey in gcps:
        px, py = latlon_to_pixel(la, lo)
        print(f"  {name:<15}: want ({ex:3d},{ey:3d}) got ({px:3d},{py:3d}) err=({px-ex:+d},{py-ey:+d})")
    print()
    print("Inverse check:")
    for name, la, lo, ex, ey in gcps[:4]:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        print(f"  {name}: ({la},{lo}) -> pix({px},{py}) -> ({la2},{lo2})")
