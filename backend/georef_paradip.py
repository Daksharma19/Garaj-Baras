# Garaj Baras - georef_paradip.py
#
# Paradip (Paradeep) radar crop: 520 x 520 px. Raw GIF is the standard 880x720
# MAXZ frame with the map panel at the bottom-left, so radar_paradip.py applies
# its own CROP_BOX = (0, 200, 520, 720) (same layout as Jaipur; Delhi's OCR box
# still works).
#
# Paradip is a coastal 250 km radar. Its coverage disc — confirmed by fitting
# the range rings over the open sea (SE quadrant) and by the disc bounding box —
# is centered at crop (259.5, 259.5) with radius 257 px, giving 250/257.5 =
# 0.9709 km/px. GCPs were generated from a north-up azimuthal-equidistant
# projection about the radar site (20.264 N, 86.611 E, = the disc/ring center)
# and validated by overlaying Odisha city markers (Rourkela, Keonjhar, Deogarh,
# Angul, Cuttack, Kendrapara, Bhadrak, Puri…) on a live frame — all landed on
# the frame's own city diamonds.
#
# Quadratic georef model (identical form to georef.py / georef_jaipur.py):
#   px = c0 + c1*lat + c2*lon + c3*lat*lon + c4*lat^2 + c5*lon^2
#   py = d0 + d1*lat + d2*lon + d3*lat*lon + d4*lat^2 + d5*lon^2

IMAGE_WIDTH  = 520
IMAGE_HEIGHT = 520

CENTER_LAT = 20.264
CENTER_LON = 86.611

CPX = (
    -10256.7637219341,
        59.8450216305,
       121.4181889756,
        -0.6910192696,
         0.0001200249,
         0.0000215511,
)

CPY = (
      146.8008948245,
     -114.5474069622,
       56.2001789885,
        0.0000666987,
        0.0001256326,
       -0.3244479687,
)


def _features(lat, lon):
    return (1.0, lat, lon, lat * lon, lat * lat, lon * lon)


def _dot(coeffs, feat):
    return sum(c * f for c, f in zip(coeffs, feat))


def latlon_to_pixel(lat, lon):
    """Convert WGS84 lat/lon to Paradip radar image pixel (x, y)."""
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
    checks = [
        ("Paradip",     20.2640, 86.6110),
        ("Cuttack",     20.4625, 85.8828),
        ("Bhubaneswar", 20.2961, 85.8245),
        ("Puri",        19.8135, 85.8312),
        ("Kendrapara",  20.5017, 86.4225),
        ("Keonjhar",    21.6289, 85.5817),
        ("Rourkela",    22.2604, 84.8536),
        ("Balasore",    21.4934, 86.9335),
    ]
    print("Paradip georef check (crop px, 520x520):")
    for name, la, lo in checks:
        px, py = latlon_to_pixel(la, lo)
        inside = is_within_radar(la, lo)
        print(f"  {name:<12}: ({la},{lo}) -> ({px:3d},{py:3d})  in={inside}")
    print("\nInverse check:")
    for name, la, lo in checks[:4]:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        print(f"  {name}: ({la},{lo}) -> pix({px},{py}) -> ({la2},{lo2})")
