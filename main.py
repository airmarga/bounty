"""Scripted Werewolf agent for make.fun's werewolf-agent-01 bounty.

Plays one seat in 30 games against seven house agents through the env at
MAKEFUN_ENV_URL. No model calls: every decision is a deterministic function of
the view the env served and MAKEFUN_TRIAL_SEED, so the trial replays exactly.

Strategy in one line: derive the full game state (claims, accusations, votes,
revealed roles) from the public log every decision, follow credible seer
claims hard when village, coordinate kills and bus a doomed partner when wolf,
and phrase statements so the house agents bandwagon the right way.
"""

import hashlib
import json
import os
import random
import re
import sys
import threading
import urllib.error
import urllib.request

ENV_URL = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
SEED = os.environ.get("MAKEFUN_TRIAL_SEED") or "0"
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE") or "/input/prompt.txt"

HTTP_TIMEOUT = 120
WOLVES = 2

# ---------------------------------------------------------------- the env client

_env_lock = threading.Lock()


def post(path, body):
    """One POST to the env: (status, parsed JSON object or None)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ENV_URL + path,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            text = res.read().decode("utf-8", "replace")
            return res.status, (json.loads(text) if text.strip() else None)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"error": "network", "message": str(e)}


def rng(game, label):
    """A seeded RNG: the same stream for the same game, label and trial seed."""
    h = hashlib.sha256(f"{SEED}|{game}|{label}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


# ---------------------------------------------------------------- parsing the view

RE_SEER_CLAIM = re.compile(r"\b(?:i am|i'm|as the|the real)\s+seer\b", re.I)
RE_DOCTOR_CLAIM = re.compile(r"\b(?:i am|i'm|as the)\s+doctor\b", re.I)
RE_VILLAGER_CLAIM = re.compile(r"\b(?:i am|i'm)\s+(?:just\s+)?(?:a|an)\s+(?:plain\s+)?villager\b", re.I)
RE_WOLF_RESULT = re.compile(r"seat\s+(\d+)[^.!?\n]{0,60}?\b(?:is|was|turned out to be)\s+a\s+werewolf\b", re.I)
RE_CLEAN_RESULT = re.compile(r"seat\s+(\d+)[^.!?\n]{0,60}?\b(?:is\s+not|not)\s+a\s+werewolf\b", re.I)
RE_ACCUSE = re.compile(r"\bvote(?:ing)?(?:\s+(?:for|to\s+exile))?\s+seat\s+(\d+)", re.I)


class Player:
    __slots__ = ("seat", "name", "alive", "role")

    def __init__(self, seat, name, alive, role):
        self.seat = seat
        self.name = name
        self.alive = alive
        self.role = role


class GameState:
    """Everything one decision needs, derived from one view alone."""

    def __init__(self, view):
        self.view = view
        self.day = view.get("day", 0)
        self.phase = view.get("phase", "")
        me = view["you"]
        self.me = me["seat"]
        self.my_name = me["name"]
        self.role = me["role"]
        self.alive = me["alive"]
        partner = me.get("partner")
        self.partner = partner["seat"] if partner else None
        self.players = {
            p["seat"]: Player(p["seat"], p["name"], p["alive"], p["role"])
            for p in view.get("players", [])
        }
        self.seer_results = [
            (r["night"], r["seat"], r["werewolf"]) for r in (view.get("seerResults") or [])
        ]
        self.last_protect = (view.get("lastProtect") or {}).get("seat")
        self.wolf_votes = [
            (v["night"], v["seat"], v["target"]) for v in (view.get("wolfVotes") or [])
        ]
        self.log = view.get("log", [])
        self._parse_log()

    # ---- public log

    def _parse_log(self):
        self.deaths = []          # (seat, role) in order, from dawn + exile
        self.speeches = []        # (seq, day, round, seat, text)
        self.votes = []           # (day, seat, target)
        self.exiles = []          # (day, seat, role, tally)
        self.tally_today = {}     # seat -> votes already cast today
        self.today_votes_seen = []
        self.claims = {}          # seat -> "seer"|"doctor" (first claim wins)
        self.seer_claims = []     # seats claiming seer, in claim order
        self.seer_results_claimed = {}  # claimer -> {seat: bool}
        self.accusations = {}     # target -> [(accuser, seq)]
        self.vote_intents = {}    # speaker -> target seat stated in speech
        self.defenses = {}        # target -> [defender]
        for e in self.log:
            kind = e.get("kind")
            day = e.get("day", 0)
            if kind == "dawn" and e.get("died") is not None:
                self.deaths.append((e["died"], e.get("role")))
                p = self.players.get(e["died"])
                if p is not None:
                    p.alive = False
                    p.role = e.get("role")
            elif kind == "speech":
                text = e.get("text") or ""
                self.speeches.append((e["seq"], day, e.get("round", 0), e["seat"], text))
                self._read_speech(e["seq"], e["seat"], text)
            elif kind == "pass":
                pass
            elif kind == "vote":
                self.votes.append((day, e["seat"], e.get("target")))
                if day == self.day and e.get("target") is not None:
                    self.tally_today[e["target"]] = self.tally_today.get(e["target"], 0) + 1
                    self.today_votes_seen.append((e["seat"], e["target"]))
            elif kind == "exile":
                self.exiles.append((day, e.get("seat"), e.get("role"), e.get("tally")))
                if e.get("seat") is not None:
                    self.deaths.append((e["seat"], e.get("role")))
                    p = self.players.get(e["seat"])
                    if p is not None:
                        p.alive = False
                        p.role = e.get("role")

    def _read_speech(self, seq, seat, text):
        m = RE_SEER_CLAIM.search(text)
        if m and "seer" not in self.claims:
            self.claims[seat] = "seer"
            self.seer_claims.append(seat)
            self.seer_results_claimed[seat] = {}
        if RE_DOCTOR_CLAIM.search(text) and "doctor" not in self.claims:
            self.claims[seat] = "doctor"
        for m in RE_WOLF_RESULT.finditer(text):
            if seat in self.seer_results_claimed:
                self.seer_results_claimed[seat][int(m.group(1))] = True
        for m in RE_CLEAN_RESULT.finditer(text):
            if seat in self.seer_results_claimed:
                self.seer_results_claimed[seat][int(m.group(1))] = False
        for m in RE_ACCUSE.finditer(text):
            t = int(m.group(1))
            if t != seat:
                self.accusations.setdefault(t, []).append((seat, seq))
                self.vote_intents[seat] = t
        # a defence: someone pushes back on an accusation against that seat
        if re.search(r"\b(?:not|no\s+way|isn't|cannot\s+be)\s+a\s+werewolf\b", text, re.I):
            for m in re.finditer(r"seat\s+(\d+)", text, re.I):
                t = int(m.group(1))
                if t != seat and t not in self.accusations:
                    self.defenses.setdefault(t, []).append(seat)

    # ---- derived facts

    def alive_seats(self):
        return [s for s in sorted(self.players) if self.players[s].alive]

    def living_others(self):
        return [s for s in self.alive_seats() if s != self.me]

    def confirmed_wolves(self):
        """Seats known for sure to be wolves (revealed or my own seer checks)."""
        out = set()
        for seat, p in self.players.items():
            if p.role == "werewolf":
                out.add(seat)
        if self.role == "seer":
            for _n, seat, wolf in self.seer_results:
                if wolf:
                    out.add(seat)
        return out

    def confirmed_clean(self):
        out = set()
        for seat, p in self.players.items():
            if p.role is not None and p.role != "werewolf":
                out.add(seat)
        if self.role == "seer":
            for _n, seat, wolf in self.seer_results:
                if not wolf:
                    out.add(seat)
        return out

    def real_seer_claimer(self):
        """The most credible living seer claimer that is not me: revealed-clean
        claimed results and no contradiction beat the rest."""
        best, best_score = None, -1.0
        for claimer in self.seer_claims:
            if claimer == self.me or not self.players[claimer].alive:
                continue
            score = 1.0
            wrong = False
            for seat, wolf in self.seer_results_claimed.get(claimer, {}).items():
                p = self.players.get(seat)
                if p is not None and p.role is not None:
                    if (p.role == "werewolf") != wolf:
                        wrong = True
            if wrong:
                score = -5.0
            elif self.seer_results_claimed.get(claimer):
                score += 1.0
            if score > best_score:
                best, best_score = claimer, score
        if best is not None and best_score < 0:
            return None
        return best

    def fake_seer(self):
        """A living seer claimer whose revealed results contradict the reveals."""
        for claimer in self.seer_claims:
            if claimer == self.me or not self.players[claimer].alive:
                continue
            for seat, wolf in self.seer_results_claimed.get(claimer, {}).items():
                p = self.players.get(seat)
                if p is not None and p.role is not None and (p.role == "werewolf") != wolf:
                    return claimer
        return None

    def accusation_weight(self, seat):
        return len({a for a, _s in self.accusations.get(seat, [])})

    def suspicion(self, seat):
        """A small, explicable suspicion score for seat."""
        s = 0.0
        if seat in self.confirmed_wolves():
            return 100.0
        if seat in self.confirmed_clean():
            return -100.0
        s += 0.8 * self.accusation_weight(seat)
        credible = self.real_seer_claimer()
        if credible is not None:
            claimed = self.seer_results_claimed.get(credible, {})
            if seat in claimed:
                s += 6.0 if claimed[seat] else -6.0
        if self.claims.get(seat) == "seer":
            s -= 0.5
        if self.claims.get(seat) == "doctor":
            s -= 0.3
        if seat in self.defenses and seat not in self.accusations:
            s -= 0.2
        # voted against a credible seer claim, or for a revealed villager
        for day, voter, target in self.votes:
            if voter == seat and target is not None:
                if credible is not None and target == credible:
                    s += 1.2
                p = self.players.get(target)
                if p is not None and p.role is not None and p.role != "werewolf":
                    s += 0.8
        # my own seer results are already in confirmed_*; unrevealed dead stay neutral
        return s

    def partner_alive(self):
        return self.partner is not None and self.players[self.partner].alive

    def current_plurality(self):
        """(seat, votes) leading today's vote so far, or (None, 0)."""
        if not self.tally_today:
            return None, 0
        seat = max(sorted(self.tally_today), key=lambda s: self.tally_today[s])
        return seat, self.tally_today[seat]


