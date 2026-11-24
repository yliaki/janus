#!/usr/bin/env python3
"""Prepare a Janus release: check, tag (locally), build the zip.

    scripts/release.py             # asks before tagging
    scripts/release.py --dry-run   # only the checks

The version is version.BASE (src/host/version.py): bump it and commit first.
Then this script:

  1. checks the working tree is clean (no changes, no untracked files: the
     packages ship untracked files too) and that the tag v<BASE> is new;
  2. tags HEAD v<BASE> (annotated, local only) and builds the zip with
     scripts/package.py --zip, which names it by the plain version because
     HEAD now carries the tag (janus-split-screen-<BASE>.zip; its VERSION
     file says <BASE>);
  3. prints the commands to publish it.

It never pushes the tag or creates a GitHub release: the maintainer does
that by hand. The website's Download button (janus_website:
hub/releases.py) sends people to the newest release's .zip, found by its
name ending in ".zip". (The .deb isn't released; package.py --deb still
builds one.)
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src' / 'host'))
from version import BASE  # noqa: E402

NAME = 'janus-split-screen'


def git(*args, check=True):
    return subprocess.run(['git', '-C', str(ROOT), *args], check=check,
                          capture_output=True, text=True).stdout.strip()


def fail(msg):
    sys.exit(f'release: {msg}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dry-run', action='store_true', help='only check, change nothing')
    ap.add_argument('-y', '--yes', action='store_true', help="don't ask before tagging")
    args = ap.parse_args()

    tag = f'v{BASE}'
    if git('status', '--porcelain'):
        fail('the working tree has changes or untracked files; commit, remove '
             'or .gitignore them first (git status)')
    if git('tag', '--list', tag):
        fail(f'tag {tag} exists already: bump BASE in src/host/version.py')
    head = git('log', '-1', '--format=%h %s')
    print(f'Prepare Janus {BASE} as {tag} from {head}')
    if args.dry_run:
        print('Dry run: all checks passed.')
        return
    if not args.yes and input('Tag (locally) and build it? [y/N] ').strip().lower() != 'y':
        fail('cancelled')

    git('tag', '-a', tag, '-m', f'Janus {BASE}')
    try:
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'package.py'), '--zip'], check=True)
    except subprocess.CalledProcessError:
        git('tag', '-d', tag)
        fail(f'the build failed; tag {tag} removed again')
    zip_path = ROOT / 'dist' / f'{NAME}-{BASE}.zip'
    if not zip_path.is_file():
        git('tag', '-d', tag)
        fail(f'not built: {zip_path.relative_to(ROOT)}; tag {tag} removed again')

    rel = zip_path.relative_to(ROOT)
    print(f'''
Tagged {tag} (local) and built {rel}.
Nothing was pushed or published. To release it, by hand:

  git push origin {tag}
  gh release create {tag} {rel} --title "Janus {BASE}" --generate-notes --verify-tag

(or create the release on GitHub and attach {rel.name}).
To undo instead: git tag -d {tag}''')


if __name__ == '__main__':
    main()
