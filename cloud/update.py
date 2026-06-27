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


STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings?season=2026"


def fetch_eliminated(resolve):
    """Read ESPN's authoritative group standings and return a set of abbrs ESPN marks
    'Eliminated'. ESPN does the full qualification maths (incl. best-third), so we just
    trust its note. Returns empty set on any failure (fail-safe: nobody greyed)."""
    out = set()
    try:
        data = fetch(STANDINGS)
    except Exception as e:
        print("WARN standings fetch failed: %s" % e)
        return out
    for grp in data.get("children", []):
        for entry in grp.get("standings", {}).get("entries", []):
            note = (entry.get("note") or {}).get("description", "") or ""
            if "eliminat" in note.lower():
                ab = resolve(entry.get("team", {}).get("displayName", ""))
                if ab:
                    out.add(ab)
    return out


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
    flair = cfg.get("flair", {})
    groups = cfg.get("groups", {})

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
            pen = None
            hsh, ash = h.get("shootoutScore"), a.get("shootoutScore")
            if hsh is not None or ash is not None:
                try:
                    if int(hsh or 0) > int(ash or 0): pen = hn
                    elif int(ash or 0) > int(hsh or 0): pen = an
                except Exception:
                    pass
            finished.append({"home": hn, "away": an,
                             "hs": int(h.get("score", 0)), "as": int(a.get("score", 0)),
                             "ko": ko, "penWinner": pen})

    print("ESPN finished=%d live=%d" % (len(finished), len(live)))

    # --- team standings from finished matches (3/1/0 on FT score) ---
    stat = {ab: {"Pld": 0, "W": 0, "D": 0, "GF": 0, "GA": 0, "Pts": 0} for ab in teams}
    team_matches = {ab: [] for ab in teams}   # per-team match log for "beyond the table" quirks
    for m in finished:
        ha, aa = resolve(m["home"]), resolve(m["away"])
        hs, as_ = m["hs"], m["as"]
        if ha in stat:
            s = stat[ha]; s["Pld"] += 1; s["GF"] += hs; s["GA"] += as_
            if hs > as_: s["W"] += 1
            elif hs == as_: s["D"] += 1
            team_matches[ha].append({"gf": hs, "ga": as_, "opp": m["away"]})
        if aa in stat:
            s = stat[aa]; s["Pld"] += 1; s["GF"] += as_; s["GA"] += hs
            if as_ > hs: s["W"] += 1
            elif as_ == hs: s["D"] += 1
            team_matches[aa].append({"gf": as_, "ga": hs, "opp": m["home"]})
    for ab, s in stat.items():
        s["Pts"] = s["W"] * 3 + s["D"]

    team_pts = {ab: s["Pts"] for ab, s in stat.items()}

    # --- eliminated teams: ESPN's authoritative group-stage notes + knockout losers ---
    elim = fetch_eliminated(resolve)              # group stage (ESPN does the best-third maths)
    team_group = {}
    for g, abs_ in groups.items():
        for ab in abs_:
            team_group[ab] = g
    for m in finished:                            # knockout = any finished inter-group tie
        ha, aa = resolve(m["home"]), resolve(m["away"])
        if not ha or not aa:
            continue
        if team_group.get(ha) and team_group.get(ha) == team_group.get(aa):
            continue                              # intra-group = group match, never elimination
        hs, as_ = m["hs"], m["as"]
        loser = None
        if hs > as_: loser = aa
        elif as_ > hs: loser = ha
        elif m.get("penWinner"): loser = aa if m["penWinner"] == ha else (ha if m["penWinner"] == aa else None)
        if loser:
            elim.add(loser)
    print("eliminated: %s" % (", ".join(sorted(elim)) if elim else "(none)"))

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
        out_ = ab in elim
        if meta:
            return {"abbr": ab, "name": meta["name"], "iso": meta["iso"], "hex": meta["hex"],
                    "fg": text_on(meta["hex"]), "pts": tp, "live": is_live, "out": out_}
        return {"abbr": ab, "name": ab, "iso": None, "hex": None, "fg": None, "pts": tp, "live": is_live, "out": out_}

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
    story = build_story(ranked, players_cfg, stat, team_pts, teams, played, team_matches, flair)

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


