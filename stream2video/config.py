"""Shared configuration defaults and validation ranges."""

CONFIG_DEFAULTS = {
    "threshold": -60.0,
    "min_silence": 2.0,
    "margin": 0.5,
    "method": "segment",
    "encoder": "h264_mf",
    "force": False,
    "delete_after": False,
    "output_dir": "",
    "theme": "dark",
}

CONFIG_RANGES = {
    "threshold": (-60, -5),
    "min_silence": (0.1, 60),
    "margin": (-3, 5),
}
