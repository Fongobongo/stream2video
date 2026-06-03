"""Shared configuration defaults and validation ranges."""

CONFIG_DEFAULTS = {
    "threshold": -20,
    "min_silence": 1.0,
    "margin": -0.5,
    "method": "batch",
    "encoder": "libx264",
    "force": False,
    "output_dir": "",
    "theme": "dark",
}

CONFIG_RANGES = {
    "threshold": (-60, -5),
    "min_silence": (0.1, 60),
    "margin": (-3, 5),
}
