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
                for gx in range(int(math.floor((min(xs) - pad) / cs)), int(math.ceil((max(xs) + pad) / cs)) + 1):
                    cx = (gx + 0.5) * cs
                    for gy in range(int(math.floor((min(ys) - pad) / cs)), int(math.ceil((max(ys) + pad) / cs)) + 1):
                        cy = (gy + 0.5) * cs
                        hit = False
                        for ox, oy in offs:
                            px, py = cx + ox, cy + oy
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


class Bot:
    name = "survival-tactician-v24"

    def __init__(self, hello):
        self.cfg = hello.get("config", {})
        mech_cfg = self.cfg.get("mech", {})
        self.reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.arena = ArenaMap(hello.get("map", {}).get("dir", "/arena"),
                              mech_radius=float(mech_cfg.get("radius", 2.5)))
        self.gun_range = float(self.cfg.get("weapons", {}).get("gun", {}).get("range", 230.0))
        self.mortar_range = float(self.cfg.get("weapons", {}).get("mortar", {}).get("range", 225.0))
        self.lightning_range = float(self.cfg.get("weapons", {}).get("lightning", {}).get("range", 45.0))
        self.laser_range = float(self.cfg.get("weapons", {}).get("laser", {}).get("range", 280.0))
        self.strafe_dir = 1.0

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
        me = obs["self"]
        x, y, z = me["pos"]
        hp = float(me["hp"])
        max_hp = float(me["max_hp"])
        hf = hp / max(max_hp, 1.0)
        eng = float(me["energy"])
        wpn = me["weapons"]
        held = [w["kind"] for w in wpn if w is not None]
        mh = float(me.get("stats", {}).get("muzzle_height", 7.0))

        act = {"tick": t}

        if obs.get("offer"):
            act["pick"] = 0

        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        enemies.sort(key=lambda m: math.dist(m["pos"], me["pos"]))

        # Loot pickup - prioritize mortar for long-range splash
        for p in obs.get("pickups", []):
            if math.dist(p["pos"][:2], (x, y)) < self.reach and abs(p["pos"][2] - z) < 4.0:
                pk = p["kind"]
                if pk in held:
                    continue
                if any(w is None for w in wpn):
                    act["take"] = p["id"]
                    break

        vx, vy = 0.0, 0.0
        zone = obs["zone"]
        nc, nr = zone["next_center"], float(zone["next_radius"])
        dn = math.hypot(nc[0] - x, nc[1] - y)
        sr = max(12.0, nr * 0.72)

        # Zone movement
        if not me.get("in_zone", True) or dn > sr:
            az = math.atan2(nc[1] - y, nc[0] - x)
            vx += math.cos(az) * 3.0
            vy += math.sin(az) * 3.0

        if enemies:
            e = enemies[0]
            ex, ey = e["pos"][0], e["pos"][1]
            d = math.hypot(ex - x, ey - y)
            at = math.atan2(ey - y, ex - x)

            # Mortar-focused: stay at 60-150m, fire mortar (splash doesn't need precision)
            # Rush to point-blank only for gun finish on low-HP targets
            if d > 150.0:
                vx += math.cos(at) * 2.0  # close slowly
                vy += math.sin(at) * 2.0
            elif d < 30.0:
                # Point-blank: strafe to break aim, gun finish
                sa = at + (math.pi * 0.5) * self.strafe_dir
                vx += math.cos(sa) * 2.0
                vy += math.sin(sa) * 2.0
            # Between 30-150m: stay put, don't expose by moving
        else:
            vx += math.cos(t * 0.1) * 0.4
            vy += math.sin(t * 0.1) * 0.4

        if math.hypot(vx, vy) < 0.05:
            vx = math.cos(t * 0.13) * 0.4
            vy = math.sin(t * 0.13) * 0.4

        da = math.atan2(vy, vx)
        dm = min(1.0, math.hypot(vx, vy))
        best_a, best_s = da, -999.0
        for k in range(16):
            ca = da + (k - 8) * (math.pi / 8.0)
            al = math.cos(ca - da)
            cl = -4.0 if self.arena.blocked_at(x + math.cos(ca) * 1.5, y + math.sin(ca) * 1.5) else 1.0
            sc = al * 1.5 + cl * 3.0
            if sc > best_s:
                best_s = sc
                best_a = ca

        act["move"] = [math.cos(best_a) * dm, math.sin(best_a) * dm]
        act["boost"] = bool(enemies and eng > 25.0)
        act["jump"] = bool(False)

        # Fire at nearest enemy
        fire = [False, False]
        aim = None
        if enemies:
            e = enemies[0]
            tpos = e["pos"]
            dt = math.dist(tpos, me["pos"])
            thp = float(e.get("hp", 1400.0))

            # Mortar: primary long-range weapon (splash doesn't need precision)
            if wpn[0] and wpn[0]["kind"] == "mortar" and 30 <= dt <= self.mortar_range:
                aim = self._aim_mortar(me, mh, tpos)
                act["aim"] = aim
                my_aim = me.get("aim", [0, 0])
                ye = abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)
                my_pitch = float(my_aim[1]) if len(my_aim) > 1 else 0.0
                pe = abs(aim[1] - my_pitch)
                if ye < 0.30 and pe < 0.15:
                    fire[0] = True
            elif wpn[1] and wpn[1]["kind"] == "mortar" and 30 <= dt <= self.mortar_range:
                aim = self._aim_mortar(me, mh, tpos)
                act["aim"] = aim
                my_aim = me.get("aim", [0, 0])
                ye = abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)
                my_pitch = float(my_aim[1]) if len(my_aim) > 1 else 0.0
                pe = abs(aim[1] - my_pitch)
                if ye < 0.30 and pe < 0.15:
                    fire[1] = True

            # Gun: point-blank finish on low-HP targets
            elif wpn[0] and wpn[0]["kind"] == "gun" and dt <= 40.0:
                aim = self._aim_gun(me, mh, tpos)
                act["aim"] = aim
                my_aim = me.get("aim", [0, 0])
                ye = abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)
                my_pitch = float(my_aim[1]) if len(my_aim) > 1 else 0.0
                pe = abs(aim[1] - my_pitch)
                if thp < 200 or (ye < 0.20 and pe < 0.08):
                    fire[0] = True
            elif wpn[1] and wpn[1]["kind"] == "gun" and dt <= 40.0:
                aim = self._aim_gun(me, mh, tpos)
                act["aim"] = aim
                my_aim = me.get("aim", [0, 0])
                ye = abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)
                my_pitch = float(my_aim[1]) if len(my_aim) > 1 else 0.0
                pe = abs(aim[1] - my_pitch)
                if thp < 200 or (ye < 0.20 and pe < 0.08):
                    fire[1] = True

            # Lightning: close range burst on low-HP targets
            elif (wpn[0] and wpn[0]["kind"] == "lightning" and dt <= self.lightning_range and eng >= 18
                  and thp < 200):
                aim = self._aim_gun(me, mh, tpos)
                act["aim"] = aim
                my_aim = me.get("aim", [0, 0])
                ye = abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)
                if ye < 0.45:
                    fire[0] = True
            elif (wpn[1] and wpn[1]["kind"] == "lightning" and dt <= self.lightning_range and eng >= 18
                  and thp < 200):
                aim = self._aim_gun(me, mh, tpos)
                act["aim"] = aim
                my_aim = me.get("aim", [0, 0])
                ye = abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)
                if ye < 0.45:
                    fire[1] = True

        if aim is None:
            act["aim"] = [(t * 0.12) % (2 * math.pi) - math.pi, 0.0]

        act["fire"] = fire
        for i, w in enumerate(wpn):
            if w and w["kind"] == "gun" and int(w.get("ammo", 12)) == 0:
                act["reload"] = True

        if t % 100 == 0:
            ed = math.dist(enemies[0]["pos"], me["pos"]) if enemies else -1.0
            mf.log(f"t={t} hp={hp:.0f} alive={obs.get('alive')} d={ed:.0f}")

        mf.draw.label(f"HP:{hp:.0f}")
        return act

    def _aim_gun(self, me, mh, tpos):
        sx, sy, sz = me["pos"][0], me["pos"][1], me["pos"][2] + mh
        tx, ty, tz = tpos[0], tpos[1], tpos[2] + 4.5
        return [math.atan2(ty - sy, tx - sx),
                clamp(math.atan2(tz - sz, max(math.hypot(tx - sx, ty - sy), 0.01)), -1.3, 1.3)]

    def _aim_mortar(self, me, mh, tpos):
        sx, sy, sz = me["pos"][0], me["pos"][1], me["pos"][2] + mh + 2.5
        tx, ty, tz = tpos[0], tpos[1], tpos[2] + 4.5
        vc = self.cfg.get("weapons", {}).get("mortar", {})
        spd = max(float(vc.get("speed", 72.0)), 1.0)
        g = float(vc.get("gravity", 22.0))
        v2 = spd * spd
        horiz = max(math.hypot(tx - sx, ty - sy) - 3.0, 0.5)
        dz = tz - sz
        tf = math.hypot(tx - sx, ty - sy) / (spd * 0.85)
        pitch = 0.35
        for _ in range(4):
            disc = v2 * v2 - g * (g * horiz * horiz + 2.0 * dz * v2)
            pitch = math.atan((v2 - math.sqrt(disc)) / (g * horiz)) if disc > 0 else 0.785
            tf = horiz / (spd * max(math.cos(pitch), 0.25))
        return [math.atan2(ty - sy, tx - sx), clamp(pitch, -1.2, 1.35)]


mf.run(Bot)
