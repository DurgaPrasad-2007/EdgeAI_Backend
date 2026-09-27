# Report generators

These scripts produced every figure and number in `EdgeFleet_Technical_Report`.

1. `extract.py` (run with the backend venv) reads the code: graph, benchmark, and traces of the head-on, deadlock, auction and dropout scenarios; writes `data.json`.
2. `figs.py`, `figs2.py` (need matplotlib) draw the figures from `data.json`; run `figs.py` then `figs2.py`.

`data.json` is the committed output of the last run. Compute-time figures vary between machines.
