# Zürich WG room scanner

A dependency-free Python 3.9+ command-line scanner for **shared-flat rooms on Flatfox and wgzimmer.ch**. Filter by monthly rent in CHF and a straight-line radius around a Swiss address, place, or coordinates. Run once, keep polling, or use your operating system's scheduler.

The default search focuses on **Bucheggstrasse 4–12, Zürich**, with a **CHF 800/month cap** and nearby alternatives within **1 km of Bucheggstrasse 10**. Matching advertised addresses at numbers 4–12 appear first and are marked `[PREFERRED]`; nearby alternatives follow by distance, then price. Move-in dates are unrestricted. Both temporary and ongoing rentals are included.

## Quick start

From this directory in PowerShell:

```powershell
# Your focused search (also used when running main.py without arguments)
python main.py scan
python main.py watch --new-only

# One scan: rooms up to CHF 1,000 within 5 km of Zürich HB
python main.py scan --max-price 1000 --center "Zürich HB" --radius-km 5 --preferred-address ""

# Run immediately, then scan again 30 minutes after each scan finishes
python main.py watch --max-price 1000 --center "Zürich HB" --radius-km 5 --preferred-address "" --interval-minutes 30 --new-only

# Use an exact point instead of geocoding a place name
python main.py scan --center "47.378178,8.540218" --max-price 1200 --radius-km 3

# A ring between 2 and 8 km, furnished rooms only
python main.py scan --min-distance-km 2 --radius-km 8 --furnished --preferred-address ""

# Search only wgzimmer, or only Flatfox
python main.py scan --sources wgzimmer --max-price 1000 --radius-km 5
python main.py scan --sources flatfox

python main.py --help
```

No packages or API keys are needed. `python main.py` scans both sources with the Bucheggstrasse defaults above. `--radius` and `--max-distance-km` are aliases for `--radius-km`; `--interval` is an alias for `--interval-minutes`. The interval applies to `watch` only. Stop watch mode with Ctrl+C. The machine must remain awake and the process running; watch mode does not install a background service. Polling intervals must be at least five minutes.

The wgzimmer adapter supports the current public search form, listing links, pagination and room details. It checks that the site applied the requested region, maximum rent and furnished filter; ignored filters or unexpected pages are reported as source errors. Flatfox continues if wgzimmer is unavailable; those exports are explicitly marked partial.

## Configuration

Copy `config.example.json` to `config.json`, edit it, then run:

```powershell
Copy-Item config.example.json config.json
python main.py scan --config config.json
python main.py watch --config config.json --new-only
```

The config is loaded only when you pass `--config`. CLI values override configuration values. `output_dir` is relative to the current working directory. Use a separate output directory for each concurrently running scanner.

`preferred_address` defaults to `"Bucheggstrasse 4-12"`. It accepts a street, a street plus house number, or an inclusive house-number range. Override it with `--preferred-address "Other Street 10-20"`, or use `--preferred-address ""` to disable the preference and sort by price, then distance. Preference is based on the advertised address, not mentions in the title or description. It changes ordering only: every room must still meet the rent and distance filters. `--center` and `--radius-km` control the search area independently. Changing the preference alone preserves seen-room history.

`sources` defaults to `["flatfox", "wgzimmer"]`. `wgzimmer_regions` defaults to `["zurich-stadt", "zurich"]`, covering Zürich city and canton. These existing config values are translated to the site's current identifiers, `zuerich` and `kanton-zuerich`. Both settings accept JSON arrays or comma-separated strings. Override regions with `--wgzimmer-regions zuerich` when you only want the city. Other regions must use the values from wgzimmer's current region selector (for example, `winterthur` or `kanton-aargau`). Regions control which wgzimmer listings are fetched; the distance filter is applied afterward. A wide radius outside those regions does not automatically expand wgzimmer coverage.

