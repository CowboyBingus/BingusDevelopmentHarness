-- HD2-Addon: mods/cowboybingus/bingus_test_agent
-- Bingus Test Agent. Development only: never packaged by a release pipeline.
--
-- BingusDevelopmentHarness deploys this addon for one session and removes it afterwards.
-- Through a file mailbox it runs short Lua commands inside the game and reports
-- state, so the harness can drive the game while its window is not focused.
-- Without the harness's session.txt it installs nothing.
--
-- Mailbox: %LOCALAPPDATA%/CowboyBingus/Helldivers2/TestAgent/
--   session.txt       written by the harness: id=, poll=, heartbeat=
--   cmd-<n>.lua       written atomically by the harness; run with `agent` as a local
--   res-<n>.json      one JSON object, then a line `END <n>`
--   hello.json, heartbeat.json, bye.json   same framing
--
-- Cost per frame while installed: idle frames add dt and compare (no calls, no
-- allocation). Every poll interval (default 0.1 s) one io.open of the next
-- command file (a miss allocates io.open's short error message); every
-- heartbeat interval (default 2 s) one read of session.txt and one small file
-- write. Neither is measured in game, so agent sessions are never used for mod cost.

local VERSION = 1
local KNOWN_MODS = {
    'BetterStratagemBounce', 'HellpodSteeringUnlocked', 'ReinforcementBeaconFixData', 'ConsistentVaulting',
    'ShallowWaterDive', 'SentryAimRetention', 'CorpseCollisionRepair', 'HoverPackCancel', 'EnemyIntelligence',
    'ArmoryPreviewCache', 'ClickableScrollbars', 'ArcThrowerRevampedInstalled', 'GalacticMenuHotkeyInstalled',
    'FlameDamageFixedInstalled', 'ModOptionsMenu', 'ModBindingsMenu', 'BetterLobbyManagement',
}

if rawget(_G, 'BingusTestAgent') then return rawget(_G, 'BingusTestAgent') end

local io_lib, os_lib = rawget(_G, 'io'), rawget(_G, 'os')
local io_open = type(io_lib) == 'table' and io_lib.open
local getenv = type(os_lib) == 'table' and os_lib.getenv
if type(io_open) ~= 'function' or type(getenv) ~= 'function' then return end
local os_remove = os_lib.remove
local os_time = os_lib.time

local base = getenv('LOCALAPPDATA')
if type(base) ~= 'string' or base == '' then return end
local dir = base .. '/CowboyBingus/Helldivers2/TestAgent/'

local function read_file(path)
    local file = io_open(path, 'rb')
    if not file then return nil end
    local ok, data = pcall(file.read, file, '*a')
    file:close()
    if ok then return data end
end

local function write_file(path, text)
    local file = io_open(path, 'wb')
    if not file then return false end
    local ok = pcall(file.write, file, text)
    file:close()
    return ok
end

local session_text = read_file(dir .. 'session.txt')
if type(session_text) ~= 'string' then return end
local session = {}
for key, value in session_text:gmatch('([%w_]+)=([^\r\n]*)') do session[key] = value end
if type(session.id) ~= 'string' or not session.id:match('^[%w%-]+$') then return end

local function clamp(value, low, high, default)
    value = tonumber(value) or default
    if value ~= value then return default end
    return math.max(low, math.min(high, value))
end
local poll_interval = clamp(session.poll, 0.02, 5, 0.1)
local heartbeat_interval = clamp(session.heartbeat, 0.5, 60, 2)

-- JSON -------------------------------------------------------------------

local api = {version = VERSION, session = session.id}
api.limits = {depth = 6, items = 400, bytes = 2000000}

local ESCAPES = {['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n',
    ['\r'] = '\\r', ['\t'] = '\\t'}
local function escape_char(c) return ESCAPES[c] or string.format('\\u%04x', c:byte()) end
local function encode_string(s) return '"' .. (s:gsub('[%c"\\]', escape_char)) .. '"' end

local function encode_number(n)
    if n ~= n then return '"nan"' end
    if n == math.huge then return '"inf"' end
    if n == -math.huge then return '"-inf"' end
    if n == math.floor(n) and math.abs(n) < 2^53 then return string.format('%.0f', n) end
    return string.format('%.17g', n)
end

-- Values a command returns are game objects of any shape: cycles, cdata,
-- functions. Everything is encoded; what JSON cannot hold becomes a string.
local function encoder(limits)
    local parts, size, full, seen = {}, 0, false, {}
    local function emit(text)
        if full then return end
        size = size + #text
        if size > limits.bytes then
            full = true
            parts[#parts + 1] = '"<output truncated>"'
            return
        end
        parts[#parts + 1] = text
    end
    local function value(v, depth)
        local kind = type(v)
        if kind == 'nil' then emit('null')
        elseif kind == 'boolean' then emit(v and 'true' or 'false')
        elseif kind == 'number' then emit(encode_number(v))
        elseif kind == 'string' then emit(encode_string(v))
        elseif kind == 'cdata' then
            local number = tonumber(v)
            if number then emit(encode_number(number)) else emit(encode_string(tostring(v))) end
        elseif kind == 'table' then
            if seen[v] then emit('"<cycle>"') return end
            if depth >= limits.depth then emit('"<table>"') return end
            seen[v] = true
            local count, keys = 0, {}
            for key in pairs(v) do
                count = count + 1
                keys[count] = key
            end
            local length, array = #v, count > 0 and count == #v
            if array then
                for i = 1, count do
                    local key = keys[i]
                    if type(key) ~= 'number' or key < 1 or key > length or key ~= math.floor(key) then
                        array = false
                        break
                    end
                end
            end
            if array then
                emit('[')
                for i = 1, math.min(length, limits.items) do
                    if i > 1 then emit(',') end
                    value(v[i], depth + 1)
                end
                if length > limits.items then emit(',"<' .. (length - limits.items) .. ' more>"') end
                emit(']')
            else
                table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
                emit('{')
                for i = 1, math.min(count, limits.items) do
                    local key = keys[i]
                    if i > 1 then emit(',') end
                    emit(encode_string(tostring(key)))
                    emit(':')
                    value(v[key], depth + 1)
                end
                if count > limits.items then emit(',"<more>":' .. (count - limits.items)) end
                emit('}')
            end
            seen[v] = nil
        else
            emit(encode_string(tostring(v)))
        end
    end
    return function(v)
        value(v, 0)
        return table.concat(parts)
    end
end

function api.encode(v, limits)
    return encoder(limits or api.limits)(v)
end

-- State ------------------------------------------------------------------

local frames, elapsed, seq, errors, last_error, stopped = 0, 0, 1, 0, nil, false

local function summary(t)
    local out, count = {}, 0
    for key, v in pairs(t) do
        local kind = type(v)
        if (kind == 'string' or kind == 'number' or kind == 'boolean') and type(key) == 'string' then
            out[key] = v
            count = count + 1
            if count >= 24 then break end
        end
    end
    return out
end

function api.state()
    local result = {frames = frames, elapsed = elapsed, seq = seq, errors = errors, last_error = last_error,
        heap_kb = collectgarbage('count'), session = session.id, version = VERSION}
    local loader = rawget(_G, 'CowboyBingusModLoader')
    if type(loader) == 'table' then
        result.loader = {version = loader.version, api = loader.api, discovery = loader.discovery,
            modules = loader.modules, jit = loader.jit}
    end
    local jit_lib = rawget(_G, 'jit')
    if type(jit_lib) == 'table' and type(jit_lib.status) == 'function' then
        result.jit = {jit_lib.status()}
        result.jit_version = jit_lib.version
    end
    local engine = rawget(_G, 'stingray')
    local application = type(engine) == 'table' and engine.Application
    if type(application) == 'table' and type(application.worlds) == 'function' then
        local ok, worlds = pcall(application.worlds)
        if ok and type(worlds) == 'table' then result.worlds = #worlds end
    end
    local mods = {}
    for _, name in ipairs(KNOWN_MODS) do
        local v = rawget(_G, name)
        if v ~= nil then mods[name] = type(v) == 'table' and summary(v) or v end
    end
    result.mods = mods
    return result
end

local deferred = {}
-- Runs fn at the next poll, after the current command's response is written.
function api.after(fn)
    assert(type(fn) == 'function', 'after needs a function')
    deferred[#deferred + 1] = fn
    return true
end

function api.quit()
    local engine = rawget(_G, 'stingray')
    local application = type(engine) == 'table' and engine.Application
    local quit = type(application) == 'table' and application.quit
    if type(quit) ~= 'function' then return false, 'stingray.Application.quit unavailable' end
    api.after(function() quit() end)
    return true
end

-- Sorted "name: type" lines for a table (default _G), optionally filtered.
function api.keys(t, pattern)
    t = t == nil and _G or t
    local out = {}
    for key, v in pairs(t) do
        local name = tostring(key)
        if not pattern or name:find(pattern) then out[#out + 1] = name .. ': ' .. type(v) end
    end
    table.sort(out)
    return out
end

-- The next LuaJIT C type id. Each call creates two C types; use sparingly.
function api.ctype_id()
    local ffi = require('ffi')
    return tonumber(ffi.typeof('struct { int bingus_test_agent_probe; }'))
end

-- Commands ---------------------------------------------------------------

local function pack(...) return {n = select('#', ...), ...} end
local traceback = type(debug) == 'table' and debug.traceback
local function handler(message)
    if traceback then return traceback(tostring(message), 2) end
    return tostring(message)
end

-- values_json, when given, is spliced in as the "values" field: a JSON array
-- that keeps every returned value in place, nils included.
local function respond(current, fields, values_json)
    fields.seq, fields.session, fields.frames, fields.elapsed = current, session.id, frames, elapsed
    local ok, text = pcall(api.encode, fields)
    if not ok then
        text = api.encode({seq = current, session = session.id, ok = false, stage = 'encode', error = tostring(text)})
    elseif values_json then
        text = text:sub(1, -2) .. ',"values":' .. values_json .. '}'
    end
    write_file(dir .. 'res-' .. current .. '.json', text .. '\nEND ' .. current .. '\n')
end

local function run(current, source)
    if type(source) ~= 'string' then
        return respond(current, {ok = false, stage = 'read', error = 'command file unreadable'})
    end
    -- `agent` is a local of the chunk; same line, so error line numbers match the command.
    local chunk, problem = loadstring('local agent = ... ' .. source, '=cmd-' .. current)
    if not chunk then return respond(current, {ok = false, stage = 'compile', error = tostring(problem)}) end
    local results = pack(xpcall(chunk, handler, api))
    if not results[1] then return respond(current, {ok = false, stage = 'run', error = tostring(results[2])}) end
    local encoded = {}
    for i = 2, results.n do
        local ok, text = pcall(api.encode, results[i])
        encoded[i - 1] = ok and text or api.encode('<encode failed: ' .. tostring(text) .. '>')
    end
    respond(current, {ok = true, n = results.n - 1}, '[' .. table.concat(encoded, ',') .. ']')
end

local command_path = dir .. 'cmd-1.lua'
local function poll()
    if #deferred > 0 then
        local list = deferred
        deferred = {}
        for i = 1, #list do pcall(list[i]) end
    end
    local file = io_open(command_path, 'rb')
    if not file then return end
    local ok, source = pcall(file.read, file, '*a')
    file:close()
    if type(os_remove) == 'function' then pcall(os_remove, command_path) end
    local current = seq
    seq = seq + 1
    command_path = dir .. 'cmd-' .. seq .. '.lua'
    run(current, ok and source or nil)
end

local function mark(name, fields)
    fields.session, fields.frames, fields.elapsed, fields.seq = session.id, frames, elapsed, seq
    fields.errors, fields.last_error, fields.stopped = errors, last_error, stopped
    fields.heap_kb = collectgarbage('count')
    if type(os_time) == 'function' then fields.time = os_time() end
    write_file(dir .. name .. '.json', api.encode(fields) .. '\nEND ' .. name .. '\n')
end

-- Hooks ------------------------------------------------------------------

-- The harness ends a session by deleting session.txt (or starting another);
-- the agent then goes inert for the rest of the game session.
local function session_alive()
    local text = read_file(dir .. 'session.txt')
    return type(text) == 'string' and text:match('id=([%w%-]+)') == session.id
end

local next_poll, next_beat = 0, 0
local next_due = 0
local function due()
    if elapsed >= next_poll then
        next_poll = elapsed + poll_interval
        poll()
    end
    if elapsed >= next_beat then
        next_beat = elapsed + heartbeat_interval
        if session_alive() then
            mark('heartbeat', {})
        else
            stopped = true
        end
    end
    next_due = math.min(next_poll, next_beat)
end

local previous_update, previous_shutdown = rawget(_G, 'update'), rawget(_G, 'shutdown')

local function update(dt, ...)
    frames = frames + 1
    if not stopped then
        -- dt in seconds from the engine; a frame of 1/60 s if it is missing, at most 1 s after a hitch.
        local step = 0.0166
        if type(dt) == 'number' and dt >= 0 then step = dt < 1 and dt or 1 end
        elapsed = elapsed + step
        if elapsed >= next_due then
            local ok, problem = pcall(due)
            if not ok then
                errors, last_error = errors + 1, tostring(problem)
                next_due = elapsed + poll_interval
                -- A broken mailbox must not cost the game anything: stop after 20 errors.
                if errors >= 20 then
                    stopped = true
                    pcall(mark, 'heartbeat', {})
                end
            end
        end
    end
    if type(previous_update) == 'function' then return previous_update(dt, ...) end
end

local function shutdown(...)
    pcall(mark, 'bye', {reason = 'shutdown'})
    stopped = true
    if type(previous_shutdown) == 'function' then return previous_shutdown(...) end
end

rawset(_G, 'BingusTestAgent', api)
rawset(_G, 'update', update)
rawset(_G, 'shutdown', shutdown)
pcall(function() mark('hello', {version = VERSION, state = api.state()}) end)
return api
