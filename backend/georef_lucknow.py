# Garaj Baras - georef_lucknow.py
#
# Lucknow radar crop: 527 x 525 px (same IMD layout as Delhi).
#
# Quadratic georef model (identical form to georef.py):
#   px = c0 + c1*lat + c2*lon + c3*lat*lon + c4*lat^2 + c5*lon^2
#   py = d0 + d1*lat + d2*lon + d3*lat*lon + d4*lat^2 + d5*lon^2
#
# Least-squares fit to 7 GCPs (max residual: 3 px):
#   Lucknow      (26.8467, 80.9462) → (224, 392)
#   Sitapur      (27.5626, 80.6823) → (203, 313)
#   Gonda        (27.1320, 81.9607) → (317, 357)
#   Shahjahanpur (27.8824, 79.9055) → (139, 284)
#   Prayagraj    (25.4358, 81.8463) → (312, 522)
#   Barabanki    (26.9257, 81.1935) → (250, 379)
#   Bareilly     (28.3670, 79.4304) → ( 94, 234)

IMAGE_WIDTH  = 527
IMAGE_HEIGHT = 525

CENTER_LAT = 26.8467   # Lucknow radar station
CENTER_LON = 80.9462

CPX = (
     17915.081217803683,
      -584.1984050918419,
      -328.6452742488677,
         4.51394758481799,
         4.028985105376416,
         1.8138475905540696,
)

CPY = (
     56169.38434593558,
      -261.5431128609946,
     -1244.8675826566352,
         5.338343864582683,
        -5.193803084741686,
         6.738490964874253,
)


def _features(lat, lon):
    return (1.0, lat, lon, lat * lon, lat * lat, lon * lon)


def _dot(coeffs, feat):
    return sum(c * f for c, f in zip(coeffs, feat))


def latlon_to_pixel(lat, lon):
    """Convert WGS84 lat/lon to Lucknow radar image pixel (x, y)."""
    feat = _features(lat, lon)
    px   = _dot(CPX, feat)
    py   = _dot(CPY, feat)
    return (int(round(px)), int(round(py)))


def _latlon_to_pixel_float(lat, lon):
    feat = _features(lat, lon)
    return _dot(CPX, feat), _dot(CPY, feat)


def pixel_to_latlon(px, py):
    """Inverse of latlon_to_pixel via Newton-Raphson (same algorithm as georef.py)."""
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
        ("Lucknow",      26.8467, 80.9462, 224, 392),
        ("Sitapur",      27.5626, 80.6823, 203, 313),
        ("Gonda",        27.1320, 81.9607, 317, 357),
        ("Shahjahanpur", 27.8824, 79.9055, 139, 284),
        ("Prayagraj",    25.4358, 81.8463, 312, 522),
        ("Barabanki",    26.9257, 81.1935, 250, 379),
        ("Bareilly",     28.3670, 79.4304,  94, 234),
    ]
    print("Lucknow GCP check:")
    for name, la, lo, ex, ey in gcps:
        px, py = latlon_to_pixel(la, lo)
        print(f"  {name:<14}: want ({ex:3d},{ey:3d}) got ({px:3d},{py:3d}) err=({px-ex:+d},{py-ey:+d})")
    print()
    print("Inverse check (pixel -> latlon -> pixel):")
    for name, la, lo, ex, ey in gcps:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        print(f"  {name:<14}: ({la},{lo}) -> pix({px},{py}) -> ({la2},{lo2})")
