"""review-run: Run a review skill on a pull request and store the resulting review draft."""

from importlib import metadata

try:
    __version__ = metadata.version("review-run")
except metadata.PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
