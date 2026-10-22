-- Unit tests for agent/bingus_test_agent.lua. Run by tests/test_agent.py in the
-- LuaJIT and in the game's lua51.dll: arg[1] = harness folder,
-- arg[2] = an empty temporary folder (CowboyBingus/Helldivers2/TestAgent is created inside).
local root, tmp = assert(arg[1], 'harness folder'), assert(arg[2], 'temporary folder')
local budget = dofile(root .. '/tests/frame_budget.lua')
local source = assert(assert(io.open(root .. '/agent/bingus_test_agent.lua', 'rb')):read('*a'))
local mailbox = tmp .. '/CowboyBingus/Helldivers2/TestAgent/'
local passed = 0

local function test(name, fn)
    fn()
    passed = passed + 1
end

local function write(path, text)
    local file = assert(io.open(path, 'wb'))
    file:write(text)
    file:close()
end

local function read(path)
    local file = io.open(path, 'rb')
    if not file then return nil end
    local text = file:read('*a')
    file:close()
    return text
end

local function clear()
    for _, name in ipairs({'session.txt', 'hello.json', 'heartbeat.json', 'bye.json'}) do os.remove(mailbox .. name) end
    for n = 1, 30 do
        os.remove(mailbox .. 'cmd-' .. n .. '.lua')
        os.remove(mailbox .. 'res-' .. n .. '.json')
    end
end

local function command(n, text)
    write(mailbox .. 'cmd-' .. n .. '.lua.tmp', text)
    assert(os.rename(mailbox .. 'cmd-' .. n .. '.lua.tmp', mailbox .. 'cmd-' .. n .. '.lua'))
end

local function response(n)
    local text = read(mailbox .. 'res-' .. n .. '.json')
    assert(text, 'no response ' .. n)
    local body = text:match('^(.*)\nEND ' .. n .. '\n$')
    assert(body, 'response ' .. n .. ' is not framed: ' .. text)
    return body
end

-- A fresh global environment per test, with this test's os/io and engine fakes.
local function environment(extra)
    local env = {}
    for key, value in pairs(_G) do env[key] = value end
    env._G = env
    env.os = {getenv = function(key) if key == 'LOCALAPPDATA' then return tmp end return os.getenv(key) end,
              remove = os.remove, time = os.time}
    env.io = {open = io.open}
    env.BingusTestAgent, env.update, env.shutdown = nil, nil, nil
    for key, value in pairs(extra or {}) do env[key] = value end
    return env
end

local function load(env)
    local chunk = assert(loadstring(source, '@bingus_test_agent.lua'))
    setfenv(chunk, env)
    return chunk()
end

local function session(id, extra)
    write(mailbox .. 'session.txt', 'id=' .. id .. '\npoll=0.1\nheartbeat=2\n' .. (extra or ''))
end

-- Frames of `dt` seconds until `seconds` of game time pass.
local function run(env, seconds, dt)
    dt = dt or 0.01
    for _ = 1, math.floor(seconds / dt + 0.5) do env.update(dt) end
end

test('stays inert without a session file', function()
    clear()
    local env = environment({update = function() return 'game' end})
    assert(load(env) == nil)
    assert(env.BingusTestAgent == nil and env.update() == 'game')
end)

test('stays inert with a malformed session id', function()
    clear()
    session('bad id!')
    local env = environment()
    assert(load(env) == nil and env.BingusTestAgent == nil)
end)

test('installs, says hello and guards against a second copy', function()
    clear()
    session('s1')
    local game = function() return 'game' end
    local env = environment({update = game})
    local api = load(env)
    assert(api and env.BingusTestAgent == api and env.update ~= game)
    assert(read(mailbox .. 'hello.json'):find('"session":"s1"', 1, true))
    assert(read(mailbox .. 'hello.json'):match('\nEND hello\n$'))
    local hooked = env.update
    assert(load(env) == api and env.update == hooked, 'second copy must change nothing')
end)

test('forwards every argument and return value', function()
    clear()
    session('s2')
    local seen
    local env = environment({update = function(...) seen = {n = select('#', ...), ...} return 1, nil, 3 end,
                             shutdown = function(...) return 4, nil, 6 end})
    load(env)
    local results = {n = 0}
    results = (function(...) return {n = select('#', ...), ...} end)(env.update(0.016, 'marker', nil, 'tail'))
    assert(seen.n == 4 and seen[1] == 0.016 and seen[2] == 'marker' and seen[4] == 'tail')
    assert(results.n == 3 and results[1] == 1 and results[2] == nil and results[3] == 3)
    local closing = (function(...) return {n = select('#', ...), ...} end)(env.shutdown('x'))
    assert(closing.n == 3 and closing[1] == 4 and closing[3] == 6)
    assert(read(mailbox .. 'bye.json'):find('"reason":"shutdown"', 1, true))
end)

test('a failing game update reaches the caller unchanged', function()
    clear()
    session('s3')
    local env = environment({update = function() error('game update failed', 0) end})
    load(env)
    local ok, message = pcall(env.update, 0.016)
    assert(not ok and message == 'game update failed')
end)

test('per-frame calls: idle frames none, polls one open, heartbeats one read and one write', function()
    clear()
    session('s4')
    local env = environment()
    local counts = budget.wrap(env.io)
    load(env)
    -- First frame: poll (1 open) + heartbeat session read (1) + heartbeat write (1).
    budget.check(budget.frame(counts, env.update, 0.01), {open = 3}, 'first frame')
    local polls, idle = 0, 0
    for _ = 1, 25 do
        local frame = budget.frame(counts, env.update, 0.01)
        if frame.open then
            budget.check(frame, {open = 1}, 'poll frame')
            polls = polls + 1
        else
            budget.check(frame, {}, 'idle frame')
            idle = idle + 1
        end
    end
    assert(polls == 2 and idle == 23, 'polls ' .. polls .. ', idle ' .. idle)
end)

