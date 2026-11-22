from __future__ import annotations

import json
import math
import os
import random
import mechfight as mf


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def angle_diff(a, b):
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def point_in_poly(x, y, ring):
    inside = False
    n = len(ring) // 2
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[2 * i], ring[2 * i + 1]
        xj, yj = ring[2 * j], ring[2 * j + 1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


class ArenaMap:
    def __init__(self, map_dir, mech_radius=2.5):
        self.blocked = set()
        self.cover_points = []
        self.cell_size = 2.0
        self.pad = mech_radius + 0.5
        world_path = os.path.join(map_dir, "v2", "world.json")
        if not os.path.exists(world_path):
            return
        try:
            with open(world_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cs = self.cell_size
            pad = self.pad
            diag = pad * 0.7071
            offsets = (
                (0.0, 0.0), (pad, 0.0), (-pad, 0.0), (0.0, pad), (0.0, -pad),
                (diag, diag), (diag, -diag), (-diag, diag), (-diag, -diag),
            )
            for prism in data.get("prisms", []):
                base = float(prism.get("base", 0.0))
                top = float(prism.get("top", 10.0))
                if base > 8.8 or top < 1.2:
                    continue
                ring = prism.get("ring", [])
                if len(ring) < 6:
                    continue
                holes = prism.get("holes", [])
                xs = ring[0::2]
                ys = ring[1::2]
                cx_p = sum(xs) / len(xs)
                cy_p = sum(ys) / len(ys)
                self.cover_points.append((cx_p, cy_p))
                min_gx = int(math.floor((min(xs) - pad) / cs))
                max_gx = int(math.ceil((max(xs) + pad) / cs))
                min_gy = int(math.floor((min(ys) - pad) / cs))
                max_gy = int(math.ceil((max(ys) + pad) / cs))
                for gx in range(min_gx, max_gx + 1):
                    cx = (gx + 0.5) * cs
                    for gy in range(min_gy, max_gy + 1):
                        cy = (gy + 0.5) * cs
                        hit = False
                        for ox, oy in offsets:
                            px, py = cx + ox, cy + oy
                            if point_in_poly(px, py, ring):
                                if not any(point_in_poly(px, py, h) for h in holes):
                                    hit = True
                                    break
                        if hit:
                            self.blocked.add((gx, gy))
        except Exception:
            pass

    def is_blocked(self, x, y):
        if not self.blocked:
            return False
        gx = int(math.floor(x / self.cell_size))
        gy = int(math.floor(y / self.cell_size))
        return (gx, gy) in self.blocked

    def path_clearance(self, x, y, angle):
        if not self.blocked:
            return 1.0
        ca, sa = math.cos(angle), math.sin(angle)