# ---------------------------------------------------------------- decision policy

def clamp_target(target, options):
    if target is not None and target in options:
        return target
    return None


def pick_seeded(options, game, label):
    r = rng(game, label)
    return options[r.randrange(len(options))] if options else None


def vote_target_village(g, game, options):
    """The village vote: confirmed info, credible claims, then suspicion."""
    me_clean = g.confirmed_clean()
    for _n, seat, wolf in g.seer_results:
        if wolf and seat in options:
            return seat
    credible = g.real_seer_claimer()
    if credible is not None:
        claimed = g.seer_results_claimed.get(credible, {})
        for seat, is_wolf in sorted(claimed.items()):
            if is_wolf and seat in options and seat != credible:
                return seat
        # follow the claimer's stated vote intent when it is not against a clean seat
        intent = g.vote_intents.get(credible)
        if intent in options and intent not in me_clean:
            return intent
    fake = g.fake_seer()
    if fake is not None and fake in options and credible is None:
        return fake
    scores = [(g.suspicion(s), s) for s in options]
    best = max(scores) if scores else (0.0, None)
    lead_seat, lead_votes = g.current_plurality()
    if best[0] >= 1.5:
        return best[1]
    if lead_seat is not None and lead_votes >= 2 and lead_seat not in me_clean:
        # bandwagon with the table when it points anywhere plausible
        return lead_seat
    if lead_seat is not None and lead_votes >= 3:
        return lead_seat
    return None