Named centers are resolved using the [Swiss Federal GeoAdmin search service](https://docs.geo.admin.ch/access-data/search.html). The scanner prints the selected location and coordinates: use coordinates for an unambiguous point. Zürich HB uses built-in coordinates and needs no geocoding request.

## Results

Each successful scan writes these files under `data/` (or `--output-dir`):

- `results.json`: current matching rooms, source, original links, rent and price basis, distance and location accuracy, availability, preferred-address match flags, new flags, filters, UTC scan timestamp, and per-source status/completeness information.
- `results.csv`: the same rooms in a spreadsheet-friendly table.
- `scanner.sqlite3`: persistent first-seen state, tracked separately for each combination of location, price, radius, and furnished filter.
- `wgzimmer-geocodes.json`: successfully resolved listing addresses, cached to avoid repeating geocoding requests.

All matches are new on the first run. Later runs flag only previously unseen matching listing IDs; price edits to an already seen room do not flag it as new. IDs include their source; duplicate listings within a provider are deduplicated, but a room advertised on both portals can appear twice. Enabling wgzimmer preserves existing Flatfox seen history. wgzimmer's seen history also includes the selected regions. `--new-only` affects terminal output; both exports always contain all current matches. Optional ntfy notifications are described below.

When one source fails, matching rooms from successful sources are exported with `complete: false`, a `[PARTIAL]` terminal heading, and an error under `sources` in JSON. Missing rooms from failed sources are not marked as seen. If all requested sources fail, previous result files are preserved. One-shot exit codes are **0** for a complete scan, **1** for a failed scan, **2** for partial results, and **3** when a scan produced results but ntfy delivery failed. Notification status is recorded separately under `notifications` in JSON; `complete` describes listing-source coverage. Watch mode retries after the configured interval. Export files are each replaced atomically; a filesystem failure between writes can leave their scan times out of sync.

## ntfy alerts for Bucheggstrasse

Set `NTFY_TOPIC` to enable notifications. Alerts cover **any advertised Bucheggstrasse address** that passes your rent, distance and furnished filters; nearby streets do not trigger them. The preferred 4–12 range still controls result ordering. Each alert includes the rent, address, move-in date, temporary/ongoing status and a clickable original listing link.

For GitHub Actions:

1. Choose a hard-to-guess topic name and subscribe to it in the ntfy app or web client on `https://ntfy.sh`.
2. In your GitHub repository, open **Settings → Secrets and variables → Actions → Secrets → New repository secret**. Add **`NTFY_TOPIC`** containing just the topic name (not its URL).
3. If your topic requires authentication, add an **`NTFY_TOKEN`** repository secret with an ntfy access token. For a different HTTPS ntfy server, set the **`NTFY_SERVER`** repository **variable** to its base URL.
4. Push the updated code and workflow, then run **Actions → Scan rooms → Run workflow**. The run summary shows how many alerts were sent or are awaiting delivery.

Locally in Bash:

```bash
export NTFY_TOPIC='your-private-topic-name'
# Optional for an authenticated topic or another server:
# export NTFY_TOKEN='your-access-token'
# export NTFY_SERVER='https://ntfy.example.com'
python main.py scan
# Or: python main.py watch --new-only
```

In PowerShell, use `$env:NTFY_TOPIC = 'your-private-topic-name'` before running the scanner. Without a topic, no notification requests are made. Keep the topic and token out of committed configuration files. Unprotected ntfy topics are accessible to anyone who knows their name; see [ntfy's topic and publishing documentation](https://docs.ntfy.sh/publish/#picking-a-topic).

The first enabled scan alerts for all current matching Bucheggstrasse listings, even if the scanner has already seen them. Successful deliveries are tracked by listing ID and destination in `data/scanner.sqlite3`, independently of scan newness. Later scans skip delivered listings; unsuccessful deliveries are retried if those listings are still present on a later scan. Changing the topic or deleting/losing the SQLite history causes current matches to be sent again. A room advertised on both portals may generate two alerts. A network timeout after ntfy accepted a message can also result in a repeat on retry.

## Scan every 30 minutes with GitHub Actions

The included [Scan rooms workflow](.github/workflows/scan.yml) runs one scan at minutes **7 and 37 of every hour (UTC)**. It also supports manual runs. Your computer can be switched off; GitHub runs the scanner.

1. Push this project, including `.github/workflows/scan.yml`, to your GitHub repository's **default branch** (usually `main`).
2. Open the repository's **Actions** tab and enable workflows if GitHub asks (including scheduled workflows on a fork).
3. Select **Scan rooms → Run workflow** to test it immediately. Scheduled runs then happen automatically.
4. Open a completed run to see its summary with new rooms and source status. Download the **room-results-…** artifact for `results.json` and `results.csv`.

The workflow reads the tracked `config.example.json`, which contains your Bucheggstrasse preference, CHF 800 cap and 1 km radius. Edit that file and push it to change the hosted search. A local, ignored `config.json` is not uploaded. Scanning needs no API keys; optional ntfy alerts use the settings described above.

Each run restores the SQLite seen-room and notification history and geocoding cache, then saves updated state under a new Actions cache key. Scans on the same branch do not overlap. Results are uploaded separately and kept for seven days. If GitHub evicts or you delete the state cache, all matching rooms will be new on the next run and ntfy alerts may repeat. The first hosted scan also starts fresh, independently of local history. The workflow summarizes new matches and sends ntfy alerts when configured.

A partially successful scan uploads its available results and emits a workflow warning. A scan where both sources fail makes the job fail and does not upload stale results. An ntfy delivery failure also fails the job, but preserves current results and successful delivery history so the next scheduled scan can retry remaining alerts. The workflow runs `scan` once per job; do not change it to the continuously running `watch` command.

GitHub schedules are approximate and can be delayed or dropped during heavy load. Scheduled workflows only run from the default branch, and public-repository schedules are disabled after 60 days without repository activity. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Private repositories use your account's Actions allowance; check [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) before leaving it running indefinitely.

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
- Distance is the great-circle distance to advertised coordinates or a geocoded wgzimmer street address, not a walking, driving, or public-transport commute. The optional inner radius excludes rooms closer than that distance. wgzimmer's map points may be approximate by about 200 m and are marked `advertised_approximate` in `location_accuracy`. When no map point is available, street addresses are resolved against GeoAdmin and cached; unrelated/fuzzy address results and city-only locations are excluded. Unresolved locations are counted in JSON and mark that source incomplete.
- Flatfox requires a known positive monthly gross rent, or net rent plus known charges. wgzimmer uses its explicitly labeled **advertised monthly rent**; verify whether utilities are included on the original listing. `price_basis` distinguishes `gross_monthly` from `advertised_monthly`. Missing/nonmonthly prices are excluded. Deposits and other one-time fees are not part of the monthly cap.
- `--furnished` requires an explicit furnished flag or a labeled "Furnished: Yes" field. wgzimmer rooms without this field are excluded when that filter is enabled; furnishing is not guessed from free text.
- Map prices are not used for filtering; full listing details are fetched first. Capped map areas are subdivided and IDs deduplicated. A request/depth limit fails the scan explicitly instead of silently returning a truncated search. Listings disappearing between discovery and detail fetch are reported, with `complete: false` in JSON.
- Each source's requests are spaced at least one second apart and transient errors are retried with backoff. wgzimmer pagination keeps a cookie session, follows all result pages, and rejects repeated/missing pagination. Each scan has a request budget (200 for Flatfox, 600 for wgzimmer); exceeding it fails that source instead of silently truncating results. Authentication, CAPTCHAs, or source restrictions are not bypassed. Public endpoints can change; failures are surfaced rather than reported as zero rooms.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use local fixtures and mock clients, without contacting listing providers. The wgzimmer fixtures include reduced public search/detail HTML snapshots from 9 November 2026, alongside legacy examples.
