from core.loaders import Disabled, Loader


class GitHubLoader(Loader):
    name = "github"
    cadence = "daily"

    def load(self, full: bool = False) -> int:
        raise Disabled("not implemented yet")
