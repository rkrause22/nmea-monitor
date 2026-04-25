# nmea-monitor project
Python scripts to monitor and filter NMEA-0183 streams

## nmea-monitor.py

This program reads an NMEA-0183 stream either from a text file or a serial port, validates sentence checksums, and watches specifically for ZDA, MWD, MDA, and GGA sentences. It uses ZDA as an interval boundary, collects all MWD wind data, MDA meteorological data, and valid-fix GGA GPS data that arrive until the next ZDA, then prints the previous ZDA plus averaged output sentences for each type that had data.

In practical terms, it acts as a filter/aggregator: noisy inbound NMEA goes in, and a reduced stream of time-stamped averaged weather/wind/GPS sentences comes out. It can auto-scan serial ports and baud rates if you don’t specify one, and --debug writes rejected sentence details to logs/nmea_monitor.log for troubleshooting.

usage: see --help options

## nmea_filter.py

This program is a command-line NMEA-0183 filtering tool. It reads NMEA sentences from a serial port or an input text file. After it sees the first valid ZDA sentence, it then uses later ZDA sentences as interval boundaries. For each interval, it collects sentences whose types match the --filter list, which must include ZDA and defaults to ZDA,MWD,MDA,GGA, and outputs those records to an output file, or the screen for debugging. Ultimately, this program will support sending the filtered results to an API.

usage: see --help options

## nmea_repository.py

This program runs a Flask REST service that stores JSON batches emitted by `nmea_filter.py`.
It creates a SQLite database at `data/nmea_repository.db` by default, or uses the database
specified by `NMEA_REPOSITORY_DATABASE_URL`.

Routes:

* `PUT /add` accepts a JSON object with `source`, `start`, and `sentences`, then stores the
  complete message in the `Messages` table keyed by `source` and normalized UTC `utc+
  `.
* `GET /last/<source>` returns the latest sentence batch for the source as plain text.
* `GET /last/<source>/<count>` returns the latest count sentence batches for the source as plain text.
* `GET /search/<source>?start=<time>&end=<time>` returns all sentence batches in the optional
  inclusive date range as plain text.

Time query values use `yyyy[-mm[-dd[:hh[:mm[:ss]]]]]`.

## How to install on Raspberry PI

### use Raspberry PI imager to create headless OS 
* 4Gb/16Gb+
* enable RP Connect so we can connect remotely

### check standard installation
* python3 --version # Python >3.13.5
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
ExecStart=/usr/bin/python3 /var/www/python/nmea/nmea_filter.py -a 3 -u http://127.0.0.1:8080/nmea/add/WSC/Barge
# Automatically restart if the script crashes
Restart=always

[Install]
WantedBy=multi-user.target
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
