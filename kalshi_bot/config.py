"""Loads settings from config.toml.

tomllib is built into Python 3.11+, so there's nothing extra to install.
"""

import tomllib
from pathlib import Path

# config.toml lives one folder above this file (the project root).
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read config.toml and return it as a nested dictionary."""
    with open(path, "rb") as f:  # tomllib requires binary mode
        return tomllib.load(f)
