"""python -m finquint.orchestration.verify --base origin/main [--fail-on-protected]"""
import sys

from .paths import main

sys.exit(main())
