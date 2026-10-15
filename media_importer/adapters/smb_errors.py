from ..model import DestinationError


class SmbError(DestinationError):
    def __init__(self, message):
        lower = str(message).lower()
        code = next((code for code, words in (
            ("destination_path", ("0xc00000cc", "bad_network_name")),
            ("authentication", ("0xc000006d", "0xc000006a", "logon_failure")),
            ("permission", ("0xc0000022", "access_denied")),
            ("destination_full", ("0xc000007f", "0xc0000044", "disk_full")),
            ("network", ("0xc000020c", "0xc00000b5")),
        ) if any(word in lower for word in words)), None)
        super().__init__(message, code=code)
