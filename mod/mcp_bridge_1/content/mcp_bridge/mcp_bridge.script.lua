-- MCP Bridge: answers requests from a local MCP server with the player's lines, vehicles, towns and buyable vehicle
-- models, so an AI assistant can analyse the network with live data. Read-only: never sends a command to the game.
--
-- The GUI Lua has no io library, so files go through the game's own userdata functions (app.saveUserdata and
-- app.loadUserdata), in a folder the game owns (mods may not create folders): the first of USERDATA_DIRS where a
-- saved file loads back. In this game build saveUserdata never overwrites a file and getAllUserdata does not show
-- new files, so every answer gets new file names and nothing is listed or removed here; the server (plain file
-- access) replaces the request and cleans up old answers. Files "<name>.lua" in that folder:
--   mcp_bridge__hello_<stamp>          written at load: bridge version; the server finds the folder by it
--   mcp_bridge__request_<stamp>        created here once per load (a new name: the game refuses names it has seen),
--                                       then rewritten by the server: { id = <number>, what = ... }; read every
--                                       3 s (twice a second for 2 minutes after a request); a new id is a new
--                                       request (what: one of KINDS)
--   mcp_bridge__<what>_<id>_p1 .. _pN   the answer as JSON split in parts: { seq = id, part = i, data = "<json>" }
--   mcp_bridge__<what>_<id>             written last: { seq = id, what = what, parts = N, bytes = <length> }
-- Collecting and writing are sliced: each frame spends at most the "budget" setting in CPU time.

local MOD = (getOwningModId and getOwningModId()) or "mcp_bridge_1"
local json = ug_require(MOD .. "::/mcp_bridge/mcp_bridge_json.lua")
local collect = ug_require(MOD .. "::/mcp_bridge/mcp_bridge_collect.lua")
local extra = ug_require(MOD .. "::/mcp_bridge/mcp_bridge_extra.lua")

local VERSION = 6
local USERDATA_DIRS = { "mod_presets", "recordings", "keyframes" }
local PREFIX = "mcp_bridge__"
local PART_CHARS = 200000
local POLL_SEC = 0.5 -- while requests are coming in
local POLL_IDLE_SEC = 3 -- otherwise: each check of the request file costs a few ms, so it is rare when nobody asks
local ACTIVE_SEC = 120 -- how long after a request the fast checks continue
-- request kinds, in the order queued requests are served
local KINDS = { "snapshot", "models", "industries", "stations", "finances", "vehicles", "warehouses" }
local KNOWN = {}
for _, k in ipairs(KINDS) do KNOWN[k] = true end

-- mod settings (mod.json params): each arrives as a 1-based index into these lists
local OPTION_VALUES = {
	budget = { 0.00075, 0.0015, 0.003 }, -- CPU seconds of work per frame: light, normal, fast
	towns = { true, false },
	logEach = { false, true },
}
local OPTION_DEFAULT = { budget = 2 }

local options
local function opt(key)
	if options == nil then
		local ok, all = pcall(function() return api.engine.config.getModParams() end)
		local raw = {}
		pcall(function() raw = (ok and all and all[MOD]) or {} end)
		options = {}
		for k, values in pairs(OPTION_VALUES) do
			local idx = nil
			pcall(function() idx = raw[k] end)
			local v = values[idx or OPTION_DEFAULT[k] or 1]
			if v == nil then v = values[OPTION_DEFAULT[k] or 1] end
			options[k] = v
		end
	end
	return options[key]
end

local function say(msg)
	local text = "[mcp_bridge] " .. msg
	if debugPrint then pcall(debugPrint, text) else print(text) end
end

local G = {
	phase = "init", -- init, idle, lines, towns, encode, models, job, flush, off
	nextPoll = 0,
	failures = 0,
	queue = {}, -- pending requests: what -> id
	jobMaxMs = 0, -- slowest frame of the current request (collect, encode and write)
	pollMaxMs = 0, -- slowest idle check of the request file since the last answer
	pollMs = 0, -- total time of the idle checks since the last answer (for the average)
	pollCount = 0,
	jobMaxPhase = nil, -- the step that took the slowest frame
}

local function appTime()
	local ok, t = pcall(api.util.getApplicationTime)
	if ok and type(t) == "number" then return t end
	return os.clock()
end

---------------------------------------------------------------------------------------------------------------------
-- userdata files

local function trySave(dir, name, tbl)
	return pcall(function() app.saveUserdata(dir, name, tbl) end)
end

local function tryLoad(dir, name)
	local ok, v = pcall(function() return app.loadUserdata(dir, name) end)
	if ok then return v end
	return nil
end

