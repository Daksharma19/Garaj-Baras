"""Lightweight, on-demand India radar composite.

This module deliberately consumes the already-produced latest frame for each
station.  It does not participate in, or retain, prediction state.
"""
from io import BytesIO
import math

import numpy as np
from PIL import Image

from optical_flow import isolate_rain

SOUTH, WEST, NORTH, EAST = 6.0, 68.0, 38.0, 98.0
WIDTH, HEIGHT = 1500, 1600
_MERC_SOUTH = math.log(math.tan(math.pi / 4 + math.radians(SOUTH) / 2))
_MERC_NORTH = math.log(math.tan(math.pi / 4 + math.radians(NORTH) / 2))
_MAX_MERCATOR_LAT = 85.05112878


def render_png(sources):
    """Return a transparent PNG with all available station echoes blended.

    ``sources`` is an iterable of (georef_module, image_path).  Pixels outside
    IMD's reflectivity palette remain transparent, letting the OSM base map
    show through.  Overlaps are weighted toward each radar's centre, avoiding
    stacked opaque images and making joins gradual.
    """
    rgb_sum = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
    weight_sum = np.zeros((HEIGHT, WIDTH), dtype=np.float32)

    for georef, frame_path in sources:
        if not frame_path:
            continue
        try:
            image = np.asarray(Image.open(frame_path).convert("RGB"))
            mask = isolate_rain(frame_path) > 0
        except Exception:
            continue
        # Sampling every other image pixel keeps this request modest on the
        # Render free tier; each sample paints a 2x2 destination footprint.
        ys, xs = np.nonzero(mask[::2, ::2])
        xs, ys = xs * 2, ys * 2
        for x, y in zip(xs.tolist(), ys.tolist()):
            try:
                lat, lon = georef.pixel_to_latlon(x, y)
            except Exception:
                continue
            # Individual radar georefs can drift a little beyond their
            # theoretical footprint near the crop edge; clamp before applying
            # Web Mercator math so one outlier cannot fail the whole mosaic.
            lat = max(-_MAX_MERCATOR_LAT, min(_MAX_MERCATOR_LAT, lat))
            ox = int((lon - WEST) / (EAST - WEST) * (WIDTH - 1))
            # Leaflet's ImageOverlay is positioned in Web Mercator; render in
            # that same vertical coordinate so the mosaic stays aligned while
            # users pan and zoom the OSM map.
            merc_lat = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
            oy = int((_MERC_NORTH - merc_lat) / (_MERC_NORTH - _MERC_SOUTH) * (HEIGHT - 1))
            if not (0 <= ox < WIDTH and 0 <= oy < HEIGHT):
                continue
            # Feather near the source edge.  The radial distance is an
            # approximation for legacy rectangular crops, but matches the
            # circular operational footprint and is stable across all stations.
            cx, cy = georef.latlon_to_pixel(georef.CENTER_LAT, georef.CENTER_LON)
            edge = max(1.0, min(georef.IMAGE_WIDTH, georef.IMAGE_HEIGHT) / 2.0)
            radial = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / edge
            weight = max(0.12, 1.0 - max(0.0, radial - 0.70) / 0.30)
            y2, x2 = min(HEIGHT, oy + 2), min(WIDTH, ox + 2)
            rgb_sum[oy:y2, ox:x2] += image[y, x].astype(np.float32) * weight
            weight_sum[oy:y2, ox:x2] += weight

    alpha = weight_sum > 0
    out = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    out[..., :3][alpha] = (rgb_sum[alpha] / weight_sum[alpha, None]).astype(np.uint8)
    out[..., 3] = np.where(alpha, 215, 0).astype(np.uint8)
    result = BytesIO()
    Image.fromarray(out, "RGBA").save(result, format="PNG", optimize=True)
    return result.getvalue()
