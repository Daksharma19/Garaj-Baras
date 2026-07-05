COLOR_TABLE = [
    (0,   25,  176, 20),
    (0,   58,  200, 25),
    (0,   71,  255, 30),
    (26,  163, 255, 35),
    (135, 241, 255, 38),
    (0,   200, 0,   41),
    (255, 255, 0,   44),
    (255, 165, 0,   50),
    (255, 0,   0,   55),
    (255, 255, 255, 60),
]

import math

def rgb_to_dbz(r, g, b):
    min_dist = float('inf')
    best_dbz = 0
    for cr, cg, cb, dbz in COLOR_TABLE:
        dist = math.sqrt((r-cr)**2 + (g-cg)**2 + (b-cb)**2)
        if dist < min_dist:
            min_dist = dist
            best_dbz = dbz
    if min_dist > 80:
        return 0
    return best_dbz

def dbz_to_label(dbz):
    if dbz == 0:
        return "No Rain", "#000000"
    elif dbz < 25:
        return "Very Light Rain", "#4169E1"
    elif dbz < 35:
        return "Light Rain", "#00BFFF"
    elif dbz < 41:
        return "Moderate Rain", "#00FF00"
    elif dbz < 55:
        return "Heavy Rain", "#FFD700"
    else:
        return "Very Heavy Rain", "#FF4500"

def get_pixel_intensity(frame_path, px, py):
    from PIL import Image
    img = Image.open(frame_path).convert('RGB')
    r, g, b = img.getpixel((px, py))
    dbz = rgb_to_dbz(r, g, b)
    label, color = dbz_to_label(dbz)
    return {
        "dbz": dbz,
        "label": label,
        "color": color,
        "rgb": (r, g, b)
    }

def enrich_results(results, waypoints_latlon,
                   latest_frame, dx, dy, lag_info=None, frame_rgb=None,
                   latlon_to_pixel_fn=None):
    from PIL import Image
    import numpy as np

    enriched = []
    if frame_rgb is not None:
        arr = frame_rgb
    else:
        img = Image.open(latest_frame).convert('RGB')
        arr = np.array(img)

    lag_mins = lag_info['lag_mins'] if lag_info else 25.0

    for r, (lat, lon, eta) in zip(results, waypoints_latlon):
        if latlon_to_pixel_fn is not None:
            px, py = latlon_to_pixel_fn(lat, lon)
        else:
            from georef import latlon_to_pixel
            px, py = latlon_to_pixel(lat, lon)

        if r["rain_expected"]:
            effective_eta = eta + lag_mins
            if r.get("src_px") is not None and r.get("src_py") is not None:
                # check_route_rain already back-projected via the hitting
                # patch's own motion vector — use that source pixel.
                shifted_px = int(r["src_px"])
                shifted_py = int(r["src_py"])
            else:
                # Legacy fallback: back-project with the global vector
                frames_ahead = effective_eta / 10.0
                shifted_px = int(px - dx * frames_ahead)
                shifted_py = int(py - dy * frames_ahead)

            # Clamp to image bounds
            h, w = arr.shape[:2]
            shifted_px = max(0, min(w - 1, shifted_px))
            shifted_py = max(0, min(h - 1, shifted_py))

            rv, gv, bv = arr[shifted_py, shifted_px]
            dbz = rgb_to_dbz(rv, gv, bv)
            label, color = dbz_to_label(dbz)

            if dbz == 0:
                label, color = dbz_to_label(20)

            message = f"{label} expected in {eta:.0f} mins"
            message += f"\n(radar ~{lag_mins:.0f} mins old — actual arrival may vary)"
        else:
            label  = "No Rain"
            color  = "#000000"
            dbz    = 0
            message = "Clear"

        enriched.append({
            "lat": lat,
            "lon": lon,
            "eta_mins": eta,
            "rain_expected": r["rain_expected"],
            "confidence": r["confidence"],
            "label": label,
            "color": color,
            "dbz": dbz,
            "message": message,
            "src_px": r.get("src_px"),
            "src_py": r.get("src_py"),
        })

    return enriched

