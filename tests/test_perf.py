#!/usr/bin/env python3
"""perf.py: frame limit + shared shader cache environment."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src' / 'host'))
import perf  # noqa: E402


def test_proton_limit():
    env = {}
    assert perf.apply_fps_limit(env, 60, proton=True)
    assert env['DXVK_CONFIG'] == 'dxgi.maxFrameRate = 60; d3d9.maxFrameRate = 60'
    assert env['VKD3D_FRAME_RATE'] == '60'
    env = {'DXVK_CONFIG': 'dxgi.syncInterval = 0'}
    perf.apply_fps_limit(env, 45, proton=True)
    assert env['DXVK_CONFIG'].startswith('dxgi.syncInterval = 0; dxgi.maxFrameRate = 45')
    env = {'DXVK_CONFIG': 'dxgi.maxFrameRate = 90', 'VKD3D_FRAME_RATE': '90'}
    perf.apply_fps_limit(env, 60, proton=True)          # the user's own wins
    assert env == {'DXVK_CONFIG': 'dxgi.maxFrameRate = 90', 'VKD3D_FRAME_RATE': '90'}


def test_unlimited():
    env = {}
    assert perf.apply_fps_limit(env, 0, proton=True) is None and env == {}
    assert perf.gamescope_rate(0) == 60 and perf.gamescope_rate(144) == 144


def test_native_limit():
    with mock.patch.object(perf, 'mangohud_available', return_value=True):
        env = {}
        assert perf.apply_fps_limit(env, 60, proton=False)
        assert env == {'MANGOHUD': '1', 'MANGOHUD_CONFIG': 'fps_only,alpha=0,background_alpha=0,fps_limit=60'}
        env = {'MANGOHUD': '1', 'MANGOHUD_CONFIG': 'fps_only'}   # user's HUD kept
        perf.apply_fps_limit(env, 30, proton=False)
        assert env['MANGOHUD_CONFIG'] == 'fps_only,fps_limit=30'
        env = {'DISABLE_MANGOHUD': '1'}
        assert perf.apply_fps_limit(env, 60, proton=False) is None
    with mock.patch.object(perf, 'mangohud_available', return_value=False):
        env = {}
        assert perf.apply_fps_limit(env, 60, proton=False) is None and env == {}


def test_shader_cache():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d) / 'steamapps'
        env = {'PRESSURE_VESSEL_FILESYSTEMS_RW': '/a', '__GL_SHADER_DISK_CACHE_PATH': '/mine'}
        with mock.patch.dict(os.environ, {'JANUS_NO_SHARED_SHADER_CACHE': ''}):
            sc = perf.apply_shader_cache(env, lib, 550)
        assert sc == lib / 'shadercache' / '550'
        assert (sc / 'nvidiav1').is_dir()               # the driver won't create it
        assert env['STEAM_COMPAT_SHADER_PATH'] == str(sc)
        assert env['MESA_SHADER_CACHE_DIR'] == str(sc)
        assert env['__GL_SHADER_DISK_CACHE_PATH'] == '/mine'
        assert env['PRESSURE_VESSEL_FILESYSTEMS_RW'] == f'/a:{sc}'
        perf.apply_shader_cache(env, lib, 550)            # no duplicate share
        assert env['PRESSURE_VESSEL_FILESYSTEMS_RW'] == f'/a:{sc}'
        env = {}
        with mock.patch.dict(os.environ, {'JANUS_NO_SHARED_SHADER_CACHE': '1'}):
            assert perf.apply_shader_cache(env, lib, 550) is None and env == {}


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print(f'ok {name}')
    print('all passed')
