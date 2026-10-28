"""ClosureOps model code.

Makes `src/` an importable package so every entry point uses the same style:
    from src.batching import Params
and every script runs from the project root as a module:
    python -m src.plan data/inactive_wells_joined.csv 50000000

WHY a package instead of plain scripts: the app, the scripts and the tests all
import these modules. Running them as a package means one import style works
everywhere, with no sys.path juggling and no "works in one folder only" bugs.
"""