if __name__ == "__main__":
    # Test 1: Color mapping
    test_pixels = [
        ("Very Light", 0,   25,  176),
        ("Light",      0,   71,  255),
        ("Moderate",   135, 241, 255),
        ("Heavy",      255, 255, 0  ),
        ("Very Heavy", 255, 0,   0  ),
        ("Background", 100, 173, 64 ),
    ]

    print("=" * 55)
    print("FUZZY INTENSITY TEST")
    print("=" * 55)
    for name, r, g, b in test_pixels:
        dbz = rgb_to_dbz(r, g, b)
        label, color = dbz_to_label(dbz)
        print(f"  {name:<12} RGB({r:3d},{g:3d},{b:3d})"
              f" -> {dbz:2.0f}dBZ -> {label:<15} {color}")

    print()

    # Test 2: Full pipeline Sec 62 Noida -> Sec 128 Noida
    print("=" * 55)
    print("ROUTE TEST: Sec 62 Noida -> Sec 128 Noida (FIXED LAG)")
    print("=" * 55)

    from radar import get_recent_frames, get_all_frames, get_radar_lag_mins
    from optical_flow import get_movement_vector, build_clutter_mask
    from georef import latlon_to_pixel, is_within_radar
    from prediction import (generate_waypoints, 
                            check_route_rain, 
                            haversine_km)

    all_frame_data    = get_all_frames()
    recent_frame_data = get_recent_frames(n=6)
    latest_ts = recent_frame_data[-1][1] if recent_frame_data else None
    lag_info         = get_radar_lag_mins(latest_ts)
    
    all_paths = [p for (p, _ts) in all_frame_data]
    clutter_mask = build_clutter_mask(all_paths)
    dx, dy, dir_from, dir_to, speed = get_movement_vector(
        recent_frame_data, clutter_mask=clutter_mask
    )
    latest_frame = recent_frame_data[-1][0]

    print(f"Lag Assumption : {lag_info['message']}")
    print(f"Rain moves     : FROM {dir_from} -> TO {dir_to} at {speed:.1f} km/h")
    print()

    # Simulate user departure at 7:00 PM IST today
    # NOTE: keep the last-radar timestamp fixed as the frame timestamp we observed at ~7 PM.
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))

    start = (25.361053, 81.403168)  # Kaushambi
    end   = (26.7606, 80.8893)      # Lucknow

    # Use live current departure time (latest live departure).
    departure_time = datetime.now(IST)

    simulated_lag = (departure_time - latest_ts).total_seconds() / 60.0 if latest_ts else lag_info['lag_mins']
    print(f"Simulated departure : {departure_time.strftime('%H:%M:%S IST')}")
    print(f"Radar available     : {latest_ts.strftime('%H:%M:%S IST') if latest_ts else 'Unknown'}")
    print(f"Simulated lag       : {simulated_lag:.0f} mins")
    print()

    start_in = is_within_radar(start[0], start[1])
    end_in   = is_within_radar(end[0], end[1])
    print(f"Start in radar: {start_in}")
    print(f"End   in radar: {end_in}")
    print()

    # We need the GIF as it was at 7 PM.
    # Best we can do: use current GIF but apply 7 PM lag manually.
    all_frame_data = get_all_frames()
    recent_frame_data = get_recent_frames(n=6)

    all_paths = [p for (p, _ts) in all_frame_data]
    clutter_mask_sim = build_clutter_mask(all_paths)

    dx, dy, dir_from, dir_to, speed = get_movement_vector(
        recent_frame_data, clutter_mask=clutter_mask_sim
    )
    print(f"Rain moves     : FROM {dir_from} -> TO {dir_to} at {speed:.1f} km/h")
    print()

    # Use the latest radar frame as base for shifting.
    latest_frame = recent_frame_data[-1][0]
    latest_ts = recent_frame_data[-1][1]
    if latest_ts:
        print("Using frame: %s (latest radar)" % latest_ts.strftime('%H:%M IST'))
    else:
        print("Using frame: latest (timestamp unknown)")

    # Generate waypoints
    waypoints_latlon = generate_waypoints(start, end, spacing_km=2.0)
    total_dist = haversine_km(start[0], start[1], end[0], end[1])

    print("Route   : Kaushambi -> Lucknow")
    print("Distance: %.1f km" % total_dist)
    print("Waypoints: %d" % len(waypoints_latlon))
    print()

    # Clamp waypoint pixels to the actual radar frame bounds.
    # OCR/georef calibration may put end-points slightly outside crop bounds,
    # which would otherwise crash prediction indexing.
    from PIL import Image
    img_latest = Image.open(latest_frame).convert("RGB")
    frame_w, frame_h = img_latest.size

    waypoints_pixels = []
    for lat, lon, eta in waypoints_latlon:
        px, py = latlon_to_pixel(lat, lon)
        px = max(0, min(frame_w - 1, px))
        py = max(0, min(frame_h - 1, py))
        waypoints_pixels.append((px, py, eta))

    max_eta = waypoints_latlon[-1][2]
    results = check_route_rain(
        waypoints_pixels, dx, dy,
        latest_frame,
        eta_minutes=max_eta,
        clutter_mask=clutter_mask_sim,
        lag_mins=simulated_lag
    )

    # Pass simulated lag for message text + internal shifting context.
    lag_info_sim = {"lag_mins": simulated_lag}
    enriched = enrich_results(
        results, waypoints_latlon,
        latest_frame, dx, dy,
        lag_info=lag_info_sim
    )

    print(f"{'WP':<5} {'ETA':>5} {'Status':<12} {'Intensity':<16} {'dBZ':>4}  Note")
    print("-" * 65)

    rain_count = 0
    for i, e in enumerate(enriched):
        in_bounds = is_within_radar(e["lat"], e["lon"])
        status = "RAIN" if e["rain_expected"] else "CLEAR"
        note = ""

        effective_eta = e["eta_mins"] + simulated_lag
        if not in_bounds:
            note = "WARNING: outside radar"
        elif effective_eta > 90:
            note = "WARNING: beyond range"

        if e["rain_expected"]:
            rain_count += 1

        print(f"WP{i+1:02d}  {e['eta_mins']:>4.0f}m  {status:<12} {e['label']:<16} {e['dbz']:>4.0f}  {note}")

    print()
    print("SUMMARY:")
    print("  Departure time  : %s" % departure_time.strftime('%H:%M:%S IST'))
    print("  Radar lag at departure: %.0f mins" % simulated_lag)
    print("  Total waypoints : %d" % len(enriched))
    print("  Rain waypoints  : %d" % rain_count)
    print("  Clear waypoints : %d" % (len(enriched) - rain_count))
    if rain_count > 0:
        first = next(e for e in enriched if e["rain_expected"])
        print("  First rain at   : ETA %.0f mins" % first["eta_mins"])
        print("  Intensity       : %s" % first["label"])
