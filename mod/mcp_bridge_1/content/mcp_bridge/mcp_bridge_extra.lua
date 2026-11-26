-- MCP Bridge: the request kinds besides "snapshot" and "models". Each job's start() returns lists of items to collect
-- (one item per frame slice) and meta() adds request-wide fields; the main script runs them sliced and writes
-- {"meta": ..., "<key>": [...], ...}. Read-only: only the official scripting API's getters are used.

local MOD = (getOwningModId and getOwningModId()) or "mcp_bridge_1"
local collect = ug_require(MOD .. "::/mcp_bridge/mcp_bridge_collect.lua")
local size, at, toArray, try, get, eachPair, num = collect.size, collect.at, collect.toArray, collect.try, collect.get,
	collect.eachPair, collect.num
local cargoName, nameOf, bboxCentre = collect.cargoName, collect.nameOf, collect.bboxCentre

local CT = api.type.ComponentType
local M = { jobs = {} }

local function comp(e, t)
	local ok, c = pcall(function() return api.engine.getComponent(e, t) end)
	if ok then return c end
	return nil
end

local function xy(v)
	local x, y = num(get(v, "x")), num(get(v, "y"))
	if x and y then return { x = math.floor(x + 0.5), y = math.floor(y + 0.5) } end
	return nil
end

local function positionOf(...)
	for _, e in ipairs({ ... }) do
		if e ~= nil then
			local x, y = bboxCentre(e)
			if x then return { x = math.floor(x + 0.5), y = math.floor(y + 0.5) } end
		end
	end
	return nil
end

