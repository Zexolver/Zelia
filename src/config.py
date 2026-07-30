"""Loads config.yaml and exposes it as a simple dict-like object."""
import os
import yaml

_DEFAULT_PATH = os.environ.get(
    "ZELIA_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.yaml"),
)


class Config(dict):
    """A dict that also allows attribute-style access, recursively."""

    def __getattr__(self, item):
        try:
            value = self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc
        if isinstance(value, dict) and not isinstance(value, Config):
            value = Config(value)
            self[item] = value
        return value


def load_config(path: str = _DEFAULT_PATH) -> Config:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config not found at {path}. Did you run install.sh? "
            f"(It generates config/config.yaml from config.yaml.template.)"
        )
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    return Config(raw)
