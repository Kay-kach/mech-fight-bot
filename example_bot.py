"""Survival-first mechfight/1 bot, v9 squad-avoidance strategy.

Key ideas vs. the previous version:
  * real navigation on the static map (A* over an inflated occupancy grid) instead of
    blind 16-way steering, so retreats and zone rotations go around buildings;
  * survival state machine: farm early, disengage and heal when hurt, only take
    favourable fights, use cover to break line of sight;
  * fire control that waits for the torso to actually line up (the old 0.22 rad gate
    misses most shots at range) and leads moving targets;
  * explicit laser charge/release management, reload timing, and weapon selection;
  * upgrades chosen from the live config by effect, not by hard-coded (and mostly
    non-existent) ids;
  * squad-aware survival: avoid voluntary fights, flee using all visible threats,
    seek cover, and counterattack only when cornered or a finish is highly favourable;
  * threat handling: dodge projectiles, break line of sight from a charging laser,
    short laser carriers with lightning when possible.

Standard library only.  Run with:  python3 -u bot.py
"""
from __future__ import annotations

import heapq
import json
import math
import os
import random
import time

import mechfight as mf

TAU = 2.0 * math.pi


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def angle_diff(a: float, b: float) -> float:
    """Shortest signed angle from b to a in [-pi, pi]."""
    return (a - b + math.pi) % TAU - math.pi


def point_in_poly(x: float, y: float, ring: list[float]) -> bool:
    """Ray-casting point-in-polygon for flat [x0, y0, x1, y1, ...] rings."""
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


class Terrain:
    """Bilinear sampler for arena/v2/terrain.i16 (int16 centimetres)."""

    def __init__(self, path: str, info: dict):
        import array
        self.nx = int(info.get("nx", 0))
        self.ny = int(info.get("ny", 0))
        self.minx = float(info.get("minX", 0.0))
        self.miny = float(info.get("minY", 0.0))
        self.step = max(float(info.get("step", 1.0)), 0.001)
        self.scale = float(info.get("scale", 0.01))
        self.data = array.array("h")
        with open(path, "rb") as f:
            self.data.fromfile(f, self.nx * self.ny)
        if len(self.data) < self.nx * self.ny:
            raise ValueError("terrain file is short")

    def z(self, x: float, y: float) -> float:
        if self.nx < 2 or self.ny < 2:
            return 0.0
        fx = (x - self.minx) / self.step
        fy = (y - self.miny) / self.step
        i = int(math.floor(fx))
        j = int(math.floor(fy))
        i = 0 if i < 0 else self.nx - 2 if i > self.nx - 2 else i
        j = 0 if j < 0 else self.ny - 2 if j > self.ny - 2 else j
        tx = clamp(fx - i, 0.0, 1.0)
        ty = clamp(fy - j, 0.0, 1.0)
        d = self.data
        nx = self.nx
        z00 = d[j * nx + i] * self.scale
        z10 = d[j * nx + i + 1] * self.scale
        z01 = d[(j + 1) * nx + i] * self.scale
        z11 = d[(j + 1) * nx + i + 1] * self.scale
        return (z00 * (1 - tx) + z10 * tx) * (1 - ty) + (z01 * (1 - tx) + z11 * tx) * ty


class MapModel:
    """Static map: a 2 m height-aware line-of-sight grid and a 5 m navigation grid."""

    def __init__(self, map_dir: str, mech_radius: float):
        self.ok = False
        self.terrain: Terrain | None = None
        self.los_cell = 3.0
        self.nav_cell = 5.0
        self.pad = max(2.7, mech_radius + 0.2)
        self.minx = self.miny = 0.0
        self.los_nx = self.los_ny = 0
        self.nav_nx = self.nav_ny = 0
        self.los_top: list[float] = []
        self.los_base: list[float] = []
        self.nav_blocked: bytearray = bytearray()
        world_path = os.path.join(map_dir, "v2", "world.json")
        terrain_path = os.path.join(map_dir, "v2", "terrain.i16")
        if not os.path.exists(world_path):
            mf.log(f"WARNING: map not found at {world_path}; navigation disabled")
            return
        try:
            with open(world_path, "r", encoding="utf-8") as f:
                world = json.load(f)
            b = world.get("bounds", {})
            self.minx = float(b.get("minX", -512.0))
            self.miny = float(b.get("minY", -333.0))
            maxx = float(b.get("maxX", 555.0))
            maxy = float(b.get("maxY", 389.0))
            try:
                self.terrain = Terrain(terrain_path, world.get("terrain", {}))
            except Exception as exc:
                mf.log(f"WARNING: terrain unavailable: {exc}")
                self.terrain = None
            # --- line-of-sight grid (raw solids, no inflation)
            self.los_nx = int(math.ceil((maxx - self.minx) / self.los_cell)) + 1
            self.los_ny = int(math.ceil((maxy - self.miny) / self.los_cell)) + 1
            n = self.los_nx * self.los_ny
            self.los_top = [-1e9] * n
            self.los_base = [1e9] * n
            for p in world.get("prisms", []):
                ring = p.get("ring") or []
                if len(ring) < 6:
                    continue
                holes = p.get("holes") or []
                base = float(p.get("base", 0.0))
                top = float(p.get("top", 10.0))
                if top <= -50.0:
                    continue
                xs = ring[0::2]
                ys = ring[1::2]
                i0 = max(0, int(math.floor((min(xs) - self.minx) / self.los_cell)))
                i1 = min(self.los_nx - 1, int(math.ceil((max(xs) - self.minx) / self.los_cell)))
                j0 = max(0, int(math.floor((min(ys) - self.miny) / self.los_cell)))
                j1 = min(self.los_ny - 1, int(math.ceil((max(ys) - self.miny) / self.los_cell)))
                for j in range(j0, j1 + 1):
                    cy = self.miny + (j + 0.5) * self.los_cell
                    row = j * self.los_nx
                    for i in range(i0, i1 + 1):
                        cx = self.minx + (i + 0.5) * self.los_cell
                        if not point_in_poly(cx, cy, ring):
                            continue
                        if any(point_in_poly(cx, cy, h) for h in holes):
                            continue
                        k = row + i
                        if base < self.los_base[k]:
                            self.los_base[k] = base
                        if top > self.los_top[k]:
                            self.los_top[k] = top
            # --- navigation grid (inflated by mech radius)
            self.nav_nx = int(math.ceil((maxx - self.minx) / self.nav_cell)) + 1
            self.nav_ny = int(math.ceil((maxy - self.miny) / self.nav_cell)) + 1
            self.nav_blocked = bytearray(self.nav_nx * self.nav_ny)
            pad = self.pad
            diag = pad * 0.70710678
            offsets = ((0.0, 0.0), (pad, 0.0), (-pad, 0.0), (0.0, pad), (0.0, -pad),
                       (diag, diag), (diag, -diag), (-diag, diag), (-diag, -diag))
            for p in world.get("prisms", []):
                ring = p.get("ring") or []
                if len(ring) < 6:
                    continue
                holes = p.get("holes") or []
                base = float(p.get("base", 0.0))
                top = float(p.get("top", 10.0))
                xs = ring[0::2]
                ys = ring[1::2]
                i0 = max(0, int(math.floor((min(xs) - pad - self.minx) / self.nav_cell)))
                i1 = min(self.nav_nx - 1, int(math.ceil((max(xs) + pad - self.minx) / self.nav_cell)))
                j0 = max(0, int(math.floor((min(ys) - pad - self.miny) / self.nav_cell)))
                j1 = min(self.nav_ny - 1, int(math.ceil((max(ys) + pad - self.miny) / self.nav_cell)))
                for j in range(j0, j1 + 1):
                    cy = self.miny + (j + 0.5) * self.nav_cell
                    row = j * self.nav_nx
                    for i in range(i0, i1 + 1):
                        k = row + i
                        if self.nav_blocked[k]:
                            continue
                        cx = self.minx + (i + 0.5) * self.nav_cell
                        gz = self.terrain.z(cx, cy) if self.terrain else 0.0
                        if not (base <= gz + 8.8 and top > gz + 1.2):
                            continue
                        hit = False
                        for ox, oy in offsets:
                            px, py = cx + ox, cy + oy
                            if point_in_poly(px, py, ring) and not any(point_in_poly(px, py, h) for h in holes):
                                hit = True
                                break
                        if hit:
                            self.nav_blocked[k] = 1
            # steep terrain is not walkable
            if self.terrain is not None:
                for j in range(1, self.nav_ny - 1):
                    cy = self.miny + (j + 0.5) * self.nav_cell
                    row = j * self.nav_nx
                    for i in range(1, self.nav_nx - 1):
                        if self.nav_blocked[row + i]:
                            continue
                        cx = self.minx + (i + 0.5) * self.nav_cell
                        z0 = self.terrain.z(cx, cy)
                        if abs(self.terrain.z(cx + self.nav_cell, cy) - z0) > 5.8 or \
                           abs(self.terrain.z(cx, cy + self.nav_cell) - z0) > 5.8:
                            self.nav_blocked[row + i] = 1
            self.height = maxy - self.miny
            self.width = maxx - self.minx
            # connected components of walkable cells: goals must be reachable,
            # not merely open (courtyards and walled pockets are open but isolated)
            n = self.nav_nx * self.nav_ny
            self.comp = [-1] * n
            cid = 0
            dirs8 = ((1, 0), (-1, 0), (0, 1), (0, -1),
                     (1, 1), (1, -1), (-1, 1), (-1, -1))
            for seed in range(n):
                if self.nav_blocked[seed] or self.comp[seed] >= 0:
                    continue
                self.comp[seed] = cid
                stack = [seed]
                while stack:
                    k = stack.pop()
                    i = k % self.nav_nx
                    j = k // self.nav_nx
                    for di, dj in dirs8:
                        ni = i + di
                        nj = j + dj
                        if ni < 0 or nj < 0 or ni >= self.nav_nx or nj >= self.nav_ny:
                            continue
                        nk = nj * self.nav_nx + ni
                        if self.nav_blocked[nk] or self.comp[nk] >= 0:
                            continue
                        if di and dj and (self.nav_blocked[j * self.nav_nx + ni] or
                                          self.nav_blocked[nj * self.nav_nx + i]):
                            continue
                        self.comp[nk] = cid
                        stack.append(nk)
                cid += 1
            self.comp_count = cid
            self.comp_sizes = [0] * cid
            for c in self.comp:
                if c >= 0:
                    self.comp_sizes[c] += 1
            self.ok = True
            mf.log(f"map: {self.los_nx}x{self.los_ny} LOS cells, "
                   f"{self.nav_nx}x{self.nav_ny} nav cells, "
                   f"{sum(self.nav_blocked)} blocked, {cid} components")
        except Exception as exc:
            mf.log(f"ERROR loading map: {exc}")
            self.ok = False

    # ---------- queries ----------
    def terrain_z(self, x: float, y: float) -> float:
        if self.terrain is None:
            return 0.0
        return self.terrain.z(x, y)

    def los(self, x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> bool:
        """True when the straight 3-D segment is not blocked by terrain or a prism."""
        if not self.ok:
            return True
        dx = x1 - x0
        dy = y1 - y0
        dz = z1 - z0
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist < 0.2:
            return True
        steps = max(2, int(dist / 2.4))
        for q in range(1, steps):
            t = q / steps
            x = x0 + dx * t
            y = y0 + dy * t
            z = z0 + dz * t
            if x < self.minx or y < self.miny or x > self.minx + self.width or y > self.miny + self.height:
                return False
            gz = self.terrain_z(x, y)
            if z < gz + 0.15:
                return False
            i = int((x - self.minx) / self.los_cell)
            j = int((y - self.miny) / self.los_cell)
            if i < 0 or j < 0 or i >= self.los_nx or j >= self.los_ny:
                return False
            k = j * self.los_nx + i
            if self.los_base[k] <= z <= self.los_top[k]:
                return False
        return True

    def nav_idx(self, x: float, y: float) -> int:
        i = int(math.floor((x - self.minx) / self.nav_cell))
        j = int(math.floor((y - self.miny) / self.nav_cell))
        if i < 0 or j < 0 or i >= self.nav_nx or j >= self.nav_ny:
            return -1
        return j * self.nav_nx + i

    def nav_blocked_xy(self, x: float, y: float) -> bool:
        if not self.ok:
            return False
        k = self.nav_idx(x, y)
        return k < 0 or self.nav_blocked[k] != 0

    def nav_clear(self, x0: float, y0: float, x1: float, y1: float, step: float = 1.5) -> bool:
        if not self.ok:
            return True
        d = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(d / step))
        for q in range(n + 1):
            t = q / n
            if self.nav_blocked_xy(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t):
                return False
        return True

    def nearest_open(self, x: float, y: float) -> tuple[float, float]:
        if not self.ok or not self.nav_blocked_xy(x, y):
            return x, y
        ci = int((x - self.minx) / self.nav_cell)
        cj = int((y - self.miny) / self.nav_cell)
        for r in range(1, 14):
            best = None
            best_d = 1e18
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    i = ci + di
                    j = cj + dj
                    if i < 0 or j < 0 or i >= self.nav_nx or j >= self.nav_ny:
                        continue
                    if self.nav_blocked[j * self.nav_nx + i]:
                        continue
                    px = self.minx + (i + 0.5) * self.nav_cell
                    py = self.miny + (j + 0.5) * self.nav_cell
                    d = (px - x) ** 2 + (py - y) ** 2
                    if d < best_d:
                        best_d = d
                        best = (px, py)
            if best is not None:
                return best
        return x, y

    def same_component(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        if not self.ok:
            return True
        k0 = self.nav_idx(x0, y0)
        k1 = self.nav_idx(x1, y1)
        if k0 < 0 or k1 < 0:
            return False
        c0 = self.comp[k0]
        return c0 >= 0 and c0 == self.comp[k1]

    def component_size(self, x: float, y: float) -> int:
        if not self.ok:
            return 1 << 30
        k = self.nav_idx(x, y)
        if k < 0:
            return 0
        c = self.comp[k]
        return 0 if c < 0 else self.comp_sizes[c]

    def nearest_reachable(self, x0: float, y0: float, gx: float, gy: float) -> tuple[float, float]:
        """Nearest open cell to the goal that shares the start's component."""
        if not self.ok:
            return gx, gy
        k0 = self.nav_idx(x0, y0)
        if k0 < 0 or self.comp[k0] < 0:
            return gx, gy
        cid = self.comp[k0]
        gi = int((gx - self.minx) / self.nav_cell)
        gj = int((gy - self.miny) / self.nav_cell)
        for r in range(0, 70):
            best = None
            best_d = 1e18
            # perimeter only: a full square scan gets expensive at large radii
            cells = []
            if r == 0:
                cells.append((0, 0))
            else:
                for di in range(-r, r + 1):
                    cells.append((di, -r))
                    cells.append((di, r))
                for dj in range(-r + 1, r):
                    cells.append((-r, dj))
                    cells.append((r, dj))
            for di, dj in cells:
                i = gi + di
                j = gj + dj
                if i < 0 or j < 0 or i >= self.nav_nx or j >= self.nav_ny:
                    continue
                k = j * self.nav_nx + i
                if self.comp[k] != cid:
                    continue
                px = self.minx + (i + 0.5) * self.nav_cell
                py = self.miny + (j + 0.5) * self.nav_cell
                d = (px - gx) ** 2 + (py - gy) ** 2
                if d < best_d:
                    best_d = d
                    best = (px, py)
            if best is not None:
                return best
        return gx, gy

    def astar(self, sx: float, sy: float, gx: float, gy: float,
              max_nodes: int = 12000, time_limit: float = 0.004) -> list[tuple[float, float]]:
        """Anytime A*; returns a (possibly partial) smoothed path.  Never raises."""
        if not self.ok:
            return [(gx, gy)]
        sx, sy = self.nearest_open(sx, sy)
        gx, gy = self.nearest_open(gx, gy)
        if not self.same_component(sx, sy, gx, gy):
            gx, gy = self.nearest_reachable(sx, sy, gx, gy)
        start = self.nav_idx(sx, sy)
        goal = self.nav_idx(gx, gy)
        if start < 0 or goal < 0:
            return []
        if not self.same_component(sx, sy, gx, gy):
            return []
        if start == goal:
            return [(gx, gy)]
        if self.nav_clear(sx, sy, gx, gy, 1.4):
            return [(gx, gy)]
        t0 = time.perf_counter()
        nx = self.nav_nx
        blocked = self.nav_blocked
        gi = goal % nx
        gj = goal // nx
        gscore = {start: 0.0}
        came: dict[int, int] = {}
        heap = [(0.0, 0.0, start)]
        closed = set()
        best_k = start
        best_h = 1e18
        nodes = 0
        dirs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.41421356), (1, -1, 1.41421356),
                (-1, 1, 1.41421356), (-1, -1, 1.41421356))

        def heur(k: int) -> float:
            i = k % nx
            j = k // nx
            dx = abs(i - gi)
            dy = abs(j - gj)
            return (dx + dy) + (1.41421356 - 2.0) * (dx if dx < dy else dy)

        found = False
        while heap:
            _f, g, k = heapq.heappop(heap)
            if k in closed:
                continue
            if k == goal:
                found = True
                break
            closed.add(k)
            nodes += 1
            if nodes > max_nodes or (nodes & 127 == 0 and time.perf_counter() - t0 > time_limit):
                break
            i = k % nx
            j = k // nx
            for di, dj, cost in dirs:
                ni = i + di
                nj = j + dj
                if ni < 0 or nj < 0 or ni >= nx or nj >= self.nav_ny:
                    continue
                nk = nj * nx + ni
                if blocked[nk]:
                    continue
                if di and dj and (blocked[j * nx + ni] or blocked[nj * nx + i]):
                    continue
                ng = g + cost
                old = gscore.get(nk)
                if old is not None and ng >= old:
                    continue
                gscore[nk] = ng
                came[nk] = k
                h = heur(nk)
                if h < best_h:
                    best_h = h
                    best_k = nk
                heapq.heappush(heap, (ng + h, ng, nk))
        target = goal if found else best_k
        if target == start:
            return []
        path = []
        k = target
        guard = 0
        while k != start and guard < 5000:
            i = k % nx
            j = k // nx
            path.append((self.minx + (i + 0.5) * self.nav_cell,
                         self.miny + (j + 0.5) * self.nav_cell))
            k = came.get(k, start)
            guard += 1
        path.reverse()
        if not path:
            return []
        # string pulling: skip waypoints we can see past
        out = []
        cx, cy = sx, sy
        idx = 0
        while idx < len(path):
            far = idx
            for q in range(min(len(path) - 1, idx + 30), idx - 1, -1):
                if self.nav_clear(cx, cy, path[q][0], path[q][1], 2.5):
                    far = q
                    break
            out.append(path[far])
            cx, cy = path[far]
            if far >= len(path) - 1:
                break
            idx = far + 1
        return out


