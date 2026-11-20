from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('sae-introspection')
except PackageNotFoundError:  # running from a source tree without install
    __version__ = 'unknown'
