import math
import os
import json
import mechfight as mf


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


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
        self.cover = []
        self.cell_size = 2.0
        self.pad = mech_radius + 0.5
        wp = os.path.join(map_dir, "v2", "world.json")
        if not os.path.exists(wp):
            return
        try:
            with open(wp) as f:
                data = json.load(f)
            cs, pad = self.cell_size, self.pad
            diag = pad * 0.7071
            offs = ((0, 0), (pad, 0), (-pad, 0), (0, pad), (0, -pad),
                    (diag, diag), (diag, -diag), (-diag, diag), (-diag, -diag))
            for p in data.get("prisms", []):
                if float(p.get("base", 0)) > 8.8 or float(p.get("top", 10)) < 1.2:
                    continue
                ring = p.get("ring", [])
                if len(ring) < 6:
                    continue
                holes = p.get("holes", [])
                xs = ring[0::2]
                ys = ring[1::2]
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)
                self.cover.append((cx, cy))
                for gx in range(int(math.floor((min(xs) - pad) / cs)), int(math.ceil((max(xs) + pad) / cs)) + 1):
                    ccx = (gx + 0.5) * cs
                    for gy in range(int(math.floor((min(ys) - pad) / cs)), int(math.ceil((max(ys) + pad) / cs)) + 1):
                        ccy = (gy + 0.5) * cs
                        hit = False
                        for ox, oy in offs:
                            px, py = ccx + ox, ccy + oy
                            if point_in_poly(px, py, ring) and not any(point_in_poly(px, py, h) for h in holes):
                                hit = True
                                break
                        if hit:
                            self.blocked.add((gx, gy))
        except Exception:
            pass

    def blocked_at(self, x, y):
        if not self.blocked:
            return False
        return (int(math.floor(x / self.cell_size)), int(math.floor(y / self.cell_size))) in self.blocked

    def nearest_cover(self, x, y):
        if not self.cover:
            return None
        return min(self.cover, key=lambda c: math.hypot(c[0] - x, c[1] - y))


