# nmea-monitor project
Python scripts and HTML pages to monitor, filter and present NMEA-0183 streams.

## nmea-monitor.html

`nmea-monitor.html` is a simple browser-based weather dashboard for the NMEA repository. It fetches the latest weather summary from the repository's `/nmea/weather/WSC/Barge` endpoint every 10 seconds and displays three current conditions: local date/time, temperature, and wind.

The page is intentionally lightweight: a single responsive table, a dynamic title/header based on the returned org and source, and a status line showing when the data was last updated or whether an error occurred. It also formats wind speed and direction cleanly, including compass symbols and degree details when available, so it works as a compact "current weather at source" display for live NMEA data.

## nmea_filter.py

`nmea_filter.py` is a command-line tool that reads NMEA-0183 data from either a serial port or an input text file, salvages any valid embedded NMEA sentences from each incoming line, and emits filtered or aggregated output for downstream storage or upload. It can auto-discover a live NMEA serial source, wait for the first valid `ZDA` before starting, and then collect only the requested sentence types, with `ZDA,MWD,MDA,GGA` as the default filter.

By default, aggregation is enabled with `-s 1`, which means the program tries to collect one valid sample of each requested secondary type (`MWD`, `MDA`, `GGA`) before emitting a batch. The batch stays open until either all requested secondary types reach their sample target or the `-x/--max-zda-seek` limit is reached, using valid `ZDA` sentences as the time anchor. Each aggregated batch contains at most one `ZDA`, one averaged `MWD`, one averaged `MDA`, and one averaged `GGA`. If `-s 0` is used, aggregation is disabled and every valid filtered sentence is emitted. Directional values use circular averaging and GPS positions use geographic averaging. Output can be written to stdout for debugging, saved as `.nmea` files, or uploaded as JSON to a configured API endpoint. The program also includes retry logic for serial/API failures and writes exceptions to dated log files under `logs/`.

Usage: see `--help` options.

## nmea_repository.py

`nmea_repository.py` is a small Flask-based REST service that stores filtered NMEA batches produced by `nmea_filter.py`. It saves each message as one JSON object per line in daily `.jsonl` files, grouped by organization and source, and gzip-compresses older daily files when a new day starts.

The service acts as a lightweight repository for uploaded NMEA frame data, with support for organization registration, bearer-token access control, retention settings, message purge operations, and retrieval/query endpoints. By default it uses a local `data/` folder, but the storage root can be overridden with `NMEA_REPOSITORY_DATA_ROOT`.

Storage layout:

* message files live under `data/messages/<org>/<source>/`
* daily files are named `org-source-yyyy-mm-dd.nmea.jsonl`
* older daily files may be compressed as `org-source-yyyy-mm-dd.nmea.jsonl.gz`
* registrations are stored in `data/registrations/registrations.json`
* `org` and `source` are treated case-insensitively by the API and normalized to lowercase in storage

Endpoints:

* `POST /nmea/add/<org>/<source>`
  Accepts a JSON body containing `start` and `sentences`, and creates or updates the stored message for that `org/source/start` time. Requires a bearer token matching the organization registration.
* `POST /nmea/registrations`
  Creates a new organization registration with its authentication token and retention settings. Creating the first `admin` registration is a bootstrap case; later registrations require the admin bearer token.
* `GET /nmea/registrations`
  Returns all registrations, including each organization's retention span and retention limit. Requires the admin bearer token.
* `GET /nmea/registrations/<org>`
  Returns the registration for the given organization. This endpoint does not require authentication.
* `DELETE /nmea/registrations/<org>`
  Deletes the specified registration and all stored messages for that organization. Returns `volume_deleted`, which is the number of stored daily files removed. Requires the admin bearer token.
* `DELETE /nmea/purge/<org>/<source>`
  Purges stored messages for the source using the registered retention settings for that organization. Returns `volume_deleted`, which is the number of stored daily files removed. Requires the organization bearer token.
* `DELETE /nmea/purge/<org>/<source>/<what>`
  Purges stored messages for the source using an explicit keep rule or override value, rather than the default registration settings. If `what` is an integer, it is treated as the number of daily files to keep. Otherwise it is treated as a timespan such as `30-days` or `1-month`. Returns `volume_deleted`, which is the number of stored daily files removed. Requires the organization bearer token.
* `GET /nmea/last/<org>/<source>`
  Returns the latest stored message for the given `org/source` as plain text.
* `GET /nmea/last/<org>/<source>/<what>`
  Returns the latest stored messages for the given `org/source` as plain text. If `what` is an integer, it returns that many latest messages. Otherwise it treats `what` as a timespan such as `1-day` and returns all messages from now minus that span.
* `GET /nmea/count`
  Returns the total number of stored messages across all organizations and sources, optionally filtered by `start`, `end`, and `span`.
* `GET /nmea/count/<org>`
  Returns the number of stored messages for the given organization, optionally filtered by `start`, `end`, and `span`.
* `GET /nmea/count/<org>/<source>`
  Returns the number of stored messages for the given organization and source, optionally filtered by `start`, `end`, and `span`. This count is the number of stored message records, meaning the number of JSONL lines that match the filter.
* `GET /nmea/find/<org>/<source>?start=<time>&end=<time>&span=<timespan>`
  Returns matching stored messages for the given `org/source`, optionally filtered by a date/time range and/or span. If no filter is supplied, it returns the latest message.
* `GET /nmea/history/<org>/<source>?start=<time>&end=<time>&span=<timespan>`
  Returns plot-ready wind history for the given `org/source` as JSON, optionally filtered by a date/time range and/or span. When only `span` is supplied, the trailing window is anchored to the latest stored record rather than current wall-clock time, so somewhat stale feeds still return history. The response includes `org`, `source`, the supplied filter values, and a `samples` array ordered from oldest to newest.
