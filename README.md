# nmea-monitor project
Python scripts and HTML pages to monitor, filter and present NMEA-0183 streams

## nmea-monitor.html

nmea-monitor.html is a simple browser-based weather dashboard for the NMEA repository. It fetches the latest weather summary from the repository’s /nmea/weather/WSC/Barge endpoint every 10 seconds and displays three current conditions: local date/time, temperature, and wind.

The page is intentionally lightweight: a single responsive table, a dynamic title/header based on the returned org and source, and a status line showing when the data was last updated or whether an error occurred. It also formats wind speed and direction cleanly, including compass symbols and degree details when available, so it works as a compact “current weather at source” display for live NMEA data.

## nmea_filter.py

nmea_filter.py is a command-line tool that reads NMEA-0183 data from either a serial port or an input text file, groups the stream into ZDA-delimited time frames, and emits filtered or aggregated output for downstream storage or upload. It can auto-discover a live NMEA serial source, wait for the first valid ZDA before starting, and then collect only the requested sentence types, with ZDA,MWD,MDA,GGA as the default filter.

When aggregation is enabled, it reduces each ZDA frame to one representative record per supported type by averaging wind, meteorological, and GPS data over the selected window, using circular averaging for directional fields and geographic averaging for latitude/longitude. Output can be written to stdout for debugging, saved as .nmea files, or uploaded as JSON to a configured API endpoint. The program also includes retry logic for serial/API failures and writes exceptions to dated log files under logs/.

usage: see --help options

## nmea_repository.py

nmea_repository.py is a small Flask-based REST service that stores filtered NMEA batches produced by nmea_filter.py. It saves each message in a SQLite database, keyed by organization, source, and UTC start time, and preserves the associated sentence payload as plain text for later retrieval.

The service acts as a lightweight repository for uploaded NMEA frame data, with support for organization registration, bearer-token access control, retention settings, message purge operations, and retrieval/query endpoints. By default it uses a local SQLite database in the data/ folder, but the database location can be overridden with NMEA_REPOSITORY_DATABASE_URL.

Endpoints:

* POST /nmea/add/`org`/`source`
  Accepts a JSON body containing start and sentences, and creates or updates the stored message for that org/source/start time. Requires a bearer token matching the organization registration.
* POST /nmea/registrations
  Creates a new organization registration with its authentication token and retention settings. Creating the first admin registration is a bootstrap case; later registrations require the admin bearer token.
* GET /nmea/registrations
  Returns all registrations, including each organization’s retention span and message limit. Requires the admin bearer token.
* GET /nmea/registrations/`org`
  Returns registrations whose organization name starts with the given prefix. Requires the admin bearer token.
* DELETE /nmea/registrations/`org`
  Deletes the specified registration and all stored messages for that organization. Requires the admin bearer token.
* DELETE /nmea/purge/`org`/`source`
  Purges stored messages for the source using the registered retention settings for that organization. Requires the organization bearer token.
* DELETE /nmea/purge/`org`/`source`/`what`
  Purges stored messages for the source using an explicit keep rule or override value, rather than the default registration settings. Requires the organization bearer token.
* GET /nmea/last/`org`/`source`
  Returns the latest stored message for the given org and source as plain text.
* GET /nmea/last/`org`/`source`/`count`
  Returns the most recent count messages for the given org and source as plain text.
* GET /nmea/count
  Returns the total number of stored messages across all organizations and sources, optionally filtered by start and end.
* GET /nmea/count/`org`
  Returns the number of stored messages for the given organization, optionally filtered by start and end.
* GET /nmea/count/`org`/`source`
  Returns the number of stored messages for the given organization and source, optionally filtered by start and end.
* GET /nmea/search/`org`/`source`?start=`time`&end=`time`
  Returns matching stored messages for the given org and source, optionally filtered by a date/time range. If no range is supplied, it returns the latest message.
* GET /nmea/weather/`org`/`source`
  Returns a weather-oriented summary view derived from the most recent stored message(s) for the org/source.
* GET /nmea/weather/`org`/`source`/`count`
  Returns a weather-oriented summary view derived from the most recent count stored messages for the org/source.
* GET /`filename`.html
  Serves a static HTML file from the repository directory.

Time query values use yyyy[-mm[-dd[:hh[:mm[:ss]]]]].

## How to install on Raspberry PI

### use Raspberry PI imager to create headless OS 
* 4Gb/16Gb+
* enable RP Connect so we can connect remotely

### check standard installation
* python3 --version # Python `3.13.5
* sudo apt install python3-pip
* sudo nmtui # configure fixed IP address 192.168.1.10
* You might also want to install CUPS/SAMBA to enable AirPrint

### Install and open firewall
* sudo apt install ufw -y
* sudo ufw allow ssh
* sudo ufw allow http
* sudo ufw allow 8080/tcp
* sudo ufw enable
* sudo ufw status verbose

### Install and configure nginx
* sudo apt install nginx -y
* sudo nano /etc/nginx/sites-available/gunicorn # create as below
```
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
* sudo ln -s /etc/nginx/sites-available/gunicorn /etc/nginx/sites-enabled/
* sudo systemctl restart nginx

### Create and prepare code directory
* sudo mkdir /var/www/python/nmea
* cd /var/www/python/nmea
* sudo scp *.py *.html requirements.txt . 
* python3 -m venv venv
* source venv/bin/activate
* python3 -m pip install flask gunicorn
* python3 -m pip install -r requirements.txt
* deactivate

### Test nmea_repository.py
* source venv/bin/activate
* gunicorn --bind 0.0.0.0:8000 nmea_repository:app
* http://192.168.1.10:8080/nmea/count
* deactivate

### Test nmea_filter.py
* source venv/bin/activate
* python3 nmea_filter.py -u http://127.0.0.1:8080/nmea/add/WSC/Barge
* deactivate

### Create nmea_repository.service
* sudo nano /etc/systemd/system/nmea_repository.service
```
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
* sudo systemctl daemon-reload
* sudo systemctl start nmea_repository.service
* sudo systemctl enable nmea_repository.service

### Create nmea_filter.service
* sudo nano /etc/systemd/system/nmea_filter.service
```
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
* where nmea_filter.txt contains
```
-u http://127.0.0.1:8080/nmea/add/WSC/Barge
-a WindWaterWaves
-g 3
```
* sudo systemctl daemon-reload
* sudo systemctl start nmea_filter.service
* sudo systemctl enable nmea_filter.service

### Troubleshooting Services
* Ensure the following user/group ownership and permissions to allow service to access
** drwxr-xr-x 12 root   root     4096 Apr 23 18:46 /var
** drwxr-xr-x 4  root   root     4096 Apr 24 12:50 /var/www
** drwxr-xr-x 4  russel www-data 4096 Apr 23 20:32 /var/www/python
* if there are errors, 
** sudo systemctl status nmea_repository.service
** journalctl -u nmea_repository | tail
** sudo ss -tulpn | grep :8000

### Install Cloudfare tunnel
* Use the "zero trust" panel in cloudflare to create wsc.arcsite.ca tunnel
