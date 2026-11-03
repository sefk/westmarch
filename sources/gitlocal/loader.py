from core.loaders import Disabled, Loader


class ProjectsLoader(Loader):
    name = "projects"
    cadence = "daily"

    def load(self, full: bool = False) -> int:
        raise Disabled("not implemented yet")


class GitLocalLoader(Loader):
    name = "gitlocal"
    cadence = "daily"

    def load(self, full: bool = False) -> int:
        raise Disabled("not implemented yet")
