# Garaj Baras - georef_bhopal.py
#
# Bhopal radar crop: 527 x 525 px (same IMD 880x720 layout as Delhi/Patna/Lucknow).
# CROP applied by extract_frames(): img.crop((0, 125, 527, 650))
#
# Quadratic georef model (identical form to georef.py / georef_patna.py):
#   px = c0 + c1*lat + c2*lon + c3*lat*lon + c4*lat^2 + c5*lon^2
#   py = d0 + d1*lat + d2*lon + d3*lat*lon + d4*lat^2 + d5*lon^2
#
# GCPs measured from full 880x720 frame (y offset -125 applied in code):
#   Bhopal       (23.2875, 77.3374) -> full(250,455) -> crop(250,330)
#   Ujjain       (23.1793, 75.7757) -> full( 86,457) -> crop( 86,332)
#   Sagar        (23.8388, 78.7348) -> full(395,393) -> crop(395,268)
#   Kota         (25.1802, 75.8652) -> full( 94,236) -> crop( 94,111)
#   Indore       (22.7217, 75.7793) -> full( 87,518) -> crop( 87,393)
#   Tikamgarh    (24.7421, 78.8309) -> full(424,294) -> crop(424,169)

IMAGE_WIDTH  = 527
IMAGE_HEIGHT = 525

CENTER_LAT = 23.2875
CENTER_LON = 77.3374

CPX = (
    -13653.358145249565,
      -583.3342045616208,
       432.05079177961926,
         7.735553609465411,
        -0.09117480202780004,
        -3.311809819987925,
)

CPY = (
      293.61514972612173,
     -526.0119486829103,
      191.96549089798765,
       -0.33345376540502253,
        9.107662196075742,
       -1.153443507362184,
)


def _features(lat, lon):
    return (1.0, lat, lon, lat * lon, lat * lat, lon * lon)


def _dot(coeffs, feat):
    return sum(c * f for c, f in zip(coeffs, feat))


def latlon_to_pixel(lat, lon):
    """Convert WGS84 lat/lon to Bhopal radar image pixel (x, y)."""
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
        ("Bhopal",     23.2875, 77.3374, 250, 330),
        ("Ujjain",     23.1793, 75.7757,  86, 332),
        ("Sagar",      23.8388, 78.7348, 395, 268),
        ("Kota",       25.1802, 75.8652,  94, 111),
        ("Indore",     22.7217, 75.7793,  87, 393),
        ("Tikamgarh",  24.7421, 78.8309, 424, 169),
    ]
    print("Bhopal GCP check:")
    for name, la, lo, ex, ey in gcps:
        px, py = latlon_to_pixel(la, lo)
        print(f"  {name:<14}: want ({ex:3d},{ey:3d}) got ({px:3d},{py:3d}) err=({px-ex:+d},{py-ey:+d})")
    print()
    print("Cross-checks:")
    checks = [
        ("Guna",        24.6474, 77.3057),
        ("Hoshangabad", 22.7450, 77.7270),
        ("Damoh",       23.8338, 79.4409),
        ("Shivpuri",    25.4267, 77.6615),
    ]
    for name, la, lo in checks:
        px, py = latlon_to_pixel(la, lo)
        print(f"  {name:<14}: ({la},{lo}) -> crop({px},{py})")
    print()
    print("Inverse check:")
    for name, la, lo, ex, ey in gcps[:3]:
        px, py = latlon_to_pixel(la, lo)
        la2, lo2 = pixel_to_latlon(px, py)
        print(f"  {name}: ({la},{lo}) -> pix({px},{py}) -> ({la2},{lo2})")