* `GET /nmea/weather/<org>/<source>`
  Returns a weather-oriented summary view derived from the most recent stored message(s) for the `org/source`.
* `GET /nmea/weather/<org>/<source>/<count>`
  Returns a weather-oriented summary view derived from the most recent `count` stored messages for the `org/source`.
* `GET /<filename>.html`
  Serves a static HTML file from the repository directory.

Time query values use `yyyy[-mm[-dd[:hh[:mm[:ss]]]]]`.

Timespan values use forms such as `30-seconds`, `15-minutes`, `2-hours`, `7-days`, `1-month`, or `1-year`.

Authorization notes:

* all protected calls use `Authorization: Bearer <token>`
* creating the first `admin` registration is a bootstrap case: the bearer token must match the posted `auth`
* later registrations require the `admin` bearer token
* adding messages and purging data require the bearer token for the target organization
* authorization failures return HTTP `402` with `{ "error": "access denied" }`

## How to install on Raspberry PI

### use Raspberry PI imager to create headless OS
* 4Gb/16Gb+
* enable RP Connect so we can connect remotely

### check standard installation
* `python3 --version` # Python `3.13.5`
* `sudo apt install python3-pip`
* `sudo nmtui` # determine IP address eg. `192.168.1.10`, setup wifi etc
* You might also want to install CUPS/SAMBA to enable AirPrint

### Install and open firewall
* `sudo apt install ufw -y`
* `sudo ufw allow ssh`
* `sudo ufw allow http`
* `sudo ufw allow 8080/tcp`
* `sudo ufw enable`
* `sudo ufw status verbose`

### Install and configure nginx
* `sudo apt install nginx -y`
* `sudo nano /etc/nginx/sites-available/gunicorn` # create as below
```text
server {
    listen 8080 default_server;

    # This tells Nginx to catch all traffic coming to the Pi's IP
    server_name _;

    location / {
        # 1. This is the most important part
        # It sends traffic to the 'bridge' (Gunicorn) running your app
        proxy_pass http://127.0.0.1:8000;

        # 2. These pass original user info (like their IP) to your Python app
        include proxy_params;
    }

    # 3. Optional: If you have images or CSS in a folder
    # location /static {
    #    alias /var/www/python/nmea/static;
    #}
}
```
* `sudo ln -s /etc/nginx/sites-available/gunicorn /etc/nginx/sites-enabled/`
* `sudo systemctl restart nginx`

### Create and prepare code directory
* `sudo mkdir /var/www/python/nmea`
* `cd /var/www/python/nmea`
* `sudo scp *.py *.html requirements.txt .`
* `python3 -m venv venv`
* `source venv/bin/activate`
* `python3 -m pip install flask gunicorn`
* `python3 -m pip install -r requirements.txt`
* `deactivate`

### Test nmea_repository.py
* `source venv/bin/activate`
* `gunicorn --bind 0.0.0.0:8000 nmea_repository:app`
* `http://192.168.1.10:8080/nmea/count`
* `deactivate`

### Test nmea_filter.py
* `source venv/bin/activate`
* `python3 nmea_filter.py -u http://127.0.0.1:8080/nmea/add/WSC/Barge -a MyApiKey -s 1 -x 5`
* `deactivate`

### Create nmea_repository.service
* `sudo nano /etc/systemd/system/nmea_repository.service`
```text
[Unit]
Description=Gunicorn instance to serve the nmea_repository App
After=network.target

[Service]
User=russel
Group=www-data
WorkingDirectory=/var/www/python/nmea
# Path to the Gunicorn executable inside your virtual environment
Environment="PATH=/var/www/python/nmea/venv/bin"
ExecStart=/var/www/python/nmea/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:8000 -m 007 nmea_repository:app
Restart=always

[Install]
WantedBy=multi-user.target
```
* `sudo systemctl daemon-reload`
* `sudo systemctl start nmea_repository.service`
* `sudo systemctl enable nmea_repository.service`

### Create nmea_filter.service
* `sudo nano /etc/systemd/system/nmea_filter.service`
```text
[Unit]
Description=Service to run nmea_filter pythong script
After=multi-user.target

[Service]
# Run as specific user (usually 'pi')
User=russel

# Set working directory so relative paths in your script work
WorkingDirectory=/var/www/python/nmea
# Use full paths for the Python interpreter and your script
ExecStart=/usr/bin/python3 /var/www/python/nmea/nmea_filter.py @/var/www/python/nmea/nmea_filter.txt
# Automatically restart if the script crashes
Restart=always

[Install]
WantedBy=multi-user.target
```
* where `nmea_filter.txt` contains
```text
-u http://127.0.0.1:8080/nmea/add/WSC/Barge
-a MyApiKey
-s 1
-x 5
```
* `sudo systemctl daemon-reload`
* `sudo systemctl start nmea_filter.service`
* `sudo systemctl enable nmea_filter.service`

### Troubleshooting Services
* Ensure the following user/group ownership and permissions to allow service to access
** `drwxr-xr-x 12 root   root     4096 Apr 23 18:46 /var`
** `drwxr-xr-x 4  root   root     4096 Apr 24 12:50 /var/www`
** `drwxr-xr-x 4  russel www-data 4096 Apr 23 20:32 /var/www/python`
* if there are errors,
** `sudo systemctl status nmea_repository.service`
** `journalctl -u nmea_repository | tail`
** `sudo ss -tulpn | grep :8000`

### Install Cloudfare tunnel
* Use the "zero trust" panel in cloudflare to create `wsc.arcsite.ca` tunnel