def build_story(ranked, players_cfg, stat, team_pts, teams, played, team_matches, flair):
    """A funny, flowing 3-paragraph round-up. Each participant (first to last) gets a
    distinct 'beyond the table' quirk, stitched together with connective tissue.
    Deterministic so it only changes when the stats do, not on every refresh."""
    if played == 0:
        return ["Not a ball kicked, not a point banked &mdash; just eight wild-eyed optimists and "
                "48 teams about to find out exactly how misplaced that confidence was. Buckle up."]
    n = len(ranked)
    used = set()
    beats = []
    for idx, rp in enumerate(ranked):
        cands = player_insights(rp, idx + 1, n, players_cfg, stat, teams, team_matches, flair)
        cands.sort(key=lambda c: -c[0])
        chosen = next((c for c in cands if c[1] not in used), cands[0] if cands else None)
        if chosen:
            used.add(chosen[1])
            beats.append(chosen[2])
        else:
            beats.append("<b>%s</b> is keeping suspiciously quiet." % rp["name"])

    p1 = ["At the very summit, ", "Hot on their heels, ", "Rounding out the smug end of the table, "]
    p2 = ["Wading into the midtable swamp, ", "Keeping them grim company, ", "And clinging on by their fingernails, "]
    p3 = ["Down in the cheap seats, ", "And scraping the very bottom of the barrel, "]

    def para(slice_, leads):
        out = []
        for i, b in enumerate(slice_):
            out.append((leads[i] if i < len(leads) else "Then ") + b)
        return " ".join(out)

    paras = [para(beats[0:3], p1), para(beats[3:6], p2), para(beats[6:], p3)]
    return [p for p in paras if p]


def tname(teams, ab):
    return teams.get(ab, {}).get("name", ab)