local function field(t, key)
	local ok, v = pcall(function() return t[key] end)
	if ok then return v end
	return nil
end

-- what getAllUserdata returns, once, for the log (it did not show new files in game build 40420)
local function listingNote(dir)
	local ok, all = pcall(function() return app.getAllUserdata(dir) end)
	if not ok then return "error " .. tostring(all) end
	local n, sample = collect.size(all), {}
	for i = 1, math.min(n, 3) do sample[#sample + 1] = tostring(collect.at(all, i)) end
	return string.format("%s with %d entries: %s", type(all), n, table.concat(sample, ", "))
end

local function findDir()
	if type(app) ~= "table" and type(app) ~= "userdata" then return nil, "app is not available" end
	local stamp = string.format("%d_%d", os.time and os.time() or 0, math.floor(appTime() * 1000) % 1000000)
	for _, dir in ipairs(USERDATA_DIRS) do
		local name = PREFIX .. "hello_" .. stamp
		trySave(dir, name, { bridgeVersion = VERSION, mod = MOD, writtenAtUnix = os.time and os.time() or nil })
		local back = tryLoad(dir, name)
		if back and field(back, "bridgeVersion") == VERSION then return dir, stamp end
	end
	return nil, "no folder kept a saved file"
end

---------------------------------------------------------------------------------------------------------------------
-- requests and answers

local function init()
	-- the MCP server runs on a PC next to the game; on consoles there is nothing to answer
	local okC, console = pcall(function() return api.util.isConsoleMode() end)
	if okC and console then
		say("console detected; the bridge is off (it needs the MCP server on a PC)")
		G.phase = "off"
		return
	end
	local dir, stamp = findDir()
	local err = stamp
	if dir == nil then
		say("cannot write userdata files (" .. tostring(err) .. "); the bridge is off")
		G.phase = "off"
		return
	end
	G.dir = dir
	-- the request file must exist before it is polled (loading a missing file logs an error)
	G.request = PREFIX .. "request_" .. stamp
	G.lastId = 0
	trySave(dir, G.request, { id = 0, what = "none" })
	if tryLoad(dir, G.request) == nil then
		say("could not create the request file; the bridge is off")
		G.phase = "off"
		return
	end
	say("ready; exchange folder: <user data>/" .. dir .. " (listing: " .. listingNote(dir) .. ")")
	G.phase = "idle"
end

-- reads the request file; a new id is queued
local function poll()
	local now = appTime()
	if now < G.nextPoll then return end
	G.nextPoll = now + ((now - (G.lastRequestAt or -1e9) < ACTIVE_SEC) and POLL_SEC or POLL_IDLE_SEC)
	local t0 = os.clock()
	local req = tryLoad(G.dir, G.request)
	local pollMs = (os.clock() - t0) * 1000
	G.pollMaxMs = math.max(G.pollMaxMs, pollMs)
	G.pollMs, G.pollCount = G.pollMs + pollMs, G.pollCount + 1
	if req == nil then
		G.nextPoll = now + 10 -- missing or being rewritten; each failed load logs an error, so retry slowly
		return
	end
	local id, what = tonumber(field(req, "id")), field(req, "what")
	if id == nil or id == 0 or id == G.lastId then return end
	G.lastId = id
	G.lastRequestAt = now
	if not KNOWN[what] then what = "snapshot" end
	if not G.sawRequest then
		say("first request received: " .. what .. " " .. tostring(id))
		G.sawRequest = true
	end
	G.queue[what] = id
end

local function startFlush(what, id, text)
	local parts = {}
	for i = 1, #text, PART_CHARS do parts[#parts + 1] = text:sub(i, i + PART_CHARS - 1) end
	if #parts == 0 then parts[1] = "" end
	G.flush = { what = what, id = id, parts = parts, index = 1, bytes = #text, maxMs = 0,
		base = PREFIX .. what .. "_" .. string.format("%d", id) }
	G.phase = "flush"
end

local function stepFlush(deadline)
	local f = G.flush
	while f.index <= #f.parts do
		local t0 = os.clock()
		local name = f.base .. "_p" .. f.index
		local ok, err = trySave(G.dir, name, { seq = f.id, part = f.index, data = f.parts[f.index] })
		if not ok then error("saving " .. name .. " failed: " .. tostring(err)) end
		f.maxMs = math.max(f.maxMs, (os.clock() - t0) * 1000)
		f.index = f.index + 1
		if os.clock() >= deadline then return end
	end
	local ok, err = trySave(G.dir, f.base, { seq = f.id, what = f.what, parts = #f.parts, bytes = f.bytes })
	if not ok then error("saving the " .. f.what .. " manifest failed: " .. tostring(err)) end
	if not G.announced or opt("logEach") then
		say(string.format("answered %s request %s: %d bytes in %d parts, slowest part %.1f ms, slowest frame %.1f ms, "
			.. "slowest idle check %.2f ms%s", f.what, tostring(f.id), f.bytes, #f.parts, f.maxMs,
			math.max(G.jobMaxMs, f.maxMs), G.pollMaxMs, f.note or ""))
		G.announced = true
	end
	G.failures = 0
	G.flush = nil
	G.jobMaxMs, G.pollMaxMs, G.pollMs, G.pollCount, G.jobMaxPhase = 0, 0, 0, 0, nil
	G.phase = "idle"
end

---------------------------------------------------------------------------------------------------------------------
-- snapshot: lines (with vehicles and stop towns), towns

local function startSnapshot(id)
	local player = api.engine.util.getPlayer()
	local t = collect.timeInfo()
	local ctx = {
		id = id, player = player, gameTime = t.gameTime, yearTicks = t.yearTicks, dayTicks = t.dayTicks, date = t.date,
		time = t, cpu = 0, frames = 0,
	}
	ctx.lines = collect.toArray(collect.try("getLinesForPlayer", function()
		return api.engine.system.lineSystem.getLinesForPlayer(player)
	end))
	ctx.problems = collect.lineProblems(player)
	if G.logNames == nil then
		for i = 1, math.min(#ctx.lines, 5) do
			local names = collect.discoverLineLogs(ctx.lines[i])
			if names then G.logNames = names; break end
		end
	end
	ctx.logNames = G.logNames
	ctx.money = collect.try("getPlayersBalance", function() return api.engine.util.finance.getPlayersBalance(player) end)
	ctx.station2town, ctx.group2town = collect.station2town(), {}
	ctx.townList, ctx.townChunks, ctx.townIndex = opt("towns") and collect.townList() or {}, {}, 1
	ctx.chunks, ctx.index, ctx.vehicles = {}, 1, 0
	G.ctx = ctx
	G.phase = "lines"
end

local function stepLines(deadline)
	local ctx = G.ctx
	while ctx.index <= #ctx.lines do
		local L = collect.line(ctx.lines[ctx.index], ctx)
		ctx.vehicles = ctx.vehicles + (L.vehicleCount or 0)
		ctx.chunks[#ctx.chunks + 1] = json.encode(L)
		ctx.index = ctx.index + 1
		if os.clock() >= deadline then return end
	end
	G.phase = "towns"
end

local function stepTowns(deadline)
	local ctx = G.ctx
	while ctx.townIndex <= #ctx.townList do
		ctx.townChunks[#ctx.townChunks + 1] = json.encode(collect.town(ctx.townList[ctx.townIndex], ctx))
		ctx.townIndex = ctx.townIndex + 1
		if os.clock() >= deadline then return end
	end
	G.phase = "encode"
end

local function encodeSnapshot()
	local ctx = G.ctx
	ctx.frames = ctx.frames + 1 -- this frame
	local meta = {
		bridgeVersion = VERSION,
		requestId = ctx.id,
		gameTime = ctx.gameTime,
		date = ctx.date,
		money = ctx.money,
		time = ctx.time,
		lineCount = #ctx.lines,
		vehicleCount = ctx.vehicles,
		townCount = #ctx.townChunks,
		collectFrames = ctx.frames,
		collectCpuMs = math.floor(ctx.cpu * 1000 + 0.5),
		slowestFrameMs = math.floor(G.jobMaxMs * 10 + 0.5) / 10,
		slowestIdleCheckMs = math.floor(G.pollMaxMs * 100 + 0.5) / 100,
		averageIdleCheckMs = G.pollCount > 0 and math.floor(G.pollMs / G.pollCount * 100 + 0.5) / 100 or nil,
		slowestFramePhase = G.jobMaxPhase,
		lineLogNames = ctx.logNames,
		writtenAtUnix = os.time and os.time() or nil,
		diagnostics = collect.diag,
	}
	local text = '{"meta":' .. json.encode(meta) .. ',"lines":' .. json.array(ctx.chunks)
		.. ',"towns":' .. json.array(ctx.townChunks) .. '}'
	G.ctx = nil
	startFlush("snapshot", ctx.id, text)
	G.flush.note = string.format(" (%d lines, %d vehicles, collected in %d frames, %d ms CPU)", #ctx.lines,
		ctx.vehicles, ctx.frames, math.floor(ctx.cpu * 1000 + 0.5))
end

---------------------------------------------------------------------------------------------------------------------
-- models (vehicle models with price and availability)

local function startModels(id)
	local t = collect.timeInfo()
	G.models = { id = id, names = collect.vehicleModelNames(), index = 1, chunks = {}, gameTime = t.gameTime, date = t.date }
	G.phase = "models"
end

local function stepModels(deadline)
	local m = G.models
	while m.index <= #m.names do
		local e = collect.modelEntry(m.names[m.index], m)
		if e then m.chunks[#m.chunks + 1] = json.encode(e) end
		m.index = m.index + 1
		if os.clock() >= deadline then return end
	end
	local meta = { bridgeVersion = VERSION, requestId = m.id, date = m.date, gameTime = m.gameTime, modelCount = #m.chunks }
	G.models = nil
	startFlush("models", m.id, '{"meta":' .. json.encode(meta) .. ',"models":' .. json.array(m.chunks) .. '}')
end

---------------------------------------------------------------------------------------------------------------------
-- the other kinds (mcp_bridge_extra.lua): lists of items collected one by one, then encoded together

local function startJob(what, id)
	local t = collect.timeInfo()
	local ctx = { id = id, player = api.engine.util.getPlayer(), gameTime = t.gameTime, yearTicks = t.yearTicks,
		date = t.date, cpu = 0, frames = 0 }
	G.job = { what = what, id = id, ctx = ctx, lists = extra.jobs[what].start(ctx), li = 1, index = 1, out = {} }
	G.phase = "job"
end

local function stepJob(deadline)
	local j = G.job
	while j.li <= #j.lists do
		local L = j.lists[j.li]
		local out = j.out[j.li] or {}
		j.out[j.li] = out
		while j.index <= #L.items do
			local ok, v = pcall(L.fn, L.items[j.index], j.ctx)
			if ok and v ~= nil then
				out[#out + 1] = json.encode(v)
			elseif not ok then
				collect.note(L.key, v)
			end
			j.index = j.index + 1
			if os.clock() >= deadline then return end
		end
		j.li, j.index = j.li + 1, 1
	end
	local ctx = j.ctx
	local meta = { bridgeVersion = VERSION, requestId = j.id, date = ctx.date, gameTime = ctx.gameTime, yearTicks = ctx.yearTicks,
		collectFrames = ctx.frames + 1, collectCpuMs = math.floor(ctx.cpu * 1000 + 0.5), diagnostics = collect.diag }
	local parts = { '{"meta":' .. json.encode(meta) }
	for i, L in ipairs(j.lists) do parts[#parts + 1] = ',"' .. L.key .. '":' .. json.array(j.out[i]) end
	parts[#parts + 1] = "}"
	G.job = nil
	startFlush(j.what, j.id, table.concat(parts))
end

---------------------------------------------------------------------------------------------------------------------

local function nextRequest()
	for _, k in ipairs(KINDS) do
		local id = G.queue[k]
		if id then
			G.queue[k] = nil
			return k, id
		end
	end
	return nil
end

local function step()
	if G.phase == "init" then init(); return end
	local t0 = os.clock()
	local startPhase = G.phase
	if G.phase == "idle" then
		poll()
		local what, id = nextRequest()
		if what == nil then return end
		if what == "snapshot" then
			startSnapshot(id)
		elseif what == "models" then
			startModels(id)
		else
			startJob(what, id)
		end
	end
	local deadline = os.clock() + opt("budget")
	if G.phase == "lines" then stepLines(deadline) end
	if G.phase == "towns" and os.clock() < deadline then stepTowns(deadline) end
	if G.phase == "encode" and os.clock() < deadline then encodeSnapshot() end
	if G.phase == "models" and os.clock() < deadline then stepModels(deadline) end
	if G.phase == "job" and os.clock() < deadline then stepJob(deadline) end
	if G.phase == "flush" and os.clock() < deadline then stepFlush(deadline) end
	local ms = (os.clock() - t0) * 1000
	if ms > G.jobMaxMs then G.jobMaxMs, G.jobMaxPhase = ms, (startPhase == "idle" and "start " or "") .. G.phase end
	local busy = G.ctx or (G.job and G.job.ctx) -- still collecting
	if busy then
		busy.cpu = busy.cpu + ms / 1000
		busy.frames = busy.frames + 1
	end
end

local script = {}

function script.guiUpdate()
	if G.phase == "off" then return end
	local ok, err = pcall(step)
	if not ok then
		G.failures = G.failures + 1
		say("error (" .. G.failures .. "): " .. tostring(err))
		G.ctx, G.models, G.flush, G.job = nil, nil, nil, nil
		G.phase = G.failures >= 5 and "off" or "idle"
		if G.phase == "off" then say("too many errors; the bridge is off until the game is reloaded") end
	end
end

-- .script.lua resources expose their exports through data()
function data()
	return script
end
