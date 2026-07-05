# Garaj Baras — bbox_mask.py
# Compact patch-mask storage.
#
# Patch/track masks used to be full-frame boolean arrays (527x525 ~ 0.28 MB
# each). With 100+ monsoon patches plus decay tracks that is tens of MB per
# radar for grids that are ~99% zeros. A BBoxMask stores only the tight crop
# around the blob plus its offset — pixel-for-pixel identical answers to every
# membership query, at ~1/1000th of the memory.

import numpy as np


class BBoxMask:
    """Boolean mask stored as a tight bounding-box crop + offset in the frame."""

    __slots__ = ("x0", "y0", "crop", "full_h", "full_w")

    def __init__(self, x0: int, y0: int, crop: np.ndarray, full_shape):
        self.x0 = int(x0)
        self.y0 = int(y0)
        self.crop = np.ascontiguousarray(crop.astype(bool))
        self.full_h = int(full_shape[0])
        self.full_w = int(full_shape[1])

    @classmethod
    def from_full(cls, full_mask: np.ndarray) -> "BBoxMask":
        """Build from a full-frame mask (bool or uint8)."""
        fm = full_mask.astype(bool)
        ys, xs = np.nonzero(fm)
        if len(ys) == 0:
            return cls(0, 0, np.zeros((0, 0), bool), fm.shape)
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        return cls(x0, y0, fm[y0:y1, x0:x1], fm.shape)

    @classmethod
    def from_labels(cls, labels: np.ndarray, comp_id: int, stats_row) -> "BBoxMask":
        """Build from cv2.connectedComponentsWithStats output (no full-frame temp)."""
        import cv2
        x0 = int(stats_row[cv2.CC_STAT_LEFT])
        y0 = int(stats_row[cv2.CC_STAT_TOP])
        w = int(stats_row[cv2.CC_STAT_WIDTH])
        h = int(stats_row[cv2.CC_STAT_HEIGHT])
        crop = labels[y0:y0 + h, x0:x0 + w] == comp_id
        return cls(x0, y0, crop, labels.shape)

    @property
    def shape(self):
        """Full-frame shape, for drop-in compatibility with ndarray masks."""
        return (self.full_h, self.full_w)

    @property
    def area(self) -> int:
        return int(self.crop.sum())

    def hit(self, px: int, py: int, radius: int = 0) -> bool:
        """
        True if any true pixel lies within `radius` (Chebyshev) of (px, py).
        Coordinates outside the frame simply can't hit — no clamping.
        """
        if self.crop.size == 0:
            return False
        # Window in crop-local coordinates
        lx0 = px - radius - self.x0
        lx1 = px + radius + 1 - self.x0
        ly0 = py - radius - self.y0
        ly1 = py + radius + 1 - self.y0
        ch, cw = self.crop.shape
        lx0 = max(0, lx0); ly0 = max(0, ly0)
        lx1 = min(cw, lx1); ly1 = min(ch, ly1)
        if lx0 >= lx1 or ly0 >= ly1:
            return False
        return bool(self.crop[ly0:ly1, lx0:lx1].any())

    def paint_into(self, full: np.ndarray) -> None:
        """OR this mask into a full-frame boolean array."""
        if self.crop.size == 0:
            return
        ch, cw = self.crop.shape
        full[self.y0:self.y0 + ch, self.x0:self.x0 + cw] |= self.crop

    def nonzero_full(self):
        """(ys, xs) in FULL-frame coordinates."""
        ys, xs = np.nonzero(self.crop)
        return ys + self.y0, xs + self.x0


def as_bbox_mask(mask, full_shape=None) -> "BBoxMask":
    """Accept either a BBoxMask or a full-frame ndarray (legacy callers/tests)."""
    if isinstance(mask, BBoxMask):
        return mask
    return BBoxMask.from_full(np.asarray(mask))
