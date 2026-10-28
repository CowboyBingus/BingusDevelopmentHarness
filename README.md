# BingusDevelopmentHarness

Launches, drives, stops and tests Helldivers 2 while it is **not** the active window, so mod tests can run
unattended: boot to the title screen or the ship, run Lua inside the game, enter a solo mission and come back,
capture the window, soak for crashes, and collect logs and crash dumps afterwards. Every file it changes is put
back and verified.

> [!WARNING]
> **HellForge only.** The harness works with the game copy of a HellForge installation, not with the vanilla game as
> Steam installs it today. Its offsets, mission addresses and crash signatures are for game build 25480438, the build
> HellForge copies run, and the Steam release has moved past that build: on it `ship`, `mission`, `map`, live
> game-state reads and crash signatures do not work, and no session ends clean. Put `--target hellforge` before every
> command or set `HD2H_TARGET=hellforge`: the default target is still `steam`.

It is a tool for mod developers, built to be driven by AI coding agents as well as by people: every command runs
without prompts and prints JSON, and everything it changes is recorded first and restored afterwards. The in-game agent
it deploys is a test fixture: it is never meant to ship inside a mod release.

**Unofficial.** This project is not affiliated with, endorsed by or supported by Arrowhead Game Studios or Sony
Interactive Entertainment. Helldivers 2 and its assets belong to their owners. The harness reads the game's memory and
calls game functions from a Lua mod; use it at your own risk, on your own account.

## Requirements

- Windows, or Linux for the HellForge target (see Linux below). On Windows the harness uses Win32 through `ctypes`
  and the registry through `winreg`; on Linux it uses `/proc` and X11.
- Python 3.10 or later (tested with 3.13). Standard library only: nothing to install.
- A HellForge installation whose game copy is build 25480438 (default `C:\HellForge` on Windows; on Linux see the
  default below), with Steam running: the copy checks game ownership through it.
