# Garaj Baras - georef.py
#
# Delhi radar crop: 527 x 525 px (see radar.py).
#
# A single affine cannot match IGI + 100 km W + Aligarh + Bijnor pixels at once
# (the plate is not affine in lat/lon). We use a quadratic map:
#
#   px = c0 + c1*lat + c2*lon + c3*lat*lon + c4*lat^2 + c5*lon^2
#   py = d0 + d1*lat + d2*lon + d3*lat*lon + d4*lat^2 + d5*lon^2
#
# Least-squares fit to 7 GCPs (max residual: 4 px):
#   IGI:           (28.5562, 77.1000) -> (284, 310)
#   100km W:       (28.5562, 76.0761) -> (114, 310)
#   Aligarh:       (27.8974, 78.0880) -> (395, 395)
#   Bijnor:        (29.3727, 78.1363) -> (398, 207)
#   Moradabad:     (28.8386, 78.7733) -> (472, 275)
#   Muzaffarnagar: (29.4730, 77.7036) -> (354, 196)
#   Sambhal:       (28.5824, 78.5667) -> (450, 306)

import math

IMAGE_WIDTH = 527
IMAGE_HEIGHT = 525

CENTER_LAT = 28.5562
CENTER_LON = 77.1000

# px coefficients (quadratic)
CPX = (
    -166432.00548457023,
      2066.248102146954,
      3409.2924157668126,
       -10.508378002822152,
       -21.73594398364605,
       -19.225625622175357,
)
# py coefficients (quadratic)
CPY = (
    4459.4701294393035,
    -166.71150367990234,
       0.882644318695064,
      -1.403895537126234,
       2.608071834655944,
       0.2535201826851706,
)


def _features(lat, lon):
    return (
        1.0,
        lat,
        lon,
        lat * lon,
        lat * lat,
        lon * lon,
    )


def _dot(coeffs, feat):
    return sum(c * f for c, f in zip(coeffs, feat))


def latlon_to_pixel(lat, lon):
    """Convert WGS84 lat/lon (degrees) to radar image pixel (x, y)."""
    feat = _features(lat, lon)
    px = _dot(CPX, feat)
    py = _dot(CPY, feat)
    return (int(round(px)), int(round(py)))


def _latlon_to_pixel_float(lat, lon):
    feat = _features(lat, lon)
    return _dot(CPX, feat), _dot(CPY, feat)


def pixel_to_latlon(px, py):
    """
    Inverse of latlon_to_pixel (numerical; quadratic has no closed form).
    """
    lat = float(CENTER_LAT)
    lon = float(CENTER_LON)
    h = 1e-5
    for _ in range(40):
        pxe, pye = _latlon_to_pixel_float(lat, lon)
        ex = pxe - px
        ey = pye - py
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
    R_KM = 6371.0
    lat_rad = math.radians(CENTER_LAT)
    dlon_deg = math.degrees(100.0 / (R_KM * math.cos(lat_rad)))
    lon_w = CENTER_LON - dlon_deg

    print("Quadratic georef (4 GCPs: IGI, 100 km W, Aligarh, Bijnor)")
    print(f"  100 km west lon = {lon_w:.6f} E")
    print()

    gcp = [
        ("IGI", 28.5562, 77.1000, 284, 310),
        ("100 km W", 28.5562, lon_w, 114, 310),
        ("Aligarh", 27.8974, 78.0880, 395, 395),
        ("Bijnor", 29.3727, 78.1363, 400, 210),
    ]
    print("GCP check:")
    for name, la, lo, ex, ey in gcp:
        px, py = latlon_to_pixel(la, lo)
        print(f"  {name}: want ({ex},{ey}) got ({px},{py})")
    print()

    test_locations = [
        ("IGI Airport", 28.5562, 77.1000),
        ("100 km W of IGI", 28.5562, lon_w),
        ("Aligarh", 27.8974, 78.0880),
        ("Bijnor (BIJ)", 29.3727, 78.1363),
        ("Noida", 28.5355, 77.3910),
        ("Gurugram", 28.4595, 77.0266),
        ("Meerut", 28.9845, 77.7064),
    ]

    print("Sample locations:")
    for name, la, lo in test_locations:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        print(f"{name}: ({la},{lo}) -> pixel({px},{py}) -> ({la2},{lo2})")
