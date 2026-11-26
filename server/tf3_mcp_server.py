#!/usr/bin/env python3
"""Transport Fever 3 MCP server (read-only).

Asks the "Network Data Bridge (MCP)" game mod for live data and exposes it as MCP tools over stdio, so an AI
assistant can look at your lines, vehicles, towns and the vehicle models you can buy. Standard library only.

The game's Lua cannot open sockets or files directly, so the exchange goes through the game's userdata files, in a
folder the game owns (the mod picks it and writes mcp_bridge__hello_<stamp>.lua there):
  - this server rewrites mcp_bridge__request_<stamp>.lua (named after the newest hello file)
    { id = <number>, what = "snapshot" | "models" }
  - the mod (reading it twice a second) answers with mcp_bridge__<what>_<id>_p1..pN.lua (JSON in parts) and then
    the manifest mcp_bridge__<what>_<id>.lua  { seq = id, parts = N }
The game cannot overwrite or delete files, so this server removes older answers and hello files.
Set TF3_MCP_BRIDGE_DIR to the exchange folder to skip the search, TF3_MCP_TIMEOUT to change the 20 s wait.
"""
import glob
import json
import os
import re
import sys
import time

SERVER_NAME = "tf3-mcp-bridge"
SERVER_VERSION = "1.3.0"
APP_ID = "3493540"
EXCHANGE_DIRS = ("recordings", "keyframes", "mod_presets")
PREFIX = "mcp_bridge__"
REQUEST_TIMEOUT_SEC = float(os.environ.get("TF3_MCP_TIMEOUT", "20"))
SNAPSHOT_REUSE_SEC = 3  # several tool calls in one answer share one snapshot
MODELS_REUSE_SEC = 600
# newer request kinds: (seconds an answer is reused, first mod bridgeVersion that answers it)
KINDS = {"industries": (60, 5), "stations": (10, 5), "finances": (10, 5), "vehicles": (2, 5),
         "warehouses": (10, 6)}

# ----------------------------------------------------------------------------------------------------------------
# locating the exchange folder


