import logging
from importlib.metadata import version as package_version

logger = logging.getLogger(__name__)


def get_version() -> str:
    """Return the iOpenPod version from installed package metadata."""

    try:
        value = package_version("iopenpod")
    except Exception:
        logger.debug("Failed to read installed iopenpod version", exc_info=True)
        value = "2.1.3.post6"
    from iOpenPod.app.updates.releases import release_label

    try:
        return release_label(value)  # "2.1.3-1": upstream 2.1.3, fork revision 1
    except ValueError:
        return value
