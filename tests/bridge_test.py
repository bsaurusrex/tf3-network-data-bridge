"""End-to-end test of the MCP Bridge: the real mod Lua runs with a fake `api` and a fake userdata store (game-like
serializer, no overwrite, a refused folder) on a background "game loop" thread, while the real server code sends
requests and reads the answers."""
import importlib.util, json, os, sys, tempfile, threading, time
from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repository root
MOD = os.path.join(ROOT, "mod", "mcp_bridge_1", "content")
out = tempfile.mkdtemp(prefix="bridge_")

lua = LuaRuntime(unpack_returned_tuples=True)
g = lua.globals()
g.MOD_ROOT = MOD.replace("\\", "/")
g.OUT_DIR = out.replace("\\", "/")

lua.execute(r'''
local files = {}
function ug_require(name)
  local path = name:gsub("^[^:]+::/", MOD_ROOT .. "/")
  if not files[path] then files[path] = dofile(path) end
  return files[path]
end
function getOwningModId() return "mcp_bridge_1" end
LOG = {}
function debugPrint(s) LOG[#LOG+1] = s; print(s) end
-- userdata store like the game's: <OUT_DIR>/<dir>/<name>.lua, "function data() return {...} end", %q strings,
-- saveUserdata never overwrites; the folder "recordings" silently refuses saves (as TF3 does for some folders)
local function ser(v, ind)
  local t = type(v)
  if t == "string" then return string.format("%q", v) end
  if t == "number" or t == "boolean" then return tostring(v) end
  if t == "table" then
    local out = { "{ \n" }
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = k end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    for _, k in ipairs(keys) do
      local key = type(k) == "string" and k:match("^[%a_][%w_]*$") and k or ("[" .. ser(k) .. "]")
      out[#out + 1] = ind .. "\t" .. key .. " = " .. ser(v[k], ind .. "\t") .. ",\n"
    end
    out[#out + 1] = ind .. "}"
    return table.concat(out)
  end
  return "nil"
end
local function upath(dir, name) return OUT_DIR .. "/" .. dir .. "/" .. name .. ".lua" end
local function exists(path) local f = io.open(path, "rb"); if f then f:close(); return true end; return false end
app = {
  getAllUserdata = function(dir) return {} end,
  saveUserdata = function(dir, name, tbl)
    if dir == "recordings" then return end
    local path = upath(dir, name)
    if exists(path) then return end
    local f = assert(io.open(path, "wb"))
    f:write("function data()\nreturn " .. ser(tbl, "\t") .. "\nend\n")
    f:close()
  end,
  loadUserdata = function(dir, name) local env = {}; assert(loadfile(upath(dir, name), "t", env))(); return env.data() end,
  removeUserdata = function(dir, name) os.remove(upath(dir, name)) end,
}

-- native-vector imitation (1-based :size()/:at())
local function vec(t) return setmetatable({}, { __index = { size = function() return #t end, at = function(_, i) return t[i] end } }) end

local CT = { NAME=1, LINE=2, TRANSPORT_VEHICLE=3, GAME_TIME=4, GAME_SPEED=5, LOG_BOOK=6, STATION_GROUP=7, TOWN=8,
  BOUNDING_VOLUME=9, TOWN_BUILDING=10, INDUSTRY=11, STOCK_LIST=12, CONSTRUCTION=13, STATION=14, VEHICLE_DEPOT=15,
  ACCOUNT=16, PLAYER_OWNED=17, WAREHOUSE=18 }
local comps = {
  [1] = { [CT.GAME_TIME] = { gameTime = 5000000 }, [CT.GAME_SPEED] = { speedup = 1, millisPerDay = 2000 } },
  [100] = { [CT.NAME] = { name = "Line A" }, [CT.LINE] = { stops = vec({ { stationGroup = 500 }, { stationGroup = 501 } }),
            vehicleInfo = { transportModes = { [3] = true } } },
            [CT.LOG_BOOK] = { name2log = { passengers = {}, income = {} } } },
  [101] = { [CT.NAME] = { name = "Line B" }, [CT.LINE] = { stops = { { stationGroup = 500 } }, vehicleInfo = { transportModes = {} } } },
  [500] = { [CT.NAME] = { name = "Central" }, [CT.STATION_GROUP] = { stations = vec({ 600 }) } },
  [501] = { [CT.NAME] = { name = 'Harbour "Pier" \\ 1\nb' }, [CT.STATION_GROUP] = { stations = { 601 } } },
  [900] = { [CT.NAME] = { name = "Springfield" }, [CT.TOWN] = { developmentActive = true, cargoNeeds = { vec({ { 4, 1.0 } }), {}, {} } } },
  [901] = { [CT.NAME] = { name = "Zürich" }, [CT.TOWN] = { developmentActive = false } },
  -- town buildings: Springfield's centre is (1050, 2000); Zürich's box sits on the building's construction (21050, 2000)
  [1000] = { [CT.BOUNDING_VOLUME] = { bbox = { min = { x = 990, y = 1990 }, max = { x = 1010, y = 2010 } } } },
  [1001] = { [CT.BOUNDING_VOLUME] = { bbox = { min = { x = 1090, y = 1990 }, max = { x = 1110, y = 2010 } } } },
  [1002] = { [CT.TOWN_BUILDING] = { construction = 1003 } },
  [1003] = { [CT.BOUNDING_VOLUME] = { bbox = { min = { x = 21040, y = 1995 }, max = { x = 21060, y = 2005 } } } },
  -- industries: a coal mine (output C5) and a steel mill (inputs C5, C6; output C7)
  [700] = { [CT.NAME] = { name = "Coal Mine" }, [CT.INDUSTRY] = { level = 1, maxLevel = 4, construction = 710, stockList = 720 } },
  [710] = { [CT.CONSTRUCTION] = { fileName = "industry/coal_mine.con" },
            [CT.BOUNDING_VOLUME] = { bbox = { min = { x = 4990, y = 2990 }, max = { x = 5010, y = 3010 } } } },
  [701] = { [CT.NAME] = { name = "Steel Mill" }, [CT.INDUSTRY] = { level = 2, maxLevel = 4, construction = 711, stockList = 721 } },
  [711] = { [CT.CONSTRUCTION] = { fileName = "industry/steel_mill.con" },
            [CT.BOUNDING_VOLUME] = { bbox = { min = { x = 15990, y = 2990 }, max = { x = 16010, y = 3010 } } } },
  -- stations and a depot
  [600] = { [CT.STATION] = { terminals = vec({ { passengersLoad = true }, { passengersUnload = true } }), catchmentAreas = { { position = { x = 1000, y = 2050 } } } } },
  [601] = { [CT.STATION] = { terminals = { { passengersLoad = true, cargoLoad = true } }, catchmentAreas = { { position = { x = 21000, y = 2050 } } } } },
  [800] = { [CT.VEHICLE_DEPOT] = { carrier = 0, transportModes = { [3] = true }, catchmentArea = { position = { x = 1100, y = 2100 } } } },
  [42] = { [CT.ACCOUNT] = { balance = 9999999, loan = 250000 } },
  -- a warehouse holding C5: a lot is delivered, little leaves again, and some had to be thrown away
  [750] = { [CT.NAME] = { name = "Planks Warehouse" }, [CT.WAREHOUSE] = { construction = 760, stockList = 770 } },
  [760] = { [CT.CONSTRUCTION] = { fileName = "warehouse/warehouse_small.con" },
            [CT.BOUNDING_VOLUME] = { bbox = { min = { x = 1190, y = 2090 }, max = { x = 1210, y = 2110 } } } },
  [770] = { [CT.STOCK_LIST] = { stocks = vec({ { capacity = 100 }, { capacity = 100 }, { capacity = 50 }, { capacity = 100 } }),
            thrownAwayCargo = 0 }, [CT.LOG_BOOK] = { name2log = { itemsUnloaded = {}, itemsLoaded = {}, itemsLoaded1 = {} } } },
  -- a warehouse whose construction (not the warehouse itself) is owned by someone else: not ours
  [751] = { [CT.NAME] = { name = "Rival Warehouse" }, [CT.WAREHOUSE] = { construction = 761, stockList = 771 } },
  [761] = { [CT.CONSTRUCTION] = { fileName = "warehouse/warehouse_small.con" }, [CT.PLAYER_OWNED] = { player = 99 } },
}
local function mkVeh(id, name, model, bought)
  comps[id] = { [CT.NAME] = { name = name }, [CT.TRANSPORT_VEHICLE] = {
    carrier = 0, state = 1, userStopped = false, sellOnArrival = false, noPath = false, daysInDepot = 0,
    transportVehicleConfig = { vehicles = vec({ { part = { modelId = model }, purchaseTime = bought, maintenanceState = 0.8 } }) },
    config = { capacities = (function() local t = {} for i = 1, 37 do t[i] = 0 end t[38] = 40 return vec(t) end)() } } } -- passengers last (id 37)
end
mkVeh(200, "Bus 1", 7, 1000000); BIG = string.rep("x", 450000); mkVeh(201, "Bus 2", 7, 4000000); mkVeh(202, BIG, 8, 100)
comps[200][CT.TRANSPORT_VEHICLE].stopIndex = 1; comps[200][CT.TRANSPORT_VEHICLE].lineStopDepartures = vec({ 1000, 5000 })
comps[201][CT.TRANSPORT_VEHICLE].stopIndex = 0; comps[201][CT.TRANSPORT_VEHICLE].lineStopDepartures = vec({ 1100, 9000 })
local STOCK = { -- stock list -> { inputs, outputs, per-cargo numbers }
  [720] = { {}, { 5 }, { produced = { [5] = 400 }, maxProd = { [5] = 800 }, shipped = { [5] = 300 } } },
  [721] = { { 5, 6 }, { 7 }, { delivered = { [5] = 100, [6] = 0 }, consumed = { [5] = 100 }, maxCons = { [5] = 400, [6] = 400 },
            produced = { [7] = 50 }, maxProd = { [7] = 400 }, shipped = { [7] = 0 } } },
  [770] = { {}, { 5 }, { delivered = { [5] = 500 }, shipped = { [5] = 100 } } },
}
local function stockVal(key) return function(sl, c) return (STOCK[sl][3][key] or {})[c] or 0 end end

local models = {
  [7] = { metadata = { transportVehicle = { carrier = 0, compartments = vec({ { loadConfigs = vec({ { cargoEntry = { capacity = 40 } } }) } }),
          transportModes = vec({ 3 }), comfortFactor = 1, priceFactor = 1, loadSpeed = 2 },
          description = { name = "City Bus" }, availability = { yearFrom = 1900, yearTo = 1960 },
          maintenance = { lifespan = 7300, runningCosts = -1 }, cost = { price = -1 },
          landVehicle = { topSpeed = 15, weightEmpty = 5000, engines = vec({ { power = 90 } }) } } },
  [8] = { metadata = { transportVehicle = { carrier = 0, compartments = {}, transportModes = {} },
          description = { name = "Old Bus" }, availability = { yearFrom = 1850, yearTo = 1910 } } },
  [9] = { metadata = { } }, -- not a vehicle
}
local modelNames = { [7] = "vehicle/bus/city.mdl", [8] = "vehicle/bus/old.mdl", [9] = "asset/tree.mdl" }

api = {
  type = {
    ComponentType = CT,
    ChartConfig = { new = function() return { interval = 0, count = 0 } end },
    enum = { Carrier = { ROAD = 0, RAIL = 1 }, TransportVehicleState = { IN_DEPOT = 0, EN_ROUTE = 1 }, TransportMode = { BUS = 3 },
             StockListType = { InputStock = 0, OutputStock = 1, StorageStock = 2 } },
    LineProblem = { NOTHING = 0, NO_PATH = 4 },
    Date = { now = function() return { year = 2026, month = 10, day = 11 } end }, -- real clock, like the game
    VehiclePart = { new = function() return {} end },
    TransportVehiclePart = { new = function() return {} end },
  },
  util = { getApplicationTime = function() return CLOCK end, getDefaultDayDuration = function() return 2000 end,
           getDefaultYearDuration = function() return 730000 end, getYearDuration = function(m) return m * 365 end },
  res = {
    modelRep = {
      get = function(id) return models[id] end, getName = function(id) return modelNames[id] end,
      find = function(n) for id, nm in pairs(modelNames) do if nm == n then return id end end return -1 end,
      isVisible = function() return true end,
      forEachModelWithMetadata = function(md, cb) for id, m in pairs(models) do if m.metadata[md] then cb(modelNames[id]) end end end,
    },
    cargoTypeRep = { getPassengerCargoTypeId = function() return 37 end,
      getName = function(i) if i >= 37 then error("Index " .. i .. " out of bounds") end return "::/cargos/c" .. i .. "/c" .. i .. ".cargo" end },
  },
  engine = {
    config = { getModParams = function() return { mcp_bridge_1 = { interval = 1, budget = 1, towns = 1, logEach = 2 } } end },
    getComponent = function(e, c) return comps[e] and comps[e][c] end,
    getEntitiesWithComponent = function(c, filters)
      if c == CT.INDUSTRY then return { 701, 700 } end
      if c == CT.STATION then return { 600, 601 } end
      if c == CT.VEHICLE_DEPOT then return { 800 } end
      if c == CT.WAREHOUSE then return { 751, 750 } end
      return {}
    end,
    util = {
      getWorld = function() return 1 end, getEntityName = function(e) return e == 800 and "Bus Depot" or nil end, getCalendarDate = function(gt) return { year = 1925, month = 3, day = 1 } end, getPlayer = function() return 42 end,
      finance = { calculateBalance = function(list, from, to, only) return list[1] == 100 and 12345 or -500 end,
                  getPlayersBalance = function() return 9999999 end, calculateEarnings = function() return 777 end,
                  computeFinanceTable = function(p, cfg) return { header = { "1924", "1925" }, total = vec({ 10, 20 }),
                    foreach_carrier = function(self, fn) fn(0, { [526] = vec({ 9, 7 }), [406] = vec({ -4, -1 }) }) end } end },
      headquarters = { getCompaniesValue = function() return { totalScore = 12, numberOfLines = 2, balance = 9999999 } end,
                       getTransportedData = function() return { passengersTransported = 5000, cargoTransported = 300 } end },
      stock = { getInputsOutputsFromRules = function(sl) return { STOCK[sl][1], STOCK[sl][2] } end,
                getCargoTypeDeliveredPerYear = stockVal("delivered"), getCargoConsumedPerYear = stockVal("consumed"),
                getCargoMaxConsumptionPerYear = stockVal("maxCons"), getCargoProducedPerYear = stockVal("produced"),
                getCargoMaxProductionPerYear = stockVal("maxProd"), getCargoTypeShippedPerYear = stockVal("shipped"),
                getProductionRating = function() return 0.5 end,
                getCargoDeliveredPerYear = function(sl) return sl == 770 and 500 or 0 end,
                getCargoShippedPerYear = function(sl) return sl == 770 and 100 or 0 end,
                getStockListsWithThrownAwayCargo = function() return { [770] = 25 } end,
                getAdjustedDeliveryTime = function(c) return 1440 end,
                getCargoLogValueDiffPerYear = function(sl, name, c)
                  if sl ~= 770 then return 0 end
                  local logs = { [5] = { itemsUnloaded = 450, itemsLoaded = 100, itemsLoaded1 = 50 }, [7] = { itemsLoaded1 = 4 } }
                  return (logs[c] or {})[name] or 0
                end,
                getStockGroupsByTypeAndCargoTypes = function(sl, byType, showEmpty, producedOnly)
                  if sl ~= 770 then return {} end
                  -- { stock list type, stock ids (0-based), cargo types, amount held now }
                  return { { 2, { 0, 1 }, { 5 }, 120 }, { 2, { 2 }, { 6 }, 0 }, { 2, { 3 }, { 5, 6, 7 }, 30 } }
                end },
      industry = { getIndustryProductivityInfo = function(e) return { producing = e == 700 } end,
                   getClosingIndustries = function() return { { 701, "industry/steel_mill.con" } } end },
      station = { calculateStationUsage = function(st) return { totalUsed = 30, overflow = 0, poolCapacity = 50, terminalCapacity = 50 } end,
                  isStationOfType = function(st, cargo) return false end },
      cargo = { getCargoQualityDataAtStationGroup = function(g, c)
                  if g == 500 and c == 37 then return { countTotal = 42, countBad = 7, averageQuality = 0.25, isVeryBad = true } end
                  return { countTotal = 0, isVeryBad = g == 500 } end,
                getCargoQualityDataForStockList = function(sl, c)
                  if sl == 720 and c == 5 then return { countTotal = 60, countBad = 12, averageQuality = 0.5, isVeryBad = false } end
                  if sl == 770 and c == 5 then return { countTotal = 140, countBad = 30, averageQuality = 0.6, isVeryBad = false } end
                  if sl == 770 and c == 7 then return { countTotal = 10, countBad = 10, averageQuality = 0.1, isVeryBad = true } end
                  return { countTotal = 0, countBad = 0, isVeryBad = false } end,
                getSummarizedCargoQualityDataForStockList = function(sl)
                  if sl == 770 then return { cargo = { countTotal = 150, countBad = 40, averageQuality = 0.57 }, passengers = { countTotal = 0 } } end
                  return { cargo = { countTotal = 0 }, passengers = { countTotal = 0 } } end,
                getSummarizedCargoQualityDataForLine = function(l)
                  if l == 100 then return { cargo = { countTotal = 0 }, passengers = { countTotal = 30, countBad = 3, averageQuality = 0.8 } } end
                  return { cargo = { countTotal = 20, countBad = 5, averageQuality = 0.4 }, passengers = { countTotal = 0 } } end },
      vehicle = { getRunningCost = function() return 3000 end, getDepreciatedValue = function() return 20000 end,
                  getPartPrice = function(p) return p.part.modelId * 1000 end,
                  getPosition = function(v) return { x = v * 10, y = 2000, z = 0 } end, getSpeed = function() return 12.5 end },
      line = { getLineCapacityUsages = function(l) if l == 100 then return { [37] = { used = 30, capacity = 120 }, [3] = { used = 0, capacity = 0 }, [4] = { used = 0, capacity = 10 } } end return {} end },
      logbook = { getLogValuePerYear = function(e, n) return #n * 10 end },
      town = { getTownCapacityUsage = function(t) return vec({ { used = t == 900 and 800 or 200, capacity = 1000 }, { used = 50, capacity = 100 }, { used = 10, capacity = 20 } }) end,
               getTownLineUsage = function() return 0.3 end,
               getTownReachability = function() return { commercialPublic = 5, commercialPrivate = 9, industrialPublic = 1, industrialPrivate = 4 } end },
    },
    system = {
      simEntityAtStockSystem = { getStockCountForCargoType = function(holder, sid, c)
        if holder == 770 and sid == 3 then return ({ [5] = 20, [7] = 10 })[c] or 0 end
        return 0
      end },
      lineSystem = { getLinesForPlayer = function() return vec({ 100, 101 }) end,
                     getLinesForStationGroup = function(g) return { 100 } end,
                     getLineStops = function(g) return { { 100, g == 500 and 0 or 1 } } end,
                     getProblemLines = function() return { { 101, 4 } } end },
      transportVehicleSystem = {
        getLineVehicles = function(l) if l == 100 then return { 200, 201 } end return { 202 } end,
        getLineCargoInfo = function() return { frequency = 0.01, totalCapacity = 80 } end,
        getDepotVehicles = function() return { 203, 204 } end,
      },
      stationSystem = { getStation2TownMap = function() return { [600] = 900, [601] = 901 } end,
                        getTown = function(st) return st == 600 and 900 or 901 end },
      stationGroupSystem = { getStationGroup = function(st) return st == 600 and 500 or 501 end },
      streetConnectorSystem = { getConstructionClosestTown = function(con) return (con == 710 or con == 760) and 900 or 901 end },
      townBuildingSystem = { getTown2personCapacitiesMap = function() return { [900] = { 1000, 100, 20 }, [901] = vec({ 300, 30, 0 }) } end,
        getTown2BuildingMap = function() return { [900] = vec({ 1000, 1001 }), [901] = { 1002 } } end,
        getCargoSupplyAndLimit = function(t) if t == 900 then return { [4] = { 5, 10, 0 } } end return {} end },
      simEntityAtVehicleSystem = { getVehicleSimEntitiesCount = function(v) return 12 end },
      simEntityAtTerminalSystem = { getLineStopSimEntitiesCount = function(l, stop, c) return (stop == 0 and c == 37) and 40 or 0 end },
    },
  },
}
CLOCK = 0
rawget, rawset, rawequal = nil, nil, nil -- absent in the game Lua
dofile(MOD_ROOT .. "/mcp_bridge/mcp_bridge.script.lua")
SCRIPT = data()
''')