class Navigator:
    def __init__(self, model: MapModel):
        self.map = model
        self.path: list[tuple[float, float]] = []
        self.goal: tuple[float, float] | None = None
        self.last_plan_tick = -9999
        self.last_plan_goal: tuple[float, float] | None = None
        self.failed = False
        self.fail_until = -9999

    def update(self, tick: int, x: float, y: float, gx: float, gy: float) -> None:
        d_goal = math.hypot(gx - x, gy - y)
        if d_goal < 3.0:
            self.path = []
            self.goal = (gx, gy)
            return
        moved = 1e9 if self.goal is None else math.hypot(gx - self.goal[0], gy - self.goal[1])
        stale = tick - self.last_plan_tick > 26
        empty = not self.path
        if self.failed and tick < self.fail_until and moved < 18.0:
            return
        if empty or stale or moved > 18.0:
            self.goal = (gx, gy)
            self.last_plan_goal = (gx, gy)
            self.last_plan_tick = tick
            self.path = self.map.astar(x, y, gx, gy)
            self.failed = not self.path
            if self.failed:
                self.fail_until = tick + 8
        while self.path and math.hypot(self.path[0][0] - x, self.path[0][1] - y) < 3.5:
            self.path.pop(0)

    def direction(self, x: float, y: float, gx: float, gy: float) -> tuple[float, float]:
        if self.path:
            wx, wy = self.path[0]
            if math.hypot(wx - x, wy - y) > 1.0:
                d = math.hypot(wx - x, wy - y)
                return (wx - x) / d, (wy - y) / d
        d = math.hypot(gx - x, gy - y)
        if d > 0.01:
            return (gx - x) / d, (gy - y) / d
        return 0.0, 0.0


