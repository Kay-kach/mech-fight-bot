from __future__ import annotations

import json
import math
import os
import mechfight as mf


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def point_in_poly(x: float, y: float, ring: list[float]) -> bool:
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
    def __init__(self, map_dir: str, mech_radius: float = 2.5):
        self.blocked: set[tuple[int, int]] = set()
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
                (0.0, 0.0), (pad, 0.0), (-pad, 0.0),
                (0.0, pad), (0.0, -pad),
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

    def is_blocked(self, x: float, y: float) -> bool:
        if not self.blocked:
            return False
        return (int(math.floor(x / self.cell_size)), int(math.floor(y / self.cell_size))) in self.blocked

    def path_clearance(self, x: float, y: float, angle: float) -> float:
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
    name = "survival-tactician-v15"

    def __init__(self, hello: mf.Hello):
        self.slot = int(hello.get("slot", 0))
        self.cfg = hello.get("config", {})
        mech_cfg = self.cfg.get("mech", {})
        self.mech_radius = float(mech_cfg.get("radius", 2.5))
        self.reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.weapons_cfg = self.cfg.get("weapons", {})
        map_dir = hello.get("map", {}).get("dir", "/arena")
        self.arena = ArenaMap(map_dir, mech_radius=self.mech_radius)

        vc = self.weapons_cfg
        self.gun_range = float(vc.get("gun", {}).get("range", 230.0))
        self.mortar_range = float(vc.get("mortar", {}).get("range", 225.0))
        self.laser_range = float(vc.get("laser", {}).get("range", 280.0))
        self.lightning_range = float(vc.get("lightning", {}).get("range", 45.0))
        self.laser_charge = float(vc.get("laser", {}).get("charge", 0.75))

        self.upgrade_priority = [
            "armor", "hp", "regen", "speed", "energy", "sensors",
            "gun_rate", "gun_dmg", "laser_charge", "mortar_cd",
        ]

        self.last_threat_yaw: float | None = None
        self.last_threat_tick: int = -999
        self.last_threat_id: int | None = None
        self.strafe_dir: float = 1.0
        self.prev_enemy_vel: dict[int, list[float]] = {}
        self.last_known_pos: dict[int, list[float]] = {}
        self.last_seen_tick: dict[int, int] = {}

        self.locked_target_id: int | None = None
        self.locked_target_h: float = 4.5
        self.lock_lost_tick: int = -999

    def act(self, obs: mf.Observation) -> mf.Action:
        try:
            return self._act(obs)
        except Exception as exc:
            mf.log(f"act() exception at tick {obs.get('tick', 0)}: {exc}")
            me_aim = obs.get("self", {}).get("aim") or [0.0, 0.0]
            return {
                "tick": int(obs.get("tick", 0)),
                "move": [0.0, 0.0],
                "aim": [float(me_aim[0]), float(me_aim[1]) if len(me_aim) > 1 else 0.0],
                "fire": [False, False],
            }

    def _choose_upgrade(self, offer: mf.Offer) -> int:
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

    def _weapon_utility(
        self, w: dict, dist: float, energy: float, enemy_weapons: list[str],
        target_hp: float, rushed: bool, charging_laser: bool,
    ) -> float:
        wk = w["kind"]

        if wk == "laser":
            charge = float(w.get("charge", 0.0))
            if charge > 0.0:
                return 100.0 if dist <= self.laser_range + 15.0 else 0.5
            if dist > self.laser_range:
                return -1.0
            if float(w.get("cooldown", 0.0)) > 0.25:
                return 0.2
            if rushed:
                return 0.1
            if target_hp < 100.0:
                return 0.5
            return 1.0

        if wk == "gun":
            if dist > self.gun_range:
                return -1.0
            if float(w.get("reload", 0.0)) > 0.15:
                return 0.1
            if target_hp < 150.0 and int(w.get("ammo", 12)) > 0:
                return 10.0
            # Gun only effective at close range (spread)
            if dist < 60.0:
                return 8.0
            if dist < 120.0:
                return 3.0
            return 1.0

        if wk == "lightning":
            if dist > self.lightning_range or energy < 18.0:
                return -1.0
            if target_hp < 150.0:
                return 9.0
            if "laser" in enemy_weapons:
                return 6.0
            return 2.0

        if wk == "mortar":
            if dist < 80.0 or dist > self.mortar_range:
                return -1.0
            if float(w.get("cooldown", 0.0)) > 0.25:
                return 0.2
            if rushed:
                return 0.1
            return 1.5

        return 0.0

    def _pickup_value(self, kind: str) -> float:
        return {"laser": 3.5, "lightning": 3.2, "mortar": 3.0, "gun": 1.0}.get(kind, 1.0)

    def _solve_aim(
        self, my_pos: list[float], muzzle_h: float, cubes: float,
        target_pos: list[float], target_vel: list[float], target_h_center: float,
        weapon_kind: str, w_stats: dict[str, float],
    ) -> list[float]:
        base_cfg = self.weapons_cfg.get(weapon_kind, {})
        sx, sy, sz = my_pos[0], my_pos[1], my_pos[2] + muzzle_h
        tx, ty, tz = target_pos[0], target_pos[1], target_pos[2] + target_h_center
        vx, vy, vz = target_vel[0], target_vel[1], target_vel[2]

        if weapon_kind in ("laser", "lightning"):
            dx, dy, dz = tx - sx, ty - sy, tz - sz
            yaw = math.atan2(dy, dx)
            pitch = math.atan2(dz, max(math.hypot(dx, dy), 0.01))
            return [yaw, clamp(pitch, -1.3, 1.3)]

        speed = max(float(w_stats.get("speed", base_cfg.get("speed", 252.0))), 1.0)

        if weapon_kind == "mortar":
            tube_offset = float(base_cfg.get("tube_offset", 2.5)) * (1.0 + 0.05 * cubes)
            sz = my_pos[2] + muzzle_h + tube_offset
            g = float(w_stats.get("gravity", base_cfg.get("gravity", 22.0)))
            v2 = speed * speed
            t_flight = math.hypot(tx - sx, ty - sy) / (speed * 0.85)
            pitch = 0.35
            for _ in range(4):
                px = tx + vx * t_flight
                py = ty + vy * t_flight
                pz = tz + vz * t_flight * 0.35
                dx, dy, dz = px - sx, py - sy, pz - sz
                horiz = max(math.hypot(dx, dy) - 3.0, 0.5)
                disc = v2 * v2 - g * (g * horiz * horiz + 2.0 * dz * v2)
                if disc > 0.0:
                    pitch = math.atan((v2 - math.sqrt(disc)) / (g * horiz))
                else:
                    pitch = 0.785
                cos_p = max(math.cos(pitch), 0.25)
                t_flight = horiz / (speed * cos_p)
            yaw = math.atan2(py - sy, px - sx)
            return [yaw, clamp(pitch, -1.2, 1.35)]

        dx, dy, dz = tx - sx, ty - sy, tz - sz
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        travel_t = max(0.0, dist - 3.0) / speed
        for _ in range(3):
            px = tx + vx * travel_t
            py = ty + vy * travel_t
            pz = tz + vz * travel_t * 0.5
            dx, dy, dz = px - sx, py - sy, pz - sz
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            travel_t = max(0.0, dist - 3.0) / speed
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(dz, max(math.hypot(dx, dy), 0.01))
        return [yaw, clamp(pitch, -1.3, 1.3)]

    def _dodge_vector(self, x: float, y: float, projectiles: list) -> tuple[float, float]:
        dodge_vx, dodge_vy = 0.0, 0.0
        for p in projectiles:
            px, py = p["pos"][0], p["pos"][1]
            pvx, pvy = p.get("vel", [0.0, 0.0])[0], p.get("vel", [0.0, 0.0])[1]
            d = math.hypot(px - x, py - y)
            if d < 45.0 and d > 2.0:
                to_me_x, to_me_y = x - px, y - py
                to_me_dist = math.hypot(to_me_x, to_me_y)
                if to_me_dist > 0.01:
                    to_me_x /= to_me_dist
                    to_me_y /= to_me_dist
                    vel_dist = math.hypot(pvx, pvy)
                    if vel_dist > 0.01:
                        align = (pvx / vel_dist) * to_me_x + (pvy / vel_dist) * to_me_y
                        if align > 0.2:
                            perp_x, perp_y = -to_me_y, to_me_x
                            clear1 = self.arena.path_clearance(x, y, math.atan2(perp_y, perp_x))
                            clear2 = self.arena.path_clearance(x, y, math.atan2(-perp_y, -perp_x))
                            if clear2 > clear1:
                                perp_x, perp_y = -perp_x, -perp_y
                            speed_factor = max(0.0, (45.0 - d) / 45.0)
                            dodge_vx += perp_x * 3.0 * speed_factor
                            dodge_vy += perp_y * 3.0 * speed_factor
        return dodge_vx, dodge_vy

    def _act(self, obs: mf.Observation) -> mf.Action:
        tick = int(obs["tick"])
        me = obs["self"]
        x, y, z = me["pos"]
        hp_frac = float(me["hp"]) / max(float(me["max_hp"]), 1.0)
        energy = float(me["energy"])
        cubes = float(me.get("cubes", 0.0))
        in_zone = bool(me.get("in_zone", True))
        weapons = me["weapons"]
        held_kinds = [w["kind"] for w in weapons if w is not None]
        is_charging_laser = any(
            w is not None and w["kind"] == "laser" and float(w.get("charge", 0.0)) > 0.0
            for w in weapons
        )

        act: mf.Action = {"tick": tick}

        if tick % 22 == 0:
            self.strafe_dir = -self.strafe_dir

        # 1. Upgrades
        if obs.get("offer"):
            act["pick"] = self._choose_upgrade(obs["offer"])

        # 2. Track incoming damage
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_threat_yaw = float(ev["bearing"]) if ev.get("bearing") is not None else None
                self.last_threat_tick = tick
                self.last_threat_id = int(ev["by"]) if ev.get("by") is not None else None

        # 3. Categorize visible enemies
        enemy_mechs = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m.get("kind") == "npc"]
        enemy_mechs.sort(key=lambda m: math.dist(m["pos"], me["pos"]))
        npcs.sort(key=lambda m: math.dist(m["pos"], me["pos"]))

        smoothed_vel: dict[int, list[float]] = {}
        for m in enemy_mechs + npcs:
            mid = int(m["id"])
            self.last_known_pos[mid] = list(m["pos"])
            self.last_seen_tick[mid] = tick
            raw_v = m.get("vel", [0.0, 0.0, 0.0])
            if mid in self.prev_enemy_vel:
                pv = self.prev_enemy_vel[mid]
                sv = [0.65 * raw_v[i] + 0.35 * pv[i] for i in range(3)]
            else:
                sv = list(raw_v)
            smoothed_vel[mid] = sv
            self.prev_enemy_vel[mid] = sv

        if tick % 100 == 0:
            stale = [mid for mid, ts in self.last_seen_tick.items() if tick - ts > 600]
            for mid in stale:
                self.last_seen_tick.pop(mid, None)
                self.last_known_pos.pop(mid, None)
                self.prev_enemy_vel.pop(mid, None)

        # 4. Weapon loot pickup - ALWAYS get a second weapon, prefer lightning/laser
        in_combat = len(enemy_mechs) > 0
        for p in obs.get("pickups", []):
            if math.dist(p["pos"][:2], (x, y)) < self.reach and abs(p["pos"][2] - z) < 4.0:
                pk = p["kind"]
                if pk in held_kinds:
                    continue
                empty = any(w is None for w in weapons)
                if empty:
                    act["take"] = p["id"]
                    break
                if not in_combat and not is_charging_laser:
                    candidates = sorted(
                        (self._pickup_value(w["kind"]), si) for si, w in enumerate(weapons) if w is not None
                    )
                    if candidates and self._pickup_value(pk) > candidates[0][0] + 0.3:
                        act["take"] = p["id"]
                        act["drop"] = candidates[0][1]
                        break

        # 5. Threat assessment
        nearby_enemies = [m for m in enemy_mechs if math.dist(me["pos"], m["pos"]) < 130.0]
        d_primary = math.dist(enemy_mechs[0]["pos"], me["pos"]) if enemy_mechs else 999.0

        primary = None
        if self.last_threat_id is not None and tick - self.last_threat_tick < 60:
            for m in enemy_mechs:
                if int(m["id"]) == self.last_threat_id:
                    primary = m
                    break
        if primary is None and enemy_mechs:
            primary = enemy_mechs[0]

        rushed = len(nearby_enemies) >= 2

        # 6. Movement
        vx_des, vy_des = 0.0, 0.0
        z_cur = obs["zone"]
        nx, ny = z_cur["next_center"]
        n_rad = float(z_cur["next_radius"])
        dist_next = math.hypot(nx - x, ny - y)
        safe_rad = max(12.0, n_rad * 0.72)

        if not in_zone or dist_next > safe_rad:
            w_zone = 4.0 if not in_zone else 2.0
            az = math.atan2(ny - y, nx - x)
            vx_des += math.cos(az) * w_zone
            vy_des += math.sin(az) * w_zone

        # Projectile dodging (highest priority)
        dodge_vx, dodge_vy = self._dodge_vector(x, y, obs.get("projectiles", []))
        dodging = math.hypot(dodge_vx, dodge_vy) > 0.1

        # Only engage house bots when we have HP/cubes advantage
        can_fight = hp_frac > 0.80 and cubes >= 2 and len(nearby_enemies) <= 1

        if must_retreat := (
            len(nearby_enemies) >= 2
            or (len(nearby_enemies) >= 1 and hp_frac < 0.60)
            or (primary is not None and d_primary < 70.0 and hp_frac < 0.80)
        ):
            # Retreat away from enemies
            for e in nearby_enemies:
                ex, ey = e["pos"][0], e["pos"][1]
                d = math.hypot(ex - x, ey - y)
                w = 1.0 + max(0.0, (130.0 - d) / 130.0)
                ang_away = math.atan2(y - ey, x - ex)
                vx_des += math.cos(ang_away) * w * 3.0
                vy_des += math.sin(ang_away) * w * 3.0
        elif dodging:
            vx_des += dodge_vx
            vy_des += dodge_vy
        elif can_fight and primary is not None:
            # Close to point-blank and kill
            ex, ey = primary["pos"][0], primary["pos"][1]
            d = math.hypot(ex - x, ey - y)
            ang_toward = math.atan2(ey - y, ex - x)
            ang_away = math.atan2(y - ey, x - ex)

            ideal_dist = 25.0  # point-blank

            if d < ideal_dist * 0.5:
                vx_des += math.cos(ang_away) * 1.0
                vy_des += math.sin(ang_away) * 1.0
            elif d > ideal_dist * 1.4:
                vx_des += math.cos(ang_toward) * 2.5
                vy_des += math.sin(ang_toward) * 2.5
            else:
                s_ang = ang_toward + (math.pi * 0.5) * self.strafe_dir
                vx_des += math.cos(s_ang) * 1.6
                vy_des += math.sin(s_ang) * 1.6
        else:
            # Avoid house bots, farm NPCs
            if enemy_mechs:
                ex, ey = primary["pos"][0], primary["pos"][1]
                d = math.hypot(ex - x, ey - y)
                ang_away = math.atan2(y - ey, x - ex)
                if d < 100.0:
                    vx_des += math.cos(ang_away) * 2.2
                    vy_des += math.sin(ang_away) * 2.2
            if npcs and hp_frac > 0.70:
                n = npcs[0]
                nd = math.dist(n["pos"], me["pos"])
                if nd < 55.0:
                    nx_, ny_ = n["pos"][0], n["pos"][1]
                    ang = math.atan2(ny_ - y, nx_ - x)
                    vx_des += math.cos(ang) * 1.6
                    vy_des += math.sin(ang) * 1.6

        for snd in obs.get("sounds", []):
            if snd.get("dist") == "near" and snd.get("kind") in ("gun", "laser", "mortar", "lightning", "explosion"):
                if must_retreat or (hp_frac < 0.65 and not enemy_mechs):
                    b = float(snd["bearing"])
                    vx_des -= math.cos(b) * 1.2
                    vy_des -= math.sin(b) * 1.2

        if not must_retreat and not dodging and not enemy_mechs and in_zone:
            if obs.get("cubes"):
                c = min(obs["cubes"], key=lambda it: math.dist(it["pos"], me["pos"]))
                if math.dist(c["pos"][:2], (x, y)) < 65.0:
                    ac = math.atan2(c["pos"][1] - y, c["pos"][0] - x)
                    vx_des += math.cos(ac) * 1.6
                    vy_des += math.sin(ac) * 1.6
            elif (weapons[0] is None or weapons[1] is None) and obs.get("pickups"):
                p = min(obs["pickups"], key=lambda it: math.dist(it["pos"], me["pos"]))
                if math.dist(p["pos"][:2], (x, y)) < 65.0:
                    ap = math.atan2(p["pos"][1] - y, p["pos"][0] - x)
                    vx_des += math.cos(ap) * 1.4
                    vy_des += math.sin(ap) * 1.4
            elif obs.get("crates") and obs.get("alive", 16) > 5:
                cr = min(obs["crates"], key=lambda it: math.dist(it["pos"], me["pos"]))
                if math.dist(cr["pos"][:2], (x, y)) < 45.0:
                    acr = math.atan2(cr["pos"][1] - y, cr["pos"][0] - x)
                    vx_des += math.cos(acr) * 1.1
                    vy_des += math.sin(acr) * 1.1

        if not must_retreat and not dodging and not enemy_mechs and hp_frac > 0.65:
            for dr in obs.get("drops", []):
                if (
                    math.dist(dr["pos"][:2], (x, y)) < 45.0
                    and float(dr.get("lands_in", 99.0)) < 4.0
                    and (dr.get("holder") is None or dr.get("holder") == self.slot)
                ):
                    adr = math.atan2(dr["pos"][1] - y, dr["pos"][0] - x)
                    vx_des += math.cos(adr) * 1.7
                    vy_des += math.sin(adr) * 1.7
                    break

        if math.hypot(vx_des, vy_des) < 0.15:
            orbit = math.atan2(y - ny, x - nx) + 0.45
            tx_idle = nx + math.cos(orbit) * (safe_rad * 0.45)
            ty_idle = ny + math.sin(orbit) * (safe_rad * 0.45)
            ai = math.atan2(ty_idle - y, tx_idle - x)
            vx_des = math.cos(ai) * 0.75
            vy_des = math.sin(ai) * 0.75

        des_ang = math.atan2(vy_des, vx_des)
        des_mag = min(1.0, math.hypot(vx_des, vy_des))
        best_move_ang = des_ang
        best_move_score = -999.0
        for k in range(16):
            cand_ang = des_ang + (k - 8) * (math.pi / 8.0)
            align = math.cos(cand_ang - des_ang)
            clear = self.arena.path_clearance(x, y, cand_ang)
            score = align * 1.4 + clear * 2.5
            if score > best_move_score:
                best_move_score = score
                best_move_ang = cand_ang

        act["move"] = [math.cos(best_move_ang) * des_mag, math.sin(best_move_ang) * des_mag]
        act["boost"] = bool((must_retreat or dodging or not in_zone) and energy > 25.0)
        act["jump"] = bool(must_retreat and me.get("grounded", False) and tick % 28 == 0 and energy > 35.0)

        # 7. Target selection & firing
        muzzle_h = float(me.get("stats", {}).get("muzzle_height", self.cfg.get("mech", {}).get("muzzle_height", 7.0)))
        my_aim = me.get("aim") or [0.0, 0.0]
        has_pitch = len(my_aim) > 1
        my_yaw = float(my_aim[0])
        my_pitch = float(my_aim[1]) if has_pitch else 0.0

        target = None
        target_h = 4.5
        holding_occluded_lock = False

        if is_charging_laser and self.locked_target_id is not None:
            all_candidates = enemy_mechs + npcs + obs.get("crates", [])
            for m in all_candidates:
                if int(m.get("id", -1)) == self.locked_target_id:
                    target = m
                    target_h = {"mech": 4.5, "npc": 1.75}.get(m.get("kind"), 1.2)
                    self.locked_target_h = target_h
                    self.lock_lost_tick = -999
                    break

            if target is None:
                last = self.last_known_pos.get(self.locked_target_id)
                if last is not None:
                    if self.lock_lost_tick == -999:
                        self.lock_lost_tick = tick
                    elapsed_ticks = tick - self.lock_lost_tick
                    dt_lost = min(elapsed_ticks * 0.05, 1.5)
                    lvel = self.prev_enemy_vel.get(self.locked_target_id, [0.0, 0.0, 0.0])
                    px = last[0] + lvel[0] * dt_lost
                    py = last[1] + lvel[1] * dt_lost
                    pz = last[2] + self.locked_target_h
                    dx_l, dy_l, dz_l = px - x, py - y, pz - (z + muzzle_h)
                    act["aim"] = [
                        math.atan2(dy_l, dx_l),
                        clamp(math.atan2(dz_l, max(math.hypot(dx_l, dy_l), 0.01)), -1.3, 1.3),
                    ]
                    holding_occluded_lock = True
                    if elapsed_ticks > 30:
                        fire = [False, False]
                        for idx, w in enumerate(weapons):
                            if w and w["kind"] == "laser" and float(w.get("charge", 0.0)) > 0.0:
                                fire[idx] = True
                        act["fire"] = fire
                        self.locked_target_id = None
                        self.lock_lost_tick = -999
                    else:
                        fire = [False, False]
                        for idx, w in enumerate(weapons):
                            if w and w["kind"] == "laser" and float(w.get("charge", 0.0)) > 0.0:
                                fire[idx] = True
                        act["fire"] = fire
                else:
                    self.locked_target_id = None
                    self.lock_lost_tick = -999

        if not holding_occluded_lock:
            if target is None and not is_charging_laser:
                self.locked_target_id = None
                self.lock_lost_tick = -999

                # Only fight house bots with HP/cubes advantage
                if can_fight and enemy_mechs:
                    target = primary
                    target_h = 4.5
                elif npcs and hp_frac > 0.70:
                    n = npcs[0]
                    if math.dist(n["pos"], me["pos"]) < 55.0:
                        target = n
                        target_h = 1.75
                elif obs.get("crates") and hp_frac > 0.70:
                    cr = min(obs["crates"], key=lambda c: math.dist(c["pos"], me["pos"]))
                    if math.dist(cr["pos"], me["pos"]) < 30.0:
                        target = cr
                        target_h = 1.2

            fire = [False, False]
            need_reload = False

            if target is not None:
                if "id" in target:
                    self.locked_target_id = int(target["id"])
                    self.locked_target_h = target_h
                else:
                    self.locked_target_id = None

                t_pos = target["pos"]
                tid = int(target["id"]) if "id" in target else -1
                t_vel = smoothed_vel.get(tid, target.get("vel", [0.0, 0.0, 0.0]))
                dist_t = math.dist(t_pos, me["pos"])
                enemy_weps = target.get("weapons", [])
                target_hp = float(target.get("hp", 1000.0))

                best_slot = None
                best_util = -0.5
                for idx, w in enumerate(weapons):
                    if w is None:
                        continue
                    u = self._weapon_utility(w, dist_t, energy, enemy_weps, target_hp, rushed, is_charging_laser)
                    if u > best_util:
                        best_util = u
                        best_slot = idx

                if best_slot is not None and weapons[best_slot] is not None:
                    pw = weapons[best_slot]
                    pk = pw["kind"]
                    ps = pw.get("stats", {})
                    aim = self._solve_aim(me["pos"], muzzle_h, cubes, t_pos, t_vel, target_h, pk, ps)
                    act["aim"] = aim

                    yaw_err = abs(angle_diff(aim[0], my_yaw))
                    pitch_err = abs(aim[1] - my_pitch) if has_pitch else 0.0

                    if pk in ("gun", "mortar"):
                        on_target = (yaw_err < 0.22) and (not has_pitch or pitch_err < 0.10)
                    elif pk == "lightning":
                        on_target = yaw_err < 0.42
                    else:
                        on_target = (yaw_err <= 0.09) and (not has_pitch or pitch_err <= 0.12)

                    if pk == "gun":
                        if on_target:
                            fire[best_slot] = True
                    elif pk == "mortar":
                        if 80.0 <= dist_t <= self.mortar_range and on_target:
                            fire[best_slot] = True
                    elif pk == "lightning":
                        if dist_t <= self.lightning_range and energy >= 18.0 and on_target:
                            fire[best_slot] = True
                    elif pk == "laser":
                        close = dist_t < 80.0
                        charge = float(pw.get("charge", 0.0))
                        full_c = float(ps.get("charge", self.laser_charge))
                        desired_charge = full_c * (0.25 if close else 0.70)
                        if target_hp < 100.0:
                            desired_charge = full_c * 0.35
                        if charge >= desired_charge and on_target:
                            fire[best_slot] = False
                            self.locked_target_id = None
                            self.lock_lost_tick = -999
                        else:
                            fire[best_slot] = True

                    # Secondary weapon
                    other_slot = 1 - best_slot
                    ow = weapons[other_slot]
                    if ow is not None and pk != "mortar" and ow["kind"] != "mortar":
                        ok = ow["kind"]
                        os_ = ow.get("stats", {})
                        base_o = self.weapons_cfg.get(ok, {})
                        if ok == "gun" and dist_t <= float(os_.get("range", base_o.get("range", self.gun_range))):
                            sec_aim = self._solve_aim(me["pos"], muzzle_h, cubes, t_pos, t_vel, target_h, ok, os_)
                            sec_yaw_err = abs(angle_diff(sec_aim[0], my_yaw))
                            sec_pitch_err = abs(sec_aim[1] - my_pitch) if has_pitch else 0.0
                            if sec_yaw_err < 0.18 and (not has_pitch or sec_pitch_err < 0.10):
                                fire[other_slot] = True
                        elif (
                            ok == "lightning"
                            and target_hp >= 150.0
                            and dist_t <= float(os_.get("range", base_o.get("range", self.lightning_range)))
                            and energy >= 22.0
                        ):
                            sec_aim = self._solve_aim(me["pos"], muzzle_h, cubes, t_pos, t_vel, target_h, ok, os_)
                            sec_yaw_err = abs(angle_diff(sec_aim[0], my_yaw))
                            if sec_yaw_err < 0.35:
                                fire[other_slot] = True
                else:
                    yaw = math.atan2(t_pos[1] - y, t_pos[0] - x)
                    act["aim"] = [yaw, 0.0]

                if not is_charging_laser:
                    for idx, w in enumerate(weapons):
                        if w and w["kind"] == "gun":
                            ammo = int(w.get("ammo", 12))
                            if ammo == 0 or (ammo <= 3 and not fire[idx] and any(fire)):
                                need_reload = True
            else:
                self.locked_target_id = None
                self.lock_lost_tick = -999
                if not enemy_mechs:
                    scan_yaw = (tick * 0.14) % (2.0 * math.pi) - math.pi
                    act["aim"] = [scan_yaw, 0.0]

                if not is_charging_laser:
                    for w in weapons:
                        if w and w["kind"] == "gun":
                            mag = int(w.get("stats", {}).get("mag", self.weapons_cfg.get("gun", {}).get("mag", 12)))
                            if int(w.get("ammo", mag)) <= int(mag * 0.4):
                                need_reload = True

            act["fire"] = fire
            if need_reload:
                act["reload"] = True

        if tick % 100 == 0:
            nearest_d = math.dist(enemy_mechs[0]["pos"], me["pos"]) if enemy_mechs else -1.0
            zone_frac = dist_next / max(safe_rad, 1.0)
            mf.log(
                f"t={tick} hp={me['hp']:.0f} alive={obs.get('alive')} "
                f"in_zone={in_zone} zfrac={zone_frac:.2f} "
                f"charging={is_charging_laser} locked={self.locked_target_id} enemy_d={nearest_d:.0f}"
            )

        mf.draw.label(f"HP:{me['hp']:.0f} En:{energy:.0f} Alive:{obs.get('alive')}")
        return act


mf.run(Bot)
