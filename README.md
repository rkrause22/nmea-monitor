# nmea-monitor project
Python scripts and HTML pages to monitor, filter and present NMEA-0183 streams.

## HTML display pages

`nmea-frame.html` is the kiosk-style landing page for the live display. It frames the operational views with the configured organization and source query parameters, so a display or browser shortcut can open one stable page such as `nmea-frame.html?org=WSC&src=Barge`.

`nmea-plot.html` shows wind history for a selected period. It uses `/nmea/history/<org>/<source>` to draw the raw sample trace, rolling average, directional indicators, and average/gust segment bars. By default it follows the latest data over a recent window and refreshes automatically for live monitoring.

`nmea-map.html` shows the source position and recent weather summary on a Google map. It uses `/nmea/weather/<org>/<source>/<count>` for smoothed current conditions, including wind, temperature, barometric pressure, and windward/startpin reference points when available.

## nmea_filter.py

`nmea_filter.py` is a command-line tool that reads NMEA-0183 data from either a serial port or an input text file, salvages any valid embedded NMEA sentences from each incoming line, and emits filtered or aggregated output for downstream storage or upload. It can auto-discover a live NMEA serial source, wait for the first valid `ZDA` before starting, and then collect only the requested sentence types, with `ZDA,MWD,MDA,GGA` as the default filter.

By default, aggregation is enabled with `-s 1`, which means the program tries to collect one valid sample of each requested secondary type (`MWD`, `MDA`, `GGA`) before emitting a batch. The batch stays open until either all requested secondary types reach their sample target or the `-x/--max-zda-seek` limit is reached, using valid `ZDA` sentences as the time anchor. Each aggregated batch contains at most one `ZDA`, one averaged `MWD`, one averaged `MDA`, and one averaged `GGA`. If `-s 0` is used, aggregation is disabled and every valid filtered sentence is emitted. Directional values use circular averaging and GPS positions use geographic averaging. Output can be written to stdout for debugging, saved as `.nmea` files, or uploaded as JSON to a configured API endpoint. The optional `--dump` flag writes the raw inbound text stream directly to stderr before parsing, which is useful for inspecting damaged LORA traffic without interfering with JSON output. The program also includes retry logic for serial/API failures and writes exceptions to dated log files under `logs/`.

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

Repository architecture:

* `nmea_repository.py` owns the Flask routes and endpoint response shapes.
* `repository_service.py`, `repository_store.py`, and `repository_store_by_file.py` own repository operations and file storage.
* `nmea_weather_window.py` parses stored records into weather samples, then builds period and segment summaries for `/nmea/history`, `/nmea/weather`, and `/nmea/pws`.
* `nmea_plot_analysis.py` adds plot-focused analysis over the history sample stream.
* `nmea_pws.py` formats and uploads Weather Underground PWS observations.
* `nmea_helpers.py` contains shared date/time, math, logging, and presentation helpers.
* `FrameAggregator` remains part of `nmea_aggregation.py` and is used by `nmea_filter.py` to produce uploaded frame records. The repository analysis endpoints do not use `FrameAggregator`.

Endpoints:

* `POST /nmea/add/<org>/<source>`
  Accepts a JSON body containing `start` and `sentences`, and appends the stored message for that `org/source` stream. Requires a bearer token matching the organization registration.
* `POST /nmea/registrations`
  Creates or updates an organization registration with its authentication token, retention settings, Google Maps key, and Weather Underground PWS credentials. Creating the first `admin` registration is a bootstrap case; later registration creates and updates require the admin bearer token. New registrations require `org` and `auth`; existing registrations can be partially updated by posting `org` plus only the fields to change. Supported mutable fields are `auth`, `span`, `limit`, `gkey`, `pwsid`, and `pwskey`.
* `GET /nmea/registrations`
  Returns all registrations, including each organization's retention span, retention limit, Google Maps key, PWS station ID, and PWS key. Requires the admin bearer token.
* `GET /nmea/registrations/<org>`
  Returns the registration for the given organization, excluding secret values such as the PWS key. This endpoint does not require authentication.
* `DELETE /nmea/registrations/<org>`
  Deletes the specified registration and all stored messages for that organization. Returns `files_deleted`, which is the number of stored daily files removed. Requires the admin bearer token.
* `DELETE /nmea/purge/<org>/<source>`
  Purges stored messages for the source using the registered retention settings for that organization. Returns `files_deleted`, which is the number of stored daily files removed. Requires the organization bearer token.
* `DELETE /nmea/purge/<org>/<source>/<what>`
  Purges stored messages for the source using an explicit keep rule or override value, rather than the default registration settings. If `what` is an integer, it is treated as the number of daily files to keep. Otherwise it is treated as a timespan such as `30-days` or `1-month`. Returns `files_deleted`, which is the number of stored daily files removed. Requires the organization bearer token.