def vote_target_wolf(g, game, options):
    """The wolf vote: bury villagers, follow the herd, never the partner (until
    the bus is the only way to survive the day)."""
    me_wolves = {g.me, g.partner} if g.partner else {g.me}
    lead_seat, lead_votes = g.current_plurality()
    lead_clean = lead_seat is not None and lead_seat not in me_wolves
    non_wolves = [s for s in options if s != g.partner and s not in g.confirmed_wolves()]
    # bus: the partner is going down anyway; join to buy credibility
    if g.partner is not None and g.partner in options:
        p_votes = g.tally_today.get(g.partner, 0)
        others = len(g.living_others())
        if p_votes >= 2 and p_votes > max([v for s, v in g.tally_today.items() if s not in me_wolves] or [0]):
            return g.partner
        if p_votes >= max(2, others - 3):
            return g.partner
    if lead_clean and lead_votes >= 2 and lead_seat in options:
        return lead_seat
    # vote the table's declared suspect so my vote matches my speech
    intent = g.vote_intents.get(g.me)
    if intent is not None and intent in non_wolves:
        return intent
    # distance from the partner when a credible claimer named them
    if g.partner is not None and g.partner in options:
        partner_named = any(g.partner in claimed for claimed in g.seer_results_claimed.values())
        credible = g.real_seer_claimer()
        if partner_named and credible is not None and credible != g.me:
            return g.partner
    scores = []
    for s in non_wolves:
        scores.append((g.accusation_weight(s) * 0.5 - 0.1 * len(g.defenses.get(s, [])), s))
    scores.sort(reverse=True)
    if scores and scores[0][0] > 0:
        return scores[0][1]
    return pick_seeded(non_wolves, game, "wolfvote")


