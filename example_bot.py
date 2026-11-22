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
    name = "survival-tactician-v19"

    def __init__(self, hello):
        self.cfg = hello.get("config", {})
        mech_cfg = self.cfg.get("mech", {})
        self.reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.arena = ArenaMap(hello.get("map", {}).get("dir", "/arena"),
                              mech_radius=float(mech_cfg.get("radius", 2.5)))
        self.gun_range = float(self.cfg.get("weapons", {}).get("gun", {}).get("range", 230.0))
        self.lightning_range = float(self.cfg.get("weapons", {}).get("lightning", {}).get("range", 45.0))
        self.strafe_dir = 1.0
        self.prev_vel = {}

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

        act = {"tick": t}

        if obs.get("offer"):
            act["pick"] = 0

        enemies = sorted((m for m in obs.get("mechs", []) if m.get("kind") == "mech"),
                         key=lambda m: math.dist(m["pos"], me["pos"]))
        npcs = sorted((m for m in obs.get("mechs", []) if m.get("kind") == "npc"),
                      key=lambda m: math.dist(m["pos"], me["pos"]))
        vel = {}
        for m in enemies + npcs:
            mid = int(m["id"])
            rv = m.get("vel", [0, 0, 0])
            if mid in self.prev_vel:
                pv = self.prev_vel[mid]
                sv = [0.7 * rv[i] + 0.3 * pv[i] for i in range(3)]
            else:
                sv = list(rv)
            vel[mid] = sv
            self.prev_vel[mid] = sv

        # Loot pickup (simple: take any new weapon into empty slot)
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
        if not me.get("in_zone", True) or dn > sr:
            az = math.atan2(nc[1] - y, nc[0] - x)
            vx += math.cos(az) * 2.5
            vy += math.sin(az) * 2.5

        # Dodge incoming projectiles (simple perpendicular dodge)
        for pj in obs.get("projectiles", []):
            px, py = pj["pos"][0], pj["pos"][1]
            pvx, pvy = pj.get("vel", [0, 0, 0])[0], pj.get("vel", [0, 0, 0])[1]
            d = math.hypot(px - x, py - y)
            if 2.0 < d < 25.0:
                tx, ty = x - px, y - py
                td = math.hypot(tx, ty)
                if td > 0.01:
                    tx /= td
                    ty /= td
                    vd = math.hypot(pvx, pvy)
                    if vd > 0.01:
                        align = (pvx / vd) * tx + (pvy / vd) * ty
                        if align > 0.1:
                            px2, py2 = -ty, tx
                            sf = max(0.0, (25.0 - d) / 25.0)
                            vx += px2 * 2.5 * sf
                            vy += py2 * 2.5 * sf

        if enemies:
            e = enemies[0]
            ex, ey = e["pos"][0], e["pos"][1]
            d = math.hypot(ex - x, ey - y)
            at = math.atan2(ey - y, ex - x)

            # Rush to point-blank (< 30m), then strafe
            if d > 30.0:
                vx += math.cos(at) * 2.5
                vy += math.sin(at) * 2.5
            else:
                sa = at + (math.pi * 0.5) * self.strafe_dir
                vx += math.cos(sa) * 1.8
                vy += math.sin(sa) * 1.8
        else:
            # No enemies: farm NPCs or wander
            if npcs:
                n = npcs[0]
                nd = math.dist(n["pos"], me["pos"])
                if nd < 50.0:
                    vx += math.cos(math.atan2(n["pos"][1] - y, n["pos"][0] - x)) * 1.8
                    vy += math.sin(math.atan2(n["pos"][1] - y, n["pos"][0] - x)) * 1.8
                else:
                    vx += math.cos(t * 0.1) * 0.5
                    vy += math.sin(t * 0.1) * 0.5
            else:
                vx += math.cos(t * 0.1) * 0.5
                vy += math.sin(t * 0.1) * 0.5

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

        # Fire only at point-blank range where spread doesn't matter
        fire = [False, False]
        aim = None
        if enemies:
            e = enemies[0]
            eid = int(e["id"])
            tpos = e["pos"]
            tvel = vel.get(eid, e.get("vel", [0, 0, 0]))
            dt = math.dist(tpos, me["pos"])
            thp = float(e.get("hp", 1400.0))

            if dt < 30.0:
                best_slot, best_u = None, -1.0
                for i, w in enumerate(wpn):
                    if w is None:
                        continue
                    wk = w["kind"]
                    if wk == "gun" and dt <= self.gun_range:
                        u = 10.0 if thp < 150 else 8.0
                    elif wk == "lightning" and dt <= self.lightning_range and eng >= 18:
                        u = 9.0 if thp < 150 else 1.0
                    elif wk == "laser":
                        u = 2.0 if dt <= 280 else -1.0
                    else:
                        continue
                    if u > best_u:
                        best_u = u
                        best_slot = i

                if best_slot is not None:
                    w = wpn[best_slot]
                    mh = float(me.get("stats", {}).get("muzzle_height", 7.0))
                    aim = self._aim(me, mh, tpos, tvel, wk, w.get("stats", {}))
                    act["aim"] = aim
                    my_aim = me.get("aim", [0, 0])
                    ye = abs((aim[0] - float(my_aim[0]) + math.pi) % (2 * math.pi) - math.pi)
                    if wk == "gun" and ye < 0.25:
                        fire[best_slot] = True
                    elif wk == "lightning" and dt <= self.lightning_range and eng >= 18 and ye < 0.4:
                        fire[best_slot] = True
                    elif wk == "laser":
                        chg = float(w.get("charge", 0.0))
                        if chg > 0.5 and ye < 0.1:
                            fire[best_slot] = False
                        else:
                            fire[best_slot] = True

        if aim is None:
            act["aim"] = [(t * 0.12) % (2 * math.pi) - math.pi, 0.0]

        act["fire"] = fire
        if any(w and w["kind"] == "gun" and int(w.get("ammo", 12)) == 0 for w in wpn):
            act["reload"] = True

        if t % 100 == 0:
            ed = math.dist(enemies[0]["pos"], me["pos"]) if enemies else -1.0
            mf.log(f"t={t} hp={hp:.0f} alive={obs.get('alive')} d={ed:.0f}")

        mf.draw.label(f"HP:{hp:.0f}")
        return act

    def _aim(self, me, mh, tpos, tvel, wk, ws):
        sx, sy, sz = me["pos"][0], me["pos"][1], me["pos"][2] + mh
        tx, ty, tz = tpos[0], tpos[1], tpos[2] + 4.5
        vx, vy, vz = tvel[0], tvel[1], tvel[2]

        if wk in ("laser", "lightning"):
            return [math.atan2(ty - sy, tx - sx),
                    clamp(math.atan2(tz - sz, max(math.hypot(tx - sx, ty - sy), 0.01)), -1.3, 1.3)]

        base = self.cfg.get("weapons", {}).get(wk, {})
        spd = max(float(ws.get("speed", base.get("speed", 252.0))), 1.0)

        if wk == "mortar":
            g = float(ws.get("gravity", base.get("gravity", 22.0)))
            v2 = spd * spd
            tf = math.hypot(tx - sx, ty - sy) / (spd * 0.85)
            pitch = 0.35
            for _ in range(4):
                px = tx + vx * tf
                py = ty + vy * tf
                pz = tz + vz * tf * 0.35
                dx, dy, dz = px - sx, py - sy, pz - sz
                hz = max(math.hypot(dx, dy) - 3.0, 0.5)
                disc = v2 * v2 - g * (g * hz * hz + 2.0 * dz * v2)
                pitch = math.atan((v2 - math.sqrt(disc)) / (g * hz)) if disc > 0 else 0.785
                tf = hz / (spd * max(math.cos(pitch), 0.25))
            return [math.atan2(py - sy, px - sx), clamp(pitch, -1.2, 1.35)]

        dx, dy, dz = tx - sx, ty - sy, tz - sz
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        tf = max(0.0, dist - 3.0) / spd
        for _ in range(3):
            px = tx + vx * tf
            py = ty + vy * tf
            pz = tz + vz * tf * 0.5
            dx, dy, dz = px - sx, py - sy, pz - sz
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            tf = max(0.0, dist - 3.0) / spd
        return [math.atan2(dy, dx), clamp(math.atan2(dz, max(math.hypot(dx, dy), 0.01)), -1.3, 1.3)]


mf.run(Bot)