test('idle frames allocate nothing', function()
    clear()
    session('s5')
    local env = environment()
    load(env)
    env.update(0.01)
    -- The same loop warms up and measures: compiling a trace allocates in the Lua heap,
    -- so the measured loop must already be compiled.
    local function idle(frames)
        for _ = 1, frames do env.update(0.000001) end
    end
    idle(2000)
    collectgarbage('collect')
    collectgarbage('stop')
    local before = collectgarbage('count')
    idle(5000)
    local after = collectgarbage('count')
    collectgarbage('restart')
    assert(after == before, string.format('idle frames allocated %.3f KB', after - before))
end)

test('runs commands in order and reports values, nils included', function()
    clear()
    session('s6')
    local env = environment()
    load(env)
    env.update(0.01)
    command(1, 'return 1 + 1, "x", nil, {a = 1, b = {2, 3}}')
    command(3, 'return "out of order"')
    run(env, 0.2)
    local body = response(1)
    assert(body:find('"ok":true', 1, true) and body:find('"n":4', 1, true), body)
    assert(body:find('"values":[2,"x",null,{"a":1,"b":[2,3]}]', 1, true), body)
    assert(read(mailbox .. 'cmd-1.lua') == nil, 'command file is removed after reading')
    assert(read(mailbox .. 'res-3.json') == nil, 'command 3 must wait for command 2')
    command(2, 'return agent.session')
    run(env, 0.3)
    assert(response(2):find('"values":["s6"]', 1, true))
    assert(response(3):find('"values":["out of order"]', 1, true))
end)

test('reports compile and runtime errors without stopping', function()
    clear()
    session('s7')
    local env = environment()
    load(env)
    env.update(0.01)
    command(1, 'return +')
    run(env, 0.2)
    assert(response(1):find('"stage":"compile"', 1, true))
    command(2, 'error("boom")')
    run(env, 0.2)
    local body = response(2)
    assert(body:find('"stage":"run"', 1, true) and body:find('boom', 1, true), body)
    command(3, 'return agent.state().errors')
    run(env, 0.2)
    assert(response(3):find('"values":[0]', 1, true), 'command errors are not agent errors')
end)

test('deferred actions run after the response, and quit uses the engine', function()
    clear()
    session('s8')
    local quits = 0
    local env = environment({stingray = {Application = {quit = function() quits = quits + 1 end}}})
    load(env)
    env.update(0.01)
    command(1, 'return agent.quit()')
    run(env, 0.15)
    assert(response(1):find('"values":[true]', 1, true))
    assert(quits == 0 or quits == 1)
    run(env, 0.2)
    assert(quits == 1, 'quit ran once at the next poll')
end)

test('quit reports when the engine has no quit function', function()
    clear()
    session('s9')
    local env = environment()
    load(env)
    env.update(0.01)
    command(1, 'return agent.quit()')
    run(env, 0.2)
    assert(response(1):find('"values":[false,"stingray.Application.quit unavailable"]', 1, true), response(1))
end)

test('goes inert when the harness ends the session', function()
    clear()
    session('s10')
    local env = environment()
    local counts = budget.wrap(env.io)
    load(env)
    env.update(0.01)
    os.remove(mailbox .. 'session.txt')
    run(env, 2.5)
    command(1, 'return 1')
    for _ = 1, 50 do budget.check(budget.frame(counts, env.update, 0.1), {}, 'inert frame') end
    assert(read(mailbox .. 'res-1.json') == nil)
end)

test('a broken mailbox stops the agent after 20 errors', function()
    clear()
    session('s11')
    local opens, broken, real_open = 0, false, io.open
    local env = environment()
    -- io.open works while the agent loads (it reads session.txt), then every call throws.
    env.io = {open = function(...)
        if broken then
            opens = opens + 1
            error('disk on fire')
        end
        return real_open(...)
    end}
    load(env)
    broken = true
    run(env, 5, 0.05)
    local state = env.BingusTestAgent.state()
    assert(state.errors == 20 and state.last_error:find('disk on fire', 1, true), 'errors: ' .. tostring(state.errors))
    local before = opens
    run(env, 5, 0.05)
    assert(opens == before, 'no mailbox access after stopping')
end)

test('encodes values JSON can and cannot hold', function()
    clear()
    session('s13')
    local api = load(environment())
    local t = {b = 1, a = {1, 2, 3}, c = 'q"\n\1', d = 0/0, e = math.huge, f = -math.huge, g = 0.5, h = 2^53}
    assert(api.encode(t) == '{"a":[1,2,3],"b":1,"c":"q\\"\\n\\u0001","d":"nan","e":"inf","f":"-inf","g":0.5,'
        .. '"h":9007199254740992}', api.encode(t))
    local cycle = {}
    cycle.self = cycle
    assert(api.encode(cycle) == '{"self":"<cycle>"}')
    assert(api.encode({[1] = 'a', [3] = 'c'}) == '{"1":"a","3":"c"}')
    assert(api.encode({}) == '{}')
    local deep = {{{{{{{{}}}}}}}}
    assert(api.encode(deep):find('"<table>"', 1, true))
    assert(api.encode(print):match('^"function'))
    local many = {}
    for i = 1, 500 do many[i] = i end
    assert(api.encode(many):find('"<100 more>"', 1, true))
end)

clear()
print('test_agent.lua: ' .. passed .. ' passed (' .. (jit and jit.version or _VERSION) .. ')')