def kill_target(g, game, options):
    """The night kill: the claimed seer, else the claimed doctor, else the loudest
    village voice, else a seeded villager."""
    if g.role != "werewolf":
        return None
    non_wolves = [s for s in options if s != g.partner]
    # as the second wolf voter, adopt the partner's pick: agreement is a kill
    for night, seat, target in g.wolf_votes:
        if night == g.day and seat != g.me and target is not None and target in options:
            return target
    for claimer in g.seer_claims:
        if claimer in options and claimer != g.me and claimer != g.partner:
            return claimer
    doctors = [s for s, c in g.claims.items() if c == "doctor" and s in options and s != g.partner]
    if doctors:
        return doctors[0]
    # the loudest credible voice: most accusations made against others
    accusers = {}
    for target, lst in g.accusations.items():
        for accuser, _seq in lst:
            if accuser in non_wolves:
                accusers[accuser] = accusers.get(accuser, 0) + 1
    if accusers:
        loudest = max(sorted(accusers), key=lambda s: accusers[s])
        if accusers[loudest] >= 1:
            return loudest
    return pick_seeded(non_wolves, game, "kill")


def protect_target(g, game, options):
    """The doctor's protection: the credible seer claimer, else a seeded body."""
    if g.last_protect is not None:
        options = [s for s in options if s != g.last_protect]
    for claimer in g.seer_claims:
        if claimer in options and claimer != g.me and g.players[claimer].alive:
            return claimer
    if g.role == "doctor":
        # protect whoever the wolves most likely hit: a loud village voice
        accusers = {}
        for target, lst in g.accusations.items():
            for accuser, _seq in lst:
                if accuser in options and accuser != g.me:
                    accusers[accuser] = accusers.get(accuser, 0) + 1
        if accusers:
            return max(sorted(accusers), key=lambda s: accusers[s])
    return pick_seeded(options, game, "protect")


def inspect_target(g, game, options):
    """The seer's inspection: the most suspicious seat not yet inspected."""
    done = {seat for _n, seat, _w in g.seer_results}
    pool = [s for s in options if s not in done]
    if not pool:
        return None
    pool.sort(key=lambda s: (-g.suspicion(s), s))
    top = g.suspicion(pool[0])
    if top <= 0.0 and len(pool) > 1:
        return pick_seeded(pool, game, "inspect")
    return pool[0]