* `POST /nmea/fix/<org>/<source>` and `POST /nmea/fix/<org>/<source>/<yyyy-mm-dd>`
  Rewrites a daily message file for the source, removing corrupt rows that cannot be parsed as stored JSON records. If no date is supplied, the current UTC date is used. Returns the target date, removed row count, and details for corrupt rows. Requires the organization bearer token.
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
  Returns plot-ready wind history for the given `org/source` as JSON, optionally filtered by a date/time range and/or span. `start` with `span` creates a window beginning at `start`; `end` with `span` creates a window ending at `end`; and `span` alone creates a trailing window ending at the current time. When both `start` and `end` are supplied, `span` is ignored. The response includes `org`, `source`, the supplied filter values, a `samples` array ordered from oldest to newest, analysis metadata, and a `window` object with six segment summaries containing average wind and gust wind for each segment. The sample stream supports the polar plot's raw and rolling traces; the segment summaries support the average/gust bar graph.
* `GET /nmea/weather/<org>/<source>`
  Returns a weather-oriented summary view derived from the most recent stored message for the `org/source`. This route also accepts `start`, `end`, and `span` query parameters like the history endpoint; when those filters are supplied, the weather summary is built from the matching messages instead of the default latest message. Weather JSON includes position, temperature, average wind, gust wind, barometric pressure, windward/startpin map positions, and a `window` summary.
* `GET /nmea/weather/<org>/<source>/<count>`
  Returns a weather-oriented summary view derived from the most recent `count` stored messages for the `org/source`. This is useful for smoothing current map/monitor output over the last few uploaded frame records.
* `GET /nmea/pws/<org>/<source>`
  Builds a 10-minute weather window from the latest stored message time, divides it into five equal two-minute segments, and uploads the latest segment's current conditions plus the 10-minute gust to Weather Underground using the PWS Upload Protocol. Requires the organization bearer token and registration fields `pwsid` and `pwskey`. The upload sends `winddir`, `windspeedmph`, `windgustmph`, `windgustdir`, `tempf`, `baromin`, `windspdmph_avg2m`, `winddir_avg2m`, `windgustmph_10m`, and `windgustdir_10m` when the source data can provide them.
* `GET /<filename>.html`
  Serves a static HTML file from the repository directory.

`/nmea/last`, `/nmea/find`, and `/nmea/count` are repository retrieval/query endpoints: they do not parse weather values or run weather-window analysis. `/nmea/history`, `/nmea/weather`, and `/nmea/pws` are analysis/reporting endpoints backed by `nmea_weather_window.py`.

Time query values use ISO-style forms such as `2026`, `2026-06`, `2026-06-20`, `2026-06-20T14`, `2026-06-20T14:00`, or `2026-06-20T14:00:00-06:00`. Missing time parts default to zero.

Timespan values use forms such as `30-seconds`, `15-minutes`, `2-hours`, `7-days`, `1-month`, or `1-year`.

Authorization notes:

* all protected calls use `Authorization: Bearer <token>`
* creating the first `admin` registration is a bootstrap case: the bearer token must match the posted `auth`
* later registrations require the `admin` bearer token
* adding messages and purging data require the bearer token for the target organization
* uploading PWS observations requires the bearer token for the target organization
* authorization failures return HTTP `401` with `{ "error": "access denied" }`

## How to install on Raspberry PI

### use Raspberry PI imager to create headless OS
* 4Gb/16Gb+
** note the name and version of the (headless) OS
*** Be sure to name the OS whenever doing any Gemini/ChatGPT queries to get the right commands!
** enable RP Connect so we can connect remotely
** create the admin user as "pi" (and write down the passwd somewhere!)
* After you're up and running, run sudo apt update / upgrade

### check standard installation
* `python3 --version` # Python `3.13.5`
* `sudo apt install python3-pip`
* `sudo nmtui` # use netmanager to setup a fixed (non-dchp) address for pi on club network
* You might also want to install CUPS/SAMBA to enable AirPrint

### Install and open firewall
* `sudo apt install ufw -y`
* `sudo ufw allow ssh`
* `sudo ufw allow http`
* `sudo ufw allow 631/tcp`
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
* Ensure your user can access the files and the service group can access them too
** sudo chown -R pi:www-data /var/www/python/nmea
** sudo chmod -R 775 /var/www/python/nmea
** sudo chmod g+s /var/www/python/nmea
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
* `curl http://127.0.0.1:8080/nmea/count` => 0
* `deactivate`

### Test nmea_filter.py
* `source venv/bin/activate`
* `python3 nmea_filter.py -u http://127.0.0.1:8080/nmea/add/WSC/Barge -a MyApiKey -s 3 -x 10`
* `deactivate`

### Create nmea-repository.service
* `sudo nano /etc/systemd/system/nmea-repository.service`
```text
[Unit]
Description=Gunicorn instance to serve the nmea_repository App
After=network.target

[Service]
User=pi
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
* `sudo systemctl start nmea-repository.service`
* `sudo systemctl enable nmea-repository.service`

### Create nmea-filter.service
* `sudo nano /etc/systemd/system/nmea-filter.service`
```text
[Unit]
Description=Service to run nmea_filter python script
After=multi-user.target

[Service]
# Run as specific user (usually 'pi')
User=pi