def steam_dirs():
    dirs = []
    if sys.platform == "win32":
        try:
            import winreg
            for root, key, value in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(root, key) as k:
                        dirs.append(winreg.QueryValueEx(k, value)[0])
                except OSError:
                    pass
        except ImportError:
            pass
        dirs.append(r"C:\Program Files (x86)\Steam")
    else:
        home = os.path.expanduser("~")
        dirs += [os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam")]
    seen, out = set(), []
    for d in dirs:
        n = os.path.normcase(os.path.normpath(d))
        if n not in seen and os.path.isdir(d):
            seen.add(n)
            out.append(d)
    return out


def userdata_dirs():
    out = []
    for steam in steam_dirs():
        out += glob.glob(os.path.join(steam, "userdata", "*", APP_ID, "local"))
    return out


def hello_files(folder):
    return sorted(glob.glob(os.path.join(folder, PREFIX + "hello*.lua")), key=os.path.getmtime, reverse=True)


def bridge_dir():
    """The exchange folder with the newest hello file (written by the mod each time a game loads)."""
    env = os.environ.get("TF3_MCP_BRIDGE_DIR")
    if env:
        return env
    found = []
    for local in userdata_dirs():
        for d in EXCHANGE_DIRS:
            folder = os.path.join(local, d)
            hellos = hello_files(folder)
            if hellos:
                found.append((os.path.getmtime(hellos[0]), folder))
    if not found:
        raise RuntimeError(
            "The game has not been connected yet: start Transport Fever 3, enable the mod 'Network Data Bridge (MCP)' "
            "for your save and load it. Searched: " + "; ".join(userdata_dirs() or ["(no Steam user data found)"]))
    found.sort(reverse=True)
    return found[0][1]


# ----------------------------------------------------------------------------------------------------------------
# reading the game's serialized Lua files ("function data() return { ... } end")


class LuaReader:
    SIMPLE_ESCAPES = {ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("v"): 11,
                      ord("\\"): 92, ord('"'): 34, ord("'"): 39, ord("\n"): 10, ord("\r"): 10}

    def __init__(self, data):
        self.s = data
        self.i = 0

    def error(self, msg):
        raise ValueError(f"{msg} at byte {self.i}")

    def peek(self, n=1):
        return self.s[self.i:self.i + n]

    def skip(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in b" \t\r\n":
                self.i += 1
            elif s.startswith(b"--", self.i):
                level = self.long_bracket_level(self.i + 2)
                if level is not None:
                    close = b"]" + b"=" * level + b"]"
                    end = s.find(close, self.i)
                    self.i = len(s) if end < 0 else end + len(close)
                else:
                    nl = s.find(b"\n", self.i)
                    self.i = len(s) if nl < 0 else nl + 1
            else:
                break

    def long_bracket_level(self, pos):
        s = self.s
        if s[pos:pos + 1] == b"[":
            j = pos + 1
            while s[j:j + 1] == b"=":
                j += 1
            if s[j:j + 1] == b"[":
                return j - pos - 1
        return None

    def long_string(self, level):
        start = self.i + level + 2
        close = b"]" + b"=" * level + b"]"
        end = self.s.find(close, start)
        if end < 0:
            self.error("unclosed long string")
        self.i = end + len(close)
        text = self.s[start:end]
        if text.startswith(b"\r\n"):
            text = text[2:]
        elif text.startswith(b"\n"):
            text = text[1:]
        return text

    def quoted(self):
        s, q = self.s, self.s[self.i]
        self.i += 1
        out = bytearray()
        while True:
            if self.i >= len(s):
                self.error("unclosed string")
            c = s[self.i]
            if c == q:
                self.i += 1
                return bytes(out)
            if c != 92:  # plain run up to the next backslash or quote
                stops = [x for x in (s.find(b"\\", self.i), s.find(bytes([q]), self.i)) if x >= 0]
                stop = min(stops) if stops else len(s)
                out += s[self.i:stop]
                self.i = stop
                continue
            e = s[self.i + 1]
            if e in self.SIMPLE_ESCAPES:
                out.append(self.SIMPLE_ESCAPES[e])
                self.i += 2
                if e == 13 and s[self.i:self.i + 1] == b"\n":
                    self.i += 1
            elif e == ord("z"):
                self.i += 2
                while self.i < len(s) and s[self.i] in b" \t\r\n":
                    self.i += 1
            elif e == ord("x"):
                out.append(int(s[self.i + 2:self.i + 4], 16))
                self.i += 4
            elif e == ord("u"):
                end = s.index(b"}", self.i)
                out += chr(int(s[self.i + 3:end], 16)).encode("utf-8")
                self.i = end + 1
            elif 48 <= e <= 57:
                j = self.i + 1
                while j < self.i + 4 and j < len(s) and 48 <= s[j] <= 57:
                    j += 1
                out.append(int(s[self.i + 1:j]))
                self.i = j
            else:
                self.error("bad escape")

    def name(self):
        s, j = self.s, self.i
        while j < len(s) and (chr(s[j]).isalnum() or s[j] == 95):
            j += 1
        word = s[self.i:j].decode("ascii")
        self.i = j
        return word

    def value(self):
        self.skip()
        c = self.peek()
        if not c:
            self.error("unexpected end")
        if c == b"{":
            return self.table()
        if c in (b'"', b"'"):
            return self.quoted()
        if c == b"[":
            level = self.long_bracket_level(self.i)
            if level is not None:
                return self.long_string(level)
        if c == b"-" and self.s.startswith(b"-math.huge", self.i):
            self.i += len(b"-math.huge")
            return float("-inf")
        if c in (b"-", b".") or c.isdigit():
            s, j = self.s, self.i + 1
            while j < len(s) and (chr(s[j]).isalnum() or s[j:j + 1] in (b".", b"+", b"-")):
                if s[j:j + 1] in (b"+", b"-") and s[j - 1:j] not in (b"e", b"E", b"p", b"P"):
                    break
                j += 1
            text = s[self.i:j].decode("ascii")
            self.i = j
            try:
                return int(text, 0)
            except ValueError:
                return float(text)
        if self.s.startswith(b"math.huge", self.i):
            self.i += len(b"math.huge")
            return float("inf")
        word = self.name()
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "nil":
            return None
        self.error(f"unexpected {word or c!r}")

    def table(self):
        self.i += 1
        items, keyed = [], {}
        while True:
            self.skip()
            c = self.peek()
            if not c:
                self.error("unclosed table")
            if c == b"}":
                self.i += 1
                break
            if c == b"[" and self.long_bracket_level(self.i) is None:
                self.i += 1
                key = self.value()
                self.skip()
                self.i += 1  # ]
                self.skip()
                self.i += 1  # =
                keyed[key.decode("utf-8") if isinstance(key, bytes) else key] = self.value()
            elif c.isalpha() or c == b"_":
                save = self.i
                word = self.name()
                self.skip()
                if self.peek() == b"=" and self.peek(2) != b"==":
                    self.i += 1
                    keyed[word] = self.value()
                else:
                    self.i = save
                    items.append(self.value())
            else:
                items.append(self.value())
            self.skip()
            if self.peek() in (b",", b";"):
                self.i += 1
        if keyed:
            for n, v in enumerate(items, 1):
                keyed[n] = v
            return keyed
        return items


def read_lua_data(path):
    with open(path, "rb") as f:
        data = f.read()
    at = data.find(b"return")
    if at < 0:
        raise ValueError(f"{path}: no 'return'")
    r = LuaReader(data)
    r.i = at + len(b"return")
    return r.value()


# ----------------------------------------------------------------------------------------------------------------
# requests


def answer_ids(folder, what):
    """Ids of the answers of one kind whose manifest exists, newest first."""
    pat = re.compile(re.escape(PREFIX + what) + r"_(\d+)\.lua$")
    found = []
    for name in os.listdir(folder):
        m = pat.match(name)
        if m:
            found.append((os.path.getmtime(os.path.join(folder, name)), int(m.group(1))))
    return [rid for _, rid in sorted(found, reverse=True)]


def read_answer(folder, what, rid):
    """(data, age seconds) of one complete answer, or None when it is absent or incomplete."""
    base = os.path.join(folder, f"{PREFIX}{what}_{rid}")
    try:
        manifest = read_lua_data(base + ".lua")
        if manifest.get("seq") != rid:
            return None
        chunks = []
        for i in range(1, int(manifest.get("parts")) + 1):
            part = read_lua_data(f"{base}_p{i}.lua")
            if part.get("seq") != rid:
                return None
            chunks.append(part.get("data") or b"")
        age = time.time() - os.path.getmtime(base + ".lua")
        return json.loads(b"".join(chunks).decode("utf-8")), age
    except Exception:  # incomplete, being written or malformed
        return None


def request_name(folder):
    """The request file of the current game load: the mod creates mcp_bridge__request_<stamp> with its hello file."""
    hellos = hello_files(folder)
    if not hellos:
        raise RuntimeError("No hello file from the mod in " + folder)
    stamp = os.path.basename(hellos[0])[len(PREFIX + "hello_"):-len(".lua")]
    return PREFIX + "request_" + stamp


def cleanup(folder, what, keep_rid):
    """Remove the other answers of this kind and all but the newest hello file (the game cannot delete files)."""
    pat = re.compile(re.escape(PREFIX + what) + r"_(\d+)(_p\d+)?\.lua$")
    for name in os.listdir(folder):
        m = pat.match(name)
        if m and int(m.group(1)) != keep_rid:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    for old in hello_files(folder)[1:]:
        try:
            os.remove(old)
        except OSError:
            pass
    current = request_name(folder) + ".lua"
    for old in glob.glob(os.path.join(folder, PREFIX + "request*.lua")):
        if os.path.basename(old) != current:
            try:
                os.remove(old)
            except OSError:
                pass


def write_request(folder, rid, what):
    path = os.path.join(folder, request_name(folder) + ".lua")
    tmp = os.path.join(folder, PREFIX + "request.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\r\n") as f:
        f.write('function data()\nreturn {\n\tid = %d,\n\twhat = "%s",\n}\nend\n' % (rid, what))
    for _ in range(20):  # the game may be reading the file at this instant
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def request(what, timeout=None):
    timeout = REQUEST_TIMEOUT_SEC if timeout is None else timeout
    folder = bridge_dir()
    rid = int(time.time() * 1000) % 2_000_000_000
    write_request(folder, rid, what)
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = read_answer(folder, what, rid)
        if got is not None:
            cleanup(folder, what, rid)
            return got[0]
        time.sleep(0.1)
    raise TimeoutError(what)


def last_answer(what):
    folder = bridge_dir()
    for rid in answer_ids(folder, what):
        got = read_answer(folder, what, rid)
        if got is not None:
            return got
    return None


_cache = {}


def fetch(what, reuse_sec):
    """Live data from the game; the last answer (with its age and a warning) when the game does not answer."""
    hit = _cache.get(what)
    if hit and time.time() - hit[1] < reuse_sec:
        return hit[0], time.time() - hit[1], None
    try:
        data = request(what)
        _cache[what] = (data, time.time())
        return data, 0.0, None
    except TimeoutError:
        old = last_answer(what)
        warning = (f"The game did not answer within {REQUEST_TIMEOUT_SEC:g} s (closed, paused in a menu or loading, or "
                   "the mod is not enabled for this save).")
        if old is None:
            raise RuntimeError(warning)
        return old[0], old[1], warning + f" Showing the last answer, {round(old[1])} s old."


def load_snapshot():
    snap, age, warning = fetch("snapshot", SNAPSHOT_REUSE_SEC)
    return snap, warning, age


def mod_version():
    """bridgeVersion from the newest hello file (written by the mod when a save loads)."""
    try:
        hellos = hello_files(bridge_dir())
        return int(read_lua_data(hellos[0]).get("bridgeVersion") or 0) if hellos else 0
    except Exception:
        return 0


def load_kind(what):
    reuse, needs = KINDS[what]
    have = mod_version()
    if have and have < needs:
        raise RuntimeError(f"This needs a newer game mod (bridge version {needs}, the game has {have}). Update "
                           "'Network Data Bridge (MCP)' from the Mod Hub, then restart the game and load the save.")
    data, age, warning = fetch(what, reuse)
    return data, warning, age


def load_models():
    models, age, warning = fetch("models", MODELS_REUSE_SEC)
    return models, warning


def freshness(warning, age):
    out = {"dataAgeSec": round(age)}
    if warning:
        out["warning"] = warning
    return out


# ----------------------------------------------------------------------------------------------------------------
# helpers


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def find_line(snap, ref):
    lines = snap.get("lines", [])
    ref_s = str(ref).strip()
    for line in lines:
        if str(line.get("id")) == ref_s:
            return line
    low = ref_s.lower()
    exact = [l for l in lines if (l.get("name") or "").lower() == low]
    if exact:
        return exact[0]
    partial = [l for l in lines if low in (l.get("name") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(f"{l.get('name')} (id {l.get('id')})" for l in partial[:15])
        raise ValueError(f"'{ref}' matches several lines: {names}")
    raise ValueError(f"No line with id or name '{ref}'")


def interval_sec(l):
    """Seconds between departures (the line window's interval); the game reports frequency = 1 / interval."""
    f = l.get("frequency")
    return round(1 / f) if isinstance(f, (int, float)) and f > 0 else None


def line_row(l):
    return {
        "id": l.get("id"),
        "name": l.get("name"),
        "carriers": l.get("carriers") or [],  # an empty Lua table arrives as {}
        "cargos": l.get("cargos") or None,
        "towns": l.get("towns") or None,
        "stops": len(l.get("stops") or []),
        "vehicles": l.get("vehicleCount"),
        "usagePct": l.get("usagePct"),
        "profit12m": l.get("profit12m"),
        "profitPerVehicle12m": l.get("profitPerVehicle12m"),
        "estIncome12m": l.get("estIncome12m"),
        "runningCostsPerYear": l.get("runningCostsPerYear"),
        "intervalSec": interval_sec(l),
        "problem": l.get("problem"),
        "cargoPastDue": (l.get("cargoQuality") or {}).get("pastDue"),
    }


def vehicle_row(v):
    units = v.get("units") or []
    models = {}
    for u in units:
        key = u.get("name") or str(u.get("model"))
        models[key] = models.get(key, 0) + 1
    return {
        "id": v.get("id"),
        "name": v.get("name"),
        "models": models,
        "state": v.get("state"),
        "load": v.get("load"),
        "capacity": v.get("capacity"),
        "ageYears": round(v["ageDays"] / 365.0, 1) if v.get("ageDays") is not None else None,
        "lifespanYears": round(v["lifespanDays"] / 365.0, 1) if v.get("lifespanDays") else None,
        "ageOfLifespan": v.get("ageOfLifespan"),
        "worstMaintenance": v.get("worstMaintenance"),
        "runningCostPerYear": v.get("runningCostPerYear"),
        "value": v.get("value"),
        "profit12m": v.get("profit12m"),
        "userStopped": v.get("userStopped") or None,
        "noPath": v.get("noPath") or None,
        "sellOnArrival": v.get("sellOnArrival") or None,
    }


def compact(d):
    return {k: v for k, v in d.items() if v is not None}


# ----------------------------------------------------------------------------------------------------------------
# tools


def tool_status(args):
    snap, warning, age = load_snapshot()
    meta = snap.get("meta", {})
    cached_models = _cache.get("models")
    out = {
        "date": meta.get("date"),
        "money": meta.get("money"),
        "lines": meta.get("lineCount"),
        "vehicles": meta.get("vehicleCount"),
        "towns": meta.get("townCount"),
        "exchangeFolder": bridge_dir(),
        "vehicleModels": cached_models[0].get("meta", {}).get("modelCount") if cached_models else None,
        "performance": {
            "slowestFrameMs": meta.get("slowestFrameMs"),
            "slowestIdleCheckMs": meta.get("slowestIdleCheckMs"),
            "averageIdleCheckMs": meta.get("averageIdleCheckMs"),
            "slowestFramePhase": meta.get("slowestFramePhase"),
        },
        "collect": {"frames": meta.get("collectFrames"), "cpuMs": meta.get("collectCpuMs")},
        "lineLogNames": meta.get("lineLogNames"),
        "diagnostics": meta.get("diagnostics") or None,
        "bridgeVersion": meta.get("bridgeVersion"),
    }
    out.update(freshness(warning, age))
    return compact(out)


SORT_KEYS = {"profit12m", "profitPerVehicle12m", "usagePct", "vehicleCount", "estIncome12m", "runningCostsPerYear", "name"}


def tool_list_lines(args):
    snap, warning, age = load_snapshot()
    lines = snap.get("lines", [])
    carrier = (args.get("carrier") or "").upper()
    if carrier:
        lines = [l for l in lines if carrier in (l.get("carriers") or [])]
    query = (args.get("name_contains") or "").lower()
    if query:
        lines = [l for l in lines if query in (l.get("name") or "").lower()]
    town = (args.get("town") or "").lower()
    if town:
        lines = [l for l in lines if any(town in (t or "").lower() for t in (l.get("towns") or []))]
    key = args.get("sort_by") or "profit12m"
    if key not in SORT_KEYS:
        raise ValueError(f"sort_by must be one of {sorted(SORT_KEYS)}")
    desc = not args.get("ascending", key == "name")
    if key == "name":
        lines.sort(key=lambda l: (l.get("name") or "").lower(), reverse=desc)
    else:
        lines.sort(key=lambda l: (l.get(key) is None, -(l.get(key) or 0) if desc else (l.get(key) or 0)))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {
        "totalMatching": len(lines),
        "lines": [compact(line_row(l)) for l in lines[:limit]],
        "notes": "profit12m = income minus running costs over the last game year (purchases excluded). "
                 "usagePct = share of capacity used on recent trips. Use tf3_get_line for vehicles and stops.",
    }
    out.update(freshness(warning, age))
    return out


def tool_get_line(args):
    snap, warning, age = load_snapshot()
    line = find_line(snap, args.get("line", ""))
    out = {k: v for k, v in line.items() if k != "vehicles"}
    vehicles = [compact(vehicle_row(v)) for v in line.get("vehicles") or []]
    vehicles.sort(key=lambda v: -(v.get("ageOfLifespan") or 0))
    out["vehicles"] = vehicles
    by_model = {}
    for v in vehicles:
        for name, n in (v.get("models") or {}).items():
            by_model[name] = by_model.get(name, 0) + n
    out["unitsByModel"] = by_model
    out.update(freshness(warning, age))
    return compact(out)


def tool_list_vehicle_models(args):
    models, warning = load_models()
    items = models.get("models", [])
    if args.get("available_only", True):
        items = [m for m in items if m.get("available")]
    if not args.get("include_hidden", False):
        items = [m for m in items if m.get("visible", True)]
    carrier = (args.get("carrier") or "").upper()
    if carrier:
        items = [m for m in items if (m.get("carrier") or "").upper() == carrier]
    mode = (args.get("transport_mode") or "").upper()
    if mode:
        items = [m for m in items if mode in [x.upper() for x in (m.get("transportModes") or [])]]
    query = (args.get("name_contains") or "").lower()
    if query:
        items = [m for m in items if query in (m.get("name") or "").lower() or query in (m.get("file") or "").lower()]
    if args.get("min_capacity"):
        items = [m for m in items if (m.get("capacity") or 0) >= int(args["min_capacity"])]
    key = args.get("sort_by") or "capacity"
    items.sort(key=lambda m: (m.get(key) is None, -(m.get(key) or 0) if isinstance(m.get(key), (int, float)) else 0))
    limit = max(1, min(int(args.get("limit") or 60), 1000))
    fields = ("id", "name", "carrier", "transportModes", "capacity", "topSpeedKmh", "powerKw", "price",
              "lifespanDays", "yearFrom", "yearTo", "comfortFactor", "loadSpeed", "weightEmptyKg", "file")
    rows = [compact({f: m.get(f) for f in fields}) for m in items[:limit]]
    return {
        "gameDate": models.get("meta", {}).get("date"),
        "warning": warning,
        "totalMatching": len(items),
        "models": rows,
        "notes": "One row per vehicle unit (a locomotive and a wagon are separate units). price is the current "
                 "purchase price of one unit. capacity is the largest load config per compartment, summed.",
    }


TOWN_SORT_KEYS = {"population", "size", "lineUsage", "linesServing", "name"}


def tool_list_towns(args):
    snap, warning, age = load_snapshot()
    towns = snap.get("towns")
    if towns is None:
        raise RuntimeError("No towns in the data: the mod setting 'Town list' is off, or the mod is outdated.")
    serving = {}
    for l in snap.get("lines", []):
        for tid in {s.get("townId") for s in l.get("stops") or [] if s.get("townId") is not None}:
            serving.setdefault(tid, []).append(l)
    query = (args.get("name_contains") or "").lower()
    rows = []
    for t in towns:
        if query and query not in (t.get("name") or "").lower():
            continue
        lines = serving.get(t.get("id"), [])
        row = {
            "id": t.get("id"),
            "name": t.get("name"),
            "population": t.get("population"),
            "size": t.get("size"),
            "capacity": t.get("capacity"),
            "used": t.get("used"),
            "lineUsage": t.get("lineUsage"),
            "reachability": t.get("reachability"),
            "developmentActive": t.get("developmentActive"),
            "linesServing": len(lines),
            "position": t.get("position"),
        }
        if args.get("include_cargo", bool(query)):
            row["cargoNeeds"] = t.get("cargoNeeds")
            row["cargoSupply"] = t.get("cargoSupply")
        if args.get("include_lines", bool(query)):
            row["lines"] = [{"id": l.get("id"), "name": l.get("name"), "carriers": l.get("carriers"),
                             "vehicles": l.get("vehicleCount"), "usagePct": l.get("usagePct"),
                             "profit12m": l.get("profit12m")} for l in lines]
        rows.append(compact(row))
    key = args.get("sort_by") or "population"
    if key not in TOWN_SORT_KEYS:
        raise ValueError(f"sort_by must be one of {sorted(TOWN_SORT_KEYS)}")
    if key == "name":
        rows.sort(key=lambda r: (r.get("name") or "").lower(), reverse=bool(args.get("descending")))
    else:
        asc = bool(args.get("ascending"))
        rows.sort(key=lambda r: (r.get(key) is None, (r.get(key) or 0) if asc else -(r.get(key) or 0)))
    limit = max(1, min(int(args.get("limit") or 100), 1000))
    out = {
        "totalMatching": len(rows),
        "towns": rows[:limit],
        "notes": "population = residential capacity in use; size = residential + commercial + industrial capacity. "
                 "linesServing = your lines with a stop in the town. lineUsage is the game's town line-usage value. "
                 "reachability: destinations reachable by public transport vs. by car. position = estimated town "
                 "centre in world metres (x, y).",
    }
    out.update(freshness(warning, age))
    return out


def tool_route_ideas(args):
    """Town pairs for new lines: straight-line distance, populations, whether a line already links them."""
    import math
    snap, warning, age = load_snapshot()
    towns = [t for t in snap.get("towns") or [] if t.get("position")]
    if not towns:
        raise RuntimeError("No town positions in the data: the mod setting 'Town list' is off, or the mod is outdated.")
    min_pop = float(args.get("min_population") or 0)
    max_km = float(args.get("max_distance_km") or 0)
    min_km = float(args.get("min_distance_km") or 0)
    town_filter = (args.get("town") or "").lower()
    linked = {}  # (townA, townB) -> line names with stops in both
    for l in snap.get("lines", []):
        ids = sorted({st.get("townId") for st in l.get("stops") or [] if st.get("townId") is not None})
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                linked.setdefault((a, b), []).append(l.get("name"))
    towns = [t for t in towns if (t.get("population") or 0) >= min_pop]
    pairs = []
    for i, a in enumerate(towns):
        for b in towns[i + 1:]:
            if town_filter and town_filter not in (a.get("name") or "").lower() \
                    and town_filter not in (b.get("name") or "").lower():
                continue
            km = math.hypot(a["position"]["x"] - b["position"]["x"], a["position"]["y"] - b["position"]["y"]) / 1000
            if (max_km and km > max_km) or km < min_km:
                continue
            lines = linked.get(tuple(sorted((a.get("id"), b.get("id")))), [])
            pa, pb = a.get("population") or 0, b.get("population") or 0
            pairs.append(compact({
                "towns": [a.get("name"), b.get("name")],
                "populations": [pa, pb],
                "distanceKm": round(km, 1),
                "linkedBy": lines or None,
                # gravity-style demand estimate: bigger and closer towns score higher (relative, not a forecast)
                "potential": round(math.sqrt(pa * pb) / max(km, 1), 1),
            }))
    if not args.get("include_linked"):
        pairs = [p for p in pairs if "linkedBy" not in p]
    pairs.sort(key=lambda p: -p["potential"])
    limit = max(1, min(int(args.get("limit") or 25), 1000))
    out = {
        "totalMatching": len(pairs),
        "pairs": pairs[:limit],
        "notes": "distanceKm is straight-line between estimated town centres; real routes are longer and depend on "
                 "terrain. potential = sqrt(popA * popB) / distanceKm, a relative ranking only. linkedBy = your lines "
                 "with stops in both towns (hidden unless include_linked). Use tf3_list_vehicle_models for vehicles.",
    }
    out.update(freshness(warning, age))
    return out



def town_positions():
    """Town names and positions from the snapshot (for nearest-town lookups), or {} when unavailable."""
    try:
        snap, _, _ = load_snapshot()
    except Exception:
        return []
    return [(t.get("name"), t["position"]) for t in snap.get("towns") or [] if t.get("position")]


def nearest_town(pos, towns):
    import math
    if not pos or not towns:
        return None
    name, best = None, None
    for n, tp in towns:
        d = math.hypot(pos["x"] - tp["x"], pos["y"] - tp["y"])
        if best is None or d < best:
            name, best = n, d
    return {"name": name, "distanceKm": round(best / 1000, 1)}


INDUSTRY_STATUS = ("missing_inputs", "not_producing", "closing", "unshipped", "all")


def industry_flags(i):
    flags = []
    ins = i.get("inputs") or []
    if ins and all((x.get("deliveredPerYear") or 0) == 0 for x in ins):
        flags.append("missing_inputs")
    elif any((x.get("deliveredPerYear") or 0) == 0 for x in ins):
        flags.append("some_inputs_missing")
    if i.get("producing") is False:
        flags.append("not_producing")
    if i.get("closing"):
        flags.append("closing")
    outs = i.get("outputs") or []
    if any((o.get("producedPerYear") or 0) > 0 and (o.get("shippedPerYear") or 0) == 0 for o in outs):
        flags.append("unshipped")
    return flags


def tool_list_industries(args):
    data, warning, age = load_kind("industries")
    items = data.get("industries") or []
    cargo = (args.get("cargo") or "").upper()
    town = (args.get("town") or "").lower()
    status = args.get("status") or "all"
    if status not in INDUSTRY_STATUS:
        raise ValueError(f"status must be one of {list(INDUSTRY_STATUS)}")
    by_cargo = {}
    rows = []
    for i in items:
        for o in i.get("outputs") or []:
            c = by_cargo.setdefault(o["cargo"], {"producedPerYear": 0, "shippedPerYear": 0, "producers": 0,
                                                 "industryDemandPerYear": 0, "deliveredToIndustriesPerYear": 0,
                                                 "consumers": 0})
            c["producedPerYear"] += o.get("producedPerYear") or 0
            c["shippedPerYear"] += o.get("shippedPerYear") or 0
            c["producers"] += 1
        for x in i.get("inputs") or []:
            c = by_cargo.setdefault(x["cargo"], {"producedPerYear": 0, "shippedPerYear": 0, "producers": 0,
                                                 "industryDemandPerYear": 0, "deliveredToIndustriesPerYear": 0,
                                                 "consumers": 0})
            c["industryDemandPerYear"] += x.get("maxConsumptionPerYear") or 0
            c["deliveredToIndustriesPerYear"] += x.get("deliveredPerYear") or 0
            c["consumers"] += 1
        flags = industry_flags(i)
        if status != "all" and status not in flags:
            continue
        if town and town not in (i.get("town") or "").lower():
            continue
        if cargo and not any(cargo in (x.get("cargo") or "") for x in (i.get("inputs") or []) + (i.get("outputs") or [])):
            continue
        row = dict(i)
        row["flags"] = flags
        row["producedPerYear"] = sum(o.get("producedPerYear") or 0 for o in i.get("outputs") or [])
        row["shippedPerYear"] = sum(o.get("shippedPerYear") or 0 for o in i.get("outputs") or [])
        rows.append(compact(row))
    key = args.get("sort_by") or "producedPerYear"
    if key not in ("producedPerYear", "shippedPerYear", "level", "name"):
        raise ValueError("sort_by must be producedPerYear, shippedPerYear, level or name")
    if key == "name":
        rows.sort(key=lambda r: (r.get("name") or "").lower())
    else:
        rows.sort(key=lambda r: -(r.get(key) or 0))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {
        "totalMatching": len(rows),
        "industries": rows[:limit],
        "notes": "Per year figures are the game's own (last 12 months). flags: missing_inputs = no input delivered, "
                 "unshipped = produces but nothing is transported, closing = the game will close it. town = closest "
                 "town. position = world metres (x, y).",
    }
    if args.get("include_cargo_summary", True):
        out["byCargo"] = by_cargo
    out.update(freshness(warning, age))
    return out


def line_cargos():
    """Line id -> cargo names it carries (from the snapshot), or {} when unavailable."""
    try:
        snap, _, _ = load_snapshot()
    except Exception:
        return {}
    return {l.get("id"): set(l.get("cargos") or []) for l in snap.get("lines", [])}


def station_kind(st, cargos_by_line):
    """passenger / cargo / mixed from what the stopping lines carry; terminal flags when no line stops there
    (road and tram stops accept cargo by default, so their flags alone say "mixed")."""
    carried = set()
    for l in st.get("lines") or []:
        carried |= cargos_by_line.get(l.get("id"), set())
    if not carried:
        return st.get("kind")
    has_pax = "PASSENGERS" in carried
    if has_pax and len(carried) > 1:
        return "mixed"
    return "passenger" if has_pax else "cargo"


def tool_list_stations(args):
    data, warning, age = load_kind("stations")
    cargos_by_line = line_cargos()
    rows = []
    kind = (args.get("kind") or "").lower()
    town = (args.get("town") or "").lower()
    query = (args.get("name_contains") or "").lower()
    for st in data.get("stations") or []:
        st = dict(st, kind=station_kind(st, cargos_by_line))
        if kind and st.get("kind") != kind:
            continue
        if town and town not in (st.get("town") or "").lower():
            continue
        if query and query not in (st.get("name") or "").lower():
            continue
        row = dict(st)
        row["waitingTotal"] = sum((st.get("waiting") or {}).values())
        u = st.get("usage") or {}
        cap = (u.get("poolCapacity") or 0) + (u.get("terminalCapacity") or 0)
        row["usagePct"] = pct(u.get("used") or 0, cap)
        row["lines"] = [l.get("name") for l in st.get("lines") or []]
        rows.append(compact(row))
    key = args.get("sort_by") or "waitingTotal"
    if key not in ("waitingTotal", "usagePct", "terminals", "name"):
        raise ValueError("sort_by must be waitingTotal, usagePct, terminals or name")
    if key == "name":
        rows.sort(key=lambda r: (r.get("name") or "").lower())
    else:
        rows.sort(key=lambda r: -(r.get(key) or 0))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {
        "totalMatching": len(rows),
        "stations": rows[:limit],
        "notes": "One row per station group (stations that share a name). waiting = passengers or cargo waiting now, "
                 "per cargo; longWaits = cargo the game marks as waiting very long; pastDue = per cargo: count, pastDue (units "
                 "past their delivery time) and quality 0-1. usage = people in the station vs "
                 "its waiting capacity (overflow = over capacity).",
    }
    out.update(freshness(warning, age))
    return out


DEPOT_WORDS = [("ship", "WATER"), ("harbo", "WATER"), ("port", "WATER"), ("tram", "TRAM"), ("train", "RAIL"),
               ("rail", "RAIL"), ("air", "AIR"), ("hangar", "AIR"), ("heli", "AIR"), ("road", "ROAD"),
               ("bus", "ROAD"), ("truck", "ROAD")]


def depot_carrier(d):
    """The game does not expose a depot's carrier to scripts in this build; fall back to its name."""
    if d.get("carrier"):
        return d["carrier"], None
    name = (d.get("name") or "").lower()
    if "maintenance" in name:
        return None, "maintenance building"
    for word, carrier in DEPOT_WORDS:
        if word in name:
            return carrier, None
    return None, None


def tool_list_depots(args):
    data, warning, age = load_kind("stations")
    towns = town_positions()
    carrier = (args.get("carrier") or "").upper()
    rows = []
    for d in data.get("depots") or []:
        row = dict(d)
        row["carrier"], kind = depot_carrier(d)
        if kind:
            row["kind"] = kind
        if carrier and row.get("carrier") != carrier:
            continue
        row["nearestTown"] = nearest_town(d.get("position"), towns)
        rows.append(compact(row))
    rows.sort(key=lambda r: ((r.get("carrier") or ""), (r.get("name") or "").lower()))
    out = {"totalMatching": len(rows), "depots": rows,
           "notes": "Your depots with carrier (from the depot's name when the game does not report it), position "
                    "(world metres), vehicles parked inside and the nearest town. Maintenance buildings are marked."}
    out.update(freshness(warning, age))
    return out


def flow_in_out(f):
    """Incoming and outgoing per year for one cargo, from the warehouse's logbook: itemsUnloaded* (unloaded from
    vehicles into the warehouse) and itemsLoaded* (loaded onto vehicles), summed over the numbered variants. The
    plain itemsLoaded equals the game's shipped counter; the numbered ones are further channels it leaves out."""
    logs = f.get("logsPerYear") or {}
    if not logs:
        return None, f.get("shippedPerYear")
    return (sum(v for k, v in logs.items() if k.startswith("itemsUnloaded")),
            sum(v for k, v in logs.items() if k.startswith("itemsLoaded")))


def warehouse_stuck_cargo(w):
    """Cargo the warehouse holds now of which none left in the last 12 months."""
    out = {f.get("cargo"): flow_in_out(f)[1] or 0 for f in w.get("flows") or []}
    return sorted(c for c, n in (w.get("holding") or {}).items() if n and not out.get(c))


def warehouse_flags(w):
    """What is wrong with this warehouse, from what the game reports."""
    flags = []
    out = sum(flow_in_out(f)[1] or 0 for f in w.get("flows") or []) or (w.get("shippedPerYear") or 0)
    waiting = w.get("waitingTotal") or 0
    if (w.get("pastDue") or 0) > 0:
        flags.append("past_due")
    if (w.get("thrownAwayCargo") or 0) > 0:
        flags.append("overflowing")
    if any((g.get("capacity") or 0) > 0 and (g.get("waiting") or 0) >= g["capacity"] for g in w.get("stocks") or []):
        flags.append("full")
    if waiting > 0 and out == 0:
        flags.append("nothing_leaves")
    if warehouse_stuck_cargo(w):
        flags.append("stuck_cargo")
    if waiting == 0 and out == 0 and not w.get("flows"):
        flags.append("unused")
    return flags


WAREHOUSE_STATUS = ("all", "past_due", "overflowing", "full", "nothing_leaves", "stuck_cargo", "unused")
WAREHOUSE_SORT = ("waitingTotal", "pastDue", "inPerYear", "outPerYear", "name")


def tool_list_warehouses(args):
    data, warning, age = load_kind("warehouses")
    items = data.get("warehouses") or []
    towns = town_positions()
    cargo = (args.get("cargo") or "").upper()
    town = (args.get("town") or "").lower()
    name_contains = (args.get("name_contains") or "").lower()
    status = args.get("status") or "all"
    if status not in WAREHOUSE_STATUS:
        raise ValueError(f"status must be one of {list(WAREHOUSE_STATUS)}")
    sort_by = args.get("sort_by") or "waitingTotal"
    if sort_by not in WAREHOUSE_SORT:
        raise ValueError(f"sort_by must be one of {list(WAREHOUSE_SORT)}")
    by_cargo = {}
    rows = []
    for w in items:
        flows = []
        for f in w.get("flows") or []:
            fin, fout = flow_in_out(f)
            flows.append(compact(dict(f, inPerYear=fin, outPerYear=fout)))
            c = by_cargo.setdefault(f["cargo"], {"waiting": 0, "pastDue": 0, "inPerYear": 0, "outPerYear": 0,
                                                 "warehouses": 0})
            c["inPerYear"] += fin or 0
            c["outPerYear"] += fout or 0
            c["pastDue"] += f.get("pastDue") or 0
            c["warehouses"] += 1
        for name, n in (w.get("holding") or {}).items():
            c = by_cargo.setdefault(name, {"waiting": 0, "pastDue": 0, "inPerYear": 0, "outPerYear": 0,
                                           "warehouses": 0})
            c["waiting"] += n or 0
        flags = warehouse_flags(w)
        if status != "all" and status not in flags:
            continue
        if town and town not in (w.get("town") or "").lower():
            continue
        if name_contains and name_contains not in (w.get("name") or "").lower():
            continue
        moved = {f.get("cargo") for f in w.get("flows") or []} | set(w.get("holding") or {})
        if cargo and not any(cargo in (c or "") for c in moved):
            continue
        row = dict(w)
        row["flags"] = flags
        if flows:
            row["flows"] = flows
            row["inPerYear"] = sum(f.get("inPerYear") or 0 for f in flows)
            row["outPerYear"] = sum(f.get("outPerYear") or 0 for f in flows)
        stuck = warehouse_stuck_cargo(w)
        if stuck:
            row["stuckCargo"] = stuck
        row["capacityTotal"] = sum((g.get("capacity") or 0) for g in w.get("stocks") or []) or None
        if row["capacityTotal"]:
            row["usagePct"] = pct(row.get("waitingTotal") or 0, row["capacityTotal"])
        row["nearestTown"] = nearest_town(w.get("position"), towns)
        rows.append(compact(row))
    if sort_by == "name":
        rows.sort(key=lambda r: (r.get("name") or "").lower())
    else:
        rows.sort(key=lambda r: -(r.get(sort_by) or 0))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {"totalMatching": len(rows), "warehouses": rows[:limit],
           "notes": "Your warehouses. stocks = one row per stock slot group: waiting now vs capacity; a mixed slot "
                    "(accepts = how many cargo types it takes, e.g. All Cargo Types) lists what it actually holds in "
                    "held. holding = amount per cargo over all slots (a cargo's room is its own slot plus any mixed "
                    "slot). pastDue = units past their delivery time, quality = 0-1 (the game's cargo quality). "
                    "flows = per cargo over 12 months: shippedPerYear (left the warehouse), logsPerYear (the "
                    "warehouse's own logbook), inPerYear / outPerYear (from the logbook: itemsUnloaded* in, itemsLoaded* "
                    "out, summed over the numbered channels; shippedPerYear only counts the plain itemsLoaded), "
                    "deliveryTimeSec (the cargo type's delivery time). deliveredPerYear is the game's counter and "
                    "reads 0 in current builds. flags: past_due, overflowing (cargo thrown away), full (a stock slot "
                    "is at capacity), nothing_leaves (holds cargo, nothing left in 12 months), stuck_cargo (holds a "
                    "cargo of which none left in 12 months), unused."}
    if by_cargo:
        out["byCargo"] = by_cargo
    out.update(freshness(warning, age))
    return out


STALE_WHERE = ("warehouses", "stations", "industries", "lines")


def tool_stale_cargo(args):
    """Every place holding cargo past its delivery time: warehouses, stations, industries' output stocks, lines."""
    cargo = (args.get("cargo") or "").upper()
    town = (args.get("town") or "").lower()
    where = args.get("where") or list(STALE_WHERE)
    if isinstance(where, str):
        where = [where]
    bad = [w for w in where if w not in STALE_WHERE]
    if bad:
        raise ValueError(f"where must be among {list(STALE_WHERE)}")
    min_past_due = max(1, int(args.get("min_past_due") or 1))
    rows, by_cargo, problems, delivery = [], {}, {}, {}
    on_lines = 0
    oldest_age = 0

    def wanted(cargo_names, towns_of):
        if cargo and not any(cargo in (c or "") for c in cargo_names):
            return False
        if town and not any(town in (t or "").lower() for t in towns_of):
            return False
        return True

    def add(kind, item, cargo_name, q, extra=None):
        n = (q or {}).get("pastDue") or 0
        if n < min_past_due or cargo_name == "PASSENGERS":
            return
        if not wanted([cargo_name], [item.get("town")]):
            return
        row = {"where": kind, "id": item.get("id"), "name": item.get("name"), "town": item.get("town"),
               "cargo": cargo_name, "pastDue": n, "count": q.get("count"), "quality": q.get("quality"),
               "veryBad": q.get("veryBad")}
        if extra:
            row.update(extra)
        rows.append(compact(row))
        c = by_cargo.setdefault(cargo_name, {"pastDue": 0, "places": 0})
        c["pastDue"] += n
        c["places"] += 1

    def load(kind):
        nonlocal oldest_age
        try:
            data, warning, age = load_kind(kind) if kind != "lines" else load_snapshot()
            oldest_age = max(oldest_age, age or 0)
            if warning:
                problems[kind] = warning
            return data
        except Exception as e:
            problems[kind] = str(e)
            return None

    if "warehouses" in where:
        data = load("warehouses") or {}
        ticks_per_day = (data.get("meta") or {}).get("yearTicks")
        ticks_per_day = ticks_per_day / 365.25 if ticks_per_day else None
        for w in data.get("warehouses") or []:
            for f in w.get("flows") or []:
                if f.get("deliveryTimeSec") and f["cargo"] not in delivery:
                    d = {"seconds": f["deliveryTimeSec"]}
                    if ticks_per_day:
                        d["gameDays"] = round(f["deliveryTimeSec"] * 1000 / ticks_per_day, 1)
                    delivery[f["cargo"]] = d
                add("warehouse", w, f.get("cargo"),
                    {"pastDue": f.get("pastDue"), "count": (w.get("holding") or {}).get(f.get("cargo")),
                     "quality": f.get("quality"), "veryBad": f.get("veryBad")},
                    {"outPerYear": flow_in_out(f)[1]})
    if "stations" in where:
        data = load("stations") or {}
        for st in data.get("stations") or []:
            for name, q in (st.get("pastDue") or {}).items():
                add("station", st, name, q, {"lines": [l.get("name") for l in st.get("lines") or []] or None})
    if "industries" in where:
        data = load("industries") or {}
        for i in data.get("industries") or []:
            for o in i.get("outputs") or []:
                add("industry", i, o.get("cargo"),
                    {"pastDue": o.get("pastDue"), "count": o.get("waiting"), "quality": o.get("quality")},
                    {"shippedPerYear": o.get("shippedPerYear"), "producedPerYear": o.get("producedPerYear")})
    if "lines" in where:
        snap = load("lines") or {}
        for l in snap.get("lines") or []:
            q = l.get("cargoQuality") or {}
            n = q.get("pastDue") or 0
            if n < min_past_due or not wanted(l.get("cargos") or [], l.get("towns") or []):
                continue
            on_lines += n
            rows.append(compact({"where": "line", "id": l.get("id"), "name": l.get("name"),
                                 "towns": l.get("towns") or None, "cargo": ", ".join(l.get("cargos") or []) or None,
                                 "pastDue": n, "count": q.get("count"), "quality": q.get("quality"),
                                 "veryBad": q.get("veryBad")}))
    rows.sort(key=lambda r: (-(r.get("pastDue") or 0), r.get("quality") if r.get("quality") is not None else 1))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {"totalPastDue": sum(r["pastDue"] for r in rows), "places": len(rows), "rows": rows[:limit],
           "byCargo": dict(sorted(by_cargo.items(), key=lambda kv: -kv[1]["pastDue"])),
           "notes": "Cargo past its delivery time (the game's cargo quality: countBad = units no longer in time), "
                    "wherever it is now. where: warehouse (stored), station (waiting for a line), industry (in the "
                    "output stock, waiting for pickup; current game builds report no stored industry output), line (on the line's vehicles or stops; not split per cargo). "
                    "count = units there now, pastDue = of those, past their delivery time, quality = average 0-1. "
                    "byCargo excludes lines. deliveryTimes: each cargo type's delivery time as the game reports it; "
                    "gameDays assumes the same clock as game time. Passengers are left out."}
    if on_lines:
        out["pastDueOnLines"] = on_lines
    if delivery:
        out["deliveryTimes"] = delivery
    if problems:
        out["warning"] = "; ".join(f"{k}: {v}" for k, v in problems.items())
    out["dataAgeSec"] = round(oldest_age)
    return out


JOURNAL_CARRIERS = ["ROAD", "RAIL", "TRAM", "OTHER", "AIR", "WATER"]  # the finance journal's own order


def summarize_finance_table(t):
    """Per carrier and year: income, costs and net from the game's finance table (row codes are undocumented)."""
    years = t.get("header") or []
    out = {}
    for code, rows in (t.get("transportByCarrier") or {}).items():
        try:
            name = JOURNAL_CARRIERS[int(code)]
        except (ValueError, IndexError):
            name = str(code)
        income = [0] * len(years)
        costs = [0] * len(years)
        for values in (rows or {}).values():
            for i, v in enumerate((values or [])[:len(years)]):
                if v >= 0:
                    income[i] += v
                else:
                    costs[i] += v
        if any(income) or any(costs):
            out[name] = {"income": income, "costs": costs, "net": [a + b for a, b in zip(income, costs)]}
    investment = [0] * len(years)
    for values in (t.get("investment") or {}).values():
        for i, v in enumerate((values or [])[:len(years)]):
            investment[i] += v
    return {"years": years, "byCarrier": out, "construction": investment, "total": t.get("total"),
            "balance": t.get("balance"), "loan": t.get("loan"), "interest": t.get("interest")}


def tool_finances(args):
    data, warning, age = load_kind("finances")
    fin = (data.get("finances") or [{}])[0]
    out = dict(fin)
    if isinstance(fin.get("financeTable"), dict):
        out["financeTable"] = summarize_finance_table(fin["financeTable"])
    try:
        snap, _, _ = load_snapshot()
        per = {}
        for l in snap.get("lines", []):
            for c in l.get("carriers") or ["OTHER"]:
                r = per.setdefault(c, {"lines": 0, "vehicles": 0, "profit12m": 0, "runningCostsPerYear": 0})
                r["lines"] += 1
                r["vehicles"] += l.get("vehicleCount") or 0
                r["profit12m"] += l.get("profit12m") or 0
                r["runningCostsPerYear"] += l.get("runningCostsPerYear") or 0
        out["linesByCarrier"] = {k: {kk: round(vv) for kk, vv in v.items()} for k, v in per.items()}
    except Exception as e:
        out["linesByCarrier"] = f"unavailable: {e}"
    out["notes"] = ("balance and loan are now; earningsThisYear is since 1 January. linesByCarrier sums your lines' "
                    "12-month profit (income minus running costs) per carrier. financeTable is the game's finance "
                    "window per year (the current year so far): income, costs and net per carrier, construction "
                    "spending, total result and balance.")
    out.update(freshness(warning, age))
    return out


def headway_check(vehicles, day_ticks):
    """Spacing of a line's vehicles from their latest departures at the busiest stop: gaps in game days."""
    best = []
    n_stops = max((len(v.get("stopDepartures") or []) for v in vehicles), default=0)
    for k in range(n_stops):
        times = sorted(v["stopDepartures"][k] for v in vehicles
                       if len(v.get("stopDepartures") or []) > k and v["stopDepartures"][k] > 0)
        if len(times) > len(best):
            best = times
    if len(best) < 3:
        return None
    gaps = [b - a for a, b in zip(best, best[1:])]
    mean = sum(gaps) / len(gaps)
    if mean <= 0:
        return None
    ratio = min(gaps) / mean
    out = {"vehiclesCompared": len(best), "evenness": round(ratio, 2), "bunched": ratio < 0.25}
    if day_ticks:
        out["gapsDays"] = [round(g / day_ticks, 1) for g in gaps]
    return out


def tool_live_vehicles(args):
    data, warning, age = load_kind("vehicles")
    meta = data.get("meta") or {}
    day = (meta.get("yearTicks") or 0) / 365 or None
    by_line = {}
    for v in data.get("vehicles") or []:
        by_line.setdefault(v.get("line"), []).append(v)
    ref = str(args.get("line") or "").strip().lower()
    if ref:
        match = [vs for lid, vs in by_line.items()
                 if str(lid) == ref or (vs[0].get("lineName") or "").lower() == ref]
        if not match:
            match = [vs for vs in by_line.values() if ref in (vs[0].get("lineName") or "").lower()]
        if not match:
            raise LookupError(f"No line matches {args.get('line')!r}")
        vs = match[0]
        out = {"line": vs[0].get("lineName"), "lineId": vs[0].get("line"), "spacing": headway_check(vs, day),
               "vehicles": [compact({k: v.get(k) for k in ("id", "name", "position", "speed", "state", "stopIndex",
                                                            "doorsOpen", "load", "noPath", "userStopped",
                                                            "timeUntilDeparture", "daysAtTerminal")}) for v in vs]}
        out.update(freshness(warning, age))
        return out
    lines, stuck = [], []
    for lid, vs in by_line.items():
        h = headway_check(vs, day)
        if h:
            lines.append(dict(h, line=vs[0].get("lineName"), lineId=lid, vehicles=len(vs)))
        for v in vs:
            why = []
            if v.get("noPath"):
                why.append("no path")
            if v.get("userStopped"):
                why.append("stopped by player")
            if (v.get("daysAtTerminal") or 0) >= 5:
                why.append(f"{v['daysAtTerminal']} days at a terminal")
            if why:
                stuck.append({"vehicle": v.get("name"), "id": v.get("id"), "line": v.get("lineName"),
                              "position": v.get("position"), "reasons": why})
    lines.sort(key=lambda r: r["evenness"])
    limit = max(1, min(int(args.get("limit") or 25), 1000))
    out = {
        "linesChecked": len(lines),
        "bunchedLines": sum(1 for r in lines if r["bunched"]),
        "lines": lines[:limit],
        "stuckVehicles": stuck[:limit],
        "notes": "evenness = smallest gap / average gap between the vehicles' latest departures at the busiest stop "
                 "(1 = perfectly even, below 0.25 = bunched). Needs 3+ vehicles. Pass line for one line's vehicles "
                 "with positions (world metres), speed, state and load.",
    }
    out.update(freshness(warning, age))
    return out


def tool_fleet_issues(args):
    snap, warning, age = load_snapshot()
    min_usage = float(args.get("low_usage_pct", 40))
    max_usage = float(args.get("high_usage_pct", 95))
    old_ratio = float(args.get("old_ratio", 1.0))
    lines_out, vehicles_out = [], []
    for l in snap.get("lines", []):
        reasons = []
        if l.get("problem") and l.get("problem") != "NOTHING":
            reasons.append(f"problem: {l['problem']}")
        if (l.get("vehicleCount") or 0) == 0:
            reasons.append("no vehicles")
        if l.get("profit12m") is not None and l["profit12m"] < 0:
            reasons.append(f"losing money: {l['profit12m']} over 12 months")
        if l.get("usagePct") is not None and l["usagePct"] < min_usage and (l.get("vehicleCount") or 0) > 1:
            reasons.append(f"low usage: {l['usagePct']}% with {l['vehicleCount']} vehicles")
        if l.get("usagePct") is not None and l["usagePct"] >= max_usage:
            reasons.append(f"full: {l['usagePct']}% usage, may need more capacity")
        if reasons:
            row = compact(line_row(l))
            row["reasons"] = reasons
            lines_out.append(row)
        for v in l.get("vehicles") or []:
            vr = []
            if v.get("ageOfLifespan") is not None and v["ageOfLifespan"] >= old_ratio:
                vr.append(f"at {int(v['ageOfLifespan'] * 100)}% of lifespan")
            if v.get("noPath"):
                vr.append("no path")
            if v.get("userStopped"):
                vr.append("stopped by player")
            if v.get("worstMaintenance") is not None and v["worstMaintenance"] < 0.3:
                vr.append(f"poor maintenance ({v['worstMaintenance']:.2f})")
            if vr:
                row = compact(vehicle_row(v))
                row["line"] = l.get("name")
                row["lineId"] = l.get("id")
                row["reasons"] = vr
                vehicles_out.append(row)
    limit = max(1, min(int(args.get("limit") or 100), 1000))
    out = {
        "lines": lines_out[:limit],
        "linesFlagged": len(lines_out),
        "vehicles": vehicles_out[:limit],
        "vehiclesFlagged": len(vehicles_out),
    }
    out.update(freshness(warning, age))
    return out


TOOLS = [
    {
        "name": "tf3_status",
        "description": "Transport Fever 3: game date, money, line and vehicle counts, and how fresh the data is. "
                       "Call first to check the game is running with the MCP Bridge mod.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_status,
    },
    {
        "name": "tf3_list_lines",
        "description": "Transport Fever 3: list the player's lines with vehicles, capacity usage, 12-month profit, "
                       "profit per vehicle and problems. Sortable and filterable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sort_by": {"type": "string", "enum": sorted(SORT_KEYS), "description": "default profit12m"},
                "ascending": {"type": "boolean", "description": "default false (largest first), except for name"},
                "carrier": {"type": "string", "enum": ["ROAD", "RAIL", "TRAM", "AIR", "WATER"]},
                "name_contains": {"type": "string"},
                "town": {"type": "string", "description": "only lines with a stop in a town whose name contains this"},
                "limit": {"type": "integer", "description": "default 50"},
            },
        },
        "handler": tool_list_lines,
    },
    {
        "name": "tf3_list_towns",
        "description": "Transport Fever 3: towns and cities with name, population, size per district, public "
                       "transport reach and how many of your lines serve each. Filter by name to see those lines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "sort_by": {"type": "string", "enum": sorted(TOWN_SORT_KEYS), "description": "default population"},
                "ascending": {"type": "boolean", "description": "default false (largest first)"},
                "include_lines": {"type": "boolean", "description": "list serving lines (default: only when filtering by name)"},
                "include_cargo": {"type": "boolean", "description": "goods each district needs and the supply vs limit (default: only when filtering by name)"},
                "limit": {"type": "integer", "description": "default 100"},
            },
        },
        "handler": tool_list_towns,
    },
    {
        "name": "tf3_route_ideas",
        "description": "Transport Fever 3: plan new lines. Town pairs ranked by a simple demand estimate (bigger and "
                       "closer towns first), with straight-line distance, populations and whether one of your lines "
                       "already links them. By default only pairs not yet linked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "town": {"type": "string", "description": "only pairs involving towns whose name contains this"},
                "min_population": {"type": "number", "description": "ignore smaller towns"},
                "min_distance_km": {"type": "number"},
                "max_distance_km": {"type": "number", "description": "e.g. 15 for buses, 150 for regional rail"},
                "include_linked": {"type": "boolean", "description": "also list pairs your lines already link"},
                "limit": {"type": "integer", "description": "default 25"},
            },
        },
        "handler": tool_route_ideas,
    },
    {
        "name": "tf3_get_line",
        "description": "Transport Fever 3: one line in detail: stops, capacity usage per cargo, profit, and every "
                       "vehicle with its model, age versus lifespan, load, running cost, value and profit.",
        "inputSchema": {
            "type": "object",
            "properties": {"line": {"type": "string", "description": "line id or (part of) its name"}},
            "required": ["line"],
        },
        "handler": tool_get_line,
    },
    {
        "name": "tf3_list_vehicle_models",
        "description": "Transport Fever 3: vehicle models, by default only those buyable in the current game year, "
                       "with price, capacity, top speed, power and lifespan. Use to pick replacements.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "carrier": {"type": "string", "enum": ["ROAD", "RAIL", "TRAM", "AIR", "WATER"]},
                "transport_mode": {"type": "string", "description": "e.g. BUS, TRUCK, TRAIN, ELECTRIC_TRAIN, TRAM"},
                "name_contains": {"type": "string"},
                "min_capacity": {"type": "integer"},
                "available_only": {"type": "boolean", "description": "default true"},
                "include_hidden": {"type": "boolean", "description": "default false"},
                "sort_by": {"type": "string", "enum": ["capacity", "price", "topSpeedKmh", "powerKw", "lifespanDays"]},
                "limit": {"type": "integer", "description": "default 60"},
            },
        },
        "handler": tool_list_vehicle_models,
    },
    {
        "name": "tf3_list_industries",
        "description": "Transport Fever 3: industries with location, closest town, level, inputs (delivered, "
                       "consumed, maximum per year) and outputs (produced, maximum, shipped per year), flags for "
                       "missing inputs, unshipped output and closing, plus a per-cargo supply and demand summary. "
                       "Use it to plan cargo chains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cargo": {"type": "string", "description": "only industries using or making this cargo, e.g. COAL"},
                "town": {"type": "string", "description": "closest town name contains"},
                "status": {"type": "string", "enum": list(INDUSTRY_STATUS), "description": "default all"},
                "sort_by": {"type": "string", "enum": ["producedPerYear", "shippedPerYear", "level", "name"]},
                "include_cargo_summary": {"type": "boolean", "description": "default true"},
                "limit": {"type": "integer", "description": "default 50"},
            },
        },
        "handler": tool_list_industries,
    },
    {
        "name": "tf3_list_stations",
        "description": "Transport Fever 3: your stations with passengers and cargo waiting now, cargo waiting very "
                       "long, terminals, how full they are, their town and the lines stopping there.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["passenger", "cargo"]},
                "town": {"type": "string"},
                "name_contains": {"type": "string"},
                "sort_by": {"type": "string", "enum": ["waitingTotal", "usagePct", "terminals", "name"]},
                "limit": {"type": "integer", "description": "default 50"},
            },
        },
        "handler": tool_list_stations,
    },
    {
        "name": "tf3_list_depots",
        "description": "Transport Fever 3: your depots with carrier, location, vehicles inside and nearest town.",
        "inputSchema": {"type": "object", "properties": {
            "carrier": {"type": "string", "enum": ["ROAD", "RAIL", "TRAM", "AIR", "WATER"]}}},
        "handler": tool_list_depots,
    },
    {
        "name": "tf3_list_warehouses",
        "description": "Transport Fever 3: your warehouses with what is stored in them now against capacity, and "
                       "how much of each cargo arrived versus left again over the last 12 months. Use it to find "
                       "cargo sitting in a warehouse that is not being carried onward.",
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string", "enum": list(WAREHOUSE_STATUS), "description": "default all"},
            "cargo": {"type": "string", "description": "only warehouses holding or moving this cargo, e.g. PLANKS"},
            "town": {"type": "string", "description": "closest town name contains"},
            "name_contains": {"type": "string"},
            "sort_by": {"type": "string", "enum": list(WAREHOUSE_SORT), "description": "default waitingTotal"},
            "limit": {"type": "integer", "description": "default 50"}}},
        "handler": tool_list_warehouses,
    },
    {
        "name": "tf3_stale_cargo",
        "description": "Transport Fever 3: cargo past its delivery time, wherever it is: stored in warehouses, "
                       "waiting at stations, sitting in industries' output stocks, or on lines. Ranked by how many "
                       "units are past due, with totals per cargo and each cargo's delivery time.",
        "inputSchema": {"type": "object", "properties": {
            "cargo": {"type": "string", "description": "only this cargo, e.g. IRON_ORE"},
            "town": {"type": "string", "description": "only places in towns whose name contains this"},
            "where": {"type": "array", "items": {"type": "string", "enum": list(STALE_WHERE)},
                      "description": "default all"},
            "min_past_due": {"type": "integer", "description": "default 1"},
            "limit": {"type": "integer", "description": "default 50"}}},
        "handler": tool_stale_cargo,
    },
    {
        "name": "tf3_finances",
        "description": "Transport Fever 3: company finances: balance, loan, earnings this year, company value and "
                       "statistics, passengers and cargo transported, the finance table per period and carrier, and "
                       "12-month line profit summed per carrier.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_finances,
    },
    {
        "name": "tf3_live_vehicles",
        "description": "Transport Fever 3: live vehicle check. Without a line: which lines have bunched vehicles "
                       "(uneven spacing) and which vehicles are stuck (no path, stopped, waiting long). With a line: "
                       "every vehicle's position, speed, state, next stop and load.",
        "inputSchema": {"type": "object", "properties": {
            "line": {"type": "string", "description": "line id or name"},
            "limit": {"type": "integer", "description": "default 25"}}},
        "handler": tool_live_vehicles,
    },
    {
        "name": "tf3_fleet_issues",
        "description": "Transport Fever 3: lines losing money, with low or full usage, problems or no vehicles, and vehicles "
                       "past their lifespan, without a path, stopped or poorly maintained.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "low_usage_pct": {"type": "number", "description": "flag lines below this usage (default 40)"},
                "high_usage_pct": {"type": "number", "description": "flag lines at or above this usage (default 95)"},
                "old_ratio": {"type": "number", "description": "flag vehicles at this share of lifespan (default 1.0)"},
                "limit": {"type": "integer", "description": "default 100 per list"},
            },
        },
        "handler": tool_fleet_issues,
    },
]
HANDLERS = {t["name"]: t["handler"] for t in TOOLS}

