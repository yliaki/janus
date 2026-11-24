# Vendored quickjs-ng Python binding

This is the `quickjs-ng` PyPI package (https://pypi.org/project/quickjs-ng/,
wrapper repo https://github.com/genotrance/quickjs-ng), extracted from the
`cp310-abi3-manylinux2014_x86_64` wheel — abi3, so it works on any CPython
>= 3.10 on x86_64 Linux without pip.

Vendored because this Debian system has no pip (`python3-pip` not installed)
and Debian ships the QuickJS *engine* (`libqjs0`) but no Python binding.
`src/host/scripting.py` adds this directory to `sys.path` as a fallback when
`import quickjs` fails; a pip-installed copy takes precedence.

To update: download the new wheel from PyPI and extract `_quickjs.abi3.so`,
`quickjs/`, and the dist-info here.

Version: 0.16.0.1 (engine quickjs-ng, wrapper release 2026-08-01).
License: MIT (see quickjs_ng-0.16.0.1.dist-info/licenses/LICENSE).

Known binding limitation that shapes Janus's design: `set_time_limit()`
cannot be combined with `add_callable()` ("Can not call into Python with a
time limit set") — so handler scripts run with a memory limit but no CPU
watchdog. See SCRIPTING.md.
