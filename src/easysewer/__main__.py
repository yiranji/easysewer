"""The same CLI as the installed easysewer console entry point."""
from .doctor import main

if __name__ == '__main__':
    raise SystemExit(main())
