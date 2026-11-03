from core.loaders import Disabled, Loader


class AgentsviewLoader(Loader):
    name = "agentsview"
    cadence = "hourly"

    def load(self, full: bool = False) -> int:
        raise Disabled("not implemented yet")