script = g.SCRIPT
for d in ("recordings", "keyframes", "mod_presets"):
    os.makedirs(os.path.join(out, d))

stop = threading.Event()
def game_loop():
    frame = 0
    while not stop.is_set():
        g.CLOCK = frame * 0.016
        script.guiUpdate()
        frame += 1
        time.sleep(0.002)
loop = threading.Thread(target=game_loop, daemon=True)
loop.start()
time.sleep(0.3)

spec = importlib.util.spec_from_file_location("srv", os.path.join(ROOT, "server", "tf3_mcp_server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
srv.userdata_dirs = lambda: [out]  # look for the hello file in the test folder

checks = []
exchange = srv.bridge_dir()
checks.append(("mod_presets chosen", os.path.basename(exchange) == "mod_presets"))
t0 = time.time()
snap, warning, age = srv.load_snapshot()
latency = time.time() - t0
models, mwarn = srv.load_models()
srv._cache.clear()
snap_b, warn_b, _ = srv.load_snapshot()
# the newer request kinds, while the game loop runs
ind = srv.tool_list_industries({})
by = {i["name"]: i for i in ind["industries"]}
st = srv.tool_list_stations({})
dep = srv.tool_list_depots({})
fin = srv.tool_finances({})
live = srv.tool_live_vehicles({"line": "line a"})
wh = srv.tool_list_warehouses({})
stale = srv.tool_stale_cargo({})
w0 = wh["warehouses"][0]
summary = srv.tool_live_vehicles({})
towns_cargo = srv.tool_list_towns({"name_contains": "spring"})
stop.set(); loop.join(2)

leftovers = os.listdir(exchange)
m = snap["meta"]
lines = {l["id"]: l for l in snap["lines"]}
snap_ids = srv.answer_ids(exchange, "snapshot")
manifest = srv.read_lua_data(os.path.join(exchange, "mcp_bridge__snapshot_%d.lua" % snap_ids[0]))
checks += [
    ("live answers, no warning", warning is None and mwarn is None and warn_b is None and snap_b["meta"]["requestId"] == manifest["seq"] != m["requestId"]),
    ("answer in several parts", manifest["parts"] >= 3),
    ("one request file, one hello", sum(1 for f in leftovers if f.startswith("mcp_bridge__request_")) == 1 and sum(1 for f in leftovers if f.startswith("mcp_bridge__hello")) == 1),
    ("one snapshot answer kept", len(snap_ids) == 1),
    ("2 lines", m["lineCount"] == 2 and len(lines) == 2),
    ("3 vehicles", m["vehicleCount"] == 3),
    ("date", m["date"] == {"year": 1925, "month": 3, "day": 1}),
    ("money", m["money"] == 9999999),
    ("log names discovered", m["lineLogNames"] == ["income", "passengers"]),
    ("escaped stop name survives", [x["name"] for x in lines[100]["stops"]] == ["Central", 'Harbour "Pier" \\ 1\nb']),
    ("line A usage 23%", lines[100]["usagePct"] == 23.1),
    ("line cargo from vehicles, usage total", lines[100]["cargos"] == ["PASSENGERS"] and lines[100]["usage"] == {"used": 30, "capacity": 130}),
    ("line A profit", lines[100]["profit12m"] == 12345 and lines[100]["profitPerVehicle12m"] == 6173),
    ("line A est income", lines[100]["estIncome12m"] == 12345 + 6000),
    ("line A logs", lines[100]["logsPerYear"] == {"income": 60, "passengers": 100}),
    ("line B problem", lines[101].get("problem") == "NO_PATH"),
    ("huge vehicle name intact", lines[101]["vehicles"][0]["name"] == "x" * 450000),
    ("vehicle model name", lines[100]["vehicles"][0]["units"][0]["name"] == "City Bus"),
    ("vehicle age", lines[100]["vehicles"][0]["ageDays"] == (5000000 - 1000000) // 2000),
    ("vehicle capacity", lines[100]["vehicles"][0]["capacity"] == 40 and lines[100]["vehicles"][0]["capacityByCargo"] == {"PASSENGERS": 40}),
    ("vehicle state", lines[100]["vehicles"][0]["state"] == "EN_ROUTE" and lines[100]["vehicles"][0]["carrier"] == "ROAD"),
    ("models: 2 vehicle models", models["meta"]["modelCount"] == 2),
    ("model availability", {x["name"]: x["available"] for x in models["models"]} == {"City Bus": True, "Old Bus": False}),
    ("model price/speed/power", [(x["price"], x.get("topSpeedKmh"), x.get("powerKw")) for x in models["models"] if x["name"] == "City Bus"] == [(7000, 54, 90)]),
    ("stop towns (UTF-8)", [x.get("town") for x in lines[100]["stops"]] == ["Springfield", "Zürich"] and lines[100]["towns"] == ["Springfield", "Zürich"]),
    ("town list", m["townCount"] == 2 and {t["name"]: t["population"] for t in snap["towns"]} == {"Springfield": 800, "Zürich": 200}),
    ("town size/reach", [(t["size"], t["reachability"]["commercialPublic"], t["developmentActive"]) for t in snap["towns"] if t["id"] == 900] == [(1120, 5, True)]),
    ("town positions", {t["id"]: t.get("position") for t in snap["towns"]} == {900: {"x": 1050, "y": 2000}, 901: {"x": 21050, "y": 2000}}),
    ("route ideas", [(r["distanceKm"], r.get("linkedBy")) for r in srv.tool_route_ideas({"include_linked": True})["pairs"]] == [(20.0, ["Line A"])]
                    and srv.tool_route_ideas({})["totalMatching"] == 0),
    ("town cargo needs/supply", [(t.get("cargoNeeds"), t.get("cargoSupply")) for t in towns_cargo["towns"]]
                                == [({"residential": ["C4"]}, {"C4": {"supply": 5, "limit": 10}})]),
    ("mod version", srv.mod_version() == 6),
    ("option logEach", sum(1 for x in g.LOG.values() if "answered" in x) >= 2),
    ("no diagnostics", m["diagnostics"] == {} or m["diagnostics"] == []),
]
# fallback: no game loop running -> last answer with a warning
srv.REQUEST_TIMEOUT_SEC = 1
srv._cache.clear()
extra_checks = [
    ("industries", ind["totalMatching"] == 2 and by["Coal Mine"]["type"] == "coal_mine" and by["Coal Mine"]["town"] == "Springfield"
                   and by["Coal Mine"]["position"] == {"x": 5000, "y": 3000} and by["Coal Mine"]["producedPerYear"] == 400),
    ("industry flags", set(by["Steel Mill"]["flags"]) == {"some_inputs_missing", "not_producing", "closing", "unshipped"}
                       and by["Steel Mill"]["inputs"][1] == {"cargo": "C6", "deliveredPerYear": 0, "consumedPerYear": 0, "maxConsumptionPerYear": 400}),
    ("cargo summary", ind["byCargo"]["C5"] == {"producedPerYear": 400, "shippedPerYear": 300, "producers": 1,
                                               "industryDemandPerYear": 400, "deliveredToIndustriesPerYear": 100, "consumers": 1}),
    ("industry filter", srv.tool_list_industries({"status": "unshipped"})["totalMatching"] == 1
                        and srv.tool_list_industries({"cargo": "c6"})["totalMatching"] == 1),
    ("warehouses", wh["totalMatching"] == 1 and w0["name"] == "Planks Warehouse"
                   and w0["type"] == "warehouse_small" and w0["town"] == "Springfield"
                   and w0["position"] == {"x": 1200, "y": 2100}
                   and (w0["waitingTotal"], w0["capacityTotal"], w0["usagePct"]) == (150, 350, 42.9)
                   and (w0["deliveredPerYear"], w0["shippedPerYear"], w0["thrownAwayCargo"]) == (500, 100, 25)
                   and w0["holding"] == {"C5": 140, "C7": 10} and (w0["pastDue"], w0["quality"]) == (40, 0.57)
                   and w0["logNames"] == ["itemsLoaded", "itemsLoaded1", "itemsUnloaded"]
                   and (w0["inPerYear"], w0["outPerYear"]) == (450, 154)),
    ("warehouse slots", w0["stocks"] == [
        {"stockType": "StorageStock", "cargos": ["C5"], "waiting": 120, "capacity": 200, "slots": 2},
        {"stockType": "StorageStock", "cargos": ["C6"], "waiting": 0, "capacity": 50, "slots": 1},
        {"stockType": "StorageStock", "cargos": ["C5", "C7"], "waiting": 30, "capacity": 100, "slots": 1,
         "mixed": True, "accepts": 3, "held": {"C5": 20, "C7": 10}}]),
    ("warehouse flows", w0["flows"] == [
        {"cargo": "C5", "deliveredPerYear": 500, "shippedPerYear": 100,
         "logsPerYear": {"itemsUnloaded": 450, "itemsLoaded": 100, "itemsLoaded1": 50},
         "pastDue": 30, "quality": 0.6, "deliveryTimeSec": 1440, "inPerYear": 450, "outPerYear": 150},
        {"cargo": "C7", "deliveredPerYear": 0, "shippedPerYear": 0, "logsPerYear": {"itemsLoaded1": 4},
         "pastDue": 10, "quality": 0.1, "veryBad": True, "deliveryTimeSec": 1440, "inPerYear": 0, "outPerYear": 4}]
                        and wh["byCargo"] == {
        "C5": {"waiting": 140, "pastDue": 30, "inPerYear": 450, "outPerYear": 150, "warehouses": 1},
        "C7": {"waiting": 10, "pastDue": 10, "inPerYear": 0, "outPerYear": 4, "warehouses": 1}}),
    ("warehouse flags", set(w0["flags"]) == {"past_due", "overflowing"} and "stuckCargo" not in w0
                        # C7 is not stuck: the game's shipped counter says 0, but itemsLoaded1 shows 4 leaving
                        and srv.warehouse_flags({"waitingTotal": 5, "holding": {"X": 5},
                                                 "flows": [{"cargo": "X", "shippedPerYear": 0}],
                                                 "stocks": [{"capacity": 5, "waiting": 5}]}) == ["full", "nothing_leaves", "stuck_cargo"]
                        and srv.tool_list_warehouses({"status": "stuck_cargo"})["totalMatching"] == 0
                        and srv.tool_list_warehouses({"status": "past_due"})["totalMatching"] == 1
                        and srv.tool_list_warehouses({"status": "unused"})["totalMatching"] == 0
                        and srv.tool_list_warehouses({"cargo": "C7"})["totalMatching"] == 1
                        and srv.tool_list_warehouses({"cargo": "C6"})["totalMatching"] == 0),
    ("station past due", st["stations"][0]["pastDue"] == {"PASSENGERS": {"count": 42, "pastDue": 7, "quality": 0.25, "veryBad": True}}
                         and "pastDue" not in st["stations"][1]),
    ("industry output past due", [(o.get("waiting"), o.get("pastDue"), o.get("quality")) for o in by["Coal Mine"]["outputs"]] == [(60, 12, 0.5)]
                                 and "pastDue" not in by["Steel Mill"]["outputs"][0]),
    ("line past due", sorted((r.get("cargoPastDue") or 0) for r in srv.tool_list_lines({})["lines"]) == [0, 5]
                      and srv.tool_get_line({"line": "line a"}).get("passengerQuality") == {"count": 30, "pastDue": 3, "quality": 0.8}),
    ("stale cargo", (stale["totalPastDue"], stale["places"]) == (57, 4)
                    and [(r["where"], r.get("cargo") if r["where"] != "line" else None, r["pastDue"]) for r in stale["rows"]]
                        == [("warehouse", "C5", 30), ("industry", "C5", 12), ("warehouse", "C7", 10), ("line", None, 5)]
                    and stale["byCargo"] == {"C5": {"pastDue": 42, "places": 2}, "C7": {"pastDue": 10, "places": 1}}
                    and stale["pastDueOnLines"] == 5 and stale["deliveryTimes"]["C5"]["seconds"] == 1440
                    and "warning" not in stale),
    ("stale cargo filters", srv.tool_stale_cargo({"cargo": "C7"})["places"] == 1
                            and srv.tool_stale_cargo({"where": ["industries"]})["places"] == 1
                            and srv.tool_stale_cargo({"min_past_due": 11})["places"] == 2),
    ("stations", [(x["name"], x.get("waitingTotal"), x["terminals"], x["town"], x["usagePct"]) for x in st["stations"]]
                 == [("Central", 42, 2, "Springfield", 30.0), ('Harbour "Pier" \\ 1\nb', 0, 1, "Zürich", 30.0)]
                 and st["stations"][0]["longWaits"] == ["PASSENGERS"] and st["stations"][0]["lines"] == ["Line A"]
                 and [x["kind"] for x in st["stations"]] == ["passenger", "passenger"]),
    ("depots", dep["depots"] == [{"id": 800, "name": "Bus Depot", "carrier": "ROAD", "position": {"x": 1100, "y": 2100},
                                  "vehiclesInside": 2, "nearestTown": {"name": "Springfield", "distanceKm": 0.1}}]),
    ("finances", fin["balance"] == 9999999 and fin["loan"] == 250000 and fin["earningsThisYear"] == 777
                 and fin["company"]["totalScore"] == 12 and fin["transported"] == {"passengers": 5000, "cargo": 300}
                 and fin["financeTable"]["years"] == ["1924", "1925"] and fin["financeTable"]["byCarrier"] == {"ROAD": {"income": [9, 7], "costs": [-4, -1], "net": [5, 6]}}
                 and fin["linesByCarrier"]["ROAD"]["profit12m"] == 11845),
    ("live vehicles", live["line"] == "Line A" and [(v["id"], v["position"], v["stopIndex"], v["load"]) for v in live["vehicles"]]
                      == [(200, {"x": 2000, "y": 2000}, 1, 12), (201, {"x": 2010, "y": 2000}, 0, 12)]),
    ("bunching check", srv.headway_check([{"stopDepartures": [t]} for t in (50, 150, 160, 350)], 10)
                       == {"vehiclesCompared": 4, "evenness": 0.1, "bunched": True, "gapsDays": [10.0, 1.0, 19.0]}
                       and summary["linesChecked"] == 0),
]
checks += extra_checks


def fake_request(what, timeout=None):
    raise TimeoutError(what)
srv.request = fake_request
snap2, warning2, age2 = srv.load_snapshot()
checks.append(("fallback to last answer with warning", snap2["meta"]["requestId"] == snap_b["meta"]["requestId"] and "did not answer" in (warning2 or "")))

for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
print("log:", [x for x in g.LOG.values()])
if not all(ok for _, ok in checks):
    print("diag:", m.get("diagnostics"))
    sys.exit(1)
print("latency %.2f s, parts %d, frames %s, cpu ms %s, out: %s" % (latency, manifest["parts"], m["collectFrames"], m["collectCpuMs"], out))