class Bot:
    name = "survival-tactician-v9"

    def __init__(self, hello: mf.Hello):
        self.slot = int(hello.get("slot", 0))
        self.rng = random.Random(int(hello.get("seed", 0)) ^ 0x5EED)
        self.cfg = hello.get("config", {}) or {}
        self.mech_cfg = self.cfg.get("mech", {}) or {}
        self.weapons_cfg = self.cfg.get("weapons", {}) or {}
        self.vision_cfg = self.cfg.get("vision", {}) or {}
        self.loot_cfg = self.cfg.get("loot", {}) or {}
        self.cubes_cfg = self.cfg.get("cubes", {}) or {}
        self.drops_cfg = self.cfg.get("drops", {}) or {}
        self.upgrades_cfg = self.cfg.get("upgrades", []) or []
        self.reach = float(self.loot_cfg.get("pickup_radius", 4.0))
        self.cube_reach = float(self.cubes_cfg.get("pickup_radius", 3.5))
        self.radius = float(self.mech_cfg.get("radius", 2.5))
        self.height = float(self.mech_cfg.get("height", 9.0))
        self.muzzle_h = float(self.mech_cfg.get("muzzle_height", 7.0))
        self.eye_h = float(self.mech_cfg.get("eye_height", 8.0))
        self.turn_rate = float(self.mech_cfg.get("turn_rate", 4.0))
        self.pitch_rate = float(self.mech_cfg.get("pitch_rate", 3.0))
        self.min_pitch = float(self.mech_cfg.get("min_pitch", -0.7))
        self.max_pitch = float(self.mech_cfg.get("max_pitch", 1.3))
        self.regen_delay = float(self.mech_cfg.get("regen_delay", 5.0))

        map_dir = (hello.get("map", {}) or {}).get("dir", "/arena")
        self.map = MapModel(map_dir, self.radius)
        self.nav = Navigator(self.map)

        self.memory: dict[int, dict] = {}
        self.prev_hp: dict[int, float] = {}
        self.last_damage_tick = -9999
        self.last_shot_tick = -9999
        self.last_threat_bearing: float | None = None
        self.last_threat_tick = -9999
        # Sound/beam cues are directional sensor data, not enemy coordinates.
        # Use them to orient the vision cone without pretending to know a target position.
        self.last_sound_bearing: float | None = None
        self.last_sound_tick = -9999
        self.last_sound_kind: str | None = None
        self.last_sound_dist: str | None = None
        self.short_until = -9999
        self.cur_tick = 0
        self.force_face_yaw: float | None = None
        self.force_face_until = -9999

        self.state = "farm"
        self.state_since = 0
        self.target_id: int | None = None
        self.target_kind: str | None = None
        self.target_since = 0
        self.strafe_sign = 1.0
        self.strafe_flip = 0
        self.last_goal: tuple[float, float] | None = None
        self.stuck_pos = (0.0, 0.0)
        self.stuck_tick = -9999
        self.stuck_until = -9999
        self.stuck_dir = 0.0
        self.flee_goal: tuple[float, float] | None = None
        self.flee_threat: tuple[float, float] | None = None
        self.flee_until = -9999
        self.zone_goal_cache: tuple[float, float] | None = None
        self.zone_goal_key = None
        self.zone_goal_until = -9999

        self.laser_slot: int | None = None
        self.laser_charge_target: int | None = None
        self.laser_lost_tick = -9999
        self.laser_last_target_tick = -9999
        self.laser_release_hold = 0

        self.drop_goal: tuple[float, float] | None = None
        self.drop_goal_until = -9999

    # ------------------------------------------------------------------ helpers
    def _log(self, tick: int, msg: str) -> None:
        if tick % 200 == 0:
            mf.log(f"t={tick} {msg}")

    def _enemy_height(self, m: dict) -> float:
        kind = m.get("kind")
        if kind == "npc":
            return 1.75
        if kind == "crate":
            return 1.2
        cubes = float(m.get("cubes", 0.0) or 0.0)
        return 4.5 * (1.0 + 0.05 * cubes)

    def _enemy_radius(self, m: dict) -> float:
        if m.get("kind") == "npc":
            return 1.8
        if m.get("kind") == "crate":
            return 1.4
        cubes = float(m.get("cubes", 0.0) or 0.0)
        return self.radius * (1.0 + 0.05 * cubes)

    def _enemy_top_height(self, m: dict) -> float:
        """Approximate a point just below the top of the target's collision body."""
        if m.get("kind") == "npc":
            return 3.25
        if m.get("kind") == "crate":
            return 2.15
        cubes = float(m.get("cubes", 0.0) or 0.0)
        return max(0.5, 9.0 * (1.0 + 0.05 * cubes) - 0.5)

    def _weapon_stats(self, kind: str, w: dict | None) -> dict:
        if w and isinstance(w.get("stats"), dict):
            return w["stats"]
        return self.weapons_cfg.get(kind, {}) or {}

    def _mech_stats(self, me: dict) -> dict:
        s = me.get("stats")
        return s if isinstance(s, dict) else {}

    def _muzzle_h(self, me: dict) -> float:
        return float(self._mech_stats(me).get("muzzle_height", self.muzzle_h))

    def _eye_h(self, me: dict) -> float:
        return float(self._mech_stats(me).get("eye_height", self.eye_h))

    def _body_h(self, me: dict) -> float:
        return float(self._mech_stats(me).get("height", self.height))

    def _turn_rate(self, me: dict) -> float:
        return max(float(self._mech_stats(me).get("turn_rate", self.turn_rate)), 0.1)

    def _pitch_rate(self, me: dict) -> float:
        return max(float(self._mech_stats(me).get("pitch_rate", self.pitch_rate)), 0.1)

    def _weapon_range(self, kind: str, w: dict | None) -> float:
        base = self.weapons_cfg.get(kind, {}) or {}
        stats = self._weapon_stats(kind, w)
        return float(stats.get("range", base.get("range", 230.0)))

    def _held_kinds(self, me: dict) -> list[str]:
        return [w["kind"] for w in (me.get("weapons") or []) if w]

    def _slot_of_kind(self, me: dict, kind: str) -> int | None:
        for i, w in enumerate(me.get("weapons") or []):
            if w and w.get("kind") == kind:
                return i
        return None

    def _aim_vector(self, yaw: float, pitch: float) -> tuple[float, float, float]:
        cp = math.cos(pitch)
        return cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)

    def _target_center(self, m: dict) -> tuple[float, float, float]:
        p = m.get("pos") or [0.0, 0.0, 0.0]
        return float(p[0]), float(p[1]), float(p[2]) + float(m.get("_aim_height", self._enemy_height(m)))

    # ------------------------------------------------------------------ upgrades
    def _upgrade_value(self, choice: dict, me: dict) -> float:
        """Estimate marginal match value from the actual configured effect size."""
        cid = choice.get("id")
        defn = next((u for u in self.upgrades_cfg if u.get("id") == cid), None)
        if defn is None:
            # Defensive fallback for a future config shape; do not blindly prefer index 0.
            desc = (str(choice.get("name", "")) + " " + str(choice.get("desc", ""))).lower()
            if any(k in desc for k in ("armor", "health", "regen", "damage reduction")):
                return 5.0
            if any(k in desc for k in ("speed", "boost", "energy", "sensor", "vision")):
                return 3.5
            if any(k in desc for k in ("damage", "fire rate", "reload", "range")):
                return 3.0
            return 1.0

        held = set(self._held_kinds(me))
        score = 0.0
        for eff in defn.get("effects", []) or []:
            path = str(eff.get("path", ""))
            mul = float(eff.get("mul", 1.0) or 1.0)
            add = float(eff.get("add", 0.0) or 0.0)
            # Convert the configured change to a positive, scale-aware benefit.
            rel = abs(mul - 1.0)
            if path == "mech.hp":
                benefit = max(0.0, mul - 1.0) + max(0.0, add) / 100.0
                score += 6.0 + 28.0 * benefit if benefit > 0.0 else 0.0
            elif path == "mech.armor":
                benefit = max(0.0, mul - 1.0) + max(0.0, add) / 10.0
                score += 7.0 + 30.0 * benefit if benefit > 0.0 else 0.0
            elif path == "mech.regen_per_s":
                benefit = max(0.0, mul - 1.0) + max(0.0, add) / 10.0
                score += 6.0 + 24.0 * benefit if benefit > 0.0 else 0.0
            elif path == "mech.regen_delay":
                # Lower delay is beneficial; the sign of add/mul matters.
                benefit = max(0.0, 1.0 - mul) + max(0.0, -add) / 10.0
                score += 4.0 + 22.0 * benefit
            elif path in ("mech.walk_speed", "mech.boost_speed"):
                benefit = max(0.0, mul - 1.0) + max(0.0, add) / 10.0
                score += 5.0 + 22.0 * benefit if benefit > 0.0 else 0.0
            elif path in ("mech.turn_rate", "mech.pitch_rate"):
                benefit = max(0.0, mul - 1.0) + max(0.0, add) / 10.0
                score += 2.0 + 12.0 * benefit if benefit > 0.0 else 0.0
            elif path in ("mech.energy", "mech.energy_regen"):
                benefit = max(0.0, mul - 1.0) + max(0.0, add) / 10.0
                score += 2.5 + 12.0 * benefit if benefit > 0.0 else 0.0
            elif path in ("mech.boost_drain", "mech.jet_drain"):
                benefit = max(0.0, 1.0 - mul) + max(0.0, -add) / 10.0
                score += 2.0 + 12.0 * benefit
            elif path in ("mech.near_range_bonus", "mech.vision_bonus", "mech.hearing"):
                benefit = max(0.0, mul - 1.0) + max(0.0, add) / 10.0
                score += 2.0 + 10.0 * benefit if benefit > 0.0 else 0.0
            elif path.startswith("weapons."):
                parts = path.split(".")
                kind = parts[1] if len(parts) > 1 else ""
                if kind not in held:
                    continue
                stat = parts[-1]
                base = {"damage": 3.2, "cooldown": 2.8, "charge": 2.6,
                        "speed": 1.8, "range": 1.7, "mag": 1.5,
                        "reload": 1.5, "energy": 2.0, "pierce": 1.2,
                        "splash": 1.4, "splash_radius": 1.2}.get(stat, 1.0)
                # Lower cooldown/reload/energy costs are good; other stats generally
                # benefit from increases. Use magnitude, not just the stat name.
                if stat in ("cooldown", "reload", "energy"):
                    benefit = max(0.0, 1.0 - mul) + max(0.0, -add) / 10.0
                else:
                    benefit = max(0.0, mul - 1.0) + max(0.0, add) / 100.0
                if benefit > 0.0:
                    score += base + 18.0 * benefit
        return score

    def _choose_upgrade(self, offer: dict, me: dict) -> int:
        best_i = 0
        best = -1e9
        for i, c in enumerate(offer.get("choices", []) or []):
            v = self._upgrade_value(c, me)
            if v > best:
                best = v
                best_i = i
        return best_i

    # ------------------------------------------------------------------ tracking
    def _update_tracks(self, obs: dict) -> None:
        tick = int(obs.get("tick", 0))
        for m in obs.get("mechs", []) or []:
            if m.get("kind") not in ("mech", "npc"):
                continue
            mid = int(m.get("id", -1))
            if mid < 0:
                continue
            prev = self.memory.get(mid)
            raw_v = m.get("vel") or [0.0, 0.0, 0.0]
            if prev and "vel" in prev:
                pv = prev["vel"]
                sv = [0.62 * float(raw_v[i]) + 0.38 * float(pv[i]) for i in range(3)]
            else:
                sv = [float(raw_v[i]) for i in range(3)]
            hp = float(m.get("hp", 0.0) or 0.0)
            if prev and "hp" in prev and hp < float(prev["hp"]) - 1.0:
                prev["damaged_tick"] = tick
            self.memory[mid] = {
                "id": mid,
                "kind": m.get("kind"),
                "pos": [float(v) for v in (m.get("pos") or [0.0, 0.0, 0.0])],
                "vel": sv,
                "hp": hp,
                "max_hp": float(m.get("max_hp", 1400.0) or 1400.0),
                "weapons": list(m.get("weapons") or []),
                "charging": bool(m.get("charging", False)),
                "cubes": float(m.get("cubes", 0.0) or 0.0),
                "aim": [float(v) for v in (m.get("aim") or [0.0, 0.0])[:2]],
                "tick": tick,
                "damaged_tick": prev.get("damaged_tick", -9999) if prev else -9999,
                "seen": True,
            }
            self.prev_hp[mid] = hp
        # memory expiry
        if tick % 40 == 0:
            mem_s = float(self.vision_cfg.get("memory_s", 5.0))
            for mid in list(self.memory.keys()):
                if tick - int(self.memory[mid].get("tick", tick)) > int(mem_s * 20) + 10:
                    self.memory.pop(mid, None)

    def _visible_enemies(self, obs: dict, me: dict) -> list[dict]:
        out = []
        for m in obs.get("mechs", []) or []:
            if m.get("kind") != "mech":
                continue
            mid = int(m.get("id", -1))
            rec = self.memory.get(mid)
            if rec is None:
                continue
            d = math.dist(rec["pos"], me.get("pos") or [0.0, 0.0, 0.0])
            out.append({"m": m, "id": mid, "dist": d, "rec": rec})
        out.sort(key=lambda e: e["dist"])
        return out

    def _los_to(self, me: dict, m: dict) -> bool:
        p = me.get("pos") or [0.0, 0.0, 0.0]
        q = m.get("pos") or [0.0, 0.0, 0.0]
        h = self._enemy_height(m)
        return self.map.los(float(p[0]), float(p[1]), float(p[2]) + self._eye_h(me),
                            float(q[0]), float(q[1]), float(q[2]) + h)

    def _aiming_at_us(self, me: dict, m: dict) -> bool:
        try:
            p = me.get("pos") or [0.0, 0.0, 0.0]
            q = m.get("pos") or [0.0, 0.0, 0.0]
            bearing = math.atan2(float(p[1]) - float(q[1]), float(p[0]) - float(q[0]))
            aim = m.get("aim") or [0.0, 0.0]
            return abs(angle_diff(bearing, float(aim[0]))) < 0.30
        except Exception:
            return False

    # ------------------------------------------------------------------ aiming
    def _solve_aim(self, kind: str, me: dict, target: dict, w: dict | None = None) -> list[float]:
        p = me.get("pos") or [0.0, 0.0, 0.0]
        q = target.get("pos") or [0.0, 0.0, 0.0]
        v = target.get("vel") or [0.0, 0.0, 0.0]
        sx = float(p[0])
        sy = float(p[1])
        sz = float(p[2]) + self._muzzle_h(me)
        tx = float(q[0])
        ty = float(q[1])
        tz = float(q[2]) + float(target.get("_aim_height", self._enemy_height(target)))
        vx = float(v[0])
        vy = float(v[1])
        vz = float(v[2])
        if kind in ("laser", "lightning"):
            dx = tx - sx
            dy = ty - sy
            dz = tz - sz
            yaw = math.atan2(dy, dx)
            pitch = math.atan2(dz, max(math.hypot(dx, dy), 0.01))
            return [yaw, clamp(pitch, self.min_pitch, self.max_pitch)]
        stats = self._weapon_stats(kind, w)
        speed = max(float(stats.get("speed", 252.0)), 1.0)
        if kind == "gun":
            t_flight = max(0.0, math.hypot(tx - sx, ty - sy) - 3.0) / speed
            dx = dy = dz = 0.0
            for _ in range(4):
                ax = tx + vx * t_flight
                ay = ty + vy * t_flight
                az = tz + vz * t_flight * 0.5
                dx = ax - sx
                dy = ay - sy
                dz = az - sz
                d = math.sqrt(dx * dx + dy * dy + dz * dz)
                t_flight = max(0.0, d - 3.0) / speed
            yaw = math.atan2(dy, dx)
            pitch = math.atan2(dz, max(math.hypot(dx, dy), 0.01))
            return [yaw, clamp(pitch, self.min_pitch, self.max_pitch)]
        if kind == "mortar":
            base = self.weapons_cfg.get("mortar", {}) or {}
            tube = float(base.get("tube_offset", 2.5)) * (1.0 + 0.05 * float(me.get("cubes", 0.0) or 0.0))
            sz = float(p[2]) + self._muzzle_h(me) + tube
            g = max(float(stats.get("gravity", base.get("gravity", 22.0))), 0.1)
            v2 = speed * speed
            horiz = max(math.hypot(tx - sx, ty - sy) - 3.0, 0.5)
            t_flight = horiz / max(speed * 0.7, 1.0)
            pitch = 0.6
            px, py, pz = tx, ty, tz
            for _ in range(4):
                px = tx + vx * t_flight
                py = ty + vy * t_flight
                pz = tz + vz * t_flight
                dx = px - sx
                dy = py - sy
                dz = pz - sz
                horiz = max(math.hypot(dx, dy) - 3.0, 0.5)
                disc = v2 * v2 - g * (g * horiz * horiz + 2.0 * dz * v2)
                if disc <= 0.0:
                    pitch = 0.75
                    t_flight = horiz / max(speed * 0.7, 1.0)
                    continue
                pitch = math.atan((v2 - math.sqrt(disc)) / (g * horiz))
                cos_p = max(math.cos(pitch), 0.2)
                t_flight = horiz / (speed * cos_p)
            yaw = math.atan2(py - sy, px - sx)
            return [yaw, clamp(pitch, self.min_pitch, self.max_pitch)]
        dx = tx - sx
        dy = ty - sy
        dz = tz - sz
        return [math.atan2(dy, dx), math.atan2(dz, max(math.hypot(dx, dy), 0.01))]

    def _mortar_arc_clear(self, me: dict, target: dict, aim: list[float], w: dict | None = None) -> bool:
        """Check the actual ballistic arc in short 3-D segments, not direct LOS."""
        if not self.map.ok:
            return True
        p = me.get("pos") or [0.0, 0.0, 0.0]
        q = target.get("pos") or [0.0, 0.0, 0.0]
        stats = self._weapon_stats("mortar", w)
        base = self.weapons_cfg.get("mortar", {}) or {}
        speed = max(float(stats.get("speed", base.get("speed", 72.0))), 1.0)
        gravity = max(float(stats.get("gravity", base.get("gravity", 22.0))), 0.1)
        cubes = float(me.get("cubes", 0.0) or 0.0)
        tube = float(base.get("tube_offset", 2.5)) * (1.0 + 0.05 * cubes)
        yaw, pitch = float(aim[0]), float(aim[1])
        cp = math.cos(pitch)
        vx = speed * cp * math.cos(yaw)
        vy = speed * cp * math.sin(yaw)
        vz = speed * math.sin(pitch)
        # Shell origin is three metres along the commanded barrel direction.
        x0 = float(p[0]) + 3.0 * cp * math.cos(yaw)
        y0 = float(p[1]) + 3.0 * cp * math.sin(yaw)
        z0 = float(p[2]) + self._muzzle_h(me) + tube + 3.0 * math.sin(pitch)
        target_v = target.get("vel") or [0.0, 0.0, 0.0]
        tx0, ty0 = float(q[0]), float(q[1])
        tvx, tvy = float(target_v[0]), float(target_v[1])
        if cp <= 0.05:
            return False
        flight = math.hypot(tx0 - x0, ty0 - y0) / max(speed * cp, 1.0)
        for _ in range(3):
            tx = tx0 + tvx * flight
            ty = ty0 + tvy * flight
            flight = math.hypot(tx - x0, ty - y0) / max(speed * cp, 1.0)
        if flight <= 0.0 or flight > 5.0:
            return False
        steps = max(4, int(math.ceil(flight * speed / 2.0)))
        prev = (x0, y0, z0)
        for i in range(1, steps + 1):
            t = flight * i / steps
            cur = (x0 + vx * t, y0 + vy * t, z0 + vz * t - 0.5 * gravity * t * t)
            if not self.map.los(prev[0], prev[1], prev[2], cur[0], cur[1], cur[2]):
                return False
            prev = cur
        return True

    def _predicted_aim_error(self, me: dict, aim: list[float]) -> tuple[float, float]:
        # Use the *current* torso aim: the trigger may be sampled before the next
        # slew step, so do not credit the torso with turning onto the target.
        cur = me.get("aim") or [0.0, 0.0]
        yaw = float(cur[0])
        pitch = float(cur[1]) if len(cur) > 1 else 0.0
        return abs(angle_diff(float(aim[0]), yaw)), abs(float(aim[1]) - pitch)

    def _aim_ok(self, kind: str, me: dict, target: dict, aim: list[float], w: dict | None = None) -> bool:
        yaw_err, pitch_err = self._predicted_aim_error(me, aim)
        dist = math.dist(me.get("pos") or [0.0, 0.0, 0.0], target.get("pos") or [0.0, 0.0, 0.0])
        ang = math.atan2(self._enemy_radius(target), max(dist, 1.0))
        stats = self._weapon_stats(kind, w)
        if kind == "gun":
            tol = max(0.009, 1.05 * ang) + math.radians(float(stats.get("spread_deg", 0.6)))
            return yaw_err <= tol and pitch_err <= tol
        if kind == "laser":
            tol = max(0.010, 0.82 * ang)
            return yaw_err <= tol and pitch_err <= tol
        if kind == "lightning":
            cone = math.radians(float(stats.get("cone_deg", 30.0)))
            return yaw_err <= max(0.05, 0.85 * cone)
        if kind == "mortar":
            splash = float(stats.get("splash_radius", 7.0))
            tol = max(0.022, 0.55 * math.atan2(splash, max(dist, 1.0)))
            return yaw_err <= tol and pitch_err <= tol * 1.3
        return False

    # ------------------------------------------------------------------ weapons
    def _choose_weapon(self, me: dict, target: dict, dist: float, state: str) -> tuple[int | None, str | None]:
        weapons = me.get("weapons") or []
        energy = float(me.get("energy", 0.0) or 0.0)
        target_kind = target.get("kind")
        target_hp = float(target.get("hp", 1000.0) or 1000.0)
        best_slot = None
        best_kind = None
        best_score = -1e9
        for i, w in enumerate(weapons):
            if not w:
                continue
            kind = w.get("kind")
            stats = self._weapon_stats(kind, w)
            wrange = self._weapon_range(kind, w)
            score = -1e9
            if kind == "gun":
                if float(w.get("reload", 0.0) or 0.0) > 0.05 or int(w.get("ammo", 0) or 0) <= 0:
                    score = -1e9
                elif dist > wrange:
                    score = -1e9
                else:
                    score = 1.0
                    if target_kind == "crate":
                        score = 2.0
                    elif target_kind == "npc":
                        score = 1.5
                    elif dist < 120:
                        score += 0.15
            elif kind == "laser":
                charge = float(w.get("charge", 0.0) or 0.0)
                if self.cur_tick < self.short_until:
                    score = -1e9
                elif float(w.get("cooldown", 0.0) or 0.0) > 0.10:
                    score = -1e9
                elif dist > wrange:
                    score = -1e9
                elif charge > 0.0:
                    score = 8.0
                elif target_kind == "crate":
                    score = -1e9
                else:
                    score = 1.15
                    if "mortar" in (target.get("weapons") or []):
                        score += 0.7
                    if target_hp < 260:
                        score += 0.4
                    if dist > 90:
                        score += 0.22
            elif kind == "lightning":
                if dist > wrange or energy < 6.0:
                    score = -1e9
                elif target_kind == "crate":
                    score = 0.4
                else:
                    score = 1.2
                    if "laser" in (target.get("weapons") or []):
                        score += 0.8
                    if dist < 40:
                        score += 0.4
            elif kind == "mortar":
                if dist < 22.0 or dist > wrange or float(w.get("cooldown", 0.0) or 0.0) > 0.10:
                    score = -1e9
                elif target_kind in ("crate", "npc"):
                    score = 0.2
                else:
                    speed = math.hypot((target.get("vel") or [0.0, 0.0])[0],
                                       (target.get("vel") or [0.0, 0.0])[1])
                    score = 0.65 if speed < 3.0 else 0.25
                    if dist > 100:
                        score += 0.15
                    # Mortar is the only weapon in this kit designed to arc over cover.
                    if not target.get("direct_los", True):
                        score += 1.4
                    if "lightning" in (target.get("weapons") or []):
                        score += 0.45
            if score > best_score:
                best_score = score
                best_slot = i
                best_kind = kind
        return best_slot, best_kind

    def _laser_ready(self, w: dict) -> bool:
        return (float(w.get("charge", 0.0) or 0.0) <= 0.0 and
                float(w.get("cooldown", 0.0) or 0.0) <= 0.10)

    def _desired_laser_charge(self, w: dict, target_hp: float) -> float:
        stats = self._weapon_stats("laser", w)
        full = max(float(stats.get("charge", 0.75)), 0.05)
        dmg = max(float(stats.get("damage", 315.0)), 1.0)
        min_frac = float(stats.get("min_frac", 0.35))
        min_charge = float(stats.get("min_charge", 0.15))
        if target_hp <= 0:
            return full
        frac = clamp(target_hp / dmg, min_frac, 1.0)
        t = min_charge + (full - min_charge) * (frac - min_frac) / max(1.0 - min_frac, 1e-6)
        return clamp(t + 0.02, min_charge, full)

    # ------------------------------------------------------------------ loot
    def _pickup_preference(self, kind: str) -> float:
        return {"laser": 3.0, "lightning": 3.1, "mortar": 1.2, "gun": 1.8}.get(kind, 0.0)

    def _desired_pickup(self, obs: dict, me: dict, me_pos: list[float]) -> dict | None:
        pickups = obs.get("pickups") or []
        weapons = me.get("weapons") or [None, None]
        held = self._held_kinds(me)
        empty = any(w is None for w in weapons)
        max_d = 150.0 if empty else 85.0
        best = None
        best_score = -1e9
        for p in pickups:
            kind = p.get("kind")
            if not kind or kind in held:
                continue
            d = math.dist(p.get("pos", [0.0, 0.0, 0.0])[:2], me_pos[:2])
            if d > max_d:
                continue
            if not self.map.same_component(me_pos[0], me_pos[1], p["pos"][0], p["pos"][1]):
                continue
            if abs(float(p["pos"][2]) - me_pos[2]) > 4.5:
                continue
            score = self._pickup_preference(kind) - d / max_d
            if kind == "mortar" and "gun" in held:
                score -= 0.8
            if empty:
                score += 1.0
            if best_score < score:
                best_score = score
                best = p
        return best

    # ------------------------------------------------------------------ zone
    def _zone_goal(self, obs: dict, me: dict, threats: list[dict]) -> tuple[float, float]:
        z = obs.get("zone") or {}
        cur = z.get("center") or [0.0, 0.0]
        cr = float(z.get("radius", 560.0))
        nxt = z.get("next_center") or cur
        nr = float(z.get("next_radius", cr))
        x, y = (me.get("pos") or [0.0, 0.0, 0.0])[:2]
        key = (int(float(nxt[0]) / 15.0), int(float(nxt[1]) / 15.0), int(nr / 10.0),
               int(float(cur[0]) / 15.0), int(float(cur[1]) / 15.0), int(cr / 10.0),
               int(threats[0]["rec"]["pos"][0] / 25.0) if threats else 0,
               int(threats[0]["rec"]["pos"][1] / 25.0) if threats else 0)
        if self.zone_goal_cache is not None and self.cur_tick < self.zone_goal_until and key == self.zone_goal_key:
            return self.zone_goal_cache
        # target a point inside the next circle, not its crowded middle
        radius = max(10.0, min(nr * 0.62, nr - 8.0)) if nr > 20 else max(2.0, nr * 0.4)
        best = (x, y)
        best_score = -1e18
        base_ang = math.atan2(y - float(nxt[1]), x - float(nxt[0]))
        for k in range(16):
            ang = base_ang + k * (TAU / 16.0)
            px = float(nxt[0]) + math.cos(ang) * radius
            py = float(nxt[1]) + math.sin(ang) * radius
            if self.map.nav_blocked_xy(px, py):
                continue
            if math.hypot(px - float(cur[0]), py - float(cur[1])) > cr - 6.0:
                continue
            if not self.map.same_component(x, y, px, py):
                continue
            score = 0.0
            # stay away from threats
            for th in threats[:10]:
                tp = th["rec"].get("pos") or [0.0, 0.0, 0.0]
                d = math.hypot(px - float(tp[0]), py - float(tp[1]))
                score += min(d, 160.0) * 0.075
                if d < 42.0:
                    score -= (42.0 - d) * 0.35
                if not self.map.los(float(tp[0]), float(tp[1]), float(tp[2]) + 4.0,
                                    px, py, self.map.terrain_z(px, py) + 4.0):
                    score += 3.0
            # prefer a short path
            score -= math.hypot(px - x, py - y) * 0.01
            if score > best_score:
                best_score = score
                best = (px, py)
        if best == (x, y):
            # no reachable point on the preferred ring: walk as far toward the
            # next centre as our component allows
            for frac in (0.75, 0.55, 0.35, 0.15):
                px = x + (float(nxt[0]) - x) * frac
                py = y + (float(nxt[1]) - y) * frac
                if self.map.nav_blocked_xy(px, py):
                    continue
                if math.hypot(px - float(cur[0]), py - float(cur[1])) > cr - 3.0:
                    continue
                if self.map.same_component(x, y, px, py):
                    best = (px, py)
                    break
        if best == (x, y) and math.hypot(x - float(cur[0]), y - float(cur[1])) > cr - 15.0:
            # no local escape point: at least aim at the next circle so the
            # fallback steering pushes against the wall instead of standing still
            best = (float(nxt[0]), float(nxt[1]))
        self.zone_goal_cache = best
        self.zone_goal_key = key
        self.zone_goal_until = self.cur_tick + 10
        return best

    # ------------------------------------------------------------------ flee
    def _flee_goal(self, obs: dict, me: dict, threats: list[dict]) -> tuple[float, float]:
        """Choose an escape point that accounts for the whole hunting squad.

        The house bots share vision and converge on a weak player. Escaping directly
        away from only the nearest mech can therefore run straight toward the rest.
        Score a broad set of reachable points for separation from every threat and
        for breaking several enemy lines of sight at once.
        """
        x, y, z = (me.get("pos") or [0.0, 0.0, 0.0])
        if not threats:
            return float(x), float(y)
        zone = obs.get("zone") or {}
        zc = zone.get("center") or [0.0, 0.0]
        zr = float(zone.get("radius", 560.0))
        # Reuse a goal briefly only while the local threat picture is essentially stable.
        signature = tuple(sorted((int(e.get("id", -1)), int(e.get("dist", 9999) / 20)) for e in threats[:6]))
        if (self.flee_goal is not None and self.cur_tick < self.flee_until and
                getattr(self, "flee_signature", None) == signature and
                math.hypot(x - self.flee_goal[0], y - self.flee_goal[1]) > 10.0):
            return self.flee_goal

        # Threats are sorted by distance. Weight close opponents much more heavily,
        # but retain the rest of the visible squad in the score.
        threat_data = []
        for e in threats[:10]:
            rec = e.get("rec") or {}
            tp = rec.get("pos") or [0.0, 0.0, 0.0]
            tx, ty = float(tp[0]), float(tp[1])
            d = math.hypot(tx - x, ty - y)
            weight = 1.0 / max(18.0, d * 0.55)
            threat_data.append((tx, ty, float(tp[2]) + 4.0, d, weight, rec))

        best = (float(x), float(y))
        best_score = -1e18
        base = math.atan2(y - threat_data[0][1], x - threat_data[0][0])
        # Full-circle sampling matters: the safest route may be sideways, not away
        # from the nearest pursuer, when the squad is spread around the player.
        for k in range(32):
            ang = base + k * (TAU / 32.0)
            for dist in (28.0, 48.0, 72.0, 100.0):
                px = x + math.cos(ang) * dist
                py = y + math.sin(ang) * dist
                if self.map.nav_blocked_xy(px, py):
                    continue
                if not self.map.nav_clear(x, y, px, py, 2.0):
                    continue
                if not self.map.same_component(x, y, px, py):
                    continue
                # Stay safely inside the present circle while there is still time
                # to rotate. Outside-zone handling is owned by the rotate state.
                if math.hypot(px - float(zc[0]), py - float(zc[1])) > max(2.0, zr - 7.0):
                    continue

                dists = [math.hypot(px - tx, py - ty) for tx, ty, _, _, _, _ in threat_data]
                nearest = min(dists) if dists else 999.0
                # Min-distance dominates: do not trade a safe gap to one enemy for
                # a tiny gap to another. The weighted sum rewards distance from the pack.
                score = min(nearest, 180.0) * 0.085
                score += sum(min(dd, 180.0) * wt for dd, (_, _, _, _, wt, _) in zip(dists, threat_data)) * 1.8
                if nearest < 38.0:
                    score -= (38.0 - nearest) * 0.65
                if nearest < 22.0:
                    score -= 18.0

                # Cover is especially valuable against the squad's shared targeting.
                blocked = 0
                exposed = 0
                for tx, ty, tz, _, _, _ in threat_data:
                    if self.map.los(tx, ty, tz, px, py, self.map.terrain_z(px, py) + 4.0):
                        exposed += 1
                    else:
                        blocked += 1
                score += blocked * 2.8
                score -= exposed * 0.7

                # Keep a route toward the safe-zone interior; don't flee to the rim.
                radial = math.hypot(px - float(zc[0]), py - float(zc[1]))
                score -= radial / max(zr, 1.0) * 2.0
                score -= dist * 0.008
                if self.map.ok and (px < self.map.minx + 28 or px > self.map.minx + self.map.width - 28 or \
                                    py < self.map.miny + 28 or py > self.map.miny + self.map.height - 28):
                    score -= 2.5
                # A recent hit bearing is a weak extra cue, not a fake enemy location.
                if self.last_threat_bearing is not None and self.cur_tick - self.last_threat_tick < 50:
                    away = self.last_threat_bearing + math.pi
                    score += math.cos(ang - away) * 0.9
                if score > best_score:
                    best_score = score
                    best = (px, py)

        self.flee_goal = best
        self.flee_signature = signature
        self.flee_until = self.cur_tick + 7
        return best

    # ------------------------------------------------------------------ dodge
    def _dodge_vector(self, obs: dict, me: dict) -> tuple[float, float]:
        """Choose a navigable dodge direction by predicted projectile clearance."""
        pos = me.get("pos") or [0.0, 0.0, 0.0]
        vel = me.get("vel") or [0.0, 0.0, 0.0]
        mx, my, mz = float(pos[0]), float(pos[1]), float(pos[2])
        vx, vy = float(vel[0]), float(vel[1])
        zc = mz + self._body_h(me) * 0.5
        energy = float(me.get("energy", 0.0) or 0.0)
        stats = self._mech_stats(me)
        walk = max(float(stats.get("walk_speed", 11.7)), 1.0)
        speed = max(float(stats.get("boost_speed", 19.5)), walk) if energy > 25.0 else walk
        accel = max(float(self.mech_cfg.get("ground_accel", 58.5)), 5.0)
        tau = max(speed / accel, 0.06)
        gravity = max(float((self.weapons_cfg.get("mortar", {}) or {}).get("gravity", 22.0)), 0.1)
        times = tuple(i * 0.1 for i in range(1, 15))
        incoming = []

        # Keep only projectiles that would pass close to our current trajectory.
        for p in obs.get("projectiles", []) or []:
            try:
                pp = p.get("pos") or [0.0, 0.0, 0.0]
                pv = p.get("vel") or [0.0, 0.0, 0.0]
                px, py, pz = float(pp[0]), float(pp[1]), float(pp[2])
                pvx, pvy, pvz = float(pv[0]), float(pv[1]), float(pv[2])
                g = gravity if p.get("kind") == "mortar" else 0.0
                trajectory = []
                base_miss = 1e9
                for t in times:
                    qx = px + pvx * t
                    qy = py + pvy * t
                    qz = pz + pvz * t - 0.5 * g * t * t
                    trajectory.append((t, qx, qy, qz))
                    ex = qx - (mx + vx * t)
                    ey = qy - (my + vy * t)
                    ez = qz - zc
                    base_miss = min(base_miss, ex * ex + ey * ey + ez * ez)
                if base_miss < 7.5 * 7.5:
                    incoming.append(trajectory)
            except (TypeError, ValueError, IndexError):
                continue
        if not incoming:
            return 0.0, 0.0

        best_dir = (0.0, 0.0)
        best_score = -1e18
        current_heading = math.atan2(vy, vx) if math.hypot(vx, vy) > 1.0 else 0.0
        for k in range(16):
            ang = k * (TAU / 16.0)
            dx, dy = math.cos(ang), math.sin(ang)
            # Reject dodges that immediately run into a wall or building.
            if not self.map.nav_clear(mx, my, mx + dx * 8.0, my + dy * 8.0, 1.5):
                continue
            target_vx, target_vy = dx * speed, dy * speed
            min_sep2 = 1e18
            for trajectory in incoming:
                for t, qx, qy, qz in trajectory:
                    decay = math.exp(-t / tau)
                    ex = mx + target_vx * t + (vx - target_vx) * tau * (1.0 - decay)
                    ey = my + target_vy * t + (vy - target_vy) * tau * (1.0 - decay)
                    rx, ry, rz = qx - ex, qy - ey, qz - zc
                    d2 = rx * rx + ry * ry + rz * rz
                    if d2 < min_sep2:
                        min_sep2 = d2
            # Small continuity preference avoids needless 180-degree reversals
            # when two directions provide effectively equal projectile clearance.
            continuity = math.cos(ang - current_heading) * 0.18
            score = min_sep2 + continuity
            if score > best_score:
                best_score = score
                best_dir = (dx, dy)
        return best_dir

    # ------------------------------------------------------------------ main
    def act(self, obs: mf.Observation) -> mf.Action:
        try:
            return self._act(obs)
        except Exception as exc:
            tick = int(obs.get("tick", 0) or 0)
            mf.log(f"act() exception at tick {tick}: {exc}")
            try:
                import traceback
                mf.log(traceback.format_exc())
            except Exception:
                pass
            me = obs.get("self", {}) or {}
            aim = me.get("aim") or [0.0, 0.0]
            return {"tick": tick, "move": [0.0, 0.0],
                    "aim": [float(aim[0]), float(aim[1]) if len(aim) > 1 else 0.0],
                    "fire": [False, False]}

    def _act(self, obs: mf.Observation) -> mf.Action:
        tick = int(obs.get("tick", 0) or 0)
        self.cur_tick = tick
        t = float(obs.get("t", tick * 0.05) or 0.0)
        me = obs.get("self", {}) or {}
        pos = me.get("pos") or [0.0, 0.0, 0.0]
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
        hp = float(me.get("hp", 0.0) or 0.0)
        max_hp = max(float(me.get("max_hp", 1400.0) or 1400.0), 1.0)
        hp_frac = hp / max_hp
        energy = float(me.get("energy", 0.0) or 0.0)
        in_zone = bool(me.get("in_zone", True))
        weapons = me.get("weapons") or []
        held = self._held_kinds(me)
        cubes = float(me.get("cubes", 0.0) or 0.0)
        act: mf.Action = {"tick": tick}

        # 1. offers
        offer = obs.get("offer")
        if offer:
            act["pick"] = self._choose_upgrade(offer, me)

        # 2. events
        for ev in obs.get("events", []) or []:
            et = ev.get("type")
            if et == "damaged":
                self.last_damage_tick = tick
                b = ev.get("bearing")
                if b is not None:
                    self.last_threat_bearing = float(b)
                    self.last_threat_tick = tick
                    if ev.get("rear"):
                        self.force_face_yaw = float(b)
                        self.force_face_until = tick + 60
            elif et == "hit":
                self.last_shot_tick = tick
            elif et == "kill":
                self.last_shot_tick = tick
            elif et == "effect":
                if ev.get("effect") == "short":
                    self.short_until = tick + 40
            elif et == "pickup":
                pass

        # Directional sound and visible beam cues improve awareness outside the
        # current 120-degree vision wedge. Bearings are noisy; never treat them as
        # precise coordinates or chase them as if they were a visible enemy.
        sound_rank = {"near": 3, "mid": 2, "far": 1}
        kind_rank = {"laser_charge": 5, "mortar": 4, "laser": 4,
                     "lightning": 3, "gun": 2, "explosion": 2, "death": 1}
        best_sound = None
        for snd in obs.get("sounds", []) or []:
            try:
                age = float(snd.get("age", 99.0))
                if age > 0.35:
                    continue
                rank = sound_rank.get(str(snd.get("dist", "far")), 0) * 10 + kind_rank.get(str(snd.get("kind", "")), 0)
                if best_sound is None or rank > best_sound[0]:
                    best_sound = (rank, snd)
            except Exception:
                continue
        if best_sound is not None:
            snd = best_sound[1]
            self.last_sound_bearing = float(snd.get("bearing", 0.0))
            self.last_sound_tick = tick
            self.last_sound_kind = str(snd.get("kind", "unknown"))
            self.last_sound_dist = str(snd.get("dist", "far"))
        for beam in obs.get("beams", []) or []:
            try:
                by = int(beam.get("by", -1))
                path = beam.get("path") or []
                if by == self.slot or not path:
                    continue
                src = path[0]
                self.last_sound_bearing = math.atan2(float(src[1]) - y, float(src[0]) - x)
                self.last_sound_tick = tick
                self.last_sound_kind = str(beam.get("kind", "beam"))
                self.last_sound_dist = "near" if math.dist(src[:2], (x, y)) < 60.0 else "mid"
            except Exception:
                continue

        # 3. tracks
        self._update_tracks(obs)
        me_pos = [x, y, z]
        enemies = self._visible_enemies(obs, me)
        # The engine has already applied its exact visibility rules. Our local map
        # raster is only an approximation; retain visible targets and annotate LOS.
        for e in enemies:
            target_view = e["m"] or e["rec"]
            center_clear = self._los_to(me, target_view)
            if center_clear:
                e["direct_los"] = True
                e["aim_height"] = self._enemy_height(target_view)
            else:
                # The engine reveals a mech when its centre OR top is visible.
                # Aim at the exposed upper body rather than discarding the shot.
                pp = me.get("pos") or [0.0, 0.0, 0.0]
                qq = target_view.get("pos") or [0.0, 0.0, 0.0]
                top_h = self._enemy_top_height(target_view)
                top_clear = self.map.los(float(pp[0]), float(pp[1]), float(pp[2]) + self._eye_h(me),
                                         float(qq[0]), float(qq[1]), float(qq[2]) + top_h)
                e["direct_los"] = bool(top_clear)
                e["aim_height"] = top_h if top_clear else self._enemy_height(target_view)
        npcs = []
        for m in obs.get("mechs", []) or []:
            if m.get("kind") == "npc":
                d = math.dist(m.get("pos") or [0.0, 0.0, 0.0], me_pos)
                npcs.append({"m": m, "id": int(m.get("id", -1)), "dist": d, "rec": m})
        npcs.sort(key=lambda e: e["dist"])

        threats = []
        for e in enemies:
            rec = e["rec"]
            d = e["dist"]
            if d > 240.0:
                continue
            th = dict(e)
            th["direct_los"] = bool(e.get("direct_los", True))
            # Carry this tactical annotation into the record passed to weapon selection.
            rec["direct_los"] = th["direct_los"]
            rec["_aim_height"] = float(e.get("aim_height", self._enemy_height(rec)))
            th["charging_at_us"] = bool(rec.get("charging")) and self._aiming_at_us(me, rec)
            th["aiming_at_us"] = self._aiming_at_us(me, rec)
            th["hp_frac"] = float(rec.get("hp", 1400.0) or 1400.0) / max(float(rec.get("max_hp", 1400.0) or 1400.0), 1.0)
            th["recently_damaged"] = (tick - int(rec.get("damaged_tick", -9999))) < 30
            threats.append(th)
        threats.sort(key=lambda e: e["dist"])

        # 4. choose state
        state = self._choose_state(tick, t, me, obs, threats, npcs, hp_frac, in_zone)
        if state != self.state:
            self.state = state
            self.state_since = tick
            if state != "engage":
                self.target_id = None
                self.target_kind = None

        # 5. behavior
        goal: tuple[float, float] | None = None
        move = [0.0, 0.0]
        boost = False
        jump = False
        combat_target: dict | None = None
        farm_target: dict | None = None
        face_target: dict | None = None
        allow_fire = state in ("engage", "farm", "survive")
        dodge = self._dodge_vector(obs, me)

        if state == "rotate":
            goal = self._zone_goal(obs, me, threats)
            # Opportunistic pickups only when they are genuinely close and safe.
            if not threats:
                pu = self._desired_pickup(obs, me, me_pos)
                if pu and math.dist(pu["pos"][:2], (x, y)) < 35.0:
                    goal = (float(pu["pos"][0]), float(pu["pos"][1]))
                dg = self._drop_goal(obs, me, threats, me_pos)
                if dg is not None:
                    goal = dg
            boost = (not in_zone and energy > 20.0) or (energy > 55.0 and math.hypot(goal[0] - x, goal[1] - y) > 90.0)
        elif state == "disengage":
            goal = self._flee_goal(obs, me, threats)
            boost = energy > 32.0
            allow_fire = False
            if threats:
                face_target = threats[0]["rec"]      # keep front armour toward the chaser
            # if cornered at point blank, fire back
            if threats and threats[0]["dist"] < 26.0 and hp_frac > 0.25:
                combat_target = threats[0]["rec"]
                allow_fire = True
            # laser release is handled even while fleeing
        elif state == "engage":
            target = self._pick_combat_target(threats, me, held)
            if target is not None:
                combat_target = target["rec"]
                self.target_id = target["id"]
                self.target_kind = "mech"
                goal = self._combat_goal(me, target, held, obs)
                allow_fire = True
                if target["charging_at_us"] and "lightning" in held and target["dist"] < 45.0:
                    allow_fire = True
                # boost to close/escape only when it helps
                if energy > 45.0 and target["dist"] > 120.0 and "lightning" in held:
                    boost = True
            else:
                state = "rotate"
                goal = self._zone_goal(obs, me, threats)
        elif state == "farm":
            farm_target, goal = self._farm_goal(obs, me, npcs, threats, me_pos, held)
            allow_fire = farm_target is not None
            if farm_target is not None and farm_target.get("kind") in ("npc", "crate"):
                combat_target = farm_target
            if threats and threats[0]["dist"] < 160.0:
                allow_fire = False
                farm_target = None
                combat_target = None
                face_target = threats[0]["rec"]
                goal = self._flee_goal(obs, me, threats)
                boost = energy > 32.0
        else:  # survive
            # Late-game survival is about holding a safe route/position, not chasing
            # the remaining squad. Only fight at point-blank range in the final circle.
            if threats and float((obs.get("zone") or {}).get("radius", 560.0)) <= 75.0 and threats[0]["dist"] < 26.0:
                target = self._pick_combat_target(threats, me, held)
                if target is not None:
                    combat_target = target["rec"]
                    self.target_id = target["id"]
                    self.target_kind = "mech"
                    goal = self._flee_goal(obs, me, threats)
                    allow_fire = True
            if goal is None:
                goal = self._zone_goal(obs, me, threats)
                if threats and threats[0]["dist"] < 170.0:
                    goal = self._flee_goal(obs, me, threats)
                    allow_fire = False
                    face_target = threats[0]["rec"]
            if not threats and (hp_frac < 0.95 or energy < 60.0):
                dg = self._drop_goal(obs, me, threats, me_pos)
                if dg is not None:
                    goal = dg
            if hp_frac < 0.5 and threats and float((obs.get("zone") or {}).get("radius", 560.0)) >= 70.0:
                goal = self._flee_goal(obs, me, threats)
                allow_fire = False
                face_target = threats[0]["rec"]
                boost = energy > 32.0

        # 6. movement
        # shot from something we cannot see (often a laser beyond vision): move
        # away from the bearing and give it nothing to track.
        if (in_zone and not threats and
                tick - self.last_damage_tick < 40 and self.last_threat_bearing is not None and
                tick - self.last_threat_tick < 80):
            b = self.last_threat_bearing + math.pi
            z = obs.get("zone") or {}
            zc = z.get("center") or [x, y]
            zr = float(z.get("radius", 560.0))
            best_ev = None
            best_ev_score = -1e18
            for off in (0.0, 0.55, -0.55, 1.1, -1.1):
                ex = x + math.cos(b + off) * 45.0
                ey = y + math.sin(b + off) * 45.0
                if self.map.nav_blocked_xy(ex, ey):
                    continue
                inside = math.hypot(ex - float(zc[0]), ey - float(zc[1])) <= zr - 6.0
                score = (1.0 if inside else -2.0) - abs(off) * 0.2
                if score > best_ev_score:
                    best_ev_score = score
                    best_ev = (ex, ey)
            if best_ev is not None:
                goal = best_ev
                allow_fire = False
                boost = energy > 30.0
        if goal is not None:
            gx, gy = goal
            if self.map.nav_blocked_xy(gx, gy):
                gx, gy = self.map.nearest_open(gx, gy)
            self.nav.update(tick, x, y, gx, gy)
            dx, dy = self.nav.direction(x, y, gx, gy)
            # local avoidance if the immediate step is blocked
            if math.hypot(dx, dy) > 0.05:
                base_ang = math.atan2(dy, dx)
                if self.map.nav_blocked_xy(x + math.cos(base_ang) * 2.6, y + math.sin(base_ang) * 2.6):
                    best_ang = base_ang
                    best_score = -1e9
                    for k in range(-6, 7):
                        ang = base_ang + k * (math.pi / 8.0)
                        cl = self.map.nav_clear(x, y, x + math.cos(ang) * 7.0, y + math.sin(ang) * 7.0, 1.3)
                        score = (1.0 if cl else -2.0) - abs(k) * 0.12
                        if score > best_score:
                            best_score = score
                            best_ang = ang
                    dx, dy = math.cos(best_ang), math.sin(best_ang)
                move[0] = dx
                move[1] = dy
        # dodge overrides / adds
        if dodge != (0.0, 0.0):
            move[0] += dodge[0] * 2.0
            move[1] += dodge[1] * 2.0
            if energy > 25.0:
                boost = True
        mag = math.hypot(move[0], move[1])
        if not math.isfinite(mag):
            move[0] = move[1] = 0.0
            mag = 0.0
        if mag > 1.0:
            move[0] /= mag
            move[1] /= mag
        if mag < 0.04:
            move[0] = move[1] = 0.0
        act["move"] = move
        act["boost"] = bool(boost)

        # stuck detection / nudge
        if tick % 40 == 0:
            if math.hypot(x - self.stuck_pos[0], y - self.stuck_pos[1]) < 2.0 and mag > 0.2:
                self.stuck_until = tick + 16
                self.stuck_dir = math.atan2(move[1], move[0]) + math.pi * (0.6 if self.rng.random() < 0.5 else -0.6)
            self.stuck_pos = (x, y)
        if tick < self.stuck_until:
            act["move"] = [math.cos(self.stuck_dir), math.sin(self.stuck_dir)]
            act["jump"] = bool(me.get("grounded", False) and energy > 20.0 and tick % 10 == 0)
        # 7. aim / fire
        aim_cmd = None
        fire = [False, False]
        reload_flag = False
        if combat_target is not None:
            tgt = combat_target
            # ensure live record fields
            tid = int(tgt.get("id", -1))
            if tid in self.memory:
                live = dict(self.memory[tid])
                live["pos"] = list(tgt.get("pos") or live["pos"])
                live["vel"] = list(self.memory[tid].get("vel") or [0.0, 0.0, 0.0])
                live["hp"] = float(tgt.get("hp", live.get("hp", 1000.0)) or live.get("hp", 1000.0))
                live["max_hp"] = float(tgt.get("max_hp", live.get("max_hp", 1400.0)) or live.get("max_hp", 1400.0))
                live["weapons"] = list(tgt.get("weapons") or live.get("weapons") or [])
                live["charging"] = bool(tgt.get("charging", live.get("charging", False)))
                tgt = live
            dist_t = math.dist(tgt.get("pos") or [0.0, 0.0, 0.0], me_pos)
            slot, kind = self._choose_weapon(me, tgt, dist_t, state)
            if slot is None or kind is None:
                # no usable weapon: face the threat for front armour
                aim_cmd = [math.atan2(float((tgt.get("pos") or [0, 0])[1]) - y,
                                      float((tgt.get("pos") or [0, 0])[0]) - x), 0.0]
            else:
                w = weapons[slot] or {}
                aim_cmd = self._solve_aim(kind, me, tgt, w)
                can_fire = self._aim_ok(kind, me, tgt, aim_cmd, w)
                # line of sight for projectile / beam
                if can_fire:
                    if kind == "mortar":
                        can_fire = self._mortar_arc_clear(me, tgt, aim_cmd, w)
                    elif not self.map.los(x, y, z + self._muzzle_h(me),
                                          float((tgt.get("pos") or [0, 0, 0])[0]),
                                          float((tgt.get("pos") or [0, 0, 0])[1]),
                                          float((tgt.get("pos") or [0, 0, 0])[2]) + self._enemy_height(tgt)):
                        can_fire = False
                if kind == "laser":
                    self.laser_last_target_tick = tick
                    charge = float(w.get("charge", 0.0) or 0.0)
                    cooldown = float(w.get("cooldown", 0.0) or 0.0)
                    if charge > 0.0:
                        stats = self._weapon_stats("laser", w)
                        full = float(stats.get("charge", 0.75) or 0.75)
                        hold_s = float(stats.get("hold_s", 2.0) or 2.0)
                        desired = self._desired_laser_charge(w, float(tgt.get("hp", 1000.0) or 1000.0))
                        if not allow_fire:
                            fire[slot] = False       # release before breaking contact
                            self.laser_charge_target = None
                        elif charge >= full + hold_s - 0.25:
                            fire[slot] = False       # avoid the automatic shot
                            self.laser_charge_target = None
                        elif can_fire and charge >= desired:
                            fire[slot] = False       # aimed release
                            self.laser_charge_target = None
                            self.laser_lost_tick = -999
                        else:
                            fire[slot] = True        # keep charging and tracking
                            self.laser_charge_target = tid
                    elif cooldown <= 0.10 and allow_fire and can_fire:
                        fire[slot] = True            # start charging
                        self.laser_charge_target = tid
                elif kind == "lightning":
                    if allow_fire and can_fire and energy >= 6.0 and self._weapon_range("lightning", w) >= dist_t:
                        fire[slot] = True
                elif kind == "gun":
                    if allow_fire and can_fire and int(w.get("ammo", 0) or 0) > 0 and float(w.get("reload", 0.0) or 0.0) <= 0.0:
                        fire[slot] = True
                elif kind == "mortar":
                    if allow_fire and can_fire and dist_t >= 22.0:
                        fire[slot] = True
                # secondary gun while the laser charges (same commanded aim)
                if kind == "laser" and fire[slot]:
                    gslot = self._slot_of_kind(me, "gun")
                    if gslot is not None and gslot != slot:
                        gw = weapons[gslot] or {}
                        if int(gw.get("ammo", 0) or 0) > 0 and float(gw.get("reload", 0.0) or 0.0) <= 0.0:
                            gaim = self._solve_aim("gun", me, tgt, gw)
                            if abs(angle_diff(gaim[0], aim_cmd[0])) < 0.02 and abs(gaim[1] - aim_cmd[1]) < 0.02:
                                if self._aim_ok("gun", me, tgt, gaim, gw):
                                    fire[gslot] = True
            # Any other laser that is already charging must be kept alive or released
            # deliberately: sending fire=false releases the beam.
            for i, ow in enumerate(weapons):
                if i == slot or not ow or ow.get("kind") != "laser":
                    continue
                if float(ow.get("charge", 0.0) or 0.0) > 0.0:
                    if (allow_fire and combat_target is not None and
                            dist_t <= self._weapon_range("laser", ow)):
                        fire[i] = True
                    else:
                        fire[i] = False
        else:
            # no current target: scan / face threat
            if face_target is not None:
                # A currently visible attacker outranks a stale rear-hit bearing.
                fp = face_target.get("pos") or [0.0, 0.0, 0.0]
                dx = float(fp[0]) - x
                dy = float(fp[1]) - y
                dz = float(fp[2]) + float(face_target.get("_aim_height", self._enemy_height(face_target))) - (z + self._muzzle_h(me))
                aim_cmd = [math.atan2(dy, dx),
                           clamp(math.atan2(dz, max(math.hypot(dx, dy), 0.01)), self.min_pitch, self.max_pitch)]
            elif self.force_face_yaw is not None and tick < self.force_face_until:
                aim_cmd = [self.force_face_yaw, 0.0]
            elif self.last_threat_bearing is not None and tick - self.last_threat_tick < 100:
                aim_cmd = [self.last_threat_bearing, 0.0]
            elif self.last_sound_bearing is not None and tick - self.last_sound_tick < 24:
                # Turn the vision wedge toward the freshest useful sound cue.
                aim_cmd = [self.last_sound_bearing, 0.0]
            else:
                # Sweep around the travel direction (or east if idle).
                base = math.atan2(move[1], move[0]) if math.hypot(move[0], move[1]) > 0.1 else 0.0
                sweep = math.sin(tick * 0.045) * 0.85
                aim_cmd = [base + sweep, 0.0]
            # A charge is worth keeping for a moment while the target is only
            # briefly behind cover; otherwise release instead of auto-firing.
            for i, ow in enumerate(weapons):
                if not ow or ow.get("kind") != "laser" or float(ow.get("charge", 0.0) or 0.0) <= 0.0:
                    continue
                rec = self.memory.get(self.laser_charge_target) if self.laser_charge_target is not None else None
                if rec is not None and tick - self.laser_last_target_tick <= 10:
                    fire[i] = True
                    lp = rec.get("pos") or [0.0, 0.0, 0.0]
                    dx = float(lp[0]) - x
                    dy = float(lp[1]) - y
                    dz = float(lp[2]) + self._enemy_height(rec) - (z + self._muzzle_h(me))
                    aim_cmd = [math.atan2(dy, dx),
                               clamp(math.atan2(dz, max(math.hypot(dx, dy), 0.01)), self.min_pitch, self.max_pitch)]
                else:
                    fire[i] = False
        if aim_cmd is None or not math.isfinite(float(aim_cmd[0])) or not math.isfinite(float(aim_cmd[1])):
            cur = me.get("aim") or [0.0, 0.0]
            aim_cmd = [float(cur[0]), float(cur[1]) if len(cur) > 1 else 0.0]
        act["aim"] = [clamp(float(aim_cmd[0]), -math.pi - 0.01, math.pi + 0.01),
                      clamp(float(aim_cmd[1]), self.min_pitch, self.max_pitch)]
        act["fire"] = [bool(fire[0]), bool(fire[1])]

        # 8. reload
        gun_slot = self._slot_of_kind(me, "gun")
        if gun_slot is not None:
            gw = weapons[gun_slot] or {}
            ammo = int(gw.get("ammo", 0) or 0)
            reloading = float(gw.get("reload", 0.0) or 0.0) > 0.0
            mag = int(self._weapon_stats("gun", gw).get("mag", 12) or 12)
            if not reloading and ammo <= 0:
                reload_flag = True
            elif not reloading and ammo <= max(3, int(mag * 0.25)):
                if combat_target is None or (threats and threats[0]["dist"] > 90.0) or state in ("disengage", "rotate"):
                    reload_flag = True
        act["reload"] = bool(reload_flag)

        # 9. take / drop
        self._handle_loot(obs, me, me_pos, act)

        # 10. small debug label
        if tick % 40 == 0:
            self._log(tick, f"state={state} hp={hp:.0f} en={energy:.0f} cubes={cubes:.0f} "
                            f"threats={len(threats)} d={threats[0]['dist'] if threats else -1:.0f} "
                            f"los={int(threats[0].get('direct_los', True)) if threats else -1} "
                            f"sound={self.last_sound_kind or '-'} weap={','.join(held)}")
        label = f"{state} hp{int(hp)} en{int(energy)} t{len(threats)}"
        mf.draw.label(label[:80])
        return act

    # ------------------------------------------------------------------ state
    def _choose_state(self, tick: int, t: float, me: dict, obs: dict,
                      threats: list[dict], npcs: list[dict], hp_frac: float, in_zone: bool) -> str:
        if not in_zone:
            return "rotate"
        zone = obs.get("zone") or {}
        zr = float(zone.get("radius", 560.0))
        nzr = float(zone.get("next_radius", zr))
        shrink_start = float(zone.get("shrink_start", 1e9))
        shrink_end = float(zone.get("shrink_end", 1e9))
        x, y = (me.get("pos") or [0.0, 0.0, 0.0])[:2]
        nc = zone.get("next_center") or [0.0, 0.0]
        cc = zone.get("center") or [0.0, 0.0]
        dist_next = math.hypot(float(nc[0]) - x, float(nc[1]) - y)
        dist_cur = math.hypot(float(cc[0]) - x, float(cc[1]) - y)
        near = [e for e in threats if e["dist"] < 180.0]
        very_near = [e for e in threats if e["dist"] < 48.0]
        close_pack = len([e for e in threats if e["dist"] < 150.0]) >= 2
        close_group = len([e for e in threats if e["dist"] < 95.0]) >= 2
        laser_at_us = any(e.get("charging_at_us") and e.get("direct_los", True)
                          for e in threats if e["dist"] < 290.0)
        critical = hp_frac < 0.30
        low = hp_frac < 0.58
        healing = tick - max(self.last_damage_tick, self.last_shot_tick) > int(self.regen_delay * 20 + 20)

        time_left = shrink_start - t
        walk = max(float(self._mech_stats(me).get("walk_speed", 11.7)), 1.0)
        need = max(0.0, dist_next - nzr * 0.78)
        travel = need / (walk * 0.82) + 5.0
        zone_soon = time_left < travel and dist_next > nzr * 0.78
        in_shrink = shrink_start <= t <= shrink_end and dist_next > nzr * 0.90
        tiny = zr <= 75.0
        must_rotate = dist_cur > zr - 9.0 or zone_soon or in_shrink
        if must_rotate:
            need_next = (zone_soon or in_shrink) and dist_next > nzr * 0.78
            if tiny and very_near and not need_next:
                return "engage"  # no useful escape space remains
            if critical and near and not need_next:
                return "disengage"
            return "rotate"

        # A laser charge is an immediate threat: break its line of sight unless
        # the circle is so small that disengaging cannot create distance.
        if laser_at_us and not tiny:
            return "disengage"

        # Treat the house bots as a coordinated pack. Two nearby enemies are a
        # disengage condition even at high health; do not wait until surrounded.
        if close_group or (close_pack and hp_frac < 0.96):
            return "engage" if tiny and very_near else "disengage"
        if low and near:
            return "engage" if tiny and very_near else "disengage"
        if very_near:
            # Only counterattack at point-blank range when there is no room to run.
            return "engage" if tiny else "disengage"

        # Do not initiate ordinary fights. A single enemy is worth engaging only
        # when already badly weakened, close enough to finish, and we are healthy.
        if threats and hp_frac > 0.82:
            best = self._pick_combat_target(threats, me, self._held_kinds(me))
            if best is not None and best["hp_frac"] < 0.22 and best["dist"] < 42.0 and len(near) <= 1:
                return "engage"
        if threats and (tick - self.last_damage_tick < 35 or tick - self.last_shot_tick < 35):
            return "disengage"

        if t > 300.0 or int(obs.get("alive", 16) or 16) <= 5:
            return "survive"
        if t < 55.0 or hp_frac < 0.82 or float(me.get("cubes", 0.0) or 0.0) < 5.0:
            return "farm"
        if threats:
            return "survive"
        return "farm"

    def _pick_combat_target(self, threats: list[dict], me: dict, held: list[str]) -> dict | None:
        hp_frac = float(me.get("hp", 1.0) or 1.0) / max(float(me.get("max_hp", 1.0) or 1.0), 1.0)
        best = None
        best_score = -1e9
        for e in threats:
            rec = e["rec"]
            d = e["dist"]
            if d > 235.0:
                continue
            if not e.get("direct_los", True) and "mortar" not in held:
                continue
            score = 0.0
            if not e.get("direct_los", True):
                score += 0.45  # indirect-fire opportunity, but not a free shot
            score += (1.0 - float(e.get("hp_frac", 1.0))) * 3.2
            score += 0.7 if e.get("recently_damaged") else 0.0
            score += 0.4 if e.get("aiming_at_us") else 0.0
            score -= d / 220.0
            ew = rec.get("weapons") or []
            if "laser" in held:
                if "mortar" in ew:
                    score += 1.1
                if "laser" in ew and d > 120:
                    score += 0.4
            if "lightning" in held:
                if d < 45:
                    score += 1.0
                if "laser" in ew:
                    score += 0.5
                if "mortar" in ew and "laser" not in held and d > 70:
                    score -= 1.2
            if "mortar" in held and "lightning" in ew:
                score += 0.9
            if "laser" in ew and d > 120 and "laser" not in held and "lightning" not in held:
                score -= 1.6
            if e.get("charging_at_us") and d > 60:
                score -= 1.0
            if d < 45:
                score += 0.8
            if self.target_id is not None and int(rec.get("id", -1)) == self.target_id:
                score += 0.55
            if score > best_score:
                best_score = score
                best = e
        if best is None:
            return None
        if best_score < -0.6 and best["dist"] > 55.0:
            return None
        return best

    def _combat_goal(self, me: dict, target: dict, held: list[str], obs: dict) -> tuple[float, float]:
        x, y = (me.get("pos") or [0.0, 0.0, 0.0])[:2]
        tx, ty = (target["rec"].get("pos") or [0.0, 0.0, 0.0])[:2]
        d = math.hypot(tx - x, ty - y)
        zone = obs.get("zone") or {}
        zc = zone.get("center") or [0.0, 0.0]
        zr = float(zone.get("radius", 560.0))
        enemy_weapons = set(target["rec"].get("weapons") or [])
        # Respect the weapon triangle instead of closing to lightning range merely
        # because a lightning pickup is in either slot.
        if "lightning" in held and "laser" in enemy_weapons and (d < 75.0 or target["rec"].get("charging")):
            ideal = 34.0       # get inside the laser's aim/charge cycle and short it
        elif "mortar" in held and "lightning" in enemy_weapons:
            ideal = 155.0      # remain well outside the 45 m arc
        elif "laser" in held and "mortar" in enemy_weapons:
            ideal = 165.0      # exploit the laser's range and mortar cook-off
        elif "laser" in held:
            ideal = 145.0
        elif "mortar" in held:
            ideal = 120.0
        elif "lightning" in held:
            ideal = 34.0
        else:
            ideal = 90.0
        ideal = min(ideal, max(12.0, zr * 0.70))
        if float(target.get("hp_frac", 1.0)) < 0.35:
            ideal = min(ideal, 50.0, max(12.0, zr * 0.5))
        # orbit
        if d < 1.0:
            ang = 0.0
        else:
            ang = math.atan2(y - ty, x - tx)
        if d < ideal * 0.7:
            ang += math.pi  # back off through the target? no: move away
            ang = math.atan2(y - ty, x - tx)
        elif d > ideal * 1.25:
            ang = math.atan2(ty - y, tx - x)
        else:
            ang += math.pi * 0.5 * self.strafe_sign
        # flip strafe periodically
        tick = int(obs.get("tick", 0) or 0)
        if tick >= self.strafe_flip:
            self.strafe_sign = -self.strafe_sign
            self.strafe_flip = tick + self.rng.randint(18, 34)
        # do not walk into a wall: fall back to tangential
        gx = x + math.cos(ang) * min(max(d * 0.55, 22.0), 60.0)
        gy = y + math.sin(ang) * min(max(d * 0.55, 22.0), 60.0)
        if zr > 12.0:
            dx = gx - float(zc[0])
            dy = gy - float(zc[1])
            dd = math.hypot(dx, dy)
            lim = max(2.0, zr - 6.0)
            if dd > lim:
                gx = float(zc[0]) + dx * lim / dd
                gy = float(zc[1]) + dy * lim / dd
        if self.map.nav_blocked_xy(gx, gy):
            gx, gy = self.map.nearest_open(gx, gy)
        if not self.map.same_component(x, y, gx, gy):
            gx, gy = x, y      # shoot from here rather than chase through walls
        return gx, gy

    def _drop_goal(self, obs: dict, me: dict, threats: list[dict], me_pos: list[float]) -> tuple[float, float] | None:
        """Return a safe supply-drop rendezvous point when the timing is viable."""
        drops = obs.get("drops") or []
        if not drops:
            return None
        x, y = float(me_pos[0]), float(me_pos[1])
        walk = max(float(self._mech_stats(me).get("walk_speed", 11.7)), 1.0)
        best = None
        best_cost = 1e18
        for dr in drops:
            holder = dr.get("holder")
            if holder not in (None, self.slot):
                continue
            p = dr.get("pos") or [x, y, me_pos[2]]
            d = math.hypot(float(p[0]) - x, float(p[1]) - y)
            eta = max(0.0, d - 6.0) / (walk * 0.82)
            lands = max(0.0, float(dr.get("lands_in", 99.0)))
            # If we are already the holder, remain in the ring until completion.
            if holder == self.slot:
                cost = d - 1000.0 * float(dr.get("progress", 0.0) or 0.0)
            else:
                if d > 220.0 or lands > eta + 24.0:
                    continue
                if not self.map.same_component(x, y, float(p[0]), float(p[1])):
                    continue
                # Avoid a drop that a visible opponent can clearly reach first.
                contested = False
                for th in threats:
                    ep = th["rec"].get("pos") or [0.0, 0.0, 0.0]
                    ed = math.hypot(float(ep[0]) - float(p[0]), float(ep[1]) - float(p[1]))
                    if ed < max(45.0, d - 15.0):
                        contested = True
                        break
                if contested:
                    continue
                if threats and threats[0]["dist"] < 100.0:
                    continue
                # Arrive shortly before/after landing; a small wait is acceptable.
                cost = d + max(0.0, lands - eta) * walk * 0.35
            if cost < best_cost:
                best_cost = cost
                best = (float(p[0]), float(p[1]))
        return best

    def _farm_goal(self, obs: dict, me: dict, npcs: list[dict], threats: list[dict],
                   me_pos: list[float], held: list[str]) -> tuple[dict | None, tuple[float, float]]:
        x, y = me_pos[0], me_pos[1]
        hp_frac = float(me.get("hp", 1.0) or 1.0) / max(float(me.get("max_hp", 1.0) or 1.0), 1.0)
        hurt = hp_frac < 0.62          # shooting resets the out-of-combat regen timer
        # weapon pickup first if a slot is empty
        weapons = me.get("weapons") or [None, None]
        empty = any(w is None for w in weapons)
        pu = self._desired_pickup(obs, me, me_pos)
        pu_d = 1e9 if pu is None else math.dist(pu["pos"][:2], (x, y))
        # a close weapon is always worth a detour; a distant one waits until
        # closer loot has been collected
        if pu is not None and (empty or not threats) and pu_d < 65.0:
            safe = (not threats) or (threats[0]["dist"] > pu_d + 30.0)
            if safe:
                return pu, (float(pu["pos"][0]), float(pu["pos"][1]))
        # cubes
        cubes = obs.get("cubes") or []
        if cubes:
            liked = 80.0 if not threats else 55.0
            for c in sorted(cubes, key=lambda it: math.dist(it.get("pos", [0, 0, 0])[:2], (x, y))):
                if math.dist(c["pos"][:2], (x, y)) >= liked:
                    break
                if abs(float(c["pos"][2]) - me_pos[2]) >= 4.0:
                    continue
                if self.map.same_component(x, y, c["pos"][0], c["pos"][1]):
                    return c, (float(c["pos"][0]), float(c["pos"][1]))
        # Supply drops can restore the entire mech and grant three cubes. Pursue
        # only when no nearby fight makes the four-second capture unsafe.
        if not threats or threats[0]["dist"] > 150.0:
            dg = self._drop_goal(obs, me, threats, me_pos)
            if dg is not None:
                return None, dg
        # crates (do not reset regen while hurt)
        crates = obs.get("crates") or []
        if crates and not hurt:
            liked = 110.0 if not threats else 65.0
            for cr in sorted(crates, key=lambda it: math.dist(it.get("pos", [0, 0, 0])[:2], (x, y))):
                d = math.dist(cr["pos"][:2], (x, y))
                if d >= liked:
                    break
                if not self.map.same_component(x, y, cr["pos"][0], cr["pos"][1]):
                    continue
                zc = self.map.terrain_z(cr["pos"][0], cr["pos"][1])
                if abs(float(cr["pos"][2]) - zc) < 2.5:
                    tgt = dict(cr)
                    tgt["kind"] = "crate"
                    goal = (float(cr["pos"][0]), float(cr["pos"][1])) if d > 14.0 else (x, y)
                    return tgt, goal
        # Do not hunt NPCs in the survival build. Optional fights reset health
        # regeneration and can leave the mech exposed when the player squad arrives.
        # a distant weapon is still worth it when nothing closer is available
        if pu is not None and (empty or not threats):
            limit = 145.0 if empty else 85.0
            if hurt:
                limit = min(limit, 80.0)
            safe = (not threats) or (threats[0]["dist"] > pu_d + 30.0)
            if pu_d < limit and safe:
                return pu, (float(pu["pos"][0]), float(pu["pos"][1]))
        # drops
        drops = obs.get("drops") or []
        if threats and threats[0]["dist"] < 110.0:
            drops = []
        for dr in drops:
            if dr.get("holder") not in (None, self.slot):
                continue
            if float(dr.get("lands_in", 99.0)) < 14.0:
                d = math.dist(dr["pos"][:2], (x, y))
                if d < 120.0 and self.map.same_component(x, y, dr["pos"][0], dr["pos"][1]):
                    return None, (float(dr["pos"][0]), float(dr["pos"][1]))
        return None, self._zone_goal(obs, me, threats)

    def _handle_loot(self, obs: dict, me: dict, me_pos: list[float], act: mf.Action) -> None:
        weapons = me.get("weapons") or [None, None]
        held = self._held_kinds(me)
        charging = any(w and w.get("kind") == "laser" and float(w.get("charge", 0.0) or 0.0) > 0.0
                       for w in weapons)
        if charging:
            return
        best = None
        best_score = -1e9
        for p in obs.get("pickups", []) or []:
            kind = p.get("kind")
            if not kind or kind in held:
                continue
            d = math.dist(p.get("pos", [0.0, 0.0, 0.0])[:2], me_pos[:2])
            if d > self.reach + 0.05 or abs(float(p["pos"][2]) - me_pos[2]) > 4.0:
                continue
            score = self._pickup_preference(kind)
            if any(w is None for w in weapons):
                score += 2.0
            if score > best_score:
                best_score = score
                best = p
        if best is None:
            return
        empty_slot = None
        for i, w in enumerate(weapons):
            if w is None:
                empty_slot = i
                break
        if empty_slot is not None:
            act["take"] = best["id"]
            return
        # both full: swap only if clearly better and safe
        if self._aiming_threat_close(obs, me_pos):
            return
        worst = None
        worst_val = 1e9
        for i, w in enumerate(weapons):
            if not w:
                continue
            v = self._pickup_preference(w.get("kind"))
            if v < worst_val:
                worst_val = v
                worst = i
        if worst is not None and weapons[worst] and weapons[worst].get("kind") == "gun":
            return
        if worst is not None and best_score > worst_val + 0.8:
            act["take"] = best["id"]
            act["drop"] = worst

    def _aiming_threat_close(self, obs: dict, me_pos: list[float]) -> bool:
        for m in obs.get("mechs", []) or []:
            if m.get("kind") != "mech":
                continue
            if math.dist(m.get("pos") or [0.0, 0.0, 0.0], me_pos) < 95.0:
                return True
        return False


if __name__ == "__main__":
    mf.run(Bot)
