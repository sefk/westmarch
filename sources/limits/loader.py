from core.loaders import Disabled, Loader


class ClaudeLimitsLoader(Loader):
    name = "claude_limits"
    cadence = "hourly"

    def load(self, full: bool = False) -> int:
        raise Disabled("not implemented yet")
