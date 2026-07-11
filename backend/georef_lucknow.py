# Garaj Baras - georef_lucknow.py
#
# Lucknow radar crop: 392 x 392 px, cropped from the raw 704x594 GIF at
# CROP_BOX (0, 176, 392, 568) — see radar_lucknow.py. NOT the same layout as
# Delhi (880x720, 527x525 crop): the raw frame size and panel offset differ.
#
# This module previously assumed a 527x525 crop reusing Delhi's crop box,
# which on Lucknow's actual (smaller, differently-offset) frame pulled in the
# right-side RHI/legend panel and black-padded the bottom — every pixel below
# was silently wrong. The GCPs were originally measured against that crop's
# real (non-padded) region, which only ever differed from the corrected crop
# by a constant 51px vertical offset (176 - 125), so the fit was recovered by
# shifting the py constant term by -51 rather than re-measuring from scratch;
# verified by re-plotting all 7 GCPs against a live corrected-crop frame.
#
# Quadratic georef model (identical form to georef.py):
#   px = c0 + c1*lat + c2*lon + c3*lat*lon + c4*lat^2 + c5*lon^2
#   py = d0 + d1*lat + d2*lon + d3*lat*lon + d4*lat^2 + d5*lon^2
#
# Least-squares fit to 7 GCPs, in corrected 392x392 crop space (max residual: 6 px):
#   Lucknow    (26.8467, 80.9462) → (195, 195)
#   Gonda      (27.1320, 81.9607) → (277, 165)
#   Bareilly   (28.3670, 79.4304) → ( 82,  57)
#   Prayagraj  (25.4358, 81.8463) → (275, 309)
#   Jaunpur    (25.7464, 82.6836) → (340, 285)
#   Banda      (25.4804, 80.3377) → (153, 308)
#   Kannauj    (27.0535, 79.9207) → ( 92, 171)

IMAGE_WIDTH  = 392
IMAGE_HEIGHT = 392

CENTER_LAT = 26.8467   # Lucknow radar station
CENTER_LON = 80.9462

CPX = (
    -11889.639397127024,
      -895.2643334524814,
       504.433517146317,
         2.6721285484117283,
        12.768499119722419,
        -3.0108627514580952,
)

CPY = (
    -4919.510862835962,
        6.658676495472395,
      152.62412436416002,
       -0.33161483156016053,
       -1.231601668492623,
       -0.8872070967171704,
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
        ("Lucknow",   26.8467, 80.9462, 195, 195),
        ("Gonda",     27.1320, 81.9607, 277, 165),
        ("Bareilly",  28.3670, 79.4304,  82,  57),
        ("Prayagraj", 25.4358, 81.8463, 275, 309),
        ("Jaunpur",   25.7464, 82.6836, 340, 285),
        ("Banda",     25.4804, 80.3377, 153, 308),
        ("Kannauj",   27.0535, 79.9207,  92, 171),
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