- The [Bingus Shared Loader](https://github.com/CowboyBingus/BingusSharedLoader) v20 manager ZIP (`HD2H_LOADER_ZIP`),
  unless the installation already has the loader enabled: it starts the agent.
- The `steam` target drives a Steam install (default folder
  `C:\Program Files (x86)\Steam\steamapps\common\Helldivers 2`, `HD2_GAME_ROOT` for another) with Bingus Shared Loader
  v15 or later deployed among its mods (the `vanilla` condition needs no loader; `packages` takes it from a package
  ZIP). It works only on an install of build 25480438, which the current Steam release is not.
- The known-build data (offsets, crash signatures, mission addresses) is for game build 25480438. On another build
  the harness still deploys, launches, runs `lua` commands and restores, but live game-state reads (state, lobby
  privacy), `ship`, `mission` and crash signatures need that build's entry in `hd2h/builds.py`. Without them `stop`
  cannot reach the ship, so it terminates the game and the session is not clean. Bingus Shared Loader v20 and later
  start no mod, the agent included, on a game build they do not support.

## Install

Clone or unpack the repository anywhere and run `python harness.py <command>` from its folder. Runs go to `runs/`
and the session manifest to `state/` inside it (set `HD2H_HOME` for another folder). Start with:

```powershell
$env:HD2H_TARGET = 'hellforge'  # or put --target hellforge before every command
python harness.py preflight     # install facts, with the game closed
python harness.py smoke         # launch -> ship -> snapshot -> quit -> verify the restore
```

| Variable | Meaning |
| --- | --- |
| `HD2_GAME_ROOT` | Steam install folder of the game (`steam` target) |
| `HD2H_HOME` | where `state/` and `runs/` go (default: the harness folder) |
| `HD2H_TARGET` | `hellforge` (see the warning above) or `steam` (the default) |
| `HD2H_HELLFORGE_ROOT`, `HD2H_HELLFORGE_PROFILE` | HellForge installation and profile |
| `HD2H_LOADER_ZIP` | Bingus Shared Loader v20 manager ZIP for HellForge sessions (default `../releases/Bingus-Shared-Loader-v20.zip`, beside the harness folder) |

## How it works

- **In-game agent** (`agent/bingus_test_agent.lua`)
  - A discovered addon, `mods/cowboybingus/bingus_test_agent`, started by Bingus Shared Loader v15+.
  - It uses no FFI.
  - It runs short Lua commands that arrive through a file mailbox in
    `%LOCALAPPDATA%\CowboyBingus\Helldivers2\TestAgent\` and writes the results back.
  - It stays inert unless the harness's `session.txt` is there, and stops once the harness ends the
    session.
- **Harness** (`harness.py`, `hd2h/`)
  - Standard-library Python only.
  - It deploys the agent, launches the game through Steam, sends commands, then quits the game through
    the engine (or kills it).
  - Afterwards it collects logs and crash dumps, classifies the crash, and puts every file it touched
    back.

No keyboard or mouse input is sent and no hooks are installed. The game's window never needs focus.
Game memory is only read, with read-only process rights (`PROCESS_QUERY_INFORMATION | PROCESS_VM_READ`).

## Known behaviour (observed live, 2026-10-03)

- **GameGuard stalls module enumeration of the game forever.** It injects `npggNT64` into other
  processes, including the harness's Python, and `EnumProcessModulesEx` on the game never returns.
  - The harness finds `game.dll` by scanning mapped images with `VirtualQueryEx` and matching the PE
    timestamp. That takes about 0.12 s.
  - Do not add module enumeration (`EnumProcessModules*`, Toolhelp module snapshots) against the game.
- **Steam launch options apply** (whatever you set for the game in Steam), because the harness launches
  through Steam.
- **Boot stops at the title screen** (state 2; 8 worlds of one unit each), and `ship` gets past it with
  one native call:
  - Game state is read from outside: `[game.dll+0x3326340]` is the game object, and the state index sits
    at `+0xAC21C`. The states are 1 splash, 2 title, 5 prepare-ship, 3 ship, 6 prepare-mission and 4
    mission.
  - `ship` has the agent call the title screen's own "a key was pressed" function (`0xad6a90`, found in
    `StateTitleScreen::update` `0xad7cb0`). It is guarded by state 2, a loaded title (`+324`) and continue
    not yet pressed.
  - The game then logs in and loads the ship (state 3) by itself; on the ship the main world holds about
    2,200 units.
- **Quitting works:** `stingray.Application.quit` exists and exits with code 0. WM_CLOSE also closed a
  running game cleanly.
- **Run times:** `smoke` takes about 45 s, and `smoke --ship` about 2 min.

## Safety guarantees

- **A running game is closed first.** `start`, `smoke` and `soak`:
  - stop and finish a harness session that is still recorded;
  - close any other running game: WM_CLOSE, then TerminateProcess (and taskkill) after 60 s.
  - `--keep-running` refuses instead.
  - Files are only changed while the game is closed, because it reads them at launch and rewrites
    them at exit.
  - The previous session's mod logs are copied to the run's `logs-previous/` first, because the
    next launch truncates them.
- **Every change is recorded in `state/active.json` before it is made.** `finish` (or `recover`) undoes
  it and checks the result by SHA-256:
  - the agent's patch files (the next free `9ba626afa44a3aa3.patch_<n>`, plus empty sidecars);
  - the vanilla, loader-only and packages stash (every `*.patch_*`, moved to `state/stash/` and
    back);
  - the loader's copy at `patch_0` (loader-only condition), removed again;
  - the archives deployed from package ZIPs (packages condition);
  - both settings files, plus every other file in `saves/` (the game may rotate its `.old` copy of
    the profile mid-session). Files the game creates there are reported, never deleted.
  - If anything does not verify, the manifest is kept and the problem is reported.
- **A patch number is one unit** (the archive plus its `.gpu_resources` and `.stream` sidecars), on
  removal and on unstash. Empty sidecars hash the same in every deployment, so a deployed unit is
  removed only while every file left is the one the harness wrote: same SHA-256 and, for sessions
  started since 2026-10-09, the same file (file id and write time, recorded at install), so an
  identical copy from a mod manager is not taken for the harness's own. A stashed unit goes back
  whole or stays in the stash whole.
- **A redeploy from outside stops the restore.** If your mod manager redeploys into `data/`
  mid-session (a patch number the session deployed or stashed now holds files the harness did not
  write), teardown removes only the files it can prove it wrote, leaves the stash as it is, and
  records the redeploy in the manifest. Every later `finish` or `recover` then changes nothing in
  `data/` and says how to resolve it by hand: compare `data/` with `inventory_before` in
  `state/active.json`, put `data/` right, then move the manifest and stash into the run folder.
  (Before this rule, a `recover` after such a redeploy deleted 8 of the user's own empty sidecars.)
- **Lobby privacy is Invite Only for every session** (`privacy_mode = 2`), and your setting is restored
  afterwards.
  - Smoke checks the live value in game memory.
  - Soak ends the run if it ever reads Public.
- No matchmaking. Missions are solo only (see Missions below), in the Invite Only lobby.
- **A session is clean only when it reached the ship.** Closing the game during startup crashes it
  (vanilla `helldivers2.exe+0xC948A`), so:
  - a graceful `stop` first takes the game to the ship (without the agent: waits for the title
    screen), and terminates it instead if that fails;
  - every run's `run.json` carries `clean` and `not_clean_because`; `clean` needs the ship (the
    title screen without the agent), the engine's own quit or close with exit code 0, no new dump
    and a verified restore;
  - `stop`, `finish`, `recover` and `watch` exit with code 1 and print `NOT CLEAN: <reasons>` when a
    session was not clean; `smoke` always goes to the ship;
  - a soak cycle that ends on a Public lobby or a hang is terminated at once.

## HellForge target

`--target hellforge` (or `HD2H_TARGET=hellforge`) drives a HellForge installation's game copy instead of the Steam
install (`hd2h/hellforge.py`). Options:

- `--hellforge-root`: the installation, default `C:\HellForge` on Windows and
  `~/.local/share/hellforge-prefix/pfx/drive_c/users/steamuser/Documents/HellForge` on Linux (see Linux below).
- `--profile`: the profile, default the launcher's selected one.
- On `start` and `smoke`, `--multiplayer launcher|on|off` and `--war launcher|live|open|frozen`. Both default to the
  launcher's settings.

Commands after `start` (`lua`, `ship`, `stop`, ...) follow the recorded session's target. A command that starts a
session of the other target is refused while one is recorded.

How it differs from the Steam target:

- **Launch.** HellForge's own session runner launches the game (`app/session/HellForge.Session.exe play`, with the
  launcher's arguments), not Steam. The runner verifies and locks the copy, checks ownership and the network block,
  runs the local backend and stages the mods.
  - The harness starts it detached, so `start` returns while the session runs.
  - A failure before the game starts is reported with the runner's own words.
  - `start` refuses when the Steam process id this process reads from the registry is not a running `steam.exe`
    (the game would stop at "STEAM NOT DETECTED"). An app container with its own registry layer can hold a stale
    copy; run the harness from a normal terminal then.
- **The copy is never written.** The agent and Bingus Shared Loader go into `<root>/mods` as the packs
  `zz-harness-agent` and `zz-harness-loader`.
  - The loader comes from the Bingus Shared Loader v20 manager ZIP (`HD2H_LOADER_ZIP`; it supports 25480438, the
    build the copies run), unless the installation already has its own enabled.
  - Both packs are recorded with every file's SHA-256 before they are written, and removed only while unchanged.
  - `mods/hellforge-mods.json` (the Mods page) is backed up and restored byte for byte.
- **Settings.** They live in `<root>/userdata/profiles/<profile>`. The preset (Invite Only) goes into every
  `saves/*_user_settings.config`: the profile can hold the game's own file and one imported from the normal game.
- **Mailbox and logs.** The game runs with `LOCALAPPDATA` in `<root>/userdata/environment/Local`, so the agent
  mailbox and the mod logs are there. Dumps are in `<root>/userdata/dumps`.
- **Ending.** The session ends when the game exits (the agent's quit, WM_CLOSE or a kill).
  - `finish` then waits for the runner to stop the backend and restore the copy.
  - If the runner does not finish, `finish` writes `stop-observation.signal` into its run folder, and gives up after
    that (the manifest is kept).
- **Collection.** The runner's run folder (`logs/sessions/<run>`: `result.json`, `backend/requests.jsonl`,
  `multiplayer.log`, the stand-ins' logs) is copied into the harness run as `hellforge-session/`.
- **Clean.** A session is clean only if the runner's status is `observed`.
- **Conditions.** Only `mods` (with the agent) is supported.
- **Public lobby (opt-in).** `start`/`smoke --privacy public` writes Public into every profile file. It is refused on
  the Steam target, and on HellForge unless Local PlayFab and the multiplayer stand-ins are both on: without them a
  HellForge game searches the app's Steam lobbies, which vanilla games publish too, under the same build string.
  `mission` then expects the Public lobby. A Public HellForge game's own Steam lobby is publicly listed (2026-10-22).

## Linux

The `hellforge` target also runs on Linux: HellForge's launcher is native and the game runs under Proton. Nothing here
has run against a real session yet; the Linux session path is covered by a fake wrapper in the tests. The `steam` target
remains Windows-only and its `start` says so.

Requirements:

- Linux with Steam running and signed in, and a HellForge installation that includes `app/linux/hellforge-linux.sh`.
  Discovery requires both `hellforge-install.json` and that wrapper; the error names the missing file.
- Python 3.10 or later, standard library only.
- The wrapper's programs: `socat`, `ip`, `ss`, `unshare`, `pgrep` and `flock` (`iproute2`/`iproute`, `socat`,
  util-linux and procps-ng). `bsdtar` is needed only for `.7z` or `.rar` mods.
- A Proton version installed in Steam, or `PROTON` set to a `proton` script; the wrapper picks one otherwise.
- On Ubuntu 24.04 and other systems where `/proc/sys/kernel/apparmor_restrict_unprivileged_userns` is 1, run the
  installation's `app/linux/allow-namespace.sh` once as root.
- The Bingus Shared Loader v20 manager ZIP (`HD2H_LOADER_ZIP`), unless the installation already has an enabled loader.

The installation defaults to
`~/.local/share/hellforge-prefix/pfx/drive_c/users/steamuser/Documents/HellForge`. `HD2H_HELLFORGE_ROOT` (or
`--hellforge-root`) takes the **host** path of another installation; `HD2H_HELLFORGE_PROFILE` (or `--profile`) chooses
the profile. Run:

```bash
export HD2H_TARGET=hellforge
python3 harness.py preflight
python3 harness.py start
python3 harness.py stop
```

The harness starts the installation's wrapper with `INSTALL`, `STEAM_COMPAT_DATA_PATH` and `TEMP`/`TMP` set and in its
own process group. `stop` and `finish` first write `stop-observation.signal` into the run folder (the runner's own stop
request), wait for the wrapper, and only then send SIGTERM and, if needed, SIGKILL to the wrapper's whole process group,
so `hf-inner.sh`, Proton, the backend and the game do not survive. The run folder is `logs/sessions/<runId>`; the
pointer in `logs/play-session.json` is converted from the installation's own Wine prefix (`C:\...`) or `Z:\...` to a
host path, and `result.json` supplies `status` and `gameExitCode`.

Works: `preflight`, `start`, `stop`, `finish`, `recover`, `watch`, `lua`, the mailbox, mod packs and the file-based
commands. Not available or not verified:

- **Screenshots** need X11 and ImageMagick (`import` or `magick import`). A window covered by another window may capture
  as garbage without a compositor's help; Wayland has no portable cross-client window capture. `shot`,
  `mission --shots` and the capture checks fail with a clear `CaptureError` instead of returning a blank image.
- **Window close** (`close`, and the WM_CLOSE fallback in `stop`) needs X11 and `_NET_WM_PID`. On Wayland no window is
  found, so the harness falls back to signals.
- **Live memory reads** (`state`'s live part, `ship`, `mission`, `leave`, `map`, and `pair`) depend on
  `/proc/<pid>/mem` and `/proc/<pid>/maps`; see below. `lua` and the mailbox still work when they do not.
- `pair.py` and `livemods.py pair-sync` have not been run on Linux; treat them as later work.

### Memory reads and Yama `ptrace_scope`

Linux memory reads open `/proc/<game>/mem` and read `/proc/<game>/maps`; both use ptrace checks. The expected outcomes
from the D.1 review, to be measured on a real session:

- `ptrace_scope=0`: expected to work for the same user if the game process is dumpable.
- `ptrace_scope=1`: only an ancestor may attach. The harness starts the wrapper, so it should be an ancestor through
  `bash -> unshare -> Proton -> wine`, but a Proton or Wine helper that daemonises or is reparented breaks that. Not
  verified.
- `ptrace_scope=2`: a normal user cannot attach at all, even as an ancestor; only `CAP_SYS_PTRACE` works.
- `hidepid=` on `/proc` hides the game from the harness entirely.

Read `/proc/sys/kernel/yama/ptrace_scope` before a run. When a read is denied, the harness's error names the value it
read; the first real Linux session must record the policy, whether the harness is an ancestor, and which `/proc` files
are readable.

## Galactic Map planet view (`map`)

`map` opens the Galactic Map and the planet view's lobby scanner without input (`hd2h/galaxy.py`):

- **Opening:** the Hologram presenter (15) through the PresenterManager entry `0x14C0350`, only while no menu is open
  (as Ship Station Hotkeys does).
- **The planet:** a campaign planet is focused through the war board's own commit with no operation; the map view
  follows.
- **The scanner:** its enabled byte (`[game+0x347CE80]+0x10BD48`, which the map screen sets on zoom-in) is set by the
  agent, the one data write. The game's own update then searches the planet every recharge.
- **Output:** `--watch S` samples the scanner and lists the scans: on a HellForge copy, two lobby searches every ~10 s
  (2026-10-22).
- **No closing yet.** Open the map last in a session and let `stop` quit from it. A deploy after a map opened this
  way crashed before, and `mission` refuses while a menu is open.

## Two installations on one PC (`pair.py`)

`pair.py` runs two HellForge installations side by side as a host and a joiner, and joins them to each other without
input (`hd2h/pair.py`): HellForge's several-installations test mode (its brokers keep a local Steam lobby and P2P
between them, HellForge 0.3.0). Each installation needs its own network block first (one session from each).

```
python pair.py run --host C:\HellForge-2 --joiner C:\HellForge-3   # the joiner joins the host's lobby; JSON verdict
python pair.py map --host C:\HellForge-2 --joiner C:\HellForge-3   # the joiner's Galactic Map shows the host's lobby
python pair.py stop                                                # ends both sessions
```

## Live mod changes (`livemods.py`)

`livemods.py` changes mods in a running HellForge session as the launcher's Mods page does, and checks each change
(`hd2h/livemods.py`). It needs a HellForge build with live mod sync (HellForge pull request #65).

- `setup` (game closed) copies test mods into the installation and sets the next session's switches; `teardown` puts
  everything back, byte for byte, and only removes files that are still the harness's.
- `change` switches mods in the running session (`POST /hellforge/live-mods`; `--preview` asks without changing).
- `scenario FILE.json` runs a list of changes with expected answers (examples in `live-mods/`).
- `pair-sync --host ... --joiner ...` is mod sync on one PC: the joiner, with other mods, joins the host, downloads its
  mods, takes them live and is invited back.

Results go to the harness run's `live-mods/` folder. `python livemods.py --help` lists every option.

## Commands

```powershell
python harness.py preflight                    # install facts (needs the game closed)
python harness.py close                        # close a running game (WM_CLOSE, then terminate)
python harness.py smoke [--ship] [--package ZIP]...  # launch -> settle -> snapshot (on the ship) -> quit -> verify
python harness.py start [--preset light] [--condition mods|loader-only|vanilla|packages] [--package ZIP]...
python harness.py ship                         # title screen -> ship, no input
python harness.py mission [--difficulty N] [--planet ID | --faction terminid|automaton|illuminate] [--shots]
                                               # ship -> a solo mission until the player has control
python harness.py leave                        # mission -> ship through the host abandon
python harness.py map [--planet ID | --faction F [--pick N]] [--watch S]
                                               # the Galactic Map's planet view and its lobby scans (see below)
python harness.py shot [name]                  # capture the game window into the run's shots/, scored
python harness.py lua "return agent.state()"   # Lua inside the game; `agent` holds the helpers
python harness.py state                        # agent state plus live game state, mission and privacy
python harness.py stop                         # leave a mission, reach the ship, quit through the engine, collect,
                                               # restore (--kill)
python harness.py smoke --mission [--difficulty N] [--planet ID | --faction F]
                                               # smoke, then into a mission, checks there, back to the ship
python harness.py watch                        # (background) wait for exit, record exit code, finish
python harness.py finish                       # collect and restore after the game exited on its own
python harness.py recover                      # same, after a harness crash
python harness.py soak --conditions mods,loader-only --where ship --minutes 22 --cycles 4
python harness.py classify <dump.dmp>...
python harness.py build-agent <folder>         # agent patch files only, nothing deployed
```

`python harness.py <command> --help` lists every option.

- Presets:
  - `private` (default for `start` and `soak`): Invite Only only, so the game runs with your real
    display settings.
  - `light` (default for `smoke`): Invite Only, plus a 30 FPS cap, muted audio, and windowed
    1280x720. The upscaler's stored input size (`render_settings.render_resolution`, in pixels) is
    scaled to the window by the same factor: left at its full-screen value (for example 2/3 of the
    screen) it is larger than the window, and the game then draws only the HUD over a flat gray world
    (2026-10-13).
- Conditions:
  - `mods`: your deployment plus the agent.
  - `loader-only`: every patch is stashed and a copy of the Shared Loader's patch is installed as
    `patch_0` (the game stops reading patches at the first missing number), plus the agent. This is
    the control that can still reach the ship.
  - `vanilla`: no patches and no agent. It stays on the title screen, is observed from outside, and is
    ended by WM_CLOSE.
  - `packages` (`--package ZIP`, repeatable; `smoke --package` selects it): every installed patch is
    stashed and the archives in the given manager ZIPs are deployed instead, numbered in the given
    order (a later package overrides an earlier one), plus the agent. Every manager option of a ZIP
    is deployed, as a player enables a pack's components; `ZIP::Option A|Option B` deploys only those.
    One package must carry the Shared Loader. Use it to run unreleased builds without touching your
    installed mods; a modpack is not deployed unless you pass it, so standalone builds are not
    shadowed by its copies.
- Soak:
  - Two conditions alternate ABBA, so drift over the day cancels.
  - `--where ship` starts each cycle's clock on the ship.
  - `summary.json` gives crashes per observed hour per condition, with signatures.
  - If whatever runs the harness stops background commands after a time limit (some agent tools stop them after
    2 hours), run long soaks in blocks, for example 4 × 22 min.

## Agent helpers (inside `lua` commands)

| Helper | Returns |
| --- | --- |
| `agent.state()` | frames, game time, heap KB, loader modules and JIT state, world count, known mod globals |
| `agent.keys(t, pattern)` | sorted `name: type` lines of a table (default `_G`) |
| `agent.after(fn)` | runs `fn` at the next poll, after this command's response |
| `agent.quit()` | schedules `stingray.Application.quit()`; `false, reason` if it is unavailable |
| `agent.ctype_id()` | the next LuaJIT C type id (creates two types per call) |
| `agent.encode(v)` | the JSON the agent would write |
| `agent.limits` | `depth`, `items` and `bytes` caps for encoding results |

A command runs to completion inside one frame, so keep commands short.

## Missions

`mission` takes a launched session (any condition with the agent) from the ship into a mission until the
player has control, with no input; `leave` brings it back. The addresses are for build 25480438
(`hd2h/builds.py`, `MissionOffsets`).

- **Steps** (`hd2h/mission.py`), each the game's own function, called by the agent after checking the
  function's prologue bytes:
  1. select: the Galactic Map's commit `0x12D18F0`. An active operation is continued as it is (never
     abandoned); otherwise the difficulty is chosen (`--difficulty`, default 1) on the ship's planet when it
     has a campaign, `--planet`, or the first campaign planet of `--faction`. The planet is focused first
     when its operations are not generated yet; travelling there takes about 18 s.
  2. the hellpod briefing and the instant seat (Ship Station Hotkeys' F8 path), intro skipped;
  3. the drop zone: the briefing map's own confirm (the map centre), else `0x12D1E30` at (0, 0);
  4. ready: the game's setter of the local ready bit; a solo host launches by itself;
  5. state 6, then 4; done when no loading screen or cutscene is up and the avatar's spawn flag is clear,
     for 1 s.
- **Leaving:** the host abandon `0x18FB720` (Load screen, state 5, then 3). `stop` leaves a mission first;
  if that fails it terminates the game instead of quitting inside the mission (the vanilla teardown
  crash `+0x626296` is on that path), and `run.json` is not clean.
- **Live timings (2026-10-13):** select 2-7 s (about 25 s with travel), briefing and seat under 1 s, ready to
  control about 40 s, abandon to the ship about 8 s.
- **Account traffic.** A mission makes the same requests to the game's backend as a player's own deploy, on your
  account: Operation/Create for a new operation, Account/ReportPosition when the ship travels,
  Operation/Mission/Start at launch, the Mission/Session abandon report on leaving. Solo sessions only (`solo
  sessions only` otherwise); the lobby must read Invite Only.
- **Not supported:** squads, joining others, finishing objectives or extracting.
- **Captures:** `shot` and `mission --shots` write `runs/<run>/shots/<nn>-<name>.png` (PrintWindow, so the
  window may be covered) and score them: `world` is false for the flat gray frame a too-small upscaler input
  gives (see the `light` preset). Bright cloud frames score like it too, so judge a step by more than one
  capture.

## Output

`runs/<time>-<scenario>/` holds:
- `run.json`: outcome, exit code and how the game ended, new dumps with their classification, restore
  problems, install facts, `missions` (each deploy's planet, difficulty, operation and step timings, or its
  error) and `shots`;
- `shots/`: captures with their scores in `run.json`;
- `logs/`: copies of the mod logs written during the session; logs left by mods that did not run (older than the session) go to `logs-stale/` and `run.json` lists them as `stale_logs`, never as this run's;
- `agent/`: hello, heartbeat and bye;
- `dumps/`: new crash dumps;
- for smoke, `snapshot.json` and `verdict.json`;
- for soak, `soak.jsonl` and `summary.json`.

Run folders hold your own logs and crash dumps (and so your machine's details): review them before sharing one.

## Crash signatures (build 25480438)

| Signature | Kind |
| --- | --- |
| `helldivers2.exe+0x6c07a6` access violation | `gpu-watchdog`: the engine's GPU fence watchdog (vanilla) |
| code `0xE24C4A04` | `lua-out-of-memory` in compiled code |
| `helldivers2.exe+0x6f9394` | `render-stale-texture` |
| `helldivers2.exe+0x5d7384` | `dump-writer-after-tdr` |
| `helldivers2.exe+0x5f5c33` | `unit-unloader-assert` (Lobby Manager v0.4 development kicks) |
| `game.dll+0x17ff734` | `mom-row-reset-alignment` (Mod Options Menu development build) |

`hd2h/builds.py` also names the two quit crashes (`quit-during-boot`, `quit-render-task-pool`) and
`set-destination-removed-entity`. Anything else is `unclassified` and needs cdb triage. Other game builds get no
signatures. Add a build and its offsets in `hd2h/builds.py`.

## Cost while the agent is installed

These costs apply only during harness sessions:
- **Idle frames:** one addition and one compare; no calls, no allocation (tested in the game's
  lua51.dll).
- **Every 0.1 s:** one `io.open` of the next command file. A miss allocates io.open's short error
  message.
- **Every 2 s:** one read of `session.txt` and one heartbeat write.

None of this has been measured in game, so never use agent sessions for ms/frame numbers. Background
windows are also scheduled differently: measure mod cost in real play, without the harness.

## Tests

```powershell
python -B -m unittest discover -s tests -p "test_*.py"
```

The suite never starts the game. The Lua tests need LuaJIT (`HD2_LUAJIT`, a `luajit` on `PATH`, or a build at
`../tools/src/LuaJIT/src/luajit.exe` beside the harness folder); without one, the agent's Lua unit tests fail with
"no Lua VM found". The other optional inputs below make more tests run; a test whose input is missing skips.

| Variable | Input | Used by |
| --- | --- | --- |
| `HD2_LUA51_DLL` | the game's `lua51.dll` (default: the Steam install's `bin\lua51.dll`) | the Lua sources of each mission step compile in it; with `HD2_GAME_LUA`, the game-VM runs |
| `HD2_GAME_LUA` | a runner, `python <runner> <script.lua> [args]`, that runs a Lua script in the game's `lua51.dll` (default `../PerformanceBaseline/game_lua.py`) | every Lua test also runs in the game's VM |
| `BINGUS_SHARED_LOADER` | a Bingus Shared Loader checkout (default `../BingusSharedLoader`) | the agent archive is byte-identical to its `scripts/archive.py` |
| `HD2H_PUBLISHED_LOADER` | a checkout of the published Bingus Shared Loader v18 | its `src/discover.lua` finds the agent |
| `HD2H_CAPTURES` | a folder of window captures from a mission deploy (`deploy3/`, `deploy4/`) | the capture score on real frames (needs PIL) |
| `HD2H_GAME_DLL_IMAGE` | build 25480438's `game.dll` as mapped in the running game | the mission steps' prologue bytes match it |
| `HD2H_WORKSPACE` | the folder the `../` defaults above are relative to (default: the harness's parent folder) | |

The suite covers:
- **The agent's Lua unit tests**, run in LuaJIT and, with a runner, in the game's lua51.dll:
  - per-frame call budget with `frame_budget.lua`;
  - zero garbage on idle frames, forwarding, errors and encoding.
- **End to end:** a Python mailbox session with the real agent in each VM.
- **Archive:**
  - the archive is byte-identical to Bingus Shared Loader's `scripts/archive.py`;
  - the published v18 `discover.lua` finds the agent.
- **File round trips:** deploy, stash and settings, LF and CRLF.
- **Outside redeploys:** a mod manager redeploying into `data/` mid-session (same names, empty
  sidecars, identical bytes, fewer patches, vanilla); teardown runs twice and nothing of yours is
  removed or moved.
- **Crash dumps:** synthetic dumps through the parser and classifier.
- **Win32 wrappers:** tested against child processes.
- **Session lifecycle and scenarios:** run against a fake game, including `smoke --mission`.
- **HellForge target:** paths, packs, launch and teardown against a fake installation and session runner.
- **Missions:** record decoding and choice rules; the step sequence against a simulated game (new and
  continued operations, faction, drop fallback, each refusal and stall named by its step, leaving from a
  loading mission); `stop` leaving a mission first or terminating; the Lua each call sends compiles in both
  VMs and checks the prologues, which match the captured build 25480438 game.dll.
- **Captures:** PNG encoding, and the score on synthetic frames (and on real gray and world captures, when given).

The Lua VMs write into `.test-tmp/` (gitignored): a `luajit.exe` that runs at low integrity cannot write under
`%TEMP%`.

## License

BSD Zero Clause License (0BSD): see [LICENSE](LICENSE).
