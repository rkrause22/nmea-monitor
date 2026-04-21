# nmea-monitor project
Python scripts to monitor and filter NMEA-0183 streams

## nmea-monitor.py

This program reads an NMEA-0183 stream either from a text file or a serial port, validates sentence checksums, and watches specifically for ZDA, MWD, MDA, and GGA sentences. It uses ZDA as an interval boundary, collects all MWD wind data, MDA meteorological data, and valid-fix GGA GPS data that arrive until the next ZDA, then prints the previous ZDA plus averaged output sentences for each type that had data.

In practical terms, it acts as a filter/aggregator: noisy inbound NMEA goes in, and a reduced stream of time-stamped averaged weather/wind/GPS sentences comes out. It can auto-scan serial ports and baud rates if you don’t specify one, and --debug writes rejected sentence details to logs/nmea_monitor.log for troubleshooting.

usage: see --help options

## nmea-filter.py

This program is a command-line NMEA-0183 filtering tool. It reads NMEA sentences from a serial port or an input text file. After it sees the first valid ZDA sentence, it then uses later ZDA sentences as interval boundaries. For each interval, it collects sentences whose types match the --filter list, which must include ZDA and defaults to ZDA,MWD,MDA,GGA, and outputs those records to an output file, or the screen for debugging. Ultimately, this program will support sending the filtered results to an API.

usage: see --help options

## nmea-repository.py

This program runs a Flask REST service that stores JSON batches emitted by `nmea-filter.py`.
It creates a SQLite database at `data/nmea-repository.db` by default, or uses the database
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
