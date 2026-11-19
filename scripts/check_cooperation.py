#!/usr/bin/env python3
"""systemd ExecCondition: disabled is skipped; configuration errors are failures."""
import argparse
from pathlib import Path
import sys

from bringup_config import load_config
from cooperation import validate
from cooperation_workspace import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--task-workspace', type=Path, required=True)
    parser.add_argument('--config', default='')
    args = parser.parse_args()
    try:
        if validate(load_config('system', args.config, resolve=False)) == 'disabled':
            print('Cooperation disabled by device configuration.')
            return 1  # ExecCondition 1..254 skips; does not trigger Restart.
        check(args.workspace.resolve(), args.task_workspace.resolve())
        if not (args.workspace / 'install/local_setup.bash').is_file():
            raise ValueError(f'build cooperation_ws first: {args.workspace}/install/local_setup.bash')
    except Exception as exc:
        print(f'Cooperation startup error: {exc}', file=sys.stderr)
        return 255  # ExecCondition failure; no silent disabled fallback.
    return 0


if __name__ == '__main__':
    sys.exit(main())