class Bot:
    name = "survival-tactician-v26"

    def __init__(self, hello):
        self.cfg = hello.get("config", {})
        mech_cfg = self.cfg.get("mech", {})
        self.reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.arena = ArenaMap(hello.get("map", {}).get("dir", "/arena"),
                              mech_radius=float(mech_cfg.get("radius", 2.5)))
        self.gun_range = float(self.cfg.get("weapons", {}).get("gun", {}).get("range", 230.0))
        self.lightning_range = float(self.cfg.get("weapons", {}).get("lightning", {}).get("range", 45.0))
        self.last_damage_tick = -9999

    def _phase(self, t):
        if t < 60:
            return 1
        elif t < 180:
            return 2
        elif t < 300:
            return 3
        return 4

    def _enemies(self, obs):
        return [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]

    def _weapon_slot(self, wpn, kind):
        for i, w in enumerate(wpn):
            if w and w["kind"] == kind:
                return i
        return None

    def _aim_point(self, me, mh, tpos):
        sx, sy, sz = me["pos"][0], me["pos"][1], me["pos"][2] + mh
        tx, ty, tz = tpos[0], tpos[1], tpos[2] + 4.5
        return [math.atan2(ty - sy, tx - sx),
                clamp(math.atan2(tz - sz, max(math.hypot(tx - sx, ty - sy), 0.01)), -1.3, 1.3)]

    def _aim_yaw_error(self, me, aim):
        my_aim = me.get("aim", [0.0, 0.0])
        return abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)

    def _pick_upgrade(self, offer, held):
        choices = offer.get("choices", [])
        priority = ["armor", "reactive", "regen", "recovery", "speed", "movement", "walk", "boost"]
        for key in priority:
            for i, c in enumerate(choices):
                if key in c.get("id", "").lower():
                    return i
        return 0

    def act(self, obs):
        try:
            return self._act(obs)
        except Exception as exc:
            mf.log(f"exc t{obs.get('tick', 0)}: {exc}")
            aim = obs.get("self", {}).get("aim") or [0.0, 0.0]
            return {"tick": int(obs.get("tick", 0)), "move": [0.0, 0.0],
                    "aim": [float(aim[0]), float(aim[1]) if len(aim) > 1 else 0.0], "fire": [False, False]}

    def _act(self, obs):
        t = int(obs["tick"])
        time = float(obs.get("t", 0))
        me = obs["self"]
        x, y, z = me["pos"]
        hp = float(me["hp"])
        max_hp = float(me["max_hp"])
        hf = hp / max(max_hp, 1.0)
        eng = float(me["energy"])
        wpn = me["weapons"]
        held = [w["kind"] for w in wpn if w is not None]
        mh = float(me.get("stats", {}).get("muzzle_height", 7.0))
        walk_speed = float(me.get("stats", {}).get("walk_speed", 11.0))

        act = {"tick": t}

        # Upgrade offer: prioritize defense (armor > regen > movement)
        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"], held)

        # Threat assessment
        enemies = self._enemies(obs)
        near_enemies = [e for e in enemies if math.dist(e["pos"], me["pos"]) < 70.0]
        safe = len(near_enemies) == 0

        # Loot pickup only when safe
        if safe:
            for p in obs.get("pickups", []):
                if math.dist(p["pos"][:2], (x, y)) < self.reach and abs(p["pos"][2] - z) < 4.0:
                    pk = p["kind"]
                    if pk in held:
                        continue
                    if any(w is None for w in wpn):
                        if pk in ("lightning", "mortar") or "gun" not in held:
                            act["take"] = p["id"]
                            break

        # Track damage taken
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damage_tick = t

        phase = self._phase(time)
        zone = obs["zone"]
        nc, nr = zone["next_center"], float(zone["next_radius"])
        dn = math.hypot(nc[0] - x, nc[1] - y)
        in_zone = me.get("in_zone", True)
        sr = max(15.0, nr * 0.75)
        zone_pressure = (not in_zone) or (dn > sr)

        # --- Movement ---
        vx, vy = 0.0, 0.0

        if near_enemies:
            # Seek cover: keep a building between us and the nearest enemy
            cover = self.arena.nearest_cover(x, y)
            if cover:
                cx, cy = cover
                ex, ey = near_enemies[0]["pos"][0], near_enemies[0]["pos"][1]
                dxc, dyc = cx - ex, cy - ey
                dc = math.hypot(dxc, dyc)
                if dc > 0.1:
                    px = cx + (dxc / dc) * 7.0
                    py = cy + (dyc / dc) * 7.0
                else:
                    px, py = cx, cy
                dx2, dy2 = px - x, py - y
                d2 = math.hypot(dx2, dy2)
                if d2 > 1.5:
                    vx += (dx2 / d2) * min(walk_speed, 10.0) * 0.85
                    vy += (dy2 / d2) * min(walk_speed, 10.0) * 0.85
                else:
                    # At cover: strafe to break enemy aim
                    strafe = math.atan2(ey - y, ex - x) + math.pi * 0.5
                    vx += math.cos(strafe) * walk_speed * 0.4
                    vy += math.sin(strafe) * walk_speed * 0.4
            else:
                # No cover: run directly away from nearest enemy
                ex, ey = near_enemies[0]["pos"][0], near_enemies[0]["pos"][1]
                at = math.atan2(y - ey, x - ex)
                vx += math.cos(at) * walk_speed * 1.0
                vy += math.sin(at) * walk_speed * 1.0

        if zone_pressure and not near_enemies:
            az = math.atan2(nc[1] - y, nc[0] - x)
            vx += math.cos(az) * walk_speed * 0.7
            vy += math.sin(az) * walk_speed * 0.7

        if phase <= 2 and not near_enemies and not zone_pressure:
            az = math.atan2(nc[1] - y, nc[0] - x)
            vx += math.cos(az) * walk_speed * 0.35
            vy += math.sin(az) * walk_speed * 0.35

        if math.hypot(vx, vy) < 0.05:
            vx = math.cos(t * 0.1) * 0.3
            vy = math.sin(t * 0.1) * 0.3

        vm = math.hypot(vx, vy)
        if vm > 1.0:
            vx, vy = vx / vm, vy / vm

        act["move"] = [vx, vy]
        act["boost"] = bool(zone_pressure and eng > 25.0 and len(near_enemies) == 0)
        act["jump"] = bool(False)

        # --- Aim: face nearest threat (rear armor) ---
        if enemies:
            e = enemies[0]
            act["aim"] = [math.atan2(e["pos"][1] - y, e["pos"][0] - x), 0.0]
        else:
            act["aim"] = [math.atan2(vy, vx), 0.0]

        # --- Fire: only when safe, helpful, and not outnumbered ---
        fire = [False, False]

        if len(near_enemies) <= 2 and safe and t - self.last_damage_tick > 20:
            best = None
            best_d = 9999
            for e in enemies:
                d = math.dist(e["pos"], me["pos"])
                if d < best_d:
                    best_d = d
                    best = e

            if best:
                d = best_d
                e = best

                # Lightning: close range, instant, good for close defense
                li = self._weapon_slot(wpn, "lightning")
                if li is not None and d <= min(self.lightning_range, 35.0) and eng >= 20:
                    aim = self._aim_point(me, mh, e["pos"])
                    act["aim"] = aim
                    ye = self._aim_yaw_error(me, aim)
                    if ye < 0.35:
                        fire[li] = True

                # Gun: short-medium range, no telegraph charge-up
                elif "gun" in held and d <= 50.0:
                    gi = self._weapon_slot(wpn, "gun")
                    aim = self._aim_point(me, mh, e["pos"])
                    act["aim"] = aim
                    ye = self._aim_yaw_error(me, aim)
                    if ye < 0.30:
                        fire[gi] = True

        act["fire"] = fire

        # Reload empty gun
        for i, w in enumerate(wpn):
            if w and w["kind"] == "gun" and int(w.get("ammo", 12)) == 0:
                act["reload"] = True

        mf.draw.label(f"P{phase} HP:{hp:.0f}")
        return act


mf.run(Bot)