-- the player's entities with a component; falls back to filtering by PLAYER_OWNED if the filter is refused
local function owned(componentType, player)
	local ok, list = pcall(function()
		return api.engine.getEntitiesWithComponent(componentType, { requireOwnedByPlayer = player })
	end)
	if ok and list ~= nil then return toArray(list) end
	local out = {}
	for _, e in ipairs(toArray(try("getEntitiesWithComponent", function()
		return api.engine.getEntitiesWithComponent(componentType)
	end))) do
		if get(comp(e, CT.PLAYER_OWNED), "player") == player then out[#out + 1] = e end
	end
	return out
end

local function shortRes(name)
	if type(name) ~= "string" then return nil end
	return name:match("([^/]+)%.con$") or name:match("([^/]+)$") or name
end

---------------------------------------------------------------------------------------------------------------------
-- industries: production, inputs, shipping, location

local stock = function() return api.engine.util.stock end

local function industry(id, ctx)
	local I = { id = id, name = nameOf(id) }
	local ind = comp(id, CT.INDUSTRY)
	if ind == nil then return I end
	I.level, I.maxLevel = num(get(ind, "level")), num(get(ind, "maxLevel"))
	I.upgradeProgress = num(get(ind, "upgradeProgress"))
	I.closing = ctx.closing[id] or nil
	local con = get(ind, "construction")
	I.type = shortRes(get(comp(con, CT.CONSTRUCTION), "fileName"))
	I.position = positionOf(con, id)
	local town = try("getConstructionClosestTown", function()
		return api.engine.system.streetConnectorSystem.getConstructionClosestTown(con)
	end)
	if town ~= nil then I.town, I.townId = nameOf(town), town end
	local info = try("getIndustryProductivityInfo", function()
		return api.engine.util.industry.getIndustryProductivityInfo(id)
	end)
	if info then I.producing = get(info, "producing") end
	local sl = get(ind, "stockList")
	if sl == nil then return I end
	I.productionRating = num(try("getProductionRating", function() return stock().getProductionRating(sl) end))
	local io = try("getInputsOutputsFromRules", function() return stock().getInputsOutputsFromRules(sl) end)
	local seen, inputs, outputs = {}, {}, {}
	local ins, outs = io and at(io, 1), io and at(io, 2)
	for k = 1, size(ins) do
		local c = at(ins, k)
		if c ~= nil and not seen["i" .. c] then
			seen["i" .. c] = true
			inputs[#inputs + 1] = {
				cargo = cargoName(c),
				deliveredPerYear = num(try("delivered", function() return stock().getCargoTypeDeliveredPerYear(sl, c) end)),
				consumedPerYear = num(try("consumed", function() return stock().getCargoConsumedPerYear(sl, c) end)),
				maxConsumptionPerYear = num(try("maxConsumption", function() return stock().getCargoMaxConsumptionPerYear(sl, c) end)),
			}
		end
	end
	for k = 1, size(outs) do
		local c = at(outs, k)
		if c ~= nil and not seen["o" .. c] then
			seen["o" .. c] = true
			outputs[#outputs + 1] = {
				cargo = cargoName(c),
				producedPerYear = num(try("produced", function() return stock().getCargoProducedPerYear(sl, c) end)),
				maxProductionPerYear = num(try("maxProduction", function() return stock().getCargoMaxProductionPerYear(sl, c) end)),
				shippedPerYear = num(try("shipped", function() return stock().getCargoTypeShippedPerYear(sl, c) end)),
			}
			local Q = collect.quality(try("getCargoQualityDataForStockList", function()
				return api.engine.util.cargo.getCargoQualityDataForStockList(sl, c)
			end))
			if Q then
				local o = outputs[#outputs]
				o.waiting, o.pastDue, o.quality = Q.count, Q.pastDue, Q.quality
			end
		end
	end
	if #inputs > 0 then I.inputs = inputs end
	if #outputs > 0 then I.outputs = outputs end
	return I
end

M.jobs.industries = {
	start = function(ctx)
		ctx.closing = {}
		local closing = try("getClosingIndustries", function() return api.engine.util.industry.getClosingIndustries() end)
		for i = 1, size(closing) do
			local pair = at(closing, i)
			local e = pair and at(pair, 1)
			if e ~= nil then ctx.closing[e] = true end
		end
		local list = toArray(try("industries", function() return api.engine.getEntitiesWithComponent(CT.INDUSTRY) end))
		table.sort(list)
		return { { key = "industries", items = list, fn = industry } }
	end,
}

---------------------------------------------------------------------------------------------------------------------
-- stations (per station group) and depots

-- cargo ids a line carries, from its vehicles' capacities (indexed by cargo id + 1)
local function lineCargos(line, ctx)
	local cached = ctx.lineCargo[line]
	if cached then return cached end
	local set, out = {}, {}
	for _, v in ipairs(toArray(try("getLineVehicles", function()
		return api.engine.system.transportVehicleSystem.getLineVehicles(line)
	end))) do
		local caps = get(comp(v, CT.TRANSPORT_VEHICLE), "config", "capacities")
		for i = 1, size(caps) do
			if (num(at(caps, i)) or 0) > 0 and not set[i - 1] then set[i - 1], out[#out + 1] = true, i - 1 end
		end
	end
	table.sort(out)
	ctx.lineCargo[line] = out
	return out
end

local function waitingAt(group, c)
	local q = try("getCargoQualityDataAtStationGroup", function()
		return api.engine.util.cargo.getCargoQualityDataAtStationGroup(group, c)
	end)
	if q == nil then return nil end
	return num(get(q, "countTotal")), get(q, "isVeryBad"), num(get(q, "averageQuality")), collect.quality(q)
end

local function stationGroup(entry, ctx)
	local g, stations = entry[1], entry[2]
	local S = { id = g, name = nameOf(g), stations = #stations }
	local terminals, used, overflow, pool, termCap, cargoStation, passengerStation = 0, 0, 0, 0, 0, false, false
	for _, st in ipairs(stations) do
		local c = comp(st, CT.STATION)
		local terms = get(c, "terminals")
		terminals = terminals + size(terms)
		for k = 1, size(terms) do
			local t = at(terms, k)
			if get(t, "cargoLoad") or get(t, "cargoUnload") then cargoStation = true end
			if get(t, "passengersLoad") or get(t, "passengersUnload") then passengerStation = true end
		end
		if S.position == nil then S.position = xy(get(c, "catchmentAreas", 1, "position")) end
		local u = try("calculateStationUsage", function() return api.engine.util.station.calculateStationUsage(st) end)
		if u then
			used = used + (num(get(u, "totalUsed")) or 0)
			overflow = overflow + (num(get(u, "overflow")) or 0)
			pool = pool + (num(get(u, "poolCapacity")) or 0)
			termCap = termCap + (num(get(u, "terminalCapacity")) or 0)
		end
	end
	S.terminals = terminals
	S.kind = (cargoStation and passengerStation) and "mixed" or (cargoStation and "cargo")
		or (passengerStation and "passenger") or nil
	S.usage = { used = used, overflow = overflow, poolCapacity = pool, terminalCapacity = termCap }
	local town = try("getTown", function() return api.engine.system.stationSystem.getTown(stations[1]) end)
	if town ~= nil then S.town, S.townId = nameOf(town), town end
	local lines, cargos, seen = {}, {}, {}
	for _, line in ipairs(toArray(try("getLinesForStationGroup", function()
		return api.engine.system.lineSystem.getLinesForStationGroup(g)
	end))) do
		lines[#lines + 1] = { id = line, name = nameOf(line) }
		for _, c in ipairs(lineCargos(line, ctx)) do
			if not seen[c] then seen[c], cargos[#cargos + 1] = true, c end
		end
	end
	S.lines = lines
	local waiting, anyWaiting, veryBad, pastDue = {}, false, {}, {}
	local stops = toArray(try("getLineStops", function() return api.engine.system.lineSystem.getLineStops(g) end))
	for _, c in ipairs(cargos) do
		local n = 0
		for _, pair in ipairs(stops) do
			local line, stopIndex = at(pair, 1), at(pair, 2)
			n = n + (num(try("getLineStopSimEntitiesCount", function()
				return api.engine.system.simEntityAtTerminalSystem.getLineStopSimEntitiesCount(line, stopIndex, c)
			end)) or 0)
		end
		local q, bad, _, Q = waitingAt(g, c)
		n = math.max(n, q or 0)
		if Q and Q.pastDue > 0 then pastDue[cargoName(c)] = Q end
		if n > 0 then
			waiting[cargoName(c)], anyWaiting = n, true
			if bad then veryBad[#veryBad + 1] = cargoName(c) end
		end
	end
	if anyWaiting then S.waiting = waiting end
	if #veryBad > 0 then S.longWaits = veryBad end
	if next(pastDue) ~= nil then S.pastDue = pastDue end
	return S
end

local function depot(d, ctx)
	local c = comp(d, CT.VEHICLE_DEPOT)
	local D = { id = d, name = nameOf(d) }
	local modes = {}
	eachPair(get(c, "transportModes"), function(mode, on)
		if on then modes[#modes + 1] = collect.modeName(mode) end
	end)
	local m = table.concat(modes, ",")
	D.carrier = (m:find("TRAM") and "TRAM") or (m:find("TRAIN") and "RAIL") or ((m:find("SHIP")) and "WATER")
		or ((m:find("AIRCRAFT") or m:find("HELICOPTER")) and "AIR") or ((m:find("BUS") or m:find("TRUCK")) and "ROAD")
		or collect.carrierName(get(c, "carrier"))
	D.position = xy(get(c, "catchmentArea", "position")) or positionOf(d)
	D.vehiclesInside = size(try("getDepotVehicles", function()
		return api.engine.system.transportVehicleSystem.getDepotVehicles(d)
	end))
	return D
end

M.jobs.stations = {
	start = function(ctx)
		ctx.lineCargo = {}
		local groups, order = {}, {}
		for _, st in ipairs(owned(CT.STATION, ctx.player)) do
			local g = try("getStationGroup", function() return api.engine.system.stationGroupSystem.getStationGroup(st) end)
			if g ~= nil then
				if groups[g] == nil then groups[g], order[#order + 1] = {}, g end
				table.insert(groups[g], st)
			end
		end
		table.sort(order)
		local items = {}
		for _, g in ipairs(order) do items[#items + 1] = { g, groups[g] } end
		local depots = owned(CT.VEHICLE_DEPOT, ctx.player)
		table.sort(depots)
		return { { key = "stations", items = items, fn = stationGroup }, { key = "depots", items = depots, fn = depot } }
	end,
}

---------------------------------------------------------------------------------------------------------------------
-- finances: account, company value, finance table (per carrier and category, as the game's finance window)

local function plain(v, depth)
	depth = depth or 0
	if depth > 4 then return nil end
	if type(v) == "number" or type(v) == "string" or type(v) == "boolean" then return v end
	local out, n = {}, size(v)
	if n > 0 then
		for i = 1, n do out[i] = plain(at(v, i), depth + 1) end
		return out
	end
	local any = false
	eachPair(v, function(k, x)
		out[tostring(k)] = plain(x, depth + 1)
		any = true
	end)
	return any and out or nil
end

local COMPANY_FIELDS = { "totalScore", "railVehicles", "trams", "roadVehicles", "aircrafts", "ships", "trackTotalLength",
	"trackElectricLength", "bridgeTotalLength", "tunnelTotalLength", "roadTotalLength", "suppliedTowns",
	"connectedIndustries", "numberOfLines", "totalStations", "railStations", "tramStations", "roadStations",
	"aircraftStations", "shipStations", "topSpeed", "topLength", "balance", "totalAssets", "debt" }

local function finances(_, ctx)
	local F = {}
	local player = ctx.player
	F.balance = num(try("getPlayersBalance", function() return api.engine.util.finance.getPlayersBalance(player) end))
	local acc = comp(player, CT.ACCOUNT)
	if acc then F.loan = num(get(acc, "loan")) end
	F.earningsThisYear = num(try("calculateEarnings", function() return api.engine.util.finance.calculateEarnings(player) end))
	local cv = try("getCompaniesValue", function() return api.engine.util.headquarters.getCompaniesValue() end)
	if cv then
		local company = {}
		for _, k in ipairs(COMPANY_FIELDS) do company[k] = num(get(cv, k)) end
		F.company = company
	end
	local td = try("getTransportedData", function() return api.engine.util.headquarters.getTransportedData() end)
	if td then
		F.transported = { passengers = num(get(td, "passengersTransported")), cargo = num(get(td, "cargoTransported")) }
	end
	-- the finance window's table: one column per year (best effort; the layout is undocumented)
	local fd = try("computeFinanceTable", function()
		local cfg = api.type.ChartConfig.new()
		cfg.interval = ctx.yearTicks and math.floor(ctx.yearTicks) or cfg.interval
		cfg.count = 5
		return api.engine.util.finance.computeFinanceTable(player, cfg)
	end)
	if fd then
		local t = {}
		for _, k in ipairs({ "header", "total", "balance", "loan", "interest", "loanBorrowing", "loanRepayment" }) do
			t[k] = plain(get(fd, k))
		end
		local carriers = {}
		pcall(function()
			fd:foreach_carrier(function(carrier, map)
				carriers[tostring(carrier)] = plain(map)
			end)
		end)
		t.transportByCarrier = carriers
		t.investment = plain(get(fd, "investment"))
		F.financeTable = t
	end
	return F
end

M.jobs.finances = {
	start = function(ctx)
		return { { key = "finances", items = { 1 }, fn = finances } }
	end,
}

---------------------------------------------------------------------------------------------------------------------
-- vehicles live: position, state and recent departures of every line vehicle (for bunching and stuck vehicles)

local function liveVehicle(item, ctx)
	local v, line = item[1], item[2]
	local tv = comp(v, CT.TRANSPORT_VEHICLE)
	local V = { id = v, line = line, lineName = ctx.lineNames[line], name = nameOf(v) }
	V.position = xy(try("getPosition", function() return api.engine.util.vehicle.getPosition(v) end))
	V.speed = num(try("getSpeed", function() return api.engine.util.vehicle.getSpeed(v) end))
	if tv then
		V.state = collect.stateName(get(tv, "state"))
		V.stopIndex = num(get(tv, "stopIndex"))
		V.doorsOpen = get(tv, "doorsOpen")
		V.noPath = get(tv, "noPath")
		V.userStopped = get(tv, "userStopped")
		V.timeUntilDeparture = num(get(tv, "timeUntilDeparture"))
		V.lastDeparture = num(get(tv, "lastLineStopDeparture"))
		local deps = get(tv, "lineStopDepartures")
		local list = {}
		for i = 1, size(deps) do list[i] = num(at(deps, i)) or 0 end
		V.stopDepartures = list
		V.daysAtTerminal = num(get(tv, "daysAtTerminal"))
	end
	V.load = num(try("getVehicleSimEntitiesCount", function()
		return api.engine.system.simEntityAtVehicleSystem.getVehicleSimEntitiesCount(v)
	end))
	return V
end

M.jobs.vehicles = {
	start = function(ctx)
		local items = {}
		ctx.lineNames = {}
		for _, line in ipairs(toArray(try("getLinesForPlayer", function()
			return api.engine.system.lineSystem.getLinesForPlayer(ctx.player)
		end))) do
			ctx.lineNames[line] = nameOf(line)
			for _, v in ipairs(toArray(try("getLineVehicles", function()
				return api.engine.system.transportVehicleSystem.getLineVehicles(line)
			end))) do
				items[#items + 1] = { v, line }
			end
		end
		return { { key = "vehicles", items = items, fn = liveVehicle } }
	end,
}

---------------------------------------------------------------------------------------------------------------------
-- warehouses: what is stored now against capacity, and the delivered and shipped rates per cargo. Cargo that keeps
-- arriving but does not leave is cargo stuck in the warehouse, which no other kind shows.

local STOCK_TYPES = { "InputStock", "OutputStock", "StorageStock" }

local function stockTypeName(v) return collect.enumName("StockListType", STOCK_TYPES, v) end

-- how much of each cargo the given stock slots hold, asking the stock system through `holder`
local function countPerCargo(holder, stockIds, cargoIds)
	local held, sum = {}, 0
	for k = 1, size(stockIds) do
		local sid = at(stockIds, k)
		for j = 1, size(cargoIds) do
			local c = at(cargoIds, j)
			local n = num(try("getStockCountForCargoType", function()
				return api.engine.system.simEntityAtStockSystem.getStockCountForCargoType(holder, sid, c)
			end)) or 0
			if n > 0 then held[c], sum = (held[c] or 0) + n, sum + n end
		end
	end
	return held, sum
end

-- what a slot group holds per cargo. A group for one cargo holds only that; a group that takes several cargo types
-- is asked per cargo. The API does not say which entity holds the stock (the warehouse, its stock list or its
-- construction), so each is tried and the one whose counts add up to the group's total is kept for the request.
local function heldInGroup(ctx, holders, stockIds, cargoIds, count)
	if size(cargoIds) == 1 then return { [at(cargoIds, 1)] = count } end
	if count <= 0 then return {} end
	if ctx.holderIndex then
		local held = countPerCargo(holders[ctx.holderIndex], stockIds, cargoIds)
		return held
	end
	for i, h in ipairs(holders) do
		local held, sum = countPerCargo(h, stockIds, cargoIds)
		if sum == count then
			ctx.holderIndex = i
			return held
		end
	end
	return nil -- contents of this mixed slot could not be resolved
end

local function warehouse(id, ctx)
	local W = { id = id, name = nameOf(id) }
	local wh = comp(id, CT.WAREHOUSE)
	if wh == nil then return W end
	local con = get(wh, "construction")
	W.type = shortRes(get(comp(con, CT.CONSTRUCTION), "fileName"))
	W.position = positionOf(con, id)
	local town = try("getConstructionClosestTown", function()
		return api.engine.system.streetConnectorSystem.getConstructionClosestTown(con)
	end)
	if town ~= nil then W.town, W.townId = nameOf(town), town end
	local sl = get(wh, "stockList")
	if sl == nil then return W end
	W.stockListId = sl
	local list = comp(sl, CT.STOCK_LIST)
	-- cargo the warehouse had to throw away: it was full and kept being delivered to
	local thrown = ctx.thrownAway[sl] or num(get(list, "thrownAwayCargo"))
	if thrown and thrown > 0 then W.thrownAwayCargo = thrown end
	local slotCapacity, stocks = {}, get(list, "stocks")
	for i = 1, size(stocks) do slotCapacity[i - 1] = num(get(at(stocks, i), "capacity")) or 0 end
	-- one group per stock type and cargo set, with how much is held right now (empty groups included, so the
	-- configured capacity shows even when nothing is in it)
	local holders = { id, sl, con }
	local groups, waiting, accepted, acceptedOrder, holding = {}, 0, {}, {}, {}
	for _, g in ipairs(toArray(try("getStockGroupsByTypeAndCargoTypes", function()
		return stock().getStockGroupsByTypeAndCargoTypes(sl, true, true, false)
	end))) do
		local ids, cargoIds, count = at(g, 2), at(g, 3), num(at(g, 4)) or 0
		local capacity = 0
		for k = 1, size(ids) do capacity = capacity + (slotCapacity[at(ids, k)] or 0) end
		for k = 1, size(cargoIds) do
			local c = at(cargoIds, k)
			if c ~= nil and not accepted[c] then accepted[c], acceptedOrder[#acceptedOrder + 1] = true, c end
		end
		local G = { stockType = stockTypeName(at(g, 1)), waiting = count, capacity = capacity, slots = size(ids) }
		local held = heldInGroup(ctx, holders, ids, cargoIds, count)
		if size(cargoIds) > 1 then G.mixed, G.accepts = true, size(cargoIds) end
		if held == nil then
			G.contentsUnknown = true
		else
			local names, per = {}, {}
			for c, n in pairs(held) do
				local cn = cargoName(c)
				if cn then
					names[#names + 1], per[cn] = cn, n
					if n > 0 then holding[cn] = (holding[cn] or 0) + n end
				end
			end
			table.sort(names)
			G.cargos = names
			if G.mixed and #names > 0 then G.held = per end
		end
		if not G.mixed and G.cargos == nil then G.cargos = { cargoName(at(cargoIds, 1)) } end
		groups[#groups + 1] = G
		waiting = waiting + count
	end
	if #groups > 0 then W.stocks = groups end
	W.waitingTotal = waiting
	if next(holding) ~= nil then W.holding = holding end
	-- logbook names on the stock list (as for lines), to read per-cargo log values below
	local logNames = {}
	eachPair(get(comp(sl, CT.LOG_BOOK), "name2log"), function(k) logNames[#logNames + 1] = tostring(k) end)
	table.sort(logNames)
	if #logNames > 0 then W.logNames = logNames end
	-- per cargo over the last 12 months, only for cargo that is held now or moved
	local flows = {}
	for _, c in ipairs(acceptedOrder) do
		local cn = cargoName(c)
		local F = {
			cargo = cn,
			deliveredPerYear = num(try("delivered", function() return stock().getCargoTypeDeliveredPerYear(sl, c) end)),
			shippedPerYear = num(try("shipped", function() return stock().getCargoTypeShippedPerYear(sl, c) end)),
		}
		local logs = {}
		for _, name in ipairs(logNames) do
			local v = num(try("getCargoLogValueDiffPerYear", function()
				return stock().getCargoLogValueDiffPerYear(sl, name, c)
			end))
			if v and v ~= 0 then logs[name] = v end
		end
		if next(logs) ~= nil then F.logsPerYear = logs end
		local Q = collect.quality(try("getCargoQualityDataForStockList", function()
			return api.engine.util.cargo.getCargoQualityDataForStockList(sl, c)
		end))
		if Q then F.pastDue, F.quality, F.veryBad = Q.pastDue, Q.quality, Q.veryBad end
		if (holding[cn] or 0) > 0 or (F.deliveredPerYear or 0) > 0 or (F.shippedPerYear or 0) > 0 or F.logsPerYear then
			if ctx.deliveryTime[c] == nil then
				ctx.deliveryTime[c] = num(try("getAdjustedDeliveryTime", function()
					return stock().getAdjustedDeliveryTime(c)
				end)) or false
			end
			F.deliveryTimeSec = ctx.deliveryTime[c] or nil
			flows[#flows + 1] = F
		end
	end
	if #flows > 0 then W.flows = flows end
	W.deliveredPerYear = num(try("deliveredTotal", function() return stock().getCargoDeliveredPerYear(sl) end))
	W.shippedPerYear = num(try("shippedTotal", function() return stock().getCargoShippedPerYear(sl) end))
	local sq = try("getSummarizedCargoQualityDataForStockList", function()
		return api.engine.util.cargo.getSummarizedCargoQualityDataForStockList(sl)
	end)
	local Q = sq ~= nil and collect.quality(get(sq, "cargo")) or nil
	if Q then W.pastDue, W.quality = Q.pastDue, Q.quality end
	return W
end

-- the player's warehouses. Unlike a station, a warehouse entity need not carry PLAYER_OWNED itself (its construction
-- may), so the owner filter of getEntitiesWithComponent would drop them all: the owner is looked up on the warehouse,
-- then on its construction, and a warehouse with no owner recorded on either is kept.
local function ownerOf(e)
	if e == nil then return nil end
	return get(comp(e, CT.PLAYER_OWNED), "player")
end

local function playerWarehouses(player)
	local all = toArray(try("getEntitiesWithComponent", function()
		return api.engine.getEntitiesWithComponent(CT.WAREHOUSE)
	end))
	local out = {}
	for _, e in ipairs(all) do
		local owner = ownerOf(e)
		if owner == nil then owner = ownerOf(get(comp(e, CT.WAREHOUSE), "construction")) end
		if owner == nil or owner == player then out[#out + 1] = e end
	end
	if #out == 0 then
		-- none found: say why in the diagnostics (warehouse entities at all, and warehouse constructions)
		local names, cons = {}, 0
		for _, con in ipairs(toArray(try("constructions", function()
			return api.engine.getEntitiesWithComponent(CT.CONSTRUCTION)
		end))) do
			local f = get(comp(con, CT.CONSTRUCTION), "fileName")
			local lf = type(f) == "string" and f:lower() or ""
			if lf:find("warehouse") or lf:find("storage") then
				cons = cons + 1
				if #names < 3 then names[#names + 1] = f end
			end
		end
		collect.note("warehouses", string.format("%d entities with a WAREHOUSE component, %d kept as yours; "
			.. "%d warehouse constructions (%s)", #all, #out, cons, table.concat(names, ", ")))
	end
	return out
end

M.jobs.warehouses = {
	start = function(ctx)
		ctx.thrownAway, ctx.deliveryTime = {}, {}
		eachPair(try("getStockListsWithThrownAwayCargo", function()
			return stock().getStockListsWithThrownAwayCargo()
		end), function(e, amount)
			local n = num(amount)
			if n and n > 0 then ctx.thrownAway[e] = n end
		end)
		local list = playerWarehouses(ctx.player)
		table.sort(list)
		return { { key = "warehouses", items = list, fn = warehouse } }
	end,
}

return M
