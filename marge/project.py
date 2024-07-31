import enum
from typing import TYPE_CHECKING, Any, Dict, List

from . import gitlab

GET = gitlab.GET


class Project(gitlab.Resource):
    @classmethod
    def fetch_by_id(cls, project_id: int, api: gitlab.Api) -> "Project":
        info = api.call(GET(f"/projects/{project_id}"))
        if TYPE_CHECKING:
            assert isinstance(info, dict)
        return cls(api, info)

    @classmethod
    def fetch_all_mine(cls, api: gitlab.Api) -> List["Project"]:
        # GitLab has an issue where projects may not show appropriate permissions in
        # nested groups (see https://gitlab.com/gitlab-org/gitlab/-/issues/22316). Using
        # `min_access_level` is known to provide the correct projects.
        projects_kwargs: Dict[str, Any] = {
            "membership": True,
            "with_merge_requests_enabled": True,
            "archived": False,
            "min_access_level": AccessLevel.developer.value,
        }

        projects_info = api.collect_all_pages(
            GET(
                "/projects",
                projects_kwargs,
            )
        )
        if TYPE_CHECKING:
            assert isinstance(projects_info, list)

        projects = []

        for project_info in projects_info:
            # We know we fetched projects with at least developer access, so we'll
            # use that as a fallback if GitLab doesn't correctly report permissions as
            # described above.
            project_info["permissions"]["marge"] = {
                "access_level": AccessLevel.developer
            }

            projects.append(cls(api, project_info))

        return projects

    @property
    def id(self) -> int:
        result = self._info["id"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def default_branch(self) -> str:
        result = self.info["default_branch"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def path_with_namespace(self) -> str:
        result = self.info["path_with_namespace"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def ssh_url_to_repo(self) -> str:
        result = self.info["ssh_url_to_repo"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def http_url_to_repo(self) -> str:
        result = self.info["http_url_to_repo"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def merge_requests_enabled(self) -> bool:
        result = self.info["merge_requests_enabled"]
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    @property
    def only_allow_merge_if_pipeline_succeeds(self) -> bool:
        result = self.info["only_allow_merge_if_pipeline_succeeds"]
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    @property
    def only_allow_merge_if_all_discussions_are_resolved(  # pylint: disable=invalid-name
        self,
    ) -> bool:
        result = self.info["only_allow_merge_if_all_discussions_are_resolved"]
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    @property
    def approvals_required(self) -> int:
        result = self.info["approvals_before_merge"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def access_level(self) -> int:
        permissions = self.info["permissions"]
        effective_access = (
            permissions["project_access"]
            or permissions["group_access"]
            or permissions.get("marge")
        )
        assert (
            effective_access is not None
        ), "GitLab failed to provide user permissions on project"
        return AccessLevel(effective_access["access_level"])


# pylint: disable=invalid-name
@enum.unique
class AccessLevel(enum.IntEnum):
    # See https://docs.gitlab.com/ce/api/access_requests.html
    none = 0
    minimal = 5
    guest = 10
    reporter = 20
    developer = 30
    maintainer = 40
    owner = 50