# ---------------------------------------------------------------- statements

def name_of(g, seat):
    p = g.players.get(seat)
    return f"seat {seat} ({p.name})" if p else f"seat {seat}"


def statement_for(g, game, decision):
    """A statement for this debate turn: a function of the view and the seed."""
    round_no = decision.get("round", 1)
    max_chars = decision.get("max_chars", 500)
    speak_round = f"{game}:speak:{g.day}:{round_no}"
    r = rng(game, speak_round)
    me_clean = g.confirmed_clean()

    if g.role == "werewolf":
        return wolf_statement(g, game, r, round_no, max_chars)
    return village_statement(g, game, r, round_no, max_chars, me_clean)


def wolf_statement(g, game, r, round_no, max_chars):
    """Blend in: back a credible claimer, echo the table's read, give the town
    exactly one plausible villain who is not me or my partner."""
    me_wolves = {g.me, g.partner} if g.partner else {g.me}
    non_wolves = [s for s in g.living_others() if s not in me_wolves]
    partner_votes = g.tally_today.get(g.partner, 0) if g.partner else 0
    top_accused_me = g.accusation_weight(g.me) + (1 if g.tally_today.get(g.me) else 0)

    # partner cornered by votes: step away from them, one clean sentence
    if partner_votes >= 2 and g.partner is not None and g.players.get(g.partner) and g.players[g.partner].alive:
        away = pick_seeded(non_wolves, game, f"bus:{g.day}:{round_no}")
        return fold(f"{name_of(g, g.partner)}'s position looks bad and I won't pretend otherwise — I judge each case on its own. My eye is on {name_of(g, away)}: quiet all game, and quiet is not innocent.", max_chars)
    # a credible seer claimer: endorse it, add my own soft read
    credible = g.real_seer_claimer()
    if credible is not None and credible != g.me:
        # a credible counter-claimer against my partner: distance from the partner
        if g.partner is not None and g.players.get(g.partner) and g.players[g.partner].alive:
            partner_named = any(g.partner in claimed for claimed in g.seer_results_claimed.values())
            if partner_named:
                pivot = pick_seeded(non_wolves, game, f"dist:{g.day}:{round_no}")
                return fold(f"I can't square what {name_of(g, credible)} is saying with how {name_of(g, g.partner)} has been talking, and I go by evidence, not loyalty. My suspicion sits with {name_of(g, pivot)} today.", max_chars)
        claimed = g.seer_results_claimed.get(credible, {})
        wolf_picks = [s for s, w in sorted(claimed.items()) if w and g.players[s].alive and s not in me_wolves]
        if wolf_picks:
            return fold(f"{name_of(g, credible)}'s seer claim holds up against the reveals. {name_of(g, wolf_picks[0])} is a wolf on their word, and I'd rather trust that than a hunch. Voting {name_of(g, wolf_picks[0])}.", max_chars)
        return fold(f"I find {name_of(g, credible)}'s seer claim credible so far — nothing it said contradicts what we've seen. Follow that read today.", max_chars)
    # I am accused: one plain defence, then pivot to a villager
    if top_accused_me and r.random() < 0.75:
        pivot = pick_seeded(non_wolves, game, f"pivot:{g.day}:{round_no}")
        extra = f" If you want heat with substance, {name_of(g, pivot)} has floated all game." if pivot else ""
        return fold(f"I'm a plain villager and the push on me is noise, not evidence.{extra}", max_chars)
    # the table's existing suspect: echo it (the house bandwagons)
    lead, lead_votes = g.current_plurality()
    target = None
    if lead is not None and lead not in me_wolves and lead_votes >= 1:
        target = lead
    if target is None:
        scored = sorted(non_wolves, key=lambda s: -g.accusation_weight(s))
        target = scored[0] if scored and g.accusation_weight(scored[0]) > 0 else None
    if target is None and non_wolves:
        target = pick_seeded(non_wolves, game, f"accuse:{g.day}:{round_no}")
    if target is None:
        return fold("Nothing solid yet — I want to hear more before I put a vote down.", max_chars)
    reasons = [
        f"{name_of(g, target)} keeps steering without ever committing to a read.",
        f"{name_of(g, target)} was quick to accuse but slow to explain why.",
        f"{name_of(g, target)}'s votes haven't matched their talk once this game.",
    ]
    return fold(f"{r.choice(reasons)} That's my read, and I'm voting {name_of(g, target)}.", max_chars)


