#!/usr/bin/env python3
"""
FIFA World Cup 2026 Sweepstake — cloud data layer (GitHub Actions).

Self-contained: fetches finished + live matches from ESPN's public World Cup API
(no key), computes team standings (3/1/0 on the full-time score), sums them into
player totals, applies the live "as it stands" overlay, builds the story/fixtures/
results, and writes data.json. The static front-end (index.html/styles.css/app.js)
fetches data.json and renders itself.

ESPN is the source of truth (the local Excel workbook is not available in CI).
Standard library only.
"""

import json
import os
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# Output to repo root by default (where index.html / app.js live), override with OUT_DIR.
OUT_DIR = os.environ.get("OUT_DIR", os.path.abspath(os.path.join(HERE, "..")))
CONFIG = os.path.join(HERE, "config.json")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates={}"

UK = timezone(timedelta(hours=1))  # BST during the tournament (Jun–Jul)

ALIASES = {
    "southkorea": "korearepublic", "korearepublic": "korearepublic",
    "unitedstates": "usa", "usa": "usa",
    "turkiye": "turkiye", "turkey": "turkiye",
    "cotedivoire": "ivorycoast", "ivorycoast": "ivorycoast",
    "caboverde": "capeverde", "capeverde": "capeverde",
    "drcongo": "congorepublic", "congodr": "congorepublic",
    "congorepublic": "congorepublic", "congo": "congorepublic",
    "iriran": "iran", "iran": "iran",
    "czechia": "czechia", "czechrepublic": "czechia",
}


