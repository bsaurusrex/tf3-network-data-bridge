-- Reads lines, their vehicles and the buyable vehicle models from the engine state, as plain Lua tables.
-- Every engine read is protected: a read that fails leaves the field out and records the error once in M.diag
-- (written to the snapshot meta), so one API difference never stops the whole export.

local M = {}

M.diag = {}
local diagSeen, diagCount = {}, 0

local function note(where, err)
	local key = where .. ": " .. tostring(err)
	if diagSeen[key] or diagCount >= 40 then return end
	diagSeen[key] = true
	diagCount = diagCount + 1
	M.diag[#M.diag + 1] = key
end

-- protected call returning the value, or nil (error noted under `where`)
local function try(where, fn, ...)
	local ok, v = pcall(fn, ...)
	if ok then return v end
	note(where, v)
	return nil
end

local function size(list)
	if list == nil then return 0 end
	local ok, n = pcall(function() return list:size() end)
	if ok and type(n) == "number" then return n end
	ok, n = pcall(function() return #list end)
	return (ok and type(n) == "number") and n or 0
end

local function at(list, i)
	-- plain tables first (the game Lua has no rawget)
	if type(list) == "table" then
		local ok, v = pcall(function() return list[i] end)
		if ok and v ~= nil then return v end
	end
	local ok, v = pcall(function() return list:at(i) end)
	if ok and v ~= nil then return v end
	ok, v = pcall(function() return list[i] end)
	if ok then return v end
	return nil
end

local function toArray(list)
	local out = {}
	for i = 1, size(list) do out[i] = at(list, i) end
	return out
end

-- iterate a Lua table or a native map
local function eachPair(map, fn)
	if map == nil then return end
	pcall(function() for k, v in pairs(map) do fn(k, v) end end)
end

-- optional nested field (metadata): walks the keys, nil as soon as one is missing, never an error
local function get(obj, ...)
	local keys = { ... }
	local ok, v = pcall(function()
		local cur = obj
		for _, k in ipairs(keys) do
			if cur == nil then return nil end
			cur = cur[k]
		end
		return cur
	end)
	if ok then return v end
	return nil
end

local function num(v)
	if type(v) == "number" then return v end
	return tonumber(v)
end

---------------------------------------------------------------------------------------------------------------------
-- cargo quality as the game reports it (UtilCargo.CargoQualityData): countBad = units no longer in time, i.e. past
-- their delivery time; averageQuality; isVeryBad. nil when nothing is there.
local function quality(q)
	if q == nil then return nil end
	local total = num(get(q, "countTotal")) or 0
	if total <= 0 then return nil end
	local avg = num(get(q, "averageQuality"))
	return { count = total, pastDue = num(get(q, "countBad")) or 0,
		quality = avg and math.floor(avg * 1000 + 0.5) / 1000 or nil, veryBad = get(q, "isVeryBad") and true or nil }
end

-- enums and names

local reverse = {}
local function enumName(enumType, names, value)
	local map = reverse[enumType]
	if map == nil then
		map = {}
		pcall(function()
			local enum = api.type.enum[enumType]
			for _, name in ipairs(names) do
				local ok, v = pcall(function() return enum[name] end)
				if ok and v ~= nil then map[v] = name end
			end
		end)
		reverse[enumType] = map
	end
	return map[value] or (value ~= nil and tostring(value) or nil)
end

local CARRIERS = { "ROAD", "RAIL", "TRAM", "AIR", "WATER", "OTHER" }
local STATES = { "IN_DEPOT", "EN_ROUTE", "AT_TERMINAL", "GOING_TO_DEPOT" }
local MODES = { "PERSON", "CARGO", "CAR", "BUS", "TRUCK", "TRAM", "ELECTRIC_TRAM", "TRAIN", "ELECTRIC_TRAIN",
	"AIRCRAFT", "SHIP", "SMALL_AIRCRAFT", "SMALL_SHIP", "HELICOPTER" }

function M.carrierName(v) return enumName("Carrier", CARRIERS, v) end
M.modeName = function(v) return modeName(v) end
local function stateName(v) return enumName("TransportVehicleState", STATES, v) end
local function modeName(v) return enumName("TransportMode", MODES, v) end

local function nameOf(entity)
	local ok, c = pcall(api.engine.getComponent, entity, api.type.ComponentType.NAME)
	if ok and c ~= nil then
		local okN, n = pcall(function() return c.name end)
		if okN and n ~= nil and n ~= "" then return n end
	end
	-- industries, depots and others without a NAME component: the name the game computes for them
	local okE, n = pcall(function() return api.engine.util.getEntityName(entity) end)
	if okE and type(n) == "string" and n ~= "" then return n end
	return nil
end

local cargoNames = {}
local passengerId = false
local function cargoName(id)
	local n = cargoNames[id]
	if n == nil then
		-- passengers have their own id outside the cargo type list (getName fails for it)
		if passengerId == false then
			local okP, pid = pcall(function() return api.res.cargoTypeRep.getPassengerCargoTypeId() end)
			passengerId = okP and pid or nil
		end
		if id == passengerId then
			n = "PASSENGERS"
		else
			local ok, name = pcall(function() return api.res.cargoTypeRep.getName(id) end)
			n = (ok and type(name) == "string" and name ~= "") and name or ("cargo " .. tostring(id))
			-- the game gives the resource path ("::/cargos/iron_ore/iron_ore.cargo"); keep "IRON_ORE"
			local short = n:match("([^/]+)%.cargo$")
			if short then n = short:upper() end
		end
		cargoNames[id] = n
	end
	return n
end

---------------------------------------------------------------------------------------------------------------------
-- time

-- ctx fields used here: gameTime, yearTicks, dayTicks
function M.timeInfo()
	local t = {}
	local gt = try("GAME_TIME", function()
		return api.engine.getComponent(api.engine.util.getWorld(), api.type.ComponentType.GAME_TIME)
	end)
	t.gameTime = gt and num(try("gameTime", function() return gt.gameTime end)) or 0
	local speed = try("GAME_SPEED", function()
		return api.engine.getComponent(api.engine.util.getWorld(), api.type.ComponentType.GAME_SPEED)
	end)
	if speed then
		t.speedup = num(try("speedup", function() return speed.speedup end))
		t.millisPerDay = num(try("millisPerDay", function() return speed.millisPerDay end))
	end
	t.defaultDayTicks = num(try("getDefaultDayDuration", function() return api.util.getDefaultDayDuration() end))
	t.defaultYearTicks = num(try("getDefaultYearDuration", function() return api.util.getDefaultYearDuration() end))
	if t.millisPerDay and t.millisPerDay > 0 then
		t.yearTicks = num(try("getYearDuration", function() return api.util.getYearDuration(t.millisPerDay) end))
	end
	t.yearTicks = t.yearTicks or t.defaultYearTicks or 730000
	t.dayTicks = t.yearTicks / 365
	-- the save's calendar date (api.type.Date.now() is the real-world clock)
	local date = try("getCalendarDate", function() return api.engine.util.getCalendarDate(t.gameTime) end)
	if date then
		t.date = {
			year = num(try("date.year", function() return date.year end)),
			month = num(try("date.month", function() return date.month end)),
			day = num(try("date.day", function() return date.day end)),
		}
	else
		local year = num(try("getYear", function() return api.engine.util.getYear() end))
		if year then t.date = { year = year } end
	end
	return t
end

---------------------------------------------------------------------------------------------------------------------
-- models (per id, cached for the session)

local modelInfo = {}

local function sumEnginePower(engines)
	local power = 0
	for i = 1, size(engines) do
		local e = at(engines, i)
		power = power + (num(try("engine.power", function() return e.power end)) or 0)
	end
	return power
end

-- capacity of a model: each compartment carries one of its load configs, so sum the largest per compartment
local function modelCapacity(tv)
	local total = 0
	local compartments = try("compartments", function() return tv.compartments end)
	for c = 1, size(compartments) do
		local comp = at(compartments, c)
		local configs = try("loadConfigs", function() return comp.loadConfigs end)
		local best = 0
		for l = 1, size(configs) do
			local cfg = at(configs, l)
			local cap = num(try("cargoEntry.capacity", function() return cfg.cargoEntry.capacity end)) or 0
			if cap > best then best = cap end
		end
		total = total + best
	end
	return total
end

-- static facts about a model id (name, lifespan, specs); nil if it is not a vehicle model
function M.model(id)
	local info = modelInfo[id]
	if info ~= nil then return info or nil end
	info = false
	local model = try("modelRep.get", function() return api.res.modelRep.get(id) end)
	local md = model and try("model.metadata", function() return model.metadata end)
	local tv = md and try("metadata.transportVehicle", function() return md.transportVehicle end)
	if tv then
		info = { id = id }
		info.file = try("modelRep.getName", function() return api.res.modelRep.getName(id) end)
		info.name = get(md, "description", "name")
		info.carrier = M.carrierName(get(tv, "carrier"))
		info.yearFrom = num(get(md, "availability", "yearFrom"))
		info.yearTo = num(get(md, "availability", "yearTo"))
		info.lifespanDays = num(get(md, "maintenance", "lifespan"))
		info.runningCostsMeta = num(get(md, "maintenance", "runningCosts"))
		info.priceMeta = num(get(md, "cost", "price"))
		info.comfortFactor = num(get(tv, "comfortFactor"))
		info.priceFactor = num(get(tv, "priceFactor"))
		info.loadSpeed = num(get(tv, "loadSpeed"))
		info.capacity = modelCapacity(tv)
		local modes = {}
		local tms = get(tv, "transportModes")
		for i = 1, size(tms) do modes[#modes + 1] = modeName(at(tms, i)) end
		info.transportModes = modes
		local moving = get(md, "landVehicle") or get(md, "railVehicle") or get(md, "roadVehicle")
			or get(md, "waterVehicle") or get(md, "airVehicle")
		if moving then
			local speed = num(get(moving, "topSpeed"))
			info.topSpeedKmh = speed and math.floor(speed * 3.6 + 0.5) or nil
			info.weightEmptyKg = num(get(moving, "weightEmpty"))
			local engines = get(moving, "engines")
			local power = engines and sumEnginePower(engines) or 0
			if power == 0 then power = num(get(moving, "availPower")) or 0 end
			info.powerKw = power > 0 and power or nil
		end
	end
	modelInfo[id] = info
	return info or nil
end

-- current purchase price of one unit of a model
function M.modelPrice(id, gameTime)
	return num(try("getPartPrice", function()
		local vp = api.type.VehiclePart.new()
		vp.modelId = id
		local part = api.type.TransportVehiclePart.new()
		part.part = vp
		part.purchaseTime = gameTime
		return api.engine.util.vehicle.getPartPrice(part)
	end))
end

-- all model resource names with transport vehicle metadata
function M.vehicleModelNames()
	local names = {}
	try("forEachModelWithMetadata", function()
		api.res.modelRep.forEachModelWithMetadata("transportVehicle", function(name) names[#names + 1] = name end)
	end)
	return names
end

function M.modelEntry(name, ctx)
	local id = try("modelRep.find", function() return api.res.modelRep.find(name) end)
	if id == nil or id < 0 then return nil end
	local info = M.model(id)
	if info == nil then return nil end
	local e = {}
	for k, v in pairs(info) do e[k] = v end
	local year = ctx.date and ctx.date.year
	if year then
		e.available = (not info.yearFrom or info.yearFrom == 0 or info.yearFrom <= year)
			and (not info.yearTo or info.yearTo == 0 or year < info.yearTo)
	end
	e.price = M.modelPrice(id, ctx.gameTime)
	local okV, visible = pcall(api.res.modelRep.isVisible, id)
	if okV then e.visible = visible end
	return e
end

---------------------------------------------------------------------------------------------------------------------
-- vehicles and lines

local function balance(entity, ctx, fromTicks)
	return num(try("calculateBalance", function()
		return api.engine.util.finance.calculateBalance({ entity }, fromTicks, ctx.gameTime, true)
	end))
end

function M.vehicle(v, ctx)
	local V = { id = v, name = nameOf(v) }
	local tv = try("TRANSPORT_VEHICLE", function()
		return api.engine.getComponent(v, api.type.ComponentType.TRANSPORT_VEHICLE)
	end)
	if tv == nil then return V end
	V.carrier = M.carrierName(try("tv.carrier", function() return tv.carrier end))
	V.state = stateName(try("tv.state", function() return tv.state end))
	V.userStopped = try("tv.userStopped", function() return tv.userStopped end)
	V.sellOnArrival = try("tv.sellOnArrival", function() return tv.sellOnArrival end)
	V.noPath = try("tv.noPath", function() return tv.noPath end)
	V.daysInDepot = num(try("tv.daysInDepot", function() return tv.daysInDepot end))

	local parts = try("transportVehicleConfig.vehicles", function() return tv.transportVehicleConfig.vehicles end)
	local units, oldest, minLife, worstMaint = {}, nil, nil, nil
	for i = 1, size(parts) do
		local p = at(parts, i)
		local modelId = num(try("part.modelId", function() return p.part.modelId end))
		local bought = num(try("purchaseTime", function() return p.purchaseTime end))
		local maint = num(try("maintenanceState", function() return p.maintenanceState end))
		local info = modelId and M.model(modelId)
		local age = bought and (ctx.gameTime - bought) / ctx.dayTicks or nil
		units[#units + 1] = {
			model = modelId, name = info and info.name, ageDays = age and math.floor(age) or nil,
			lifespanDays = info and info.lifespanDays, maintenance = maint,
		}
		if age and (oldest == nil or age > oldest) then oldest = age end
		if info and info.lifespanDays and (minLife == nil or info.lifespanDays < minLife) then minLife = info.lifespanDays end
		if maint and (worstMaint == nil or maint < worstMaint) then worstMaint = maint end
	end
	V.units = units
	V.ageDays = oldest and math.floor(oldest) or nil
	V.lifespanDays = minLife
	if oldest and minLife and minLife > 0 then V.ageOfLifespan = math.floor(oldest / minLife * 100 + 0.5) / 100 end
	V.worstMaintenance = worstMaint

	local capacity, byCargo = 0, {}
	local caps = try("config.capacities", function() return tv.config.capacities end)
	for i = 1, size(caps) do
		local c = num(at(caps, i)) or 0
		if c > 0 then
			capacity = capacity + c
			byCargo[cargoName(i - 1)] = c
			if ctx.cargoIds then ctx.cargoIds[i - 1] = true end
		end
	end
	V.capacity = capacity
	V.capacityByCargo = byCargo
	V.load = num(try("getVehicleSimEntitiesCount", function()
		return api.engine.system.simEntityAtVehicleSystem.getVehicleSimEntitiesCount(v)
	end))
	V.runningCostPerYear = num(try("getRunningCost", function() return api.engine.util.vehicle.getRunningCost(v) end))
	V.value = num(try("getDepreciatedValue", function() return api.engine.util.vehicle.getDepreciatedValue(v) end))
	V.profit12m = balance(v, ctx, ctx.gameTime - ctx.yearTicks)
	return V
end

-- names of the logs kept in a line's logbook (read once per session from the first line that has one)
function M.discoverLineLogs(line)
	local names = {}
	local book = try("LOG_BOOK", function() return api.engine.getComponent(line, api.type.ComponentType.LOG_BOOK) end)
	if book == nil then return nil end
	eachPair(try("name2log", function() return book.name2log end), function(k) names[#names + 1] = tostring(k) end)
	table.sort(names)
	return names
end

function M.line(id, ctx)
	local L = { id = id, name = nameOf(id) }
	local comp = try("LINE", function() return api.engine.getComponent(id, api.type.ComponentType.LINE) end)
	if comp then
		local stops, towns, seenTown = {}, {}, {}
		local list = try("line.stops", function() return comp.stops end)
		for i = 1, size(list) do
			local s = at(list, i)
			local group = num(try("stop.stationGroup", function() return s.stationGroup end))
			local stop = { stationGroup = group, name = group and nameOf(group) }
			local town = group and ctx.station2town and M.groupTown(group, ctx)
			if town then
				stop.townId = town
				stop.town = nameOf(town)
				if not seenTown[town] then
					seenTown[town] = true
					towns[#towns + 1] = stop.town or tostring(town)
				end
			end
			stops[#stops + 1] = stop
		end
		L.stops = stops
		L.towns = towns
		local modes = {}
		eachPair(try("vehicleInfo.transportModes", function() return comp.vehicleInfo.transportModes end),
			function(k, v) if v then modes[#modes + 1] = modeName(k) end end)
		L.transportModes = modes
	end

	local vehicles, carriers = {}, {}
	local running, value, loadNow, capNow = 0, 0, 0, 0
	local list = try("getLineVehicles", function() return api.engine.system.transportVehicleSystem.getLineVehicles(id) end)
	ctx.cargoIds = {} -- cargo the line's vehicles can carry (filled by M.vehicle)
	for i = 1, size(list) do
		local V = M.vehicle(at(list, i), ctx)
		vehicles[#vehicles + 1] = V
		if V.carrier then carriers[V.carrier] = true end
		running = running + (V.runningCostPerYear or 0)
		value = value + (V.value or 0)
		loadNow = loadNow + (V.load or 0)
		capNow = capNow + (V.capacity or 0)
	end
	L.vehicles = vehicles
	L.vehicleCount = #vehicles
	local cs = {}
	for c in pairs(carriers) do cs[#cs + 1] = c end
	table.sort(cs)
	L.carriers = cs
	L.runningCostsPerYear = running
	L.fleetValue = value
	L.loadNow = loadNow
	L.capacityNow = capNow

	-- cargo the line carries, from its vehicles' capacities (cargo ids verified in game)
	local cargoIds, cargoNamesList = {}, {}
	for c in pairs(ctx.cargoIds) do cargoIds[#cargoIds + 1] = c end
	table.sort(cargoIds)
	for _, c in ipairs(cargoIds) do cargoNamesList[#cargoNamesList + 1] = cargoName(c) end
	L.cargos = cargoNamesList
	local firstCargo = cargoIds[1]

	-- capacity used over recent trips (what the line window shows as usage). The game's per-cargo keys do not match
	-- cargo ids in this build (a passenger line came out as GRAIN), so only the total is used.
	local used, cap = 0, 0
	eachPair(try("getLineCapacityUsages", function() return api.engine.util.line.getLineCapacityUsages(id, false) end),
		function(_, u)
			used = used + (num(try("usage.used", function() return u.used end)) or 0)
			cap = cap + (num(try("usage.capacity", function() return u.capacity end)) or 0)
		end)
	L.usage = { used = used, capacity = cap }
	if cap > 0 then L.usagePct = math.floor(used / cap * 1000 + 0.5) / 10 end

	if firstCargo ~= nil then
		local info = try("getLineCargoInfo", function()
			return api.engine.system.transportVehicleSystem.getLineCargoInfo(id, firstCargo)
		end)
		if info then
			L.frequency = num(try("cargoInfo.frequency", function() return info.frequency end))
			L.totalCapacity = num(try("cargoInfo.totalCapacity", function() return info.totalCapacity end))
		end
	end

	L.profit12m = balance(id, ctx, ctx.gameTime - ctx.yearTicks)
	L.profitAllTime = balance(id, ctx, 0)
	if L.profit12m then L.estIncome12m = L.profit12m + running end
	if L.profit12m and #vehicles > 0 then L.profitPerVehicle12m = math.floor(L.profit12m / #vehicles + 0.5) end

	if ctx.logNames then
		local logs = {}
		for _, name in ipairs(ctx.logNames) do
			logs[name] = num(try("getLogValuePerYear", function() return api.engine.util.logbook.getLogValuePerYear(id, name) end))
		end
		L.logsPerYear = logs
	end
	-- cargo and passengers on the line that are past their delivery time
	local sq = try("getSummarizedCargoQualityDataForLine", function()
		return api.engine.util.cargo.getSummarizedCargoQualityDataForLine(id)
	end)
	if sq ~= nil then
		L.cargoQuality = quality(get(sq, "cargo"))
		L.passengerQuality = quality(get(sq, "passengers"))
	end
	L.problem = ctx.problems and ctx.problems[id] or nil
	return L
end

---------------------------------------------------------------------------------------------------------------------
-- towns

-- station entity -> town entity, as a Lua table (read once per snapshot)
function M.station2town()
	local out = {}
	eachPair(try("getStation2TownMap", function() return api.engine.system.stationSystem.getStation2TownMap() end),
		function(station, town) out[station] = town end)
	return out
end

-- town of a station group (the town of its first station that has one), cached in ctx.group2town
function M.groupTown(group, ctx)
	local cache = ctx.group2town
	local t = cache[group]
	if t ~= nil then return t or nil end
	t = false
	local sg = try("STATION_GROUP", function() return api.engine.getComponent(group, api.type.ComponentType.STATION_GROUP) end)
	local stations = sg and try("stationGroup.stations", function() return sg.stations end)
	for i = 1, size(stations) do
		local town = ctx.station2town[at(stations, i)]
		if town ~= nil then t = town; break end
	end
	cache[group] = t
	return t or nil
end

-- {town entity, {residential, commercial, industrial capacity}} for every town on the map
function M.townList()
	local out = {}
	eachPair(try("getTown2personCapacitiesMap", function()
		return api.engine.system.townBuildingSystem.getTown2personCapacitiesMap()
	end), function(town, caps) out[#out + 1] = { town, toArray(caps) } end)
	table.sort(out, function(a, b) return a[1] < b[1] end)
	return out
end

local LAND_USES = { "residential", "commercial", "industrial" }

-- town centre (world x/y in metres), estimated from a sample of the town's buildings; cached for the session since
-- towns barely move, so the building map is only read when a town has no position yet
local townPos = {}
local TOWN_SAMPLE = 24

local function bboxCentre(e)
	local ok, x, y = pcall(function()
		local bv = api.engine.getComponent(e, api.type.ComponentType.BOUNDING_VOLUME)
		if bv == nil then return nil end
		local b = bv.bbox
		return (b.min.x + b.max.x) / 2, (b.min.y + b.max.y) / 2
	end)
	if ok and type(x) == "number" and type(y) == "number" then return x, y end
	return nil
end

local function townPosition(id, ctx)
	if townPos[id] ~= nil then return townPos[id] or nil end
	if ctx.town2buildings == nil then
		ctx.town2buildings = {}
		eachPair(try("getTown2BuildingMap", function()
			return api.engine.system.townBuildingSystem.getTown2BuildingMap()
		end), function(town, list) ctx.town2buildings[town] = list end)
	end
	local list = ctx.town2buildings[id]
	local n = size(list)
	local sx, sy, count = 0, 0, 0
	local step = math.max(1, math.floor(n / TOWN_SAMPLE))
	for i = 1, n, step do
		local b = at(list, i)
		local x, y = bboxCentre(b)
		if x == nil then -- the box may sit on the building's construction
			local okC, con = pcall(function()
				return api.engine.getComponent(b, api.type.ComponentType.TOWN_BUILDING).construction
			end)
			if okC and con ~= nil then x, y = bboxCentre(con) end
		end
		if x ~= nil then sx, sy, count = sx + x, sy + y, count + 1 end
		if count >= TOWN_SAMPLE then break end
	end
	if count == 0 then
		townPos[id] = false
		return nil
	end
	townPos[id] = { x = math.floor(sx / count + 0.5), y = math.floor(sy / count + 0.5) }
	return townPos[id]
end

function M.town(entry, ctx)
	local id, caps = entry[1], entry[2]
	local T = { id = id, name = nameOf(id) }
	local capacity, total = {}, 0
	for i, key in ipairs(LAND_USES) do
		local c = num(caps[i]) or 0
		capacity[key] = c
		total = total + c
	end
	T.capacity = capacity
	T.size = total
	local usage = try("getTownCapacityUsage", function() return api.engine.util.town.getTownCapacityUsage(id) end)
	if usage then
		local used = {}
		for i, key in ipairs(LAND_USES) do
			local u = at(usage, i)
			used[key] = u and num(try("townUsage.used", function() return u.used end)) or nil
		end
		T.used = used
		T.population = used.residential
	end
	T.lineUsage = num(try("getTownLineUsage", function() return api.engine.util.town.getTownLineUsage(id) end))
	local reach = try("getTownReachability", function() return api.engine.util.town.getTownReachability(id) end)
	if reach then
		T.reachability = {
			commercialPublic = num(get(reach, "commercialPublic")), commercialPrivate = num(get(reach, "commercialPrivate")),
			industrialPublic = num(get(reach, "industrialPublic")), industrialPrivate = num(get(reach, "industrialPrivate")),
		}
	end
	local comp = try("TOWN", function() return api.engine.getComponent(id, api.type.ComponentType.TOWN) end)
	if comp then T.developmentActive = get(comp, "developmentActive") end
	T.position = townPosition(id, ctx)
	-- goods each district wants, and the town's current supply and limit per cargo
	if comp then
		local needs, any = {}, false
		for i, key in ipairs(LAND_USES) do
			local list = get(comp, "cargoNeeds", i)
			local names = {}
			for k = 1, size(list) do
				local pair = at(list, k)
				local c = pair and at(pair, 1)
				if c ~= nil then names[#names + 1] = cargoName(c) end
			end
			if #names > 0 then needs[key], any = names, true end
		end
		if any then T.cargoNeeds = needs end
	end
	local supply, anySupply = {}, false
	eachPair(try("getCargoSupplyAndLimit", function()
		return api.engine.system.townBuildingSystem.getCargoSupplyAndLimit(id)
	end), function(c, v)
		local s, l = num(at(v, 1)), num(at(v, 2))
		if (s or 0) > 0 or (l or 0) > 0 then supply[cargoName(c)], anySupply = { supply = s, limit = l }, true end
	end)
	if anySupply then T.cargoSupply = supply end
	return T
end

-- line -> problem name, for the player's lines that have one
local PROBLEMS = { "NOTHING", "ZERO_OR_ONE_STATION", "DOUBLE_STATIONS", "INCOMPATIBLE_STATIONS", "NO_PATH", "BAD_ALTERNATIVE_TERMINAL" }
function M.lineProblems(player)
	local out = {}
	local list = try("getProblemLines", function() return api.engine.system.lineSystem.getProblemLines(player) end)
	for i = 1, size(list) do
		local pair = at(list, i)
		local line, problem = at(pair, 1), at(pair, 2)
		if line ~= nil then
			local name = enumName("LineProblem", PROBLEMS, problem)
			if problem ~= nil and name == tostring(problem) then
				-- LineProblem holds plain integers, not an enum
				pcall(function()
					for _, n in ipairs(PROBLEMS) do if api.type.LineProblem[n] == problem then name = n end end
				end)
			end
			out[line] = name
		end
	end
	return out
end

M.size, M.at, M.toArray, M.try = size, at, toArray, try
M.get, M.eachPair, M.num, M.note, M.enumName = get, eachPair, num, note, enumName
M.cargoName, M.nameOf, M.bboxCentre, M.stateName = cargoName, nameOf, bboxCentre, stateName
M.quality = quality

return M