def village_statement(g, game, r, round_no, max_chars, me_clean):
    """Village talk: seer reveals, claim-following, plain reads, defenses."""
    # I am the seer: reveal everything, every round
    if g.role == "seer":
        wolves = [(n, s) for n, s, w in g.seer_results if w and g.players[s].alive]
        cleans = [(n, s) for n, s, w in g.seer_results if not w]
        parts = [f"I am the seer — no act, here are my checks."]
        for _n, s in wolves:
            parts.append(f"{name_of(g, s)} is a werewolf.")
        for _n, s in cleans[:3]:
            parts.append(f"{name_of(g, s)} came back clean.")
        target = vote_target_village(g, game, [s for s in g.living_others() if s != g.me])
        if target is not None:
            parts.append(f"Everyone who believes me: vote {name_of(g, target)}.")
        return fold(" ".join(parts), max_chars)
    # a fake seer is outed by revealed results: call it
    fake = g.fake_seer()
    if fake is not None:
        return fold(f"{name_of(g, fake)}'s seer claim is proven wrong by the reveals — that's a wolf lying. Vote {name_of(g, fake)}.", max_chars)
    # a credible claimer: amplify it
    credible = g.real_seer_claimer()
    if credible is not None and credible != g.me:
        claimed = g.seer_results_claimed.get(credible, {})
        wolf_picks = [s for s, w in sorted(claimed.items()) if w and g.players[s].alive]
        if wolf_picks and round_no == 1:
            return fold(f"{name_of(g, credible)}'s seer claim checks out against the reveals so far. {name_of(g, wolf_picks[0])} is a wolf on their word — I'm voting {name_of(g, wolf_picks[0])}.", max_chars)
        if wolf_picks:
            return fold(f"Staying with the seer claim from {name_of(g, credible)}: vote {name_of(g, wolf_picks[0])}. Splitting the town now only helps the wolves.", max_chars)
        return fold(f"I believe {name_of(g, credible)} is the real seer. Protect that claim and follow their read.", max_chars)
    # I am accused: defend once, plainly
    top_accused_me = g.accusation_weight(g.me) + (1 if g.tally_today.get(g.me) else 0)
    if top_accused_me and r.random() < 0.75:
        role_line = {
            "doctor": "I'm the doctor, and I've been protecting quietly.",
            "villager": "I'm a plain villager with nothing to hide.",
        }.get(g.role, "I'm a plain villager with nothing to hide.")
        pivot = pick_seeded([s for s in g.living_others() if s not in me_clean], game, f"defend:{g.day}")
        extra = f" If you want a real suspect, {name_of(g, pivot)} has been sliding all game." if pivot else ""
        return fold(f"{role_line} The push on me has no evidence behind it.{extra}", max_chars)
    # ordinary village talk: my read and my vote
    target = vote_target_village(g, game, [s for s in g.living_others() if s != g.me])
    if target is None:
        lead, votes = g.current_plurality()
        if lead is not None and votes >= 2:
            target = lead
    if target is None:
        quiet = [s for s in g.living_others() if len([1 for _a, _s in g.accusations.get(s, [])]) == 0]
        if quiet and r.random() < 0.5:
            q = pick_seeded(quiet, game, f"quiet:{g.day}")
            return fold(f"Nobody has put heat on {name_of(g, q)} yet — I want them to actually argue today before I vote.", max_chars)
        return fold("I don't have a solid read yet. Claim what you actually are and back it — I'll vote with the best case.", max_chars)
    w = g.accusation_weight(target)
    credible_accusers = [a for a, _s in g.accusations.get(target, []) if g.claims.get(a) == "seer"]
    if credible_accusers:
        return fold(f"Multiple reads point at {name_of(g, target)} and the seer claim backs them. I'm voting {name_of(g, target)}.", max_chars)
    if w >= 2:
        return fold(f"{name_of(g, target)} has drawn heat from {w} different players without a good answer. I'm voting {name_of(g, target)}.", max_chars)
    reasons = [
        f"{name_of(g, target)} hasn't given this town a single useful read.",
        f"{name_of(g, target)}'s behaviour only makes sense if they're a wolf.",
        f"{name_of(g, target)} keeps voting with whoever shouts last.",
    ]
    return fold(f"{r.choice(reasons)} I'm voting {name_of(g, target)} unless they say something real.", max_chars)