# Set working directory so relative paths in your script work
WorkingDirectory=/var/www/python/nmea
# Use full paths for the Python interpreter and your script
ExecStart=/var/www/python/nmea/venv/bin/python3 /var/www/python/nmea/nmea_filter.py @/var/www/python/nmea/nmea_filter.txt
# Automatically restart if the script crashes
Restart=always

[Install]
WantedBy=multi-user.target
```
* where `nmea_filter.txt` contains
```text
-u http://127.0.0.1:8080/nmea/add/WSC/Barge
-a MyApiKey
-s 3
-x 10
```
* `sudo systemctl daemon-reload`
* `sudo systemctl start nmea-filter.service`
* `sudo systemctl enable nmea-filter.service`

### Create nmea-pws.service and nmea-pws.timer
* Store the organization bearer token outside the unit file:
```text
sudo nano /var/www/python/nmea/nmea_pws.env
```
```text
NMEA_PWS_URL=http://127.0.0.1:8000/nmea/pws/WSC/Barge
NMEA_PWS_AUTH=MyApiKey
```
* Restrict access to the token file:
```text
sudo chown pi:www-data /var/www/python/nmea/nmea_pws.env
sudo chmod 640 /var/www/python/nmea/nmea_pws.env
```
* `sudo nano /etc/systemd/system/nmea-pws.service`
```text
[Unit]
Description=Upload NMEA weather to Weather Underground PWS
After=nmea-repository.service

[Service]
Type=oneshot
EnvironmentFile=/var/www/python/nmea/nmea_pws.env
ExecStart=/usr/bin/curl --fail --silent --show-error --max-time 60 -H "Authorization: Bearer ${NMEA_PWS_AUTH}" "${NMEA_PWS_URL}"
```
* `sudo nano /etc/systemd/system/nmea-pws.timer`
```text
[Unit]
Description=Run NMEA PWS upload every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=nmea-pws.service

[Install]
WantedBy=timers.target
```
* `sudo systemctl daemon-reload`
* `sudo systemctl enable --now nmea-pws.timer`
* Check status with `systemctl list-timers nmea-pws.timer`, `sudo systemctl status nmea-pws.service`, and `journalctl -u nmea-pws.service -n 50`
* Stop future uploads with `sudo systemctl stop nmea-pws.timer`; disable startup with `sudo systemctl disable nmea-pws.timer`

### Install Cloudfare tunnel
* Use the "zero trust" panel in cloudflare to create `wsc.arcsite.ca` tunnel route

### Create Kiosk
* sudo adduser kiosk (Write down the passwd somewhere!)
* sudo apt install -y --no-install-recommends xserver-xorg xinit x11-xserver-utils openbox chromium lightdm unclutter
* sudo raspi-config => 1 System Options > S5 Boot / Auto Login > Select B4 Desktop Autologin
** sudo nano /etc/lightdm/lightdm.conf
** autologin-user=kiosk
** autologin-user-timeout=0
** allow-guest=false
* sudo systemctl set-default graphical.target
* sudo su - kiosk
** mkdir -p ~/.config/openbox
** nano ~/.config/openbox/autostart
```
# Disable X11 screen savers and power blanking monitors
# xset s off
# xset s noblank
# xset -dpms

# Force 270-degree monitor orientation layout (rotate left)
# xrandr --output HDMI-1 --rotate left

# echo "Waiting for Cloudflare Tunnel network backbone..."
# until curl -sI https://wsc.arcsite.ca | grep -q "HTTP/"; do
#   sleep 2
# done

# Hide the mouse cursor after 1 second of inactivity
unclutter -idle 1 -root &

# Block Chromium crash-warning error notifications on power-loss
sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' ~/.config/chromium/Default/Preferences
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' ~/.config/chromium/Default/Preferences

# Launch Chromium in an unclosable full-screen loop using your URL
while true; do
  chromium --kiosk --noerrdialogs --disable-infobars --check-for-update-interval=31536000 "http://localhost"8080/nmea-frame.html?org=WSC&src=Barge"
  sleep 5
done
```
** exit
* sudo reboot

## Add keyboard breakout mode for Kiosk (optional)
* sudo su - kiosk
* mkdir -p ~/.config/openbox
* cp /etc/xdg/openbox/rc.xml ~/.config/openbox/rc.xml
* nano ~/.config/openbox/rc.xml
** Just above </keyboard>, add the following:
```
  <!-- Custom Emergency Kiosk Exit Shortcut -->
  <keybind key="C-A-x">
    <action name="Execute">
      <command>pkill -f chromium</command>
    </action>
    <action name="Exit"/>
  </keybind>
```
* Ctrl-Alt-x to break from Chromium
* Ctrl-Alt-F2 to switch to terminal


### Troubleshooting Services
* Ensure the following user/group ownership and permissions to allow service to access
** `drwxr-xr-x 12 root   root     4096 Apr 23 18:46 /var`
** `drwxr-xr-x 4  root   root     4096 Apr 24 12:50 /var/www`
** `drwxr-xr-x 4  pi     www-data 4096 Apr 23 20:32 /var/www/python`
* if there are errors,
** `sudo systemctl status nmea-repository.service`
** `journalctl -u nmea-repository.service | tail`
** `sudo ss -tulpn | grep :8000`
