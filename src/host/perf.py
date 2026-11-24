"""Performance measures applied to every launched game.

Frame limit (SessionConfig.fps_limit, default 60): gamescope's own -r/-o only
paces games that present with vsync (FIFO); a game with vsync off (MAILBOX)
runs at whatever it can, e.g. Deep Rock Galactic at 120 fps on a 60 Hz panel,
and gamescope's --framerate-limit / GAMESCOPE_FPS_LIMIT don't act on a nested
gamescope. So the limit is applied inside the game:
  - Proton: DXVK's dxgi/d3d9 maxFrameRate (DXVK_CONFIG; current DXVK dropped
    the DXVK_FRAME_RATE variable) and vkd3d-proton's VKD3D_FRAME_RATE;
  - native: MangoHud's limiter (invisible HUD), when MangoHud is installed
    (Vulkan games; OpenGL would need MangoHud's LD_PRELOAD library).
Measured: NOTES.md "Performance: frame limit, shared shader cache".

Shared shader cache: the variables Steam itself sets for a game, so every
instance (every profile, isolated homes too) reads and writes the game's
steamapps/shadercache/<appid>/ like a Steam launch does, instead of a cold
per-profile ~/.cache that, for Proton games, wasn't even kept between
sessions. JANUS_NO_SHARED_SHADER_CACHE=1 turns it off.
"""
import glob
import os
from pathlib import Path
from typing import Dict, Optional

DEFAULT_FPS_LIMIT = 60
FPS_LIMITS = (0, 30, 40, 45, 60, 72, 90, 120, 144, 165, 240)  # 0 = unlimited


def gamescope_rate(fps_limit: int) -> int:
    """gamescope -r/-o (its nested refresh): the limit, else 60 as before."""
    return int(fps_limit) if fps_limit and int(fps_limit) > 0 else 60


def mangohud_available() -> bool:
    dirs = ['/usr/share/vulkan/implicit_layer.d', '/etc/vulkan/implicit_layer.d',
            os.path.expanduser('~/.local/share/vulkan/implicit_layer.d')]
    return any(glob.glob(os.path.join(d, 'MangoHud*.json')) for d in dirs)


def _append_config(value: str, extra: str, sep: str) -> str:
    return f"{value}{sep}{extra}" if value else extra


def apply_fps_limit(env: Dict[str, str], fps_limit: int, proton: bool) -> Optional[str]:
    """Set the game-side frame limiter in *env*. Returns how it's limited
    (for the log), None when it isn't. Variables the user already set win."""
    fps = int(fps_limit or 0)
    if fps <= 0:
        return None
    if proton:
        cfg = env.get('DXVK_CONFIG', '')
        if 'maxFrameRate' not in cfg:
            env['DXVK_CONFIG'] = _append_config(
                cfg, f"dxgi.maxFrameRate = {fps}; d3d9.maxFrameRate = {fps}", '; ')
        env.setdefault('VKD3D_FRAME_RATE', str(fps))
        return f"DXVK/vkd3d limit {fps} fps"
    if mangohud_available() and env.get('DISABLE_MANGOHUD') != '1':
        cfg = env.get('MANGOHUD_CONFIG', '')
        if 'fps_limit' not in cfg:
            # An invisible HUD unless the user asked for one (MANGOHUD=1 + own
            # config). Not no_display: that switches off MangoHud's present
            # hook, its limiter included.
            base = (cfg if (env.get('MANGOHUD') == '1' and cfg)
                    else 'fps_only,alpha=0,background_alpha=0')
            env['MANGOHUD_CONFIG'] = _append_config(base, f'fps_limit={fps}', ',')
        env['MANGOHUD'] = '1'
        return f"MangoHud limit {fps} fps (Vulkan games)"
    return None


def shader_cache_dir(library_steamapps: Path, appid: int) -> Path:
    return Path(library_steamapps) / 'shadercache' / str(appid)


def apply_shader_cache(env: Dict[str, str], library_steamapps: Path,
                       appid: int) -> Optional[Path]:
    """Point the GPU drivers' shader caches at Steam's per-game cache, as a
    Steam launch does (values copied from a Steam-launched game's environment),
    and share it with the pressure-vessel container. Returns the dir."""
    if os.environ.get('JANUS_NO_SHARED_SHADER_CACHE') == '1':
        return None
    sc = shader_cache_dir(library_steamapps, appid)
    try:
        # The NVIDIA driver doesn't create its cache dir: without it there is
        # no cache at all (Valheim: 3-4 s hitches every session).
        (sc / 'nvidiav1').mkdir(parents=True, exist_ok=True)
        (sc / 'DXVK_state_cache').mkdir(exist_ok=True)
    except OSError as e:
        print(f"[janus] Shared shader cache unavailable ({e})", flush=True)
        return None
    s = str(sc)
    for k, v in (
        ('STEAM_COMPAT_SHADER_PATH', s),
        ('__GL_SHADER_DISK_CACHE', '1'),
        ('__GL_SHADER_DISK_CACHE_PATH', f'{s}/nvidiav1'),
        ('__GL_SHADER_DISK_CACHE_APP_NAME', 'steamapp_shader_cache'),
        ('__GL_SHADER_DISK_CACHE_READ_ONLY_APP_NAME',
         'steam_shader_cache;steamapp_merged_shader_cache'),
        ('__GL_SHADER_DISK_CACHE_SKIP_CLEANUP', '1'),
        ('MESA_SHADER_CACHE_DIR', s),
        ('MESA_SHADER_CACHE_MAX_SIZE', '5G'),
        ('MESA_GLSL_CACHE_DIR', s),
        ('MESA_GLSL_CACHE_MAX_SIZE', '5G'),
        ('MESA_DISK_CACHE_SINGLE_FILE', '1'),
        ('MESA_DISK_CACHE_READ_ONLY_FOZ_DBS', 'steam_cache,steam_precompiled'),
        ('DXVK_STATE_CACHE_PATH', f'{s}/DXVK_state_cache'),
    ):
        env.setdefault(k, v)
    # Isolated homes hide everything outside PRESSURE_VESSEL_HOME.
    rw = env.get('PRESSURE_VESSEL_FILESYSTEMS_RW', '')
    if s not in rw.split(':'):
        env['PRESSURE_VESSEL_FILESYSTEMS_RW'] = f"{rw}:{s}" if rw else s
    return sc
