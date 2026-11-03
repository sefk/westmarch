from core.loaders import Disabled, Loader


class HerdrLoader(Loader):
    name = "herdr"
    cadence = "daily"

    def load(self, full: bool = False) -> int:
        raise Disabled("not implemented yet")
