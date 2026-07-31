# Tests

- `run_live.py` — end-to-end live run against the real API. Writes `.tex`, `.pdf`, `.json`
  and a summary line to `results/`.
- `check_offline.py` — fast, no-API checks: templates compile, prompts format, guards fire.
- `results/` — accumulated run artifacts and the benchmark table.

```bash
set -a && . ./.env && set +a          # ANTHROPIC_API_KEY
python3 tests/check_offline.py         # seconds, no API cost
python3 tests/run_live.py <name> <jd-file> <dump-file> [aggressiveness] [template]
```
