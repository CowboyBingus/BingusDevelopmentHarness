-- Runs the loader's own discovery scan over given patch files and prints what it finds.
-- arg[1] = discover.lua, arg[2] = folder, arg[3..] = patch file names in that folder.
local discovery = dofile(assert(arg[1]))
local folder, paths = assert(arg[2]), {}
for index = 3, #arg do paths[#paths + 1] = folder .. '/' .. arg[index] end
local entries, warnings = discovery.scan(paths, discovery.hasher(require('ffi'), require('bit')), io.open)
print('entries=' .. table.concat(entries, ','))
print('warnings=' .. table.concat(warnings, ';'))