def pick_name(g, r, pool):
    if not pool:
        return "someone quiet"
    return name_of(g, pool[r.randrange(len(pool))])


def fold(text, max_chars):
    """Fold to one line the env's way and cut to the limit."""
    text = re.sub(r"[\t\n\v\f\r\u0085\u2028\u2029]+", " ", text)
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    chars = list(text)
    if len(chars) > max_chars:
        text = "".join(chars[:max_chars]).rstrip()
    return text


# ---------------------------------------------------------------- decision dispatch

def decide(g, game, decision, options):
    kind = decision
    if kind == "vote":
        if g.role == "werewolf":
            return vote_target_wolf(g, game, options)
        return vote_target_village(g, game, options)
    if kind == "kill":
        return kill_target(g, game, options)
    if kind == "protect":
        return protect_target(g, game, options)
    if kind == "inspect":
        return inspect_target(g, game, options)
    return None


def handle(pending, stats):
    game = pending["game"]
    kind = pending["decision"]
    view = pending["view"]
    options = pending.get("options")
    g = GameState(view)

    def bump(key):
        stats[key] = stats.get(key, 0) + 1

    try:
        if kind == "speak":
            text = statement_for(g, game, pending)
            if text:
                status, _b = post("/act", {"game": game, "decision": "speak", "text": text})
                if status == 200:
                    bump("speaks")
                    return
                bump(f"refused_speak_{status}")
            status, _b = post("/act", {"game": game, "decision": "speak", "pass": True})
            if status != 200:
                bump(f"refused_pass_{status}")
            return
        target = decide(g, game, kind, options or [])
        status, _b = post("/act", {"game": game, "decision": kind, "target": target})
        if status == 200:
            bump(kind)
            return
        bump(f"refused_{kind}_{status}")
        status2, _b2 = post("/act", {"game": game, "decision": kind, "target": None})
        bump(f"abstain_{status2}")
    except Exception as e:
        # never stall the trial on one bad decision
        try:
            if kind == "speak":
                post("/act", {"game": game, "decision": "speak", "pass": True})
            else:
                post("/act", {"game": game, "decision": kind, "target": None})
        except Exception:
            pass
        bump("errors")
        print(f"game {game} {kind}: {e}", file=sys.stderr)


# ---------------------------------------------------------------- the run

def run():
    brief = ""
    if PROMPT_FILE and os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            brief = f.read()
    if not ENV_URL:
        raise SystemExit("MAKEFUN_ENV_URL is required")
    print(f"werewolf scripted agent: {len(brief)} bytes of brief, seed {SEED[:8]}...")
    stats = {}
    seen = set()
    while True:
        status, obs = post("/observe", {})
        if status != 200 or not isinstance(obs, dict):
            print(f"/observe answered {status}; retrying", file=sys.stderr)
            time.sleep(0.5)
            continue
        if obs.get("done"):
            break
        for p in obs.get("pending", []):
            key = f"{p['game']}:{p['decision']}:{len(p.get('view', {}).get('log', []))}"
            if key in seen:
                continue
            seen.add(key)
            handle(p, stats)
    print(f"done: {json.dumps(stats, sort_keys=True)}")


if __name__ == "__main__":
    run()
