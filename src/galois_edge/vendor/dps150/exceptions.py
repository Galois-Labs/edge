class DPS150Error(Exception):
    """Base exception for DPS-150 errors."""


class ChecksumError(DPS150Error):
    """Invalid checksum on received packet."""


class ConnectionError(DPS150Error):
    """Serial port connection issues."""


class SessionError(DPS150Error):
    """Session lifecycle problems."""
