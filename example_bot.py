from __future__ import annotations

import json
import math
import os
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
            offsets = ((0, 0), (pad, 0), (-pad, 0), (0, pad), (0, -pad),
                       (diag, diag), (diag, -diag), (-diag, diag), (-diag, -diag))
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
        return (int(math.floor(x / self.cell_size)), int(math.floor(y / self.cell_size))) in self.blocked

    def path_clearance(self, x, y, angle):
        if not self.blocked:
            return 1.0
        ca, sa = math.cos(angle), math.sin(angle)
        if self.is_blocked(x + ca * 1.5, y + sa * 1.5):
            return -4.0
        score = 0.0
        for dist, weight in ((3.5, 0.45), (7.0, 0.35), (12.0, 0.20)):
            if self.is_blocked(x + ca * dist, y + sa * dist):
                score -= 0.18
                break
            score += weight
        return score


class Bot:
    name = "survival-tactician-v16"

    def __init__(self, hello):
        self.cfg = hello.get("config", {})
        mech_cfg = self.cfg.get("mech", {})
        self.mech_radius = float(mech_cfg.get("radius", 2.5))
        self.reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.weapons_cfg = self.cfg.get("weapons", {})
        map_dir = hello.get("map", {}).get("dir", "/arena")
        self.arena = ArenaMap(map_dir, mech_radius=self.mech_radius)

        vc = self.weapons_cfg
        self.gun_range = float(vc.get("gun", {}).get("range", 230.0))
        self.lightning_range = float(vc.get("lightning", {}).get("range", 45.0))

        self.upgrade_priority = ["armor", "hp", "regen", "speed", "energy",
                                 "gun_rate", "gun_dmg", "laser_charge", "mortar_cd"]

        self.last_threat_tick = -999
        self.strafe_dir = 1.0
        self.prev_enemy_vel = {}
        self.last_known_pos = {}
        self.last_seen_tick = {}
        self.locked_target_id = None
        self.lock_lost_tick = -999

    def act(self, obs):
        try:
            return self._act(obs)
        except Exception as exc:
            mf.log(f"act() exception at tick {obs.get('tick', 0)}: {exc}")
            me_aim = obs.get("self", {}).get("aim") or [0.0, 0.0]
            return {"tick": int(obs.get("tick", 0)), "move": [0.0, 0.0],
                    "aim": [float(me_aim[0]), float(me_aim[1]) if len(me_aim) > 1 else 0.0],
                    "fire": [False, False]}

    def _choose_upgrade(self, offer):
        best_idx = 0
        best_rank = 999
        for i, c in enumerate(offer.get("choices", [])):
            cid = c.get("id", "")
            if cid in self.upgrade_priority:
                rank = self.upgrade_priority.index(cid)
                if rank < best_rank:
                    best_rank = rank
                    best_idx = i
        return best_idx

    def _dodge(self, x, y, projectiles):
        dvx, dvy = 0.0, 0.0
        for p in projectiles:
            px, py = p["pos"][0], p["pos"][1]
            pvx, pvy = p.get("vel", [0, 0, 0])[0], p.get("vel", [0, 0, 0])[1]
            d = math.hypot(px - x, py - y)
            if 2.0 < d < 40.0:
                tx, ty = x - px, y - py
                td = math.hypot(tx, ty)
                if td > 0.01:
                    tx /= td
                    ty /= td
                    vd = math.hypot(pvx, pvy)
                    if vd > 0.01:
                        align = (pvx / vd) * tx + (pvy / vd) * ty
                        if align > 0.2:
                            px_, py_ = -ty, tx
                            c1 = self.arena.path_clearance(x, y, math.atan2(py_, px_))
                            c2 = self.arena.path_clearance(x, y, math.atan2(-py_, -px_))
                            if c2 > c1:
                                px_, py_ = -px_, -py_
                            sf = max(0.0, (40.0 - d) / 40.0)
                            dvx += px_ * 3.0 * sf
                            dvy += py_ * 3.0 * sf
        return dvx, dvy

    def _act(self, obs):
        tick = int(obs["tick"])
        me = obs["self"]
        x, y, z = me["pos"]
        hp = float(me["hp"])
        max_hp = float(me["max_hp"])
        hp_frac = hp / max(max_hp, 1.0)
        energy = float(me["energy"])
        weapons = me["weapons"]
        held = [w["kind"] for w in weapons if w is not None]

        act = {"tick": tick}

        if obs.get("offer"):
            act["pick"] = self._choose_upgrade(obs["offer"])

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_threat_tick = tick

        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m.get("kind") == "npc"]
        enemies.sort(key=lambda m: math.dist(m["pos"], me["pos"]))
        npcs.sort(key=lambda m: math.dist(m["pos"], me["pos"]))

        vel = {}
        for m in enemies + npcs:
            mid = int(m["id"])
            self.last_known_pos[mid] = list(m["pos"])
            self.last_seen_tick[mid] = tick
            rv = m.get("vel", [0, 0, 0])
            if mid in self.prev_enemy_vel:
                pv = self.prev_enemy_vel[mid]
                sv = [0.65 * rv[i] + 0.35 * pv[i] for i in range(3)]
            else:
                sv = list(rv)
            vel[mid] = sv
            self.prev_enemy_vel[mid] = sv

        if tick % 100 == 0:
            stale = [mid for mid, ts in self.last_seen_tick.items() if tick - ts > 600]
            for mid in stale:
                self.last_seen_tick.pop(mid, None)
                self.last_known_pos.pop(mid, None)
                self.prev_enemy_vel.pop(mid, None)

        nearby = [m for m in enemies if math.dist(me["pos"], m["pos"]) < 130.0]
        d_primary = math.dist(enemies[0]["pos"], me["pos"]) if enemies else 999.0
        primary = enemies[0] if enemies else None

        # Loot pickup
        for p in obs.get("pickups", []):
            if math.dist(p["pos"][:2], (x, y)) < self.reach and abs(p["pos"][2] - z) < 4.0:
                pk = p["kind"]
                if pk in held:
                    continue
                if any(w is None for w in weapons):
                    act["take"] = p["id"]
                    break
                if hp_frac > 0.7 and not enemies:
                    cand = sorted(({"laser": 3.5, "lightning": 3.2, "mortar": 3.0, "gun": 1.0}.get(
                        w["kind"], 1.0), si) for si, w in enumerate(weapons) if w is not None)
                    if cand and {"laser": 3.5, "lightning": 3.2, "mortar": 3.0, "gun": 1.0}.get(pk, 1.0) > cand[0][0] + 0.3:
                        act["take"] = p["id"]
                        act["drop"] = cand[0][1]
                        break

        # Movement
        vx, vy = 0.0, 0.0
        zone = obs["zone"]
        nc, nr = zone["next_center"], float(zone["next_radius"])
        dn = math.hypot(nc[0] - x, nc[1] - y)
        sr = max(12.0, nr * 0.72)

        if not me.get("in_zone", True) or dn > sr:
            az = math.atan2(nc[1] - y, nc[0] - x)
            vx += math.cos(az) * 3.0
            vy += math.sin(az) * 3.0

        dvx, dvy = self._dodge(x, y, obs.get("projectiles", []))
        dodging = math.hypot(dvx, dvy) > 0.05

        outnumbered = len(nearby) >= 2
        low_hp = hp_frac < 0.35

        if outnumbered or low_hp:
            for e in nearby:
                ex, ey = e["pos"][0], e["pos"][1]
                d = math.hypot(ex - x, ey - y)
                w = 1.0 + (130.0 - d) / 130.0
                aa = math.atan2(y - ey, x - ex)
                vx += math.cos(aa) * w * 3.0
                vy += math.sin(aa) * w * 3.0
        elif dodging:
            vx += dvx
            vy += dvy
        elif primary is not None:
            ex, ey = primary["pos"][0], primary["pos"][1]
            d = math.hypot(ex - x, ey - y)
            at = math.atan2(ey - y, ex - x)
            aa = math.atan2(y - ey, x - ex)

            if d < 20.0:
                vx += math.cos(aa) * 1.0
                vy += math.sin(aa) * 1.0
            elif d > 35.0:
                vx += math.cos(at) * 2.5
                vy += math.sin(at) * 2.5
            else:
                sa = at + (math.pi * 0.5) * self.strafe_dir
                vx += math.cos(sa) * 2.0
                vy += math.sin(sa) * 2.0
        else:
            vx += dvx or math.cos(tick * 0.1)
            vy += dvy or math.sin(tick * 0.1)

        for s in obs.get("sounds", []):
            if s.get("dist") == "near" and s.get("kind") in ("gun", "laser", "mortar", "lightning", "explosion"):
                if outnumbered or low_hp or not enemies:
                    b = float(s["bearing"])
                    vx -= math.cos(b) * 1.2
                    vy -= math.sin(b) * 1.2

        if not (outnumbered or dodging) and not enemies and me.get("in_zone", True):
            if obs.get("npcs"):
                n = npcs[0]
                nd = math.dist(n["pos"], me["pos"])
                if nd < 50.0:
                    vx += math.cos(math.atan2(n["pos"][1] - y, n["pos"][0] - x)) * 1.6
                    vy += math.sin(math.atan2(n["pos"][1] - y, n["pos"][0] - x)) * 1.6

        if math.hypot(vx, vy) < 0.1:
            vx = math.cos(tick * 0.13) * 0.5
            vy = math.sin(tick * 0.13) * 0.5

        da = math.atan2(vy, vx)
        dm = min(1.0, math.hypot(vx, vy))
        best_a, best_s = da, -999.0
        for k in range(16):
            ca = da + (k - 8) * (math.pi / 8.0)
            al = math.cos(ca - da)
            cl = self.arena.path_clearance(x, y, ca)
            sc = al * 1.4 + cl * 2.5
            if sc > best_s:
                best_s = sc
                best_a = ca

        act["move"] = [math.cos(best_a) * dm, math.sin(best_a) * dm]
        act["boost"] = bool((outnumbered or dodging or not me.get("in_zone", True)) and energy > 25.0)
        act["jump"] = bool(outnumbered and me.get("grounded", False) and tick % 28 == 0 and energy > 35.0)

        # Firing
        muzzle_h = float(me.get("stats", {}).get("muzzle_height", 7.0))
        maim = me.get("aim") or [0.0, 0.0]
        has_pitch = len(maim) > 1
        my_yaw = float(maim[0])
        my_pitch = float(maim[1]) if has_pitch else 0.0

        target = None
        target_h = 4.5
        holding = False

        # Simple target: nearest enemy at point-blank, or NPC if safe
        if enemies and hp_frac > 0.50 and d_primary < 40.0:
            target = primary
            target_h = 4.5
        elif npcs and hp_frac > 0.70 and not enemies:
            n = npcs[0]
            if math.dist(n["pos"], me["pos"]) < 45.0:
                target = n
                target_h = 1.75

        fire = [False, False]
        if target is not None:
            tid = int(target["id"])
            self.locked_target_id = tid
            tpos = target["pos"]
            tvel = vel.get(tid, target.get("vel", [0, 0, 0]))
            dt = math.dist(tpos, me["pos"])
            thp = float(target.get("hp", 1000.0))

            best_slot, best_u = None, -0.5
            for idx, w in enumerate(weapons):
                if w is None:
                    continue
                wk = w["kind"]
                if wk == "gun":
                    if dt > self.gun_range:
                        continue
                    u = 10.0 if thp < 150 else 8.0 if dt < 60 else 3.0 if dt < 120 else 1.0
                elif wk == "lightning":
                    if dt > self.lightning_range or energy < 18:
                        continue
                    u = 9.0 if thp < 150 else 2.0
                elif wk == "laser":
                    chg = float(w.get("charge", 0.0))
                    if chg > 0.0:
                        u = 100.0 if dt <= float(wc if False else self.weapons_cfg.get("laser", {}).get("range", 280.0)) + 15 else 0.5
                    else:
                        u = 0.5
                elif wk == "mortar":
                    if dt < 70 or dt > float(self.weapons_cfg.get("mortar", {}).get("range", 225.0)):
                        continue
                    u = 2.0
                else:
                    continue
                if float(w.get("reload", 0.0)) > 0.15:
                    u *= 0.1
                if u > best_u:
                    best_u = u
                    best_slot = idx

            if best_slot is not None:
                w = weapons[best_slot]
                aim = self._aim(me, muzzle_h, tpos, tvel, target_h, w["kind"], w.get("stats", {}))
                act["aim"] = aim
                ye = abs(angle_diff(aim[0], my_yaw))
                pe = abs(aim[1] - my_pitch) if has_pitch else 0.0
                ot = (ye < 0.22 and (not has_pitch or pe < 0.10)) if w["kind"] in ("gun", "mortar") else \
                     (ye < 0.42) if w["kind"] == "lightning" else \
                     (ye <= 0.09 and (not has_pitch or pe <= 0.12))

                if w["kind"] == "gun" and ot:
                    fire[best_slot] = True
                elif w["kind"] == "lightning" and dt <= self.lightning_range and energy >= 18 and ot:
                    fire[best_slot] = True
                elif w["kind"] == "laser":
                    chg = float(w.get("charge", 0.0))
                    fc = float(w.get("stats", {}).get("charge", 0.75))
                    dc = fc * 0.30
                    if chg >= dc and ot:
                        fire[best_slot] = False
                        self.locked_target_id = None
                    else:
                        fire[best_slot] = True
                elif w["kind"] == "mortar" and 70 <= dt and ot:
                    fire[best_slot] = True

                # Secondary
                os_ = 1 - best_slot
                ow = weapons[os_]
                if ow is not None and w["kind"] != "mortar" and ow["kind"] != "mortar":
                    ok = ow["kind"]
                    if ok == "gun" and dt <= self.gun_range:
                        sa = self._aim(me, muzzle_h, tpos, tvel, target_h, ok, ow.get("stats", {}))
                        sye = abs(angle_diff(sa[0], my_yaw))
                        spe = abs(sa[1] - my_pitch) if has_pitch else 0.0
                        if sye < 0.18 and (not has_pitch or spe < 0.10):
                            fire[os_] = True
                    elif ok == "lightning" and thp >= 150 and dt <= self.lightning_range and energy >= 22:
                        sa = self._aim(me, muzzle_h, tpos, tvel, target_h, ok, ow.get("stats", {}))
                        sye = abs(angle_diff(sa[0], my_yaw))
                        if sye < 0.35:
                            fire[os_] = True
        else:
            self.locked_target_id = None
            if not enemies:
                act["aim"] = [(tick * 0.14) % (2 * math.pi) - math.pi, 0.0]

        act["fire"] = fire
        if any(w and w["kind"] == "gun" and (int(w.get("ammo", 12)) == 0 or int(w.get("ammo", 12)) <= 3) for w in weapons):
            act["reload"] = True

        if tick % 100 == 0:
            nd = math.dist(enemies[0]["pos"], me["pos"]) if enemies else -1.0
            mf.log(f"t={tick} hp={hp:.0f} alive={obs.get('alive')} enemy_d={nd:.0f}")

        mf.draw.label(f"HP:{hp:.0f} En:{energy:.0f}")
        return act

    def _aim(self, me, muzzle_h, tpos, tvel, target_h, wk, ws):
        sx, sy, sz = me["pos"][0], me["pos"][1], me["pos"][2] + muzzle_h
        tx, ty, tz = tpos[0], tpos[1], tpos[2] + target_h
        vx, vy, vz = tvel[0], tvel[1], tvel[2]

        if wk in ("laser", "lightning"):
            return [math.atan2(ty - sy, tx - sx),
                    clamp(math.atan2(tz - sz, max(math.hypot(tx - sx, ty - sy), 0.01)), -1.3, 1.3)]

        base = self.weapons_cfg.get(wk, {})
        speed = max(float(ws.get("speed", base.get("speed", 252.0))), 1.0)

        if wk == "mortar":
            g = float(ws.get("gravity", base.get("gravity", 22.0)))
            v2 = speed * speed
            tf = math.hypot(tx - sx, ty - sy) / (speed * 0.85)
            pitch = 0.35
            for _ in range(4):
                px = tx + vx * tf
                py = ty + vy * tf
                pz = tz + vz * tf * 0.35
                dx, dy, dz = px - sx, py - sy, pz - sz
                hz = max(math.hypot(dx, dy) - 3.0, 0.5)
                disc = v2 * v2 - g * (g * hz * hz + 2.0 * dz * v2)
                pitch = math.atan((v2 - math.sqrt(disc)) / (g * hz)) if disc > 0 else 0.785
                tf = hz / (speed * max(math.cos(pitch), 0.25))
            return [math.atan2(py - sy, px - sx), clamp(pitch, -1.2, 1.35)]

        dx, dy, dz = tx - sx, ty - sy, tz - sz
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        tf = max(0.0, dist - 3.0) / speed
        for _ in range(3):
            px = tx + vx * tf
            py = ty + vy * tf
            pz = tz + vz * tf * 0.5
            dx, dy, dz = px - sx, py - sy, pz - sz
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            tf = max(0.0, dist - 3.0) / speed
        return [math.atan2(dy, dx), clamp(math.atan2(dz, max(math.hypot(dx, dy), 0.01)), -1.3, 1.3)]


mf.run(Bot)
