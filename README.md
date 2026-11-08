# Zürich WG room scanner

A dependency-free Python 3.9+ command-line scanner for **shared-flat rooms on Flatfox and wgzimmer.ch**. Filter by monthly rent in CHF and a straight-line radius around a Swiss address, place, or coordinates. Run once, keep polling, or use your operating system's scheduler.

## Quick start

From this directory in PowerShell:

```powershell
# One scan: rooms up to CHF 1,000 within 5 km of Zürich HB
python main.py scan --max-price 1000 --center "Zürich HB" --radius-km 5

# Run immediately, then scan again 30 minutes after each scan finishes
python main.py watch --max-price 1000 --center "Zürich HB" --radius-km 5 --interval-minutes 30 --new-only

# Use an exact point instead of geocoding a place name
python main.py scan --center "47.378178,8.540218" --max-price 1200 --radius-km 3

# A ring between 2 and 8 km, furnished rooms only
python main.py scan --min-distance-km 2 --radius-km 8 --furnished

# Search only wgzimmer, or only Flatfox
python main.py scan --sources wgzimmer --max-price 1000 --radius-km 5
python main.py scan --sources flatfox

python main.py --help
```

No packages or API keys are needed. `python main.py` scans both sources with a CHF 1,200 maximum and a 5 km radius around Zürich HB. `--radius` and `--max-distance-km` are aliases for `--radius-km`; `--interval` is an alias for `--interval-minutes`. The interval applies to `watch` only. Stop watch mode with Ctrl+C. The machine must remain awake and the process running; watch mode does not install a background service. Polling intervals must be at least five minutes.

**Live wgzimmer verification is currently blocked:** the site fails TLS certificate validation from this environment. The adapter is implemented and tested against synthetic fixtures matching its publicly documented HTML structure, but no successful live wgzimmer scan has been verified. Search forms or CAPTCHA responses are reported as source errors. TLS verification stays enabled. Flatfox continues when wgzimmer is unavailable; those exports are explicitly marked partial.

## Configuration

Copy `config.example.json` to `config.json`, edit it, then run:

```powershell
Copy-Item config.example.json config.json
python main.py scan --config config.json
python main.py watch --config config.json --new-only
```

The config is loaded only when you pass `--config`. CLI values override configuration values. `output_dir` is relative to the current working directory. Use a separate output directory for each concurrently running scanner.

`sources` defaults to `["flatfox", "wgzimmer"]`. `wgzimmer_regions` defaults to `["zurich-stadt", "zurich"]`, covering Zürich city and its surrounding region. Both accept JSON arrays or comma-separated strings. Override regions with `--wgzimmer-regions zurich-stadt` when you only want the city. Regions control which wgzimmer listings are fetched; the distance filter is applied afterward. A wide radius outside those regions does not automatically expand wgzimmer coverage.

Named centers are resolved using the [Swiss Federal GeoAdmin search service](https://docs.geo.admin.ch/access-data/search.html). The scanner prints the selected location and coordinates: use coordinates for an unambiguous point. Zürich HB uses built-in coordinates and needs no geocoding request.

## Results

Each successful scan writes these files under `data/` (or `--output-dir`):

- `results.json`: current matching rooms, source, original links, rent and price basis, distance and location accuracy, availability, new flags, filters, UTC scan timestamp, and per-source status/completeness information.
- `results.csv`: the same rooms in a spreadsheet-friendly table.
- `scanner.sqlite3`: persistent first-seen state, tracked separately for each combination of location, price, radius, and furnished filter.
- `wgzimmer-geocodes.json`: successfully resolved listing addresses, cached to avoid repeating geocoding requests.

All matches are new on the first run. Later runs flag only previously unseen matching listing IDs; price edits to an already seen room do not flag it as new. IDs include their source; duplicate listings within a provider are deduplicated, but a room advertised on both portals can appear twice. Enabling wgzimmer preserves existing Flatfox seen history. wgzimmer's seen history also includes the selected regions. `--new-only` affects terminal output; both exports always contain all current matches. The scanner prints results and original listing links; it does not send external notifications.

When one source fails, matching rooms from successful sources are exported with `complete: false`, a `[PARTIAL]` terminal heading, and an error under `sources` in JSON. Missing rooms from failed sources are not marked as seen. If all requested sources fail, previous result files are preserved. One-shot exit codes are **0** for a complete scan, **1** for a failed scan, and **2** for partial results. Watch mode retries all selected sources after the configured interval. Export files are each replaced atomically; a filesystem failure between writes can leave their scan times out of sync.

## Schedule with Windows Task Scheduler

For periodic scans without leaving a terminal open, create a basic task in **Task Scheduler**, then edit its trigger to repeat every 30 minutes. Under **Actions → Start a program**, use:

- **Program/script:** your full Python executable path (find it with `python -c "import sys; print(sys.executable)"`).
- **Add arguments:** `main.py scan --config config.json --new-only`
- **Start in:** `C:\Users\Jakub\Desktop\room-scanner`

In task settings, choose **Do not start a new instance** if the task is already running. Choose whether Windows should wake the computer to run it. This repository does not register a scheduled task automatically.

For cron on Linux/macOS, use absolute paths:

```cron
*/30 * * * * cd /path/to/room-scanner && /usr/bin/python3 main.py scan --config config.json >> scanner.log 2>&1
```

## Coverage and limits

- Sources are Flatfox's public shared-room map/listing endpoints and wgzimmer's English public search/detail pages. WOKO's own website, Homegate, and other portals are not scanned. A WOKO room advertised on a supported portal may appear.
- Only active Swiss rental listings explicitly categorized as shared rooms are included. This is not a scan of all Zürich housing portals.
- Distance is the great-circle distance to advertised coordinates or a geocoded wgzimmer street address, not a walking, driving, or public-transport commute. The optional inner radius excludes rooms closer than that distance. Advertised locations may be approximate. wgzimmer addresses are resolved against GeoAdmin and cached; unrelated/fuzzy address results and city-only locations are excluded. `location_accuracy` identifies advertised versus geocoded locations, and unresolved locations are counted in JSON and mark that source incomplete.
- Flatfox requires a known positive monthly gross rent, or net rent plus known charges. wgzimmer uses its explicitly labeled **advertised monthly rent**; verify whether utilities are included on the original listing. `price_basis` distinguishes `gross_monthly` from `advertised_monthly`. Missing/nonmonthly prices are excluded. Deposits and other one-time fees are not part of the monthly cap.
- `--furnished` requires an explicit furnished flag or a labeled "Furnished: Yes" field. wgzimmer rooms without this field are excluded when that filter is enabled; furnishing is not guessed from free text.
- Map prices are not used for filtering; full listing details are fetched first. Capped map areas are subdivided and IDs deduplicated. A request/depth limit fails the scan explicitly instead of silently returning a truncated search. Listings disappearing between discovery and detail fetch are reported, with `complete: false` in JSON.
- Each source's requests are spaced at least one second apart and transient errors are retried with backoff. wgzimmer pagination keeps a cookie session, follows all result pages, and rejects repeated/missing pagination. Each scan has a request budget (200 for Flatfox, 600 for wgzimmer); exceeding it fails that source instead of silently truncating results. Authentication, CAPTCHAs, or source restrictions are not bypassed. Public endpoints can change; failures are surfaced rather than reported as zero rooms.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use local fixtures and mock clients, without contacting listing providers.