# ----------------------------------------------------------------------------------------------------------------
# MCP over stdio (newline-delimited JSON-RPC 2.0)


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Read-only data from a running Transport Fever 3 game (MCP Bridge mod). Start with "
                            "tf3_status; money values are in game currency; times are game time.",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            raise LookupError(f"unknown tool {name}")
        try:
            result = handler(params.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}]}
        except Exception as e:  # tool errors go back to the model, not as protocol errors
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    if rid is None:
        return None  # notification (e.g. notifications/initialized)
    raise LookupError(f"method not found: {method}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        rid = req.get("id")
        try:
            result = handle(req)
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "result": result})
        except LookupError as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": str(e)}})
        except Exception as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})


# ----------------------------------------------------------------------------------------------------------------
# --setup: registers this server with the Claude desktop app and Claude Code


def desktop_configs():
    """Claude desktop config files: the regular install and the Microsoft Store (MSIX) one, where present."""
    found = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        found.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
                                        "claude_desktop_config.json"))
    if sys.platform == "darwin":
        found.append(os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json"))
    return [p for p in found if os.path.isdir(os.path.dirname(p))]


def setup():
    import shutil
    import subprocess
    script = os.path.abspath(__file__)
    entry = {"command": sys.executable, "args": ["-I", script]}
    print("Server:", script)
    print("Python:", sys.executable)
    done = False
    for path in desktop_configs():
        config = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    config = json.load(f)
            except ValueError:
                print("Claude desktop: could not read", path, "(not valid JSON); add the entry below by hand")
                continue
            backup = path + ".bak-tf3"
            if not os.path.exists(backup):  # keep the config from before the first setup
                shutil.copyfile(path, backup)
        config.setdefault("mcpServers", {})["tf3"] = entry
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print("Claude desktop: added 'tf3' to", path, "(backup: .bak-tf3). Quit Claude from the tray and reopen it.")
        done = True
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", "-s", "user", "tf3"], capture_output=True)
        r = subprocess.run([claude, "mcp", "add", "-s", "user", "tf3", "--", sys.executable, "-I", script],
                           capture_output=True, text=True)
        print("Claude Code:", "added 'tf3' for all folders" if r.returncode == 0 else "failed: " + r.stderr.strip())
        done = done or r.returncode == 0
    if not done:
        print("No Claude app found. Add this to your MCP client's config under \"mcpServers\":")
        print(json.dumps({"tf3": entry}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        setup()
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--print":  # quick look without an MCP client
        tool = sys.argv[2] if len(sys.argv) > 2 else "tf3_status"
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(HANDLERS[tool](args), ensure_ascii=False, indent=1))
    else:
        main()
