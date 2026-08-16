"""attention — an autonomous audience engine.

One audience = one folder under audiences/<name>/ with an audience.yml.
The engine loads that config, pulls data from the configured SOURCES,
turns it into POSTS with the configured SHAPES, renders the SITE, and
publishes through the configured CHANNELS. Every part is a plugin.
"""
__version__ = "0.1.0"