def canon(s):
    if not s:
        return ""
    t = unicodedata.normalize("NFD", s)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = "".join(c for c in t if c.isalnum())
    return ALIASES.get(t, t)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "sweepstake-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_iso(s):
    """Robust ISO-8601 parse for ESPN dates (handles trailing 'Z' and missing seconds)
    across Python versions. Returns an aware UTC-based datetime or None."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def text_on(hexcol):
    h = hexcol.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "#0a1730" if lum > 150 else "#ffffff"


def heat_bg(val, mx, target_hex):
    f = 0.0 if mx <= 0 else float(val) / float(mx)
    f = max(0.0, min(1.0, f))
    base = (15, 37, 70)
    t = target_hex.lstrip("#")
    tr, tg, tb = int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
    r = int(base[0] + (tr - base[0]) * f)
    g = int(base[1] + (tg - base[1]) * f)
    b = int(base[2] + (tb - base[2]) * f)
    return "#%02X%02X%02X" % (r, g, b)


def main():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    teams = cfg["teams"]
    players_cfg = cfg["players"]
    total_matches = cfg["totalMatches"]

    # canonical full-name -> abbr
    canon_to_abbr = {canon(meta["name"]): ab for ab, meta in teams.items()}

    def resolve(name):
        return canon_to_abbr.get(canon(name))

    # --- date window: tournament start .. today (UTC) ---
    start = datetime.strptime(cfg["tournamentStart"], "%Y%m%d").date()
    today = datetime.now(timezone.utc).date()
    days = []
    d = start
    while d <= today:
        days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)

    finished = []   # {home,away,hs,as,ko(datetime utc)}
    live = []       # {home,away,hs,as,clock,detail}
    for ds in days:
        try:
            data = fetch(ESPN.format(ds))
        except Exception as e:
            print("WARN fetch %s failed: %s" % (ds, e))
            continue
        for e in data.get("events", []):
            comp = e["competitions"][0]
            cs = comp["competitors"]
            h = next((c for c in cs if c["homeAway"] == "home"), None)
            a = next((c for c in cs if c["homeAway"] == "away"), None)
            if not h or not a:
                continue
            hn, an = h["team"]["displayName"], a["team"]["displayName"]
            ko = parse_iso(e.get("date"))
            st = e["status"]["type"]
            state = st.get("state", "")
            if state == "in":
                live.append({"home": hn, "away": an,
                             "hs": int(h.get("score", 0)), "as": int(a.get("score", 0)),
                             "clock": e["status"].get("displayClock", ""),
                             "detail": st.get("shortDetail", "")})
                continue
            if st.get("name") != "STATUS_FULL_TIME":
                continue
            finished.append({"home": hn, "away": an,
                             "hs": int(h.get("score", 0)), "as": int(a.get("score", 0)),
                             "ko": ko})

    print("ESPN finished=%d live=%d" % (len(finished), len(live)))

    # --- team standings from finished matches (3/1/0 on FT score) ---
    stat = {ab: {"Pld": 0, "W": 0, "D": 0, "GF": 0, "GA": 0, "Pts": 0} for ab in teams}
    for m in finished:
        ha, aa = resolve(m["home"]), resolve(m["away"])
        hs, as_ = m["hs"], m["as"]
        if ha in stat:
            s = stat[ha]; s["Pld"] += 1; s["GF"] += hs; s["GA"] += as_
            if hs > as_: s["W"] += 1
            elif hs == as_: s["D"] += 1
        if aa in stat:
            s = stat[aa]; s["Pld"] += 1; s["GF"] += as_; s["GA"] += hs
            if as_ > hs: s["W"] += 1
            elif as_ == hs: s["D"] += 1
    for ab, s in stat.items():
        s["Pts"] = s["W"] * 3 + s["D"]

    team_pts = {ab: s["Pts"] for ab, s in stat.items()}

    # --- live overlay: provisional pts/goals from in-progress matches ---
    owner_of = {}
    for p in players_cfg:
        for ab in p["picks"]:
            owner_of[ab] = p["name"]

    players = []
    for p in players_cfg:
        agg = {"Pts": 0, "Pld": 0, "GF": 0, "GA": 0}
        for ab in p["picks"]:
            s = stat.get(ab)
            if s:
                agg["Pts"] += s["Pts"]; agg["Pld"] += s["Pld"]
                agg["GF"] += s["GF"]; agg["GA"] += s["GA"]
        players.append({"name": p["name"], "picks": list(p["picks"]),
                        "Pts": agg["Pts"], "Pld": agg["Pld"], "GF": agg["GF"], "GA": agg["GA"],
                        "LivePts": 0, "LivePld": 0, "LiveGF": 0, "LiveGA": 0, "LiveTeams": []})
    by_name = {p["name"]: p for p in players}

    live_cards = []
    for lm in live:
        ha, aa = resolve(lm["home"]), resolve(lm["away"])
        hs, as_ = lm["hs"], lm["as"]
        hp = 3 if hs > as_ else (1 if hs == as_ else 0)
        ap = 3 if as_ > hs else (1 if as_ == hs else 0)
        h_own = owner_of.get(ha); a_own = owner_of.get(aa)
        if h_own and ha:
            p = by_name[h_own]; p["LivePts"] += hp; p["LivePld"] += 1; p["LiveGF"] += hs; p["LiveGA"] += as_; p["LiveTeams"].append(ha)
        if a_own and aa:
            p = by_name[a_own]; p["LivePts"] += ap; p["LivePld"] += 1; p["LiveGF"] += as_; p["LiveGA"] += hs; p["LiveTeams"].append(aa)
        live_cards.append({"home": lm["home"], "away": lm["away"], "hs": hs, "as": as_,
                           "clock": lm["clock"], "detail": lm["detail"], "hOwn": h_own, "aOwn": a_own,
                           "ha": ha, "aa": aa})
    any_live = len(live) > 0

    # projected (as-it-stands) + sort
    for p in players:
        p["ProjPts"] = p["Pts"] + p["LivePts"]
        p["ProjPld"] = p["Pld"] + p["LivePld"]
        p["ProjGF"] = p["GF"] + p["LiveGF"]
        p["ProjGA"] = p["GA"] + p["LiveGA"]
        p["ProjGD"] = p["ProjGF"] - p["ProjGA"]
    ranked = sorted(players, key=lambda p: (-p["ProjPts"], -p["ProjGD"], -p["ProjGF"]))

    max_pts = max([p["ProjPts"] for p in ranked] + [1])
    max_gf = max([p["ProjGF"] for p in ranked] + [1])
    max_ga = max([p["ProjGA"] for p in ranked] + [1])

    def pick_obj(ab, live_teams):
        meta = teams.get(ab)
        tp = team_pts.get(ab, 0)
        is_live = ab in live_teams
        if meta:
            return {"abbr": ab, "name": meta["name"], "iso": meta["iso"], "hex": meta["hex"],
                    "fg": text_on(meta["hex"]), "pts": tp, "live": is_live}
        return {"abbr": ab, "name": ab, "iso": None, "hex": None, "fg": None, "pts": tp, "live": is_live}

    standings = []
    for i, p in enumerate(ranked):
        rank = i + 1
        pts, gf, ga = p["ProjPts"], p["ProjGF"], p["ProjGA"]
        gd, pld = p["ProjGD"], p["ProjPld"]
        standings.append({
            "rank": rank, "name": p["name"], "isLeader": rank == 1,
            "pts": pts, "pld": pld, "gd": gd, "gf": gf, "ga": ga,
            "livePts": p["LivePts"], "livePld": p["LivePld"],
            "hasLive": (p["LivePts"] > 0 or len(p["LiveTeams"]) > 0), "delta": 0,
            "ptsBg": heat_bg(pts, max_pts, "#D4AF37"), "ptsFg": text_on(heat_bg(pts, max_pts, "#D4AF37")),
            "gfBg": heat_bg(gf, max_gf, "#63BE7B"), "gfFg": text_on(heat_bg(gf, max_gf, "#63BE7B")),
            "gaBg": heat_bg(ga, max_ga, "#F8696B"), "gaFg": text_on(heat_bg(ga, max_ga, "#F8696B")),
            "picks": [pick_obj(ab, p["LiveTeams"]) for ab in p["picks"]],
        })

    # --- match-object builder (results / live / fixtures) ---
    def match_obj(home, away, hwin, awin):
        hi, ai = teams.get(resolve(home) or ""), teams.get(resolve(away) or "")
        return {
            "home": home, "away": away,
            "homeAbbr": resolve(home) or home, "awayAbbr": resolve(away) or away,
            "homeIso": hi["iso"] if hi else None, "awayIso": ai["iso"] if ai else None,
            "hOwn": owner_of.get(resolve(home)), "aOwn": owner_of.get(resolve(away)),
            "hWin": hwin, "aWin": awin,
        }

    # --- recent results: finished sorted by kickoff desc, last 8 ---
    fin_sorted = sorted(finished, key=lambda m: m["ko"] or datetime(2000, 1, 1, tzinfo=timezone.utc), reverse=True)[:8]
    results = []
    for m in fin_sorted:
        hs, as_ = m["hs"], m["as"]
        hw = 1 if hs > as_ else (-1 if hs < as_ else 0)
        aw = 1 if as_ > hs else (-1 if as_ < hs else 0)
        o = match_obj(m["home"], m["away"], hw, aw)
        o["hs"] = hs; o["as"] = as_
        results.append(o)

    # --- live cards as match objects ---
    live_data = []
    for lc in live_cards:
        hs, as_ = lc["hs"], lc["as"]
        hw = 1 if hs > as_ else (-1 if hs < as_ else 0)
        aw = 1 if as_ > hs else (-1 if as_ < hs else 0)
        clk = lc["clock"] or lc["detail"] or "LIVE"
        o = match_obj(lc["home"], lc["away"], hw, aw)
        o["hs"] = hs; o["as"] = as_; o["clock"] = clk; o["detail"] = lc["detail"]
        live_data.append(o)

    # --- today's fixtures (BST kickoff times, player matchup) ---
    fixtures = []
    try:
        sb = fetch(ESPN.format(today.strftime("%Y%m%d")))
        rows = []
        for e in sb.get("events", []):
            comp = e["competitions"][0]
            cs = comp["competitors"]
            h = next((c for c in cs if c["homeAway"] == "home"), None)
            a = next((c for c in cs if c["homeAway"] == "away"), None)
            if not h or not a:
                continue
            ko = parse_iso(e.get("date"))
            ko = ko.astimezone(UK) if ko else datetime.now(UK)
            rows.append({"ko": ko, "home": h["team"]["displayName"], "away": a["team"]["displayName"],
                         "state": e["status"]["type"].get("name", ""),
                         "hs": h.get("score"), "as": a.get("score")})
        for m in sorted(rows, key=lambda r: r["ko"]):
            hw = aw = 0
            state = "ko"; hs = as_ = None
            if m["state"] == "STATUS_FULL_TIME":
                hs, as_ = int(m["hs"]), int(m["as"]); state = "ft"
                hw = 1 if hs > as_ else (-1 if hs < as_ else 0)
                aw = 1 if as_ > hs else (-1 if as_ < hs else 0)
            elif "HALF" in m["state"] or m["state"] == "STATUS_IN_PROGRESS" or "FIRST" in m["state"] or "SECOND" in m["state"]:
                hs, as_ = int(m["hs"]), int(m["as"]); state = "live"
            o = match_obj(m["home"], m["away"], hw, aw)
            o["state"] = state; o["ko"] = m["ko"].strftime("%H:%M")
            if hs is not None:
                o["hs"] = hs; o["as"] = as_
            fixtures.append(o)
    except Exception as e:
        print("WARN fixtures: %s" % e)

    # --- The Story So Far ---
    played = len(finished)
    story = build_story(ranked, players_cfg, stat, team_pts, teams, played)

    leader = ranked[0]
    stamp = datetime.now(UK).strftime("%d %b %Y %H:%M")
    og = ("%s leads on %d pts after %d/%d matches" % (leader["name"], leader["ProjPts"], played, total_matches)) \
        if played > 0 else "8 players, 48 teams, 104 matches. Kicks off 11 June."

    payload = {
        "meta": {"stamp": stamp, "playedCount": played, "totalMatches": total_matches,
                 "leaderName": leader["name"], "leaderPts": leader["ProjPts"], "ogDesc": og},
        "anyLive": any_live,
        "standings": standings,
        "live": live_data,
        "story": story,
        "fixtures": fixtures,
        "results": results,
    }

    out = os.path.join(OUT_DIR, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("DATA written: %s (played=%d, leader=%s %d)" % (out, played, leader["name"], leader["ProjPts"]))


def player_agg(name, players_cfg, stat):
    picks = next(p["picks"] for p in players_cfg if p["name"] == name)
    tot = {"Pts": 0, "Pld": 0, "GF": 0, "GA": 0, "Wins": 0, "Scored": 0, "TeamsPlayed": 0, "Contrib": {}}
    for ab in picks:
        s = stat.get(ab)
        if s:
            tot["Pts"] += s["Pts"]; tot["Pld"] += s["Pld"]; tot["GF"] += s["GF"]; tot["GA"] += s["GA"]; tot["Wins"] += s["W"]
            if s["Pld"] > 0: tot["TeamsPlayed"] += 1
            if s["GF"] > 0: tot["Scored"] += 1
            tot["Contrib"][ab] = s["Pts"]
        else:
            tot["Contrib"][ab] = 0
    return tot


def build_story(ranked, players_cfg, stat, team_pts, teams, played):
    bits = []
    if played == 0:
        bits.append("Nothing's kicked off yet. Eight brave souls, 48 teams, and a frankly unhealthy amount of office trash talk waiting to be unleashed. Strap in.")
        return bits
    leader, second, last = ranked[0], ranked[1], ranked[-1]
    lp, sp2 = leader["ProjPts"], second["ProjPts"]
    gap = lp - sp2
    lA = player_agg(leader["name"], players_cfg, stat)
    sA = player_agg(second["name"], players_cfg, stat)
    bits.append("%s tops the pile on %d pts &mdash; their six teams have banked %d points off %d games between them." % (leader["name"], lp, lA["Pts"], lA["Pld"]))
    in_hand = sA["Pld"] - lA["Pld"]
    if gap == 0:
        bits.append("It's dead level at the summit with %s &mdash; this one's going to the wire." % second["name"])
    elif in_hand > 0:
        bits.append("%s sits %d back, but their teams have played %d more game(s) than the leader's &mdash; %s has fixtures in hand." % (second["name"], gap, in_hand, leader["name"]))
    elif in_hand < 0:
        bits.append("%s trails by %d &mdash; and their teams have %d fewer game(s) banked than the leader. Ground to make up." % (second["name"], gap, abs(in_hand)))
    else:
        bits.append("%s is %d back with the same games played &mdash; a straight shootout." % (second["name"], gap))
    # reliance: one team carrying a contender
    for nm in (leader["name"], second["name"]):
        a = player_agg(nm, players_cfg, stat)
        if a["Pts"] > 0:
            best_ab = max(a["Contrib"], key=lambda k: a["Contrib"][k])
            share = round(100.0 * a["Contrib"][best_ab] / max(a["Pts"], 1))
            if share >= 60:
                bn = teams.get(best_ab, {}).get("name", best_ab)
                bits.append("%s is leaning hard on %s &mdash; that one team is propping up %d%% of their total." % (nm, bn, share))
                break
    # dead weight
    for p in players_cfg:
        done = False
        for ab in p["picks"]:
            s = stat.get(ab)
            if s and s["Pld"] >= 1 and s["Pts"] == 0:
                bits.append("Spare a thought for %s, lumbered with %s &mdash; played, and contributed precisely nothing." % (p["name"], teams.get(ab, {}).get("name", ab)))
                done = True
                break
        if done:
            break
    # goal machine
    best_gf, best_gf_p = -1, ""
    for p in players_cfg:
        a = player_agg(p["name"], players_cfg, stat)
        if a["GF"] > best_gf:
            best_gf, best_gf_p = a["GF"], p["name"]
    if best_gf > 0:
        bits.append("%s's nations are the entertainers, banging in %d goals between them." % (best_gf_p, best_gf))
    # wooden spoon
    lowA = player_agg(last["name"], players_cfg, stat)
    bits.append("Rock bottom: %s on %d. Their six have managed just %d win(s) and %d goal(s) &mdash; the warm sausage rolls beckon." % (last["name"], last["ProjPts"], lowA["Wins"], lowA["GF"]))
    return bits


if __name__ == "__main__":
    main()
