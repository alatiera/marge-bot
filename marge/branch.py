import urllib.parse
from typing import TYPE_CHECKING

from . import gitlab

GET = gitlab.GET
DELETE = gitlab.DELETE


class Branch(gitlab.Resource):
    @classmethod
    def fetch_by_name(cls, project_id: int, branch: str, api: gitlab.Api) -> "Branch":
        info = api.call(
            GET(
                f"/projects/{project_id}/repository/branches/{branch}",
            )
        )
        if TYPE_CHECKING:
            assert isinstance(info, dict)
        return cls(api, info)

    @classmethod
    def delete_by_name(cls, project_id: int, branch: str, api: gitlab.Api) -> None:
        api.call(
            DELETE(
                f"projects/{project_id}/repository/branches/"
                f'{urllib.parse.quote(branch, safe="")}'
            )
        )

    @property
    def id(self) -> int:
        raise NotImplementedError()

    @property
    def name(self) -> str:
        name = self.info["name"]
        if TYPE_CHECKING:
            assert isinstance(name, str)
        return name

    @property
    def protected(self) -> bool:
        protected = self.info["protected"]
        if TYPE_CHECKING:
            assert isinstance(protected, bool)
        return protected
