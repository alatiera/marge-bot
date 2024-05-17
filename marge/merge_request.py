import datetime
import logging as log
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from . import gitlab
from . import user as mb_user
from .approvals import Approvals

NO_JOBS_MESSAGE = "No stages / jobs for this pipeline."
RULES_PREVENT_JOBS_MESSAGE = (
    "Pipeline will not run for the selected trigger. "
    "The rules configuration prevented any jobs from being added to the pipeline."
)


class MergeRequest(gitlab.Resource):
    # pylint: disable=too-many-public-methods
    @classmethod
    def create(
        cls, api: gitlab.Api, project_id: int, params: Dict[str, Any]
    ) -> "MergeRequest":
        merge_request_info = api.call(
            gitlab.POST(
                f"/projects/{project_id}/merge_requests",
                params,
            )
        )
        if TYPE_CHECKING:
            assert isinstance(merge_request_info, dict)
        return cls(api, merge_request_info)

    @classmethod
    def search(
        cls, api: gitlab.Api, project_id: int, params: Dict[str, Any]
    ) -> List["MergeRequest"]:
        merge_requests = api.collect_all_pages(
            gitlab.GET(
                f"/projects/{project_id}/merge_requests",
                params,
            )
        )
        return [cls(api, merge_request) for merge_request in merge_requests]

    @classmethod
    def fetch_by_iid(
        cls, project_id: int, merge_request_iid: int, api: gitlab.Api
    ) -> "MergeRequest":
        merge_request = cls(api, {"iid": merge_request_iid, "project_id": project_id})
        merge_request.refetch_info()
        return merge_request

    @classmethod
    def fetch_assigned_at(
        cls, user: mb_user.User, api: gitlab.Api, merge_request: Dict[str, Any]
    ) -> float:
        assigned_at = 0.0
        all_discussions = api.collect_all_pages(
            gitlab.GET(
                f'/projects/{merge_request.get("project_id")}/'
                + f'merge_requests/{merge_request.get("iid")}/discussions'
            )
        )
        match_body = f"assigned to @{user.username}"
        for discussion in all_discussions:
            for note in discussion.get("notes", []):
                if match_body in note.get("body"):
                    date_string = note.get("created_at")
                    date_format = "%Y-%m-%dT%H:%M:%S.%f%z"
                    if (sys.version_info.major, sys.version_info.minor) <= (3, 6):
                        assigned = (
                            datetime.datetime.strptime(
                                date_string[:-1], date_format[:-2]
                            )
                            .replace(tzinfo=datetime.timezone.utc)
                            .timestamp()
                        )
                    else:
                        assigned = datetime.datetime.strptime(
                            date_string, date_format
                        ).timestamp()
                    if assigned > assigned_at:
                        assigned_at = assigned
        return assigned_at

    @classmethod
    def fetch_all_open_for_user(
        cls, project_id: int, user: mb_user.User, api: gitlab.Api, merge_order: str
    ) -> List["MergeRequest"]:
        request_merge_order = (
            "created_at" if merge_order == "assigned_at" else merge_order
        )

        all_merge_request_infos = api.collect_all_pages(
            gitlab.GET(
                f"/projects/{project_id}/merge_requests",
                {"state": "opened", "order_by": request_merge_order, "sort": "asc"},
            )
        )
        my_merge_request_infos = [
            mri
            for mri in all_merge_request_infos
            if ((mri.get("assignee", {}) or {}).get("id") == user.id)
            or (
                user.id
                in [assignee.get("id") for assignee in (mri.get("assignees", []) or [])]
            )
        ]

        if merge_order == "assigned_at":
            my_merge_request_infos.sort(
                key=lambda mri: cls.fetch_assigned_at(user, api, mri)
            )

        return [
            cls(api, merge_request_info)
            for merge_request_info in my_merge_request_infos
        ]

    @property
    def id(self) -> int:
        result = self._info["id"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def project_id(self) -> int:
        result = self.info["project_id"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def iid(self) -> int:
        result = self.info["iid"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def title(self) -> str:
        result = self.info["title"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def state(self) -> str:
        result = self.info["state"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def merge_status(self) -> str:
        result = self.info["merge_status"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def rebase_in_progress(self) -> bool:
        result = self.info.get("rebase_in_progress", False)
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    @property
    def merge_error(self) -> Optional[str]:
        result = self.info.get("merge_error")
        if TYPE_CHECKING:
            if result is not None:
                assert isinstance(result, str)
        return result

    @property
    def assignee_ids(self) -> List[Optional[int]]:
        if "assignees" in self.info:
            return [assignee.get("id") for assignee in (self.info["assignees"] or [])]
        return [(self.info.get("assignee", {}) or {}).get("id")]

    @property
    def author_id(self) -> Optional[int]:
        author = self.info["author"]
        if TYPE_CHECKING:
            assert isinstance(author, dict)
        return author.get("id")

    @property
    def source_branch(self) -> str:
        result = self.info["source_branch"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def target_branch(self) -> str:
        result = self.info["target_branch"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def sha(self) -> str:
        result = self.info["sha"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def squash(self) -> bool:
        return self.info.get("squash", False)  # missing means auto-squash not supported

    @property
    def source_project_id(self) -> int:
        result = self.info["source_project_id"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def target_project_id(self) -> int:
        result = self.info["target_project_id"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def draft(self) -> bool:
        result = self.info["draft"]
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    @property
    def approved_by(self) -> int:
        # FIXME(jlvillal): Unable to find this field in the GitLab docs
        result = self.info["approved_by"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def web_url(self) -> str:
        result = self.info["web_url"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def blocking_discussions_resolved(self) -> bool:
        result = self.info["blocking_discussions_resolved"]
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    @property
    def force_remove_source_branch(self) -> bool:
        result = self.info["force_remove_source_branch"]
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    def update_sha(self, sha: str) -> None:
        """record the updated sha. We don't use refetch_info instead as it may hit cache."""
        self._info["sha"] = sha

    def refetch_info(self) -> None:
        result = self._api.call(
            gitlab.GET(
                f"/projects/{self.project_id}/merge_requests/{self.iid}",
                {"include_rebase_in_progress": "true"},
            ),
        )
        if TYPE_CHECKING:
            assert isinstance(result, dict)
        self._info = result

    def comment(self, message: str) -> Dict[str, Any]:
        if self._api.version().release >= (9, 2, 2):
            notes_url = f"/projects/{self.project_id}/merge_requests/{self.iid}/notes"
        else:
            # GitLab botched the v4 api before 9.2.2
            notes_url = f"/projects/{self.project_id}/merge_requests/{self.id}/notes"

        result = self._api.call(gitlab.POST(notes_url, {"body": message}))
        if TYPE_CHECKING:
            assert isinstance(result, dict)
        return result

    def rebase(self) -> None:
        self.refetch_info()

        if not self.rebase_in_progress:
            self._api.call(
                gitlab.PUT(
                    f"/projects/{self.project_id}/merge_requests/{self.iid}/rebase",
                )
            )
        else:
            # We wanted to rebase and someone just happened to press the button for us!
            log.info("A rebase was already in progress on the merge request!")

        max_attempts = 30
        wait_between_attempts_in_secs = 1

        for _ in range(max_attempts):
            self.refetch_info()
            if not self.rebase_in_progress:
                if self.merge_error:
                    raise MergeRequestRebaseFailed(self.merge_error)
                return

            time.sleep(wait_between_attempts_in_secs)

        raise TimeoutError("Waiting for merge request to be rebased by GitLab")

    def accept(
        self,
        remove_branch: bool = False,
        sha: Optional[str] = None,
        merge_when_pipeline_succeeds: bool = True,
    ) -> Dict[str, Any]:
        result = self._api.call(
            gitlab.PUT(
                f"/projects/{self.project_id}/merge_requests/{self.iid}/merge",
                {
                    "should_remove_source_branch": remove_branch,
                    "merge_when_pipeline_succeeds": merge_when_pipeline_succeeds,
                    "sha": sha
                    or self.sha,  # if provided, ensures what is merged is what we want (or fails)
                },
            )
        )
        if TYPE_CHECKING:
            assert isinstance(result, dict)
        return result

    def close(self) -> Dict[str, Any]:
        result = self._api.call(
            gitlab.PUT(
                f"/projects/{self.project_id}/merge_requests/{self.iid}",
                {"state_event": "close"},
            )
        )
        if TYPE_CHECKING:
            assert isinstance(result, dict)
        return result

    def assign_to(self, user_id: int) -> Dict[str, Any]:
        result = self._api.call(
            gitlab.PUT(
                f"/projects/{self.project_id}/merge_requests/{self.iid}",
                {"assignee_id": user_id},
            )
        )
        if TYPE_CHECKING:
            assert isinstance(result, dict)
        return result

    def unassign(self) -> Dict[str, Any]:
        return self.assign_to(0)

    def fetch_approvals(self) -> Approvals:
        # 'id' needed for for GitLab 9.2.2 hack (see Approvals.refetch_info())
        info = {"id": self.id, "iid": self.iid, "project_id": self.project_id}
        approvals = Approvals(self.api, info)
        approvals.refetch_info()
        return approvals

    def fetch_commits(self) -> List[Dict[str, Any]]:
        result = self._api.call(
            gitlab.GET(f"/projects/{self.project_id}/merge_requests/{self.iid}/commits")
        )
        if TYPE_CHECKING:
            assert isinstance(result, list)
        return result

    def trigger_pipeline(self) -> Dict[str, Any]:
        """
        Triggers a pipeline for the merge request.

        At first, try to trigger a merge request pipeline, which is different
        from a normal Gitlab CI pipeline and should be configured[0].
        If this fails due to unavailable merge request job definitions, trigger
        a normal pipeline for the source branch.

        [0]: https://docs.gitlab.com/ee/ci/pipelines/merge_request_pipelines.html
        """
        try:
            result = self._api.call(
                gitlab.POST(
                    f"/projects/{self.project_id}/merge_requests/{self.iid}/pipelines"
                )
            )
        except gitlab.BadRequest as exc:
            if exc.error_message is None:
                raise

            should_trigger_branch = False

            if NO_JOBS_MESSAGE in exc.error_message:
                # Failed due to unavailable merge request job definitions
                should_trigger_branch = True
                log.info(
                    "The pipeline is not configured for MR jobs, triggering a usual pipeline."
                )

            elif RULES_PREVENT_JOBS_MESSAGE in exc.error_message:
                # This does not necessarily always mean that we just need to trigger a branch pipeline,
                # but that is one possible reason. Worth trying.

                log.info(
                    "Error trying to trigger MR pipeline: '%s', triggering a branch pipeline as fallback.",
                    exc.error_message,
                )
                should_trigger_branch = True

            if not should_trigger_branch:
                # Propagate the error if we are not going to just trigger the branch pipeline
                raise

            result = self._api.call(
                gitlab.POST(
                    f"/projects/{self.project_id}/pipeline?ref={self.source_branch}"
                )
            )
        if TYPE_CHECKING:
            assert isinstance(result, dict)
        return result


class MergeRequestRebaseFailed(Exception):
    pass
