# Garaj Baras - georef_patna.py
#
# Patna radar crop: 527 x 525 px (same IMD 880x720 layout as Delhi).
#
# Quadratic georef model (identical form to georef.py / georef_lucknow.py):
#   px = c0 + c1*lat + c2*lon + c3*lat*lon + c4*lat^2 + c5*lon^2
#   py = d0 + d1*lat + d2*lon + d3*lat*lon + d4*lat^2 + d5*lon^2
#
# Minimum-norm least-squares fit to 4 GCPs (0 px residual on all 4):
#   Patna Airport    (25.5913, 85.0956) → (242, 430)
#   Motihari         (26.6543, 84.9162) → (225, 316)
#   Gaya Airport     (24.7440, 84.9514) → (234, 516)
#   Darbhanga Airport(26.1912, 85.9102) → (321, 370)

IMAGE_WIDTH  = 527
IMAGE_HEIGHT = 525

CENTER_LAT = 25.5913   # Patna radar station (Lok Nayak Jayaprakash Airport area)
CENTER_LON = 85.0956

CPX = (
    -1.9586246869800599,
    -25.625608169260854,
    -83.72366931141534,
    -2.193464504107668,
     4.066558936888271,
     1.3999966348949173,
)

CPY = (
     0.7977623074921709,
    10.426486884799665,
    34.10360259842307,
     0.01506852131913807,
    -2.263189792167349,
    -0.17818862254962853,
)


def _features(lat, lon):
    return (1.0, lat, lon, lat * lon, lat * lat, lon * lon)


def _dot(coeffs, feat):
    return sum(c * f for c, f in zip(coeffs, feat))


def latlon_to_pixel(lat, lon):
    """Convert WGS84 lat/lon to Patna radar image pixel (x, y)."""
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
        ("Patna Airport",     25.5913, 85.0956, 242, 430),
        ("Motihari",          26.6543, 84.9162, 225, 316),
        ("Gaya Airport",      24.7440, 84.9514, 234, 516),
        ("Darbhanga Airport", 26.1912, 85.9102, 321, 370),
    ]
    print("Patna GCP check:")
    for name, la, lo, ex, ey in gcps:
        px, py = latlon_to_pixel(la, lo)
        print(f"  {name:<22}: want ({ex:3d},{ey:3d}) got ({px:3d},{py:3d}) err=({px-ex:+d},{py-ey:+d})")
    print()
    print("Inverse check (pixel -> latlon -> pixel):")
    for name, la, lo, ex, ey in gcps:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        print(f"  {name:<22}: ({la},{lo}) -> pix({px},{py}) -> ({la2},{lo2})")
    print()
    print("Coverage boundary check (250 km radius from Patna):")
    import math
    r = 6371.0
    dlat = math.degrees(250 / r)
    dlon = math.degrees(250 / (r * math.cos(math.radians(CENTER_LAT))))
    for label, la, lo in [
        ("N edge", CENTER_LAT + dlat, CENTER_LON),
        ("S edge", CENTER_LAT - dlat, CENTER_LON),
        ("E edge", CENTER_LAT, CENTER_LON + dlon),
        ("W edge", CENTER_LAT, CENTER_LON - dlon),
    ]:
        px, py = latlon_to_pixel(la, lo)
        in_b = is_within_radar(la, lo)
        print(f"  {label}: ({la:.2f},{lo:.2f}) -> px={px}, py={py}  in_bounds={in_b}")
