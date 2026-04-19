# nmea-monitor
Python script to monitor and filter NMEA-0183 stream

## Synopsis

This program reads an NMEA-0183 stream either from a text file or a serial port, validates sentence checksums, and watches specifically for ZDA, MWD, MDA, and GGA sentences. It uses ZDA as an interval boundary, collects all MWD wind data, MDA meteorological data, and valid-fix GGA GPS data that arrive until the next ZDA, then prints the previous ZDA plus averaged output sentences for each type that had data.

In practical terms, it acts as a filter/aggregator: noisy inbound NMEA goes in, and a reduced stream of time-stamped averaged weather/wind/GPS sentences comes out. It can auto-scan serial ports and baud rates if you don’t specify one, and --debug writes rejected sentence details to logs/nmea_monitor.log for troubleshooting.

### How it runs

* --file <path> reads recorded NMEA text from a file.
* --port <port> --baud <rate> reads a live serial feed.
* With neither option, it probes common serial ports and baud rates to find a valid NMEA source.