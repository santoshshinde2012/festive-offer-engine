# Optional checkout browser verification

This harness starts a dedicated loopback server with temporary SQLite databases,
the real `OfferApi`, a frozen synthetic clock and `FakeProvider`. It opens the
customer UI in Chrome, exercises quote → application → capture → benefit delivery,
and takes desktop and mobile screenshots. It never calls a bank or a cloud service.

Run from the repository root with Python 3.10+, Node 18+ and a local Chrome/Chromium executable.
Playwright is an optional verification dependency; the application does not need it.
The observed browser run used Playwright 1.65.0.

```sh
BROWSER_QA_MODULES="$(mktemp -d -t festive-browser-modules)"
npm install --prefix "$BROWSER_QA_MODULES" playwright@1.65.0 --no-audit --no-fund
NODE_PATH="$BROWSER_QA_MODULES/node_modules" node tools/browser_qa.cjs
```

On macOS the runner uses the installed Google Chrome application. On another
system, set `CHROME_PATH` to a local executable. Set `PYTHON` if the Python
interpreter has a different command or absolute path.

```sh
CHROME_PATH=/path/to/chromium NODE_PATH="$BROWSER_QA_MODULES/node_modules" \
  node tools/browser_qa.cjs --output output/browser-qa/my-run
```

The default destination is a fresh `output/browser-qa-<timestamp>/` directory.
An existing destination is rejected unless `--replace` is explicit. Screenshots
and the verification manifest are copied into the destination only after every
check passes. A supplied `--replace` overwrites those three named artifacts; it
does not delete other files. Retained `docs/` evidence is never a default target.

The checks cover missing credentials, trusted quote values, full nominal
reservation, retry with the identical idempotency key after a lost response,
separate OWED/FULFILLED states, funding conservation, one durable payment and
obligation, dynamic text escaping, no browser credential storage, reset semantics
stale checkout and credential changes, malformed or mismatched response guards,
and layout at 1280px and 390px. The manifest records browser/tool versions and
hashes for every Python module under `src/festive_offers/`, the synthetic
dataset, all three web assets and both harness files.
It checks the entire source set before and after the browser run; a source
change during execution fails verification rather than binding a partial revision. No entered token, HTTP
headers or temporary database is retained.

The fixture token embedded in the harness is a deliberately public test string,
accepted only by the ephemeral fixture server. It is not a deployment credential.
Test-only `/_qa/` actions exist solely in `browser_qa_server.py`; they are absent
from the runtime API. The fixture's frozen clock also checks that browser wall
clock differences do not block a server-valid quote.

The runner closes Chrome, stops its server and removes temporary databases on
completion. Remove the optional npm directory when finished:

```sh
rm -rf "$BROWSER_QA_MODULES"
```
