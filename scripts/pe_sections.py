#!/usr/bin/env python3
"""List PE section names of a Windows exe. A ".bind" section = SteamStub DRM
wrapper (needs Steamless before Goldberg can work). Usage: pe_sections.py <exe>..."""
import struct, sys

def sections(path):
    with open(path, 'rb') as f:
        d = f.read(8192)
    if d[:2] != b'MZ':
        return None
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    n = struct.unpack_from('<H', d, pe + 6)[0]
    opt = struct.unpack_from('<H', d, pe + 20)[0]
    off = pe + 24 + opt
    return [d[off + 40*i: off + 40*i + 8].rstrip(b'\0').decode(errors='replace')
            for i in range(n)]

if __name__ == '__main__':
    for p in sys.argv[1:]:
        s = sections(p)
        tag = '  <-- SteamStub DRM (.bind)' if s and '.bind' in s else ''
        print(f"{p}: {s}{tag}")