def player_insights(rp, rank, n, players_cfg, stat, teams, team_matches, flair):
    """Return a list of (salience, type, html) candidate observations for one player.
    Texts are name-led and self-contained (paragraph assembly handles position)."""
    name = rp["name"]
    picks = next(p["picks"] for p in players_cfg if p["name"] == name)
    pts = gf = ga = w = d = l = pld = 0
    contrib = {}
    yet = []
    played_teams = []
    zero_played = []
    best_match = None
    big_rout = None
    worst_def = None
    clean = 0
    for ab in picks:
        s = stat.get(ab, {"Pld": 0, "W": 0, "D": 0, "GF": 0, "GA": 0, "Pts": 0})
        pts += s["Pts"]; gf += s["GF"]; ga += s["GA"]; w += s["W"]; d += s["D"]; pld += s["Pld"]
        contrib[ab] = s["Pts"]
        if s["Pld"] == 0:
            yet.append(ab)
        else:
            played_teams.append(ab)
            if s["Pts"] == 0:
                zero_played.append(ab)
        for m in team_matches.get(ab, []):
            margin = m["gf"] - m["ga"]
            if m["ga"] == 0:
                clean += 1
            if m["gf"] > m["ga"] and (best_match is None or margin > best_match[0]):
                best_match = (margin, m["gf"], m["ga"], ab)
            if big_rout is None or m["gf"] > big_rout[0]:
                big_rout = (m["gf"], m["ga"], ab)
            if m["ga"] > m["gf"] and (worst_def is None or (m["ga"] - m["gf"]) > worst_def[0]):
                worst_def = (m["ga"] - m["gf"], m["gf"], m["ga"], ab)
    l = pld - w - d
    gd = gf - ga
    best_ab = max(contrib, key=lambda k: contrib[k]) if contrib else None
    share = round(100.0 * contrib[best_ab] / pts) if (best_ab and pts > 0) else 0
    sign = "+" if gd >= 0 else ""
    nm = "<b>%s</b>" % name

    def tnf(ab):  # team name + national cliché, e.g. "Germany (ruthlessly, tediously efficient)"
        base = tname(teams, ab)
        fl = flair.get(ab, "")
        return ("%s (%s)" % (base, fl)) if fl else base

    c = []

    if pts > 0 and share >= 50 and contrib[best_ab] > 0:
        c.append((share + 20, "reliance",
                  "%s is running a full-blown one-nation hostage situation &mdash; %s alone props up %d%% of their points, and if that lot trip over their laces the whole house of cards flutters off into the sea." % (nm, tnf(best_ab), share)))
    if pld >= 2 and l == 0 and w >= 2 and d == 0:
        c.append((92, "perfect",
                  "%s is being utterly unbearable about it: every single team they own that's kicked off has won. Somebody frisk them for a crystal ball." % nm))
    if pld >= 2 and w == 0:
        tail = ("just %d draw%s for company" % (d, "" if d == 1 else "s")) if d > 0 else "and precisely nothing to show for it"
        c.append((88, "winless",
                  "%s is still hunting a first win like a 2am kebab &mdash; %d games deep, zero victories, %s." % (nm, pld, tail)))
    if len(yet) >= 1:
        names = ", ".join(tname(teams, a) for a in yet)
        c.append((40 + 12 * len(yet), "inhand",
                  "%s still has %d team(s) yet to so much as lace a boot (%s) &mdash; either untapped genius or fresh heartbreak, still loading." % (nm, len(yet), names)))
    if zero_played:
        c.append((62, "deadweight",
                  "%s is hauling %s around like a dead fridge up a fire escape &mdash; it's played, and contributed the square root of sod all." % (nm, tnf(zero_played[0]))))
    if big_rout and big_rout[0] >= 4:
        c.append((58 + big_rout[0], "rout",
                  "%s bagged the pasting of the tournament so far, %s romping home %d-%d. Borderline rude." % (nm, tnf(big_rout[2]), big_rout[0], big_rout[1])))
    elif best_match and best_match[0] >= 2:
        c.append((50 + best_match[0], "bigwin",
                  "%s's pride and joy is a tidy %d-%d spanking from %s &mdash; ruthless, efficient, faintly smug." % (nm, best_match[1], best_match[2], tnf(best_match[3]))))
    if gf >= 6:
        topgf = max(picks, key=lambda a: stat.get(a, {}).get("GF", 0))
        c.append((40 + gf, "goals",
                  "%s's lot have collectively decided defending is for cowards &mdash; %d goals banged in (GD %s%d), spearheaded by %s." % (nm, gf, sign, gd, tnf(topgf))))
    if ga >= 7:
        leak = max(played_teams, key=lambda a: stat[a]["GA"]) if played_teams else None
        leaktxt = (", with %s alone waving in %d." % (tnf(leak), stat[leak]["GA"])) if leak else "."
        c.append((38 + ga, "leaky",
                  "%s's backline has the structural integrity of a wet paper bag &mdash; %d shipped already%s" % (nm, ga, leaktxt)))
    if clean >= 2:
        c.append((44 + 6 * clean, "cleansheets",
                  "%s is grinding out %d clean sheets like a man morally opposed to fun &mdash; ugly, effective, and a touch boring." % (nm, clean)))
    if d >= 3:
        c.append((40 + 6 * d, "draws",
                  "%s is the undisputed sultan of the stalemate: %d draws and counting. Edge-of-the-seat stuff it is not." % (nm, d)))
    if worst_def and worst_def[0] >= 3:
        c.append((42 + worst_def[0], "thumped",
                  "%s watched %s get taken to the absolute cleaners %d-%d &mdash; a scoreline best discussed in hushed, pitying tones." % (nm, tnf(worst_def[3]), worst_def[1], worst_def[2])))
    scoring_teams = [a for a in played_teams if contrib[a] > 0]
    if pts >= 6 and len(scoring_teams) >= 4 and share <= 35:
        c.append((46, "spread",
                  "%s has spread the love like a sensible little pension fund &mdash; %d teams all chipping in, not a single passenger. Boringly effective." % (nm, len(scoring_teams))))

    c.append((5, "summary",
              "%s is simply... there &mdash; %d pts from %d games (%dW %dD %dL), the beige wallpaper of the leaderboard." % (nm, pts, pld, w, d, l)))
    return c


if __name__ == "__main__":
    main()
