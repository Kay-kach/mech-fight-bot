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
    name = "survival-tactician-v27"

    def __init__(self, hello):
        self.cfg = hello.get("config", {})
        mech_cfg = self.cfg.get("mech", {})
        self.reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.arena = ArenaMap(hello.get("map", {}).get("dir", "/arena"),
                              mech_radius=float(mech_cfg.get("radius", 2.5)))
        self.gun_range = float(self.cfg.get("weapons", {}).get("gun", {}).get("range", 230.0))
        self.lightning_range = float(self.cfg.get("weapons", {}).get("lightning", {}).get("range", 45.0))
        self.laser_range = float(self.cfg.get("weapons", {}).get("laser", {}).get("range", 280.0))
        self.mortar_range = float(self.cfg.get("weapons", {}).get("mortar", {}).get("range", 235.0))
        self.upgrade_priority = ["damage", "speed", "movement", "walk", "boost",
                                 "armor", "health", "hp", "regen", "recovery",
                                 "gun", "mortar", "laser", "lightning"]

    def _enemies(self, obs):
        return [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]

    def _weapon_slot(self, wpn, kind):
        for i, w in enumerate(wpn):
            if w and w["kind"] == kind:
                return i
        return None

    def _held(self, wpn):
        return [w["kind"] for w in wpn if w is not None]

    def _pick_upgrade(self, offer, held):
        choices = offer.get("choices", [])
        for key in self.upgrade_priority:
            for i, c in enumerate(choices):
                if key in c.get("id", "").lower():
                    return i
        return 0

    def _aim_point(self, me, mh, tpos):
        sx, sy, sz = me["pos"][0], me["pos"][1], me["pos"][2] + mh
        tx, ty, tz = tpos[0], tpos[1], tpos[2] + 4.5
        return [math.atan2(ty - sy, tx - sx),
                clamp(math.atan2(tz - sz, max(math.hypot(tx - sx, ty - sy), 0.01)), -1.3, 1.3)]

    def _aim_yaw_error(self, me, aim):
        my_aim = me.get("aim", [0.0, 0.0])
        return abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)

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
        held = self._held(wpn)
        mh = float(me.get("stats", {}).get("muzzle_height", 7.0))
        walk_speed = float(me.get("stats", {}).get("walk_speed", 11.0))

        act = {"tick": t}

        # E. Upgrades: damage > movement > health/armor > regen/energy > weapon-specific
        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"], held)

        # Enemies visible now
        enemies = self._enemies(obs)
        near_enemies = [e for e in enemies if math.dist(e["pos"], me["pos"]) < 80.0]
        nearest = min(enemies, key=lambda e: math.dist(e["pos"], me["pos"])) if enemies else None
        nearest_d = math.dist(nearest["pos"], me["pos"]) if nearest else 9999.0

        zone = obs["zone"]
        nc, nr = zone["next_center"], float(zone["next_radius"])
        dn = math.hypot(nc[0] - x, nc[1] - y)
        in_zone = me.get("in_zone", True)
        sr = max(15.0, nr * 0.75)
        zone_pressure = (not in_zone) or (dn > sr)

        # Loot pickup: weigh benefit vs danger (E)
        take_id = None
        if near_enemies:
            # Only take if very close and no immediate threat
            for p in obs.get("pickups", []):
                d = math.dist(p["pos"][:2], (x, y))
                if d < self.reach and abs(p["pos"][2] - z) < 4.0:
                    pk = p["kind"]
                    if pk not in held and any(w is None for w in wpn):
                        if d < 6.0:
                            take_id = p["id"]
                            break
        else:
            for p in obs.get("pickups", []):
                d = math.dist(p["pos"][:2], (x, y))
                if d < self.reach and abs(p["pos"][2] - z) < 4.0:
                    pk = p["kind"]
                    if pk not in held and any(w is None for w in wpn):
                        if pk in ("lightning", "mortar", "laser") or "gun" not in held:
                            take_id = p["id"]
                            break
        if take_id:
            act["take"] = take_id

        # --- A. Movement: continuous, never stop unnecessarily ---
        vx, vy = 0.0, 0.0

        # 1. Move toward safe zone (base priority)
        az = math.atan2(nc[1] - y, nc[0] - x)
        zone_strength = 0.6 if zone_pressure else 0.3
        vx += math.cos(az) * walk_speed * zone_strength
        vy += math.sin(az) * walk_speed * zone_strength

        # 4. Avoid moving directly toward groups; strafe around nearby enemies instead
        if near_enemies:
            # Strafe tangentially around nearest enemy (keep firing position)
            ex, ey = nearest["pos"][0], nearest["pos"][1]
            at = math.atan2(ey - y, ex - x)
            strafe = at + (math.pi * 0.5) * (1.0 if t % 200 < 100 else -1.0)
            vx += math.cos(strafe) * walk_speed * 0.5
            vy += math.sin(strafe) * walk_speed * 0.5

            # Avoid obstacles/buildings between us and enemy (3. Avoid obstacles)
            # If moving toward enemy would hit a wall, slide around
            cover = self.arena.nearest_cover(x, y)
            if cover:
                cx, cy = cover
                dxc, dyc = cx - x, cy - y
                dc = math.hypot(dxc, dyc)
                if dc > 2.0 and dc < 25.0:
                    # Nudge toward cover for firing-line break (D)
                    vx += (dxc / dc) * walk_speed * 0.25
                    vy += (dyc / dc) * walk_speed * 0.25
        else:
            # No enemies near: keep drifting toward zone, slow strafe
            vx += math.cos(t * 0.08) * walk_speed * 0.2
            vy += math.sin(t * 0.08) * walk_speed * 0.2

        # 5. Reposition when dangerous (zone pressure or taking fire)
        if zone_pressure and not near_enemies:
            vx += math.cos(az) * walk_speed * 0.4
            vy += math.sin(az) * walk_speed * 0.4

        # Obstacle avoidance: nudge away from blocked cells
        for k in range(8):
            ca = math.atan2(vy, vx) + (k - 4) * (math.pi / 8.0)
            px = x + math.cos(ca) * 2.0
            py = y + math.sin(ca) * 2.0
            if self.arena.blocked_at(px, py):
                # push away from blocked direction
                vx -= math.cos(ca) * walk_speed * 0.3
                vy -= math.sin(ca) * walk_speed * 0.3

        # Never stop: ensure minimum movement
        vm = math.hypot(vx, vy)
        if vm < 0.15:
            vx = math.cos(t * 0.1) * 0.4
            vy = math.sin(t * 0.1) * 0.4
            vm = 0.4

        if vm > 1.0:
            vx, vy = vx / vm, vy / vm

        act["move"] = [vx, vy]
        act["boost"] = bool(zone_pressure and eng > 20.0 and len(near_enemies) == 0)
        act["jump"] = bool(False)

        # --- Aim: face nearest threat (rear armor) ---
        if enemies:
            e = enemies[0]
            act["aim"] = [math.atan2(e["pos"][1] - y, e["pos"][0] - x), 0.0]
        else:
            act["aim"] = [math.atan2(vy, vx), 0.0]

        # --- B & C. Fire: gun primary, keep firing while moving ---
        fire = [False, False]
        aim_cmd = None

        # Choose best target: nearest valid threat
        target = None
        target_d = 9999.0
        for e in enemies:
            d = math.dist(e["pos"], me["pos"])
            if d < target_d:
                target_d = d
                target = e

        if target:
            aim_cmd = self._aim_point(me, mh, target["pos"])
            act["aim"] = aim_cmd
            ye = self._aim_yaw_error(me, aim_cmd)

            # B. Gun is reliable primary - fire whenever target in range and ready
            gi = self._weapon_slot(wpn, "gun")
            if gi is not None and target_d <= self.gun_range:
                w = wpn[gi]
                ammo = int(w.get("ammo", 12))
                if ammo > 0 and ye < 0.35:
                    fire[gi] = True

            # B. Lightning: close-range defense only (not reason to approach)
            li = self._weapon_slot(wpn, "lightning")
            if li is not None and not fire[gi] and target_d <= min(self.lightning_range, 35.0) and eng >= 18:
                ye2 = self._aim_yaw_error(me, aim_cmd)
                if ye2 < 0.35:
                    fire[li] = True

            # B. Laser: high-value damage when safe firing opportunity exists
            lsi = self._weapon_slot(wpn, "laser")
            if lsi is not None and not fire[gi] and not fire[li] and target_d <= self.laser_range:
                w = wpn[lsi]
                charge = float(w.get("charge", 0.0))
                # Fire if charged enough, or if we're at safe distance
                if charge >= 0.15 and ye < 0.30:
                    fire[lsi] = True

            # B. Mortar: attack enemies behind cover or groups (short-range lob)
            mi = self._weapon_slot(wpn, "mortar")
            if mi is not None and not any(fire) and target_d <= 60.0 and ye < 0.40:
                fire[mi] = True

        if aim_cmd is None:
            act["aim"] = [math.atan2(vy, vx), 0.0]

        act["fire"] = fire

        # Reload empty gun
        for i, w in enumerate(wpn):
            if w and w["kind"] == "gun" and int(w.get("ammo", 12)) == 0:
                act["reload"] = True

        mf.draw.label(f"HP:{hp:.0f}")
        return act

    def act(self, obs):
        try:
            return self._act(obs)
        except Exception as exc:
            mf.log(f"exc t{obs.get('tick', 0)}: {exc}")
            aim = obs.get("self", {}).get("aim") or [0.0, 0.0]
            return {"tick": int(obs.get("tick", 0)), "move": [0.0, 0.0],
                    "aim": [float(aim[0]), float(aim[1]) if len(aim) > 1 else 0.0], "fire": [False, False]}


mf.run(Bot)
