-- Runs the real agent with real time passing until it is asked to quit, so the
-- Python mailbox client can talk to it from another process (tests/test_agent.py).
-- arg[1] = harness folder, arg[2] = folder standing in for %LOCALAPPDATA%.
local root, tmp = assert(arg[1]), assert(arg[2])
local ffi = require('ffi')
ffi.cdef [[
    void hd2h_test_Sleep(uint32_t milliseconds) __asm__("Sleep");
    uint32_t hd2h_test_GetTickCount(void) __asm__("GetTickCount");
]]
local kernel = ffi.load('kernel32')
local source = assert(assert(io.open(root .. '/agent/bingus_test_agent.lua', 'rb')):read('*a'))

local quit = false
local env = {}
for key, value in pairs(_G) do env[key] = value end
env._G = env
env.os = {getenv = function(key) if key == 'LOCALAPPDATA' then return tmp end return os.getenv(key) end,
          remove = os.remove, time = os.time}
env.stingray = {Application = {quit = function() quit = true end}}
env.update = function(dt) return dt end
env.shutdown = function() return 'game shutdown' end
env.CowboyBingusModLoader = {version = 17, api = 1, modules = {['mods/cowboybingus/bingus_test_agent'] = 'loaded'}}
local chunk = assert(loadstring(source, '@bingus_test_agent.lua'))
setfenv(chunk, env)
assert(chunk(), 'agent did not install (no session?)')

local start = kernel.hd2h_test_GetTickCount()
local last = start
while not quit do
    local now = kernel.hd2h_test_GetTickCount()
    env.update((now - last) / 1000)
    last = now
    assert(now - start < 30000, 'driver timed out without a quit')
    kernel.hd2h_test_Sleep(5)
end
assert(env.shutdown() == 'game shutdown')
print('driver: quit after ' .. (kernel.hd2h_test_GetTickCount() - start) .. ' ms')
