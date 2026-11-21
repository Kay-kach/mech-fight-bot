from __future__ import annotations

import json
import math
import os
import random
import mechfight as mf


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def angle_diff(a: float, b: float) -> float:
    """Shortest signed angle from b to a in [-pi, pi]."""
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
            mf.log(f"WARNING: Map file not found at {world_path}; obstacle avoidance disabled.")
            return
        try:
            with open(world_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cs = self.cell_size
            pad = self.pad
            diag = pad * 0.7071
            offsets = (
                (0.0, 0.0),
                (pad, 0.0),
                (-pad, 0.0),
                (0.0, pad),
                (0.0, -pad),
                (diag, diag),
                (diag, -diag),
                (-diag, diag),
                (-diag, -diag),
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
            mf.log(f"Loaded {len(self.blocked)} blocked map cells from {world_path}")
        except Exception as exc:
            mf.log(f"ERROR loading map {world_path}: {exc}")

    def is_blocked(self, x: float, y: float) -> bool:
        if not self.blocked:
            return False
        gx = int(math.floor(x / self.cell_size))
        gy = int(math.floor(y / self.cell_size))
        return (gx, gy) in self.blocked

    def path_clearance(self, x: float, y: float, angle: float) -> float:
        if not self.blocked:
            return 1.0
        ca, sa = math.cos(angle), math.sin(angle)
        if self.is_blocked(x + ca * 1.5, y + sa * 1.5):
            return -4.0
        probes = ((3.5, 0.50), (7.0, 0.40), (12.0, 0.25))
        score = 0.0
        for dist, weight in probes:
            if self.is_blocked(x + ca * dist, y + sa * dist):
                score -= 0.20
                break
            score += weight
        return score


class Bot:
    name = "survival-tactician-v6.2"

    def __init__(self, hello: mf.Hello):
        self.slot = int(hello.get("slot", 0))
        self.rng = random.Random(int(hello.get("seed", 0)))
        self.cfg = hello.get("config", {})
        self.reach = float(self.cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.weapons_cfg = self.cfg.get("weapons", {})
        mech_radius = float(self.cfg.get("mech", {}).get("radius", 2.5))
        map_dir = hello.get("map", {}).get("dir", "/arena")
        self.arena = ArenaMap(map_dir, mech_radius=mech_radius)

        self.upgrade_priority = [
            "armor",
            "hp",
            "regen",
            "energy",
            "speed",
            "sensors",
            "gun_rate",
            "gun_dmg",
            "laser_charge",
            "mortar_cd",
        ]

        self.last_threat_yaw: float | None = None
        self.last_threat_tick: int = -999
        self.strafe_dir: float = 1.0
        self.prev_enemy_vel: dict[int, list[float]] = {}
        self.last_known_pos: dict[int, list[float]] = {}
        self.last_seen_tick: dict[int, int] = {}

        self.locked_target_id: int | None = None
        self.locked_target_h: float = 4.5
        self.lock_lost_tick: int = -999

        self.last_jump_tick: int = -999
        self.last_boost_tick: int = -999

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
        hp = float(offer.get("self", {}).get("hp", 1.0))
        energy = float(offer.get("self", {}).get("energy", 1.0))

        # Dynamic upgrade priority: boost survival upgrades first when low
        priority = self.upgrade_priority.copy()
        if hp < 0.5:
            priority.insert(0, "hp")
            priority.insert(1, "armor")
        if energy < 0.4:
            priority.insert(2, "energy")

        best_idx = 0
        best_rank = 999
        for i, c in enumerate(offer.get("choices", [])):
            cid = c.get("id", "")
            if cid in priority:
                rank = priority.index(cid)
                if rank < best_rank:
                    best_rank = rank
                    best_idx = i
        return best_idx

    def _weapon_utility(
        self,
        w: dict,
        dist: float,
        energy: float,
        enemy_weapons: list[str],
        target_hp: float,
    ) -> float:
        """Evaluate weapon usefulness more finely considering enemy armament and ammo."""
        wk = w["kind"]
        ws = w.get("stats", {})
        w_cfg = self.weapons_cfg.get(wk, {})
        w_range = float(ws.get("range", w_cfg.get("range", 230.0)))
        energy_cost = float(ws.get("energy", 0.0))

        if wk == "laser":
            charge = float(w.get("charge", 0.0))
            if charge > 0.0:
                return 100.0 if dist <= w_range + 15.0 else 0.5
            if dist > w_range:
                return -1.0
            cooldown = float(w.get("cooldown", 0.0))
            if cooldown > 0.20:
                return 0.3
            base_score = 4.0
            if "mortar" in enemy_weapons:
                base_score += 2.0
            if target_hp < 150:
                base_score *= 1.2
            return base_score

        if wk == "gun":
            if dist > w_range:
                return -1.0
            if float(w.get("reload", 0.0)) > 0.15:
                return 0.15
            ammo = int(w.get("ammo", 12))
            if ammo == 0:
                return -1.0
            if target_hp < 150:
                return 5.0
            return 2.5

        if wk == "lightning":
            l_range = float(ws.get("range", self.weapons_cfg.get("lightning", {}).get("range", 45.0)))
            if dist > l_range or energy < 20.0:
                return -1.0
            if target_hp < 150.0:
                return 3.8
            score = 5.0
            if "laser" in enemy_weapons:
                score += 2.5
            return score

        if wk == "mortar":
            if dist < 20.0 or dist > 225.0:
                return -1.0
            cooldown = float(w.get("cooldown", 0.0))
            if cooldown > 0.20:
                return 0.25
            score = 3.5
            if "lightning" in enemy_weapons:
                score += 1.7
            return score

        return 0.0

    def _pickup_value(self, kind: str) -> float:
        return {"laser": 4.0, "lightning": 3.8, "mortar": 3.2, "gun": 1.5}.get(kind, 1.0)

    def _solve_aim(
        self,
        my_pos: list[float],
        muzzle_h: float,
        cubes: float,
        target_pos: list[float],
        target_vel: list[float],
        target_h_center: float,
        weapon_kind: str,
        w_stats: dict[str, float],
    ) -> list[float]:
        base_cfg = self.weapons_cfg.get(weapon_kind, {})
        sx, sy, sz = my_pos[0], my_pos[1], my_pos[2] + muzzle_h
        tx, ty, tz = target_pos[0], target_pos[1], target_pos[2] + target_h_center
        vx, vy, vz = target_vel[0], target_vel[1], target_vel[2]

        # For instant-hit weapons, basic aim + velocity prediction
        if weapon_kind in ("laser", "lightning"):
            dt = 0.18  # Anticipate 4 frames ahead (0.2s)
            px = tx + vx * dt
            py = ty + vy * dt
            pz = tz + vz * (dt * 0.7)
            dx, dy, dz = px - sx, py - sy, pz - sz
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
            pitch = 0.4

            for _ in range(6):
                px = tx + vx * t_flight
                py = ty + vy * t_flight
                pz = tz + vz * t_flight * 0.35
                dx, dy, dz = px - sx, py - sy, pz - sz
                horiz = max(math.hypot(dx, dy) - 2.5, 0.5)
                disc = v2 * v2 - g * (g * horiz * horiz + 2.0 * dz * v2)
                if disc > 0.0:
                    pitch = math.atan((v2 - math.sqrt(disc)) / (g * horiz))
                else:
                    pitch = 0.85
                cos_p = max(math.cos(pitch), 0.3)
                t_flight = horiz / (speed * cos_p)

            yaw = math.atan2(py - sy, px - sx)
            return [yaw, clamp(pitch, -1.2, 1.4)]

        # Other projectile weapons with travel time
        dx, dy, dz = tx - sx, ty - sy, tz - sz
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        travel_t = max(0.0, dist - 2.5) / speed

        for _ in range(5):
            px = tx + vx * travel_t
            py = ty + vy * travel_t
            pz = tz + vz * travel_t * 0.5
            dx, dy, dz = px - sx, py - sy, pz - sz
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            travel_t = max(0.0, dist - 2.5) / speed

        yaw = math.atan2(dy, dx)
        pitch = math.atan2(dz, max(math.hypot(dx, dy), 0.01))
        return [yaw, clamp(pitch, -1.3, 1.3)]

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

        if tick % 18 == 0:
            self.strafe_dir = -self.strafe_dir  # More frequent direction switch for unpredictability

        # 1. Upgrades
        if obs.get("offer"):
            act["pick"] = self._choose_upgrade(obs["offer"])

        # 2. Track incoming damage bearing and maintain last threat
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                self.last_threat_yaw = float(ev["bearing"])
                self.last_threat_tick = tick

        # 3. Categorize visible enemies & NPCs and update tracking memory
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
                sv = [0.6 * raw_v[i] + 0.4 * pv[i] for i in range(3)]
            else:
                sv = list(raw_v)
            smoothed_vel[mid] = sv
            self.prev_enemy_vel[mid] = sv

        # Prune stale tracking entries older than 30s
        if tick % 100 == 0:
            stale_ids = [mid for mid, t_seen in self.last_seen_tick.items() if tick - t_seen > 600]
            for mid in stale_ids:
                self.last_seen_tick.pop(mid, None)
                self.last_known_pos.pop(mid, None)
                self.prev_enemy_vel.pop(mid, None)

        # 4. Weapon Loot Pickup (no swap in combat or charge)
        in_combat = len(enemy_mechs) + len(npcs) > 0
        for p in obs.get("pickups", []):
            if math.dist(p["pos"][:2], (x, y)) < self.reach and abs(p["pos"][2] - z) < 4.0:
                pk = p["kind"]
                if pk in held_kinds:
                    continue
                if not in_combat and not is_charging_laser:
                    if weapons[1] is None or weapons[0] is None:
                        act["take"] = p["id"]
                        break
                    else:
                        # Swap out the lowest utility weapon if pickup better
                        utility_list = [(self._pickup_value(w["kind"]), i) for i, w in enumerate(weapons) if w]
                        utility_list.sort()
                        if self._pickup_value(pk) > utility_list[0][0] + 0.5:
                            act["take"] = p["id"]
                            act["drop"] = utility_list[0][1]
                            break

        # 5. Tactical Situation Assessment
        third_party_opportunity = False
        nearby_enemies = [m for m in enemy_mechs if math.dist(me["pos"], m["pos"]) < 130.0]
        if len(enemy_mechs) >= 2:
            d_me_e0 = math.dist(me["pos"], enemy_mechs[0]["pos"])
            d_e0_e1 = math.dist(enemy_mechs[0]["pos"], enemy_mechs[1]["pos"])
            nearest_weak = (
                float(enemy_mechs[0].get("hp", 1400.0)) / max(float(enemy_mechs[0].get("max_hp", 1400.0)), 1.0)
            ) < 0.45
            if hp_frac >= 0.55 and (d_e0_e1 < d_me_e0 * 0.85 or nearest_weak) and d_me_e0 > 70.0:
                third_party_opportunity = True

        must_retreat = False
        if hp_frac < 0.52 and len(nearby_enemies) > 0:
            must_retreat = True
        elif len(nearby_enemies) >= 2 and not third_party_opportunity:
            must_retreat = True

        # 6. Movement Calculation

        vx_des, vy_des = 0.0, 0.0
        z_cur = obs["zone"]
        nx, ny = z_cur["next_center"]
        n_rad = float(z_cur["next_radius"])
        dist_next = math.hypot(nx - x, ny - y)
        safe_rad = max(14.0, n_rad * 0.75)

        # Move towards safe zone if outside or near edge
        if not in_zone or dist_next > safe_rad:
            weight_zone = 4.5 if not in_zone else 2.2
            az = math.atan2(ny - y, nx - x)
            vx_des += math.cos(az) * weight_zone
            vy_des += math.sin(az) * weight_zone

        # Engage enemy positioning logic - more tactical and unpredictable
        for i, e in enumerate(enemy_mechs):
            ex, ey = e["pos"][0], e["pos"][1]
            d = math.hypot(ex - x, ey - y)
            ang_away = math.atan2(y - ey, x - ex)
            e_hp_frac = float(e.get("hp", 1400.0)) / max(float(e.get("max_hp", 1400.0)), 1.0)

            if must_retreat:
                # Smart retreat with map-awareness (no pushing into walls)
                retreat_strength = 3.2 if energy > 25 else 2.2
                vx_des += math.cos(ang_away) * retreat_strength
                vy_des += math.sin(ang_away) * retreat_strength
            elif third_party_opportunity:
                # Keep opponents between you and allies, kite around enemies
                if d < 70.0:
                    vx_des += math.cos(ang_away) * 2.0
                    vy_des += math.sin(ang_away) * 2.0
                elif d > 95.0:
                    vx_des -= math.cos(ang_away) * 1.0
                    vy_des -= math.sin(ang_away) * 1.0
            else:
                ideal_dist = 28.0 if "lightning" in held_kinds else 60.0
                if len(enemy_mechs) == 1 and e_hp_frac < 0.25 and hp_frac > 0.75:
                    ideal_dist = 24.0
                if d < ideal_dist * 0.78:
                    vx_des += math.cos(ang_away) * 2.0
                    vy_des += math.sin(ang_away) * 2.0
                elif d > ideal_dist * 1.4 and hp_frac > 0.70:
                    vx_des -= math.cos(ang_away) * 1.1
                    vy_des -= math.sin(ang_away) * 1.1

            # Add aggressive side strafing for first enemy if within range
            if i == 0 and d < 130.0:
                s_ang = ang_away + (math.pi * 0.5) * self.strafe_dir
                vx_des += math.cos(s_ang) * 1.4
                vy_des += math.sin(s_ang) * 1.4

        # React to nearby sounds for evasive maneuvers
        for snd in obs.get("sounds", []):
            if snd.get("dist") == "near" and snd.get("kind") in ("gun", "laser", "mortar", "lightning", "explosion"):
                if must_retreat or (hp_frac < 0.75 and not enemy_mechs):
                    b = float(snd["bearing"])
                    vx_des -= math.cos(b) * 1.4
                    vy_des -= math.sin(b) * 1.4

        # Opportunistic looting when safe and healthy
        if not must_retreat and not enemy_mechs and in_zone and hp_frac > 0.65:
            # Prefer cubes/pickups/crates closeby
            if obs.get("cubes"):
                closest_cube = min(obs["cubes"], key=lambda c: math.dist(c["pos"], me["pos"]))
                if math.dist(closest_cube["pos"][:2], (x, y)) < 60.0:
                    a = math.atan2(closest_cube["pos"][1] - y, closest_cube["pos"][0] - x)
                    vx_des += math.cos(a) * 1.9
                    vy_des += math.sin(a) * 1.9
            elif (weapons[0] is None or weapons[1] is None) and obs.get("pickups"):
                closest_pickup = min(obs["pickups"], key=lambda c: math.dist(c["pos"], me["pos"]))
                if math.dist(closest_pickup["pos"][:2], (x, y)) < 60.0:
                    ap = math.atan2(closest_pickup["pos"][1] - y, closest_pickup["pos"][0] - x)
                    vx_des += math.cos(ap) * 1.6
                    vy_des += math.sin(ap) * 1.6
            elif obs.get("crates") and obs.get("alive", 16) > 6:
                closest_crate = min(obs["crates"], key=lambda c: math.dist(c["pos"], me["pos"]))
                if math.dist(closest_crate["pos"][:2], (x, y)) < 45.0:
                    acr = math.atan2(closest_crate["pos"][1] - y, closest_crate["pos"][0] - x)
                    vx_des += math.cos(acr) * 1.25
                    vy_des += math.sin(acr) * 1.25

            for dr in obs.get("drops", []):
                if (
                    math.dist(dr["pos"][:2], (x, y)) < 40.0
                    and float(dr.get("lands_in", 99.0)) < 3.0
                    and (dr.get("holder") is None or dr.get("holder") == self.slot)
                ):
                    adr = math.atan2(dr["pos"][1] - y, dr["pos"][0] - x)
                    vx_des += math.cos(adr) * 1.9
                    vy_des += math.sin(adr) * 1.9
                    break

        if math.hypot(vx_des, vy_des) < 0.18:
            # Orbit around safe zone center if idle (circular movement)
            orbit = math.atan2(y - ny, x - nx) + 0.52
            tx_idle = nx + math.cos(orbit) * (safe_rad * 0.50)
            ty_idle = ny + math.sin(orbit) * (safe_rad * 0.50)
            ai = math.atan2(ty_idle - y, tx_idle - x)
            vx_des = math.cos(ai) * 0.85
            vy_des = math.sin(ai) * 0.85

        des_angle = math.atan2(vy_des, vx_des)
        des_magnitude = min(1.0, math.hypot(vx_des, vy_des))

        # Choose movement angle optimizing collision clearance and alignment
        best_move_ang = des_angle
        best_move_score = -999.0
        for k in range(16):
            candidate_angle = des_angle + (k - 8) * (math.pi / 16.0)
            align = math.cos(candidate_angle - des_angle)
            clearance = self.arena.path_clearance(x, y, candidate_angle)
            score = align * 1.5 + clearance * 3.0
            if score > best_move_score:
                best_move_score = score
                best_move_ang = candidate_angle

        act["move"] = [math.cos(best_move_ang) * des_magnitude, math.sin(best_move_ang) * des_magnitude]

        # Smarter boost logic, avoid spamming boosts, preferring boosts when retreating or out of zone
        if (must_retreat or not in_zone) and energy > 25.0 and tick - self.last_boost_tick > 15:
            act["boost"] = True
            self.last_boost_tick = tick
        else:
            act["boost"] = False

        # Jumping only if retreating, on ground, and cooldown passed
        if must_retreat and me.get("grounded", False) and tick - self.last_jump_tick > 30 and energy > 40.0:
            act["jump"] = True
            self.last_jump_tick = tick
        else:
            act["jump"] = False

        # 7. Targeting & Engagement
        muzzle_h = float(me.get("stats", {}).get("muzzle_height", self.cfg.get("mech", {}).get("muzzle_height", 7.0)))
        my_aim = me.get("aim") or [0.0, 0.0]
        has_pitch = len(my_aim) > 1
        my_yaw = float(my_aim[0])
        my_pitch = float(my_aim[1]) if has_pitch else 0.0

        target = None
        target_h = 4.5
        holding_occluded_lock = False

        if is_charging_laser and self.locked_target_id is not None:
            candidates = enemy_mechs + npcs + obs.get("crates", [])
            for m in candidates:
                if int(m.get("id", -1)) == self.locked_target_id:
                    target = m
                    target_h = {"mech": 4.5, "npc": 1.75}.get(m.get("kind"), 1.2)
                    self.locked_target_h = target_h
                    self.lock_lost_tick = -999
                    break

            if target is None:
                last_pos = self.last_known_pos.get(self.locked_target_id)
                if last_pos is not None:
                    if self.lock_lost_tick == -999:
                        self.lock_lost_tick = tick
                    elapsed = tick - self.lock_lost_tick
                    dt_lost = min(elapsed * 0.05, 1.7)
                    lvel = self.prev_enemy_vel.get(self.locked_target_id, [0.0, 0.0, 0.0])
                    px = last_pos[0] + lvel[0] * dt_lost
                    py = last_pos[1] + lvel[1] * dt_lost
                    pz = last_pos[2] + self.locked_target_h
                    dx_l, dy_l, dz_l = px - x, py - y, pz - (z + muzzle_h)
                    act["aim"] = [
                        math.atan2(dy_l, dx_l),
                        clamp(math.atan2(dz_l, max(math.hypot(dx_l, dy_l), 0.01)), -1.3, 1.3),
                    ]
                    holding_occluded_lock = True

                    # Timeout lock after 1.5s with extrapolated shot
                    if elapsed > 30:
                        act["fire"] = [False, False]
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

                # Prioritize weakest visible low-HP enemy (hp fraction + distance)
                if enemy_mechs:
                    target = min(
                        enemy_mechs,
                        key=lambda m: float(m.get("hp", 1200.0)) / max(float(m.get("max_hp", 1400.0)), 1.0) + math.dist(m["pos"], me["pos"]) / 40.0,
                    )
                    target_h = 4.5
                elif npcs and hp_frac > 0.75 and obs.get("alive", 16) > 5:
                    if math.dist(npcs[0]["pos"], me["pos"]) < 55.0:
                        target = npcs[0]
                        target_h = 1.75
                elif obs.get("crates") and hp_frac > 0.7:
                    cr = min(obs["crates"], key=lambda c: math.dist(c["pos"], me["pos"]))
                    if math.dist(cr["pos"], me["pos"]) < 35.0:
                        target = cr
                        target_h = 1.2

            fire = [False, False]
            need_reload = False

            if target is not None:
                tid = int(target["id"]) if "id" in target else -1
                t_pos = target["pos"]
                t_vel = smoothed_vel.get(tid, target.get("vel", [0.0, 0.0, 0.0]))
                dist_t = math.dist(t_pos, me["pos"])
                enemy_weps = target.get("weapons", [])
                target_hp = float(target.get("hp", 1000.0))

                # Select best weapon slot based on utility
                best_slot = None
                best_util = -0.5
                for idx, w in enumerate(weapons):
                    if w is None:
                        continue
                    utility = self._weapon_utility(w, dist_t, energy, enemy_weps, target_hp)
                    if utility > best_util:
                        best_util = utility
                        best_slot = idx

                if best_slot is not None and weapons[best_slot] is not None:
                    pw = weapons[best_slot]
                    pk = pw["kind"]
                    ps = pw.get("stats", {})
                    base_p = self.weapons_cfg.get(pk, {})
                    aim = self._solve_aim(me["pos"], muzzle_h, cubes, t_pos, t_vel, target_h, pk, ps)
                    act["aim"] = aim

                    yaw_err = abs(angle_diff(aim[0], my_yaw))
                    pitch_err = abs(aim[1] - my_pitch) if has_pitch else 0.0

                    # Determine on-target tolerance based on weapon kind
                    if pk == "gun" or pk == "mortar":
                        on_target = (yaw_err < 0.20) and (not has_pitch or pitch_err < 0.10)
                    elif pk == "lightning":
                        on_target = yaw_err < 0.38
                    else:  # laser
                        on_target = (yaw_err <= 0.08) and (not has_pitch or pitch_err <= 0.12)

                    # Fire logic tuned with range, energy, and ammo
                    if pk == "gun":
                        if on_target:
                            fire[best_slot] = True
                    elif pk == "mortar":
                        if 20.0 <= dist_t <= 225.0 and on_target and energy > 22.0:
                            fire[best_slot] = True
                    elif pk == "lightning":
                        l_range = float(ps.get("range", base_p.get("range", 45.0)))
                        if dist_t <= l_range and energy >= 20.0 and on_target:
                            fire[best_slot] = True
                    elif pk == "laser":
                        charge = float(pw.get("charge", 0.0))
                        full_c = float(ps.get("charge", base_p.get("charge", 0.75)))
                        desired_charge = full_c * (0.45 if target_hp < 120.0 else 0.95)
                        if charge >= desired_charge and on_target:
                            fire[best_slot] = False
                            self.locked_target_id = None
                            self.lock_lost_tick = -999
                        else:
                            fire[best_slot] = True

                    # Secondary weapon firing logic (avoid mortar overlap, energy efficient)
                    other_slot = 1 - best_slot
                    ow = weapons[other_slot]
                    if ow is not None and pk != "mortar" and ow["kind"] != "mortar":
                        ok = ow["kind"]
                        os_ = ow.get("stats", {})
                        base_o = self.weapons_cfg.get(ok, {})
                        sec_aim = self._solve_aim(me["pos"], muzzle_h, cubes, t_pos, t_vel, target_h, ok, os_)
                        sec_yaw_err = abs(angle_diff(sec_aim[0], my_yaw))
                        sec_pitch_err = abs(sec_aim[1] - my_pitch) if has_pitch else 0.0

                        if ok == "gun" and dist_t <= float(os_.get("range", base_o.get("range", 230.0))):
                            if sec_yaw_err < 0.18 and (not has_pitch or sec_pitch_err < 0.09):
                                fire[other_slot] = True
                        elif (
                            ok == "lightning"
                            and target_hp >= 150.0
                            and dist_t <= float(os_.get("range", base_o.get("range", 45.0)))
                            and energy >= 22.0
                        ):
                            if sec_yaw_err < 0.36:
                                fire[other_slot] = True

                else:
                    # Aim roughly at target if we cannot fire
                    yaw = math.atan2(t_pos[1] - y, t_pos[0] - x)
                    act["aim"] = [yaw, 0.0]

                # Reload logic: reload guns only at safe times, avoid reload during active fights
                if not is_charging_laser:
                    for idx, w in enumerate(weapons):
                        if w and w["kind"] == "gun":
                            ammo = int(w.get("ammo", 12))
                            mag = int(w.get("stats", {}).get("mag", self.weapons_cfg.get("gun", {}).get("mag", 12)))
                            if ammo == 0 or (ammo <= 3 and any(fire)):
                                need_reload = True
                            elif ammo <= mag * 0.3 and not fire[idx]:
                                need_reload = True
            else:
                self.locked_target_id = None
                self.lock_lost_tick = -999

                # Aim toward last damage bearing if recently hit
                if tick - self.last_threat_tick < 45 and self.last_threat_yaw is not None:
                    act["aim"] = [self.last_threat_yaw, 0.0]
                else:
                    # Random scanning when no target
                    scan_yaw = (tick * 0.15) % (2.0 * math.pi) - math.pi
                    act["aim"] = [scan_yaw, 0.0]

                if not is_charging_laser:
                    for w in weapons:
                        if w and w["kind"] == "gun":
                            mag = int(w.get("stats", {}).get("mag", self.weapons_cfg.get("gun", {}).get("mag", 12)))
                            if int(w.get("ammo", mag)) <= mag * 0.4:
                                need_reload = True

            act["fire"] = fire
            if need_reload:
                act["reload"] = True

        if tick % 100 == 0:
            nearest_dist = math.dist(enemy_mechs[0]["pos"], me["pos"]) if enemy_mechs else -1.0
            zone_frac = dist_next / max(safe_rad, 1.0)
            mf.log(
                f"t={tick} hp={me['hp']:.0f} alive={obs.get('alive')} "
                f"in_zone={in_zone} zone_frac={zone_frac:.2f} "
                f"charging={is_charging_laser} locked={self.locked_target_id} "
                f"nearest_enemy_dist={nearest_dist:.0f}"
            )

        mf.draw.label(f"HP:{me['hp']:.0f} En:{energy:.0f} Alive:{obs.get('alive')}")
        return act


mf.run(Bot)
