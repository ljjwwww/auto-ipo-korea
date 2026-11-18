"""Noninteractive scheduler entry: explicitly restore per-user tool discovery."""
import os
from pathlib import Path
import sys


def prepare_path(original, home, python):
    paths=[str(Path(python).parent),str(Path(home)/'.local'/'bin')]
    paths.extend(original.split(os.pathsep))
    return os.pathsep.join(dict.fromkeys(path for path in paths if path))


def main(argv=None):
    os.environ['PATH']=prepare_path(os.environ.get('PATH',''),Path.home(),sys.executable)
    from . import main as cli
    return cli(sys.argv[1:] if argv is None else argv)


if __name__=='__main__':
    raise SystemExit(main())
