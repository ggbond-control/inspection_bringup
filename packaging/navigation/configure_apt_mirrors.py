#!/usr/bin/env python3
"""Replace Ubuntu/ROS apt endpoints while preserving suites and signing keys."""

from pathlib import Path
import re
import subprocess

MIRROR = 'https://mirror.sysu.edu.cn'


def configure(apt, architecture):
    if architecture not in ('amd64', 'arm64'):
        raise ValueError(f'unsupported builder architecture: {architecture}')
    ubuntu = MIRROR + ('/ubuntu/' if architecture == 'amd64' else '/ubuntu-ports/')
    patterns = [
        (r'https?://(?:(?:[\w-]+\.)?archive\.ubuntu\.com/ubuntu|'
         r'security\.ubuntu\.com/ubuntu|ports\.ubuntu\.com/ubuntu-ports)/*', ubuntu),
        (r'https?://packages\.ros\.org/ros2/ubuntu/*', MIRROR + '/ros2/ubuntu/'),
    ]
    files = [apt / 'sources.list', *sorted((apt / 'sources.list.d').glob('*.list')),
             *sorted((apt / 'sources.list.d').glob('*.sources'))]
    counts = [0, 0]
    for path in files:
        if not path.is_file():
            continue
        original = text = path.read_text()
        for index, (pattern, replacement) in enumerate(patterns):
            text, count = re.subn(pattern, replacement, text)
            counts[index] += count
        # SYSU mirrors ROS binaries but does not publish the source index.
        if path.suffix == '.sources':
            stanzas = text.split('\n\n')
            for index, stanza in enumerate(stanzas):
                if MIRROR + '/ros2/ubuntu/' in stanza:
                    stanzas[index] = re.sub(r'^Types:.*$', 'Types: deb', stanza, flags=re.MULTILINE)
            text = '\n\n'.join(stanzas)
        else:
            text = ''.join(line for line in text.splitlines(keepends=True)
                           if not (line.lstrip().startswith('deb-src ') and
                                   MIRROR + '/ros2/ubuntu/' in line))
        if text != original:
            path.write_text(text)
    if not all(counts):
        raise ValueError(f'expected Ubuntu and ROS 2 sources in base image; replacements={counts}')


if __name__ == '__main__':
    configure(Path('/etc/apt'), subprocess.check_output(
        ['dpkg', '--print-architecture'], text=True).strip())
