# /// script
# requires-python = ">=3.14"
# dependencies = ["pycromanager>=1.0.2"]
# ///

from pycromanager import Studio, JavaObject
from types import TracebackType
from typing import Optional, Type
import logging

# Set up a logger specific to your light-sheet plugin
logger = logging.getLogger("LightSheetManager")

# uncomment to see errors
# logging.basicConfig(level=logging.DEBUG)

class LightSheetManager:
    """
    A high-level Python wrapper for the Micro-Manager Light Sheet Manager plugin.

    Handles the cross-language bridge between Python and Java, providing
    automatic lifecycle management using the Python 'with' statement.

    Explicit lifecycle management is provided though manual calls to 'open' and 'close'.
    """
    def __init__(self) -> None:
        self.lsm: Optional[JavaObject] = None

    def open(self) -> JavaObject:
        """Connect to Micro-Manager and initialize Light Sheet Manager.

        Returns:
            JavaObject - The initialized Java Light Sheet Manager instance.

        Raises:
            ConnectionError - the pycromanager bridge or Java initialization failed
        """
        if self.lsm is not None:
            return self.lsm

        try:
            studio = Studio()
            self.lsm = JavaObject("org.micromanager.lightsheetmanager.LightSheetManager", args=[studio])

            if not self.lsm.setup():
                raise RuntimeError("Java LightSheetManager setup() returned False.")

            logger.info("Light Sheet Manager initialized.")
            return self.lsm
        except Exception as e:
            self.close() # cleanup if initialization fails
            raise ConnectionError(f"Could not initialize Light Sheet Manager: {e}") from e

    def close(self) -> None:
        """Call the Java AutoCloseable close routine on the LSM JavaObject."""
        if self.lsm is None:
            return

        logger.info("Requesting Light Sheet Manager resource cleanup...")
        try:
            self.lsm.close()
            logger.info("Java close() request sent successfully.")
        except Exception as e:
            logger.warning(f"Failed to communicate with Java close() routine: {e}")
        finally:
            self.lsm = None # help garbage collection

    def __enter__(self) -> JavaObject:
        """Entering the context block opens the bridge and returns the LSM JavaObject."""
        return self.open()

    def __exit__(self,
                 exc_type: Optional[Type[BaseException]],
                 exc_val: Optional[BaseException],
                 exc_tb: Optional[TracebackType]) -> bool:
        """Automatically clean up LSM resources resources when the exiting the context block."""
        if exc_type:
            logger.error(f"Context block exited with an error: {exc_val}", exc_info=True)

        self.close()
        return False # do not suppress exceptions


def main() -> None:
    with LightSheetManager() as lsm:
        active_camera = lsm.devices().first_active_camera_name()
        print(f"Active Camera: {active_camera}")


if __name__ == "__main__":
    main()
