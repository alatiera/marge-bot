from typing import TYPE_CHECKING, Any, Dict, List, Union, cast

from . import gitlab

if TYPE_CHECKING:
    from . import merge_request as mb_merge_request


class Approvals(gitlab.Resource):
    """Approval info for a MergeRequest.
    https://docs.gitlab.com/ee/api/merge_request_approvals.html"""

    def refetch_info(self) -> None:
        gitlab_version = self._api.version()
        if gitlab_version.release >= (9, 2, 2):
            approver_url = (
                f"/projects/{self.project_id}/merge_requests/{self.iid}/approvals"
            )
        else:
            # GitLab botched the v4 api before 9.2.3
            approver_url = (
                f"/projects/{self.project_id}/merge_requests/{self.id}/approvals"
            )

        # Approvals are in CE since 13.2
        if gitlab_version.is_ee or gitlab_version.release >= (13, 2, 0):
            info = self._api.call(gitlab.GET(approver_url))
        else:
            info = dict(self._info, approvals_left=0, approved_by=[])
        if TYPE_CHECKING:
            assert isinstance(info, dict)
        self._info = info

    @property
    def id(self) -> int:
        raise NotImplementedError()

    @property
    def iid(self) -> int:
        return cast(int, self.info["iid"])

    @property
    def project_id(self) -> int:
        return cast(int, self.info["project_id"])

    @property
    def approvals_left(self) -> int:
        return cast(int, self.info.get("approvals_left", 0))

    @property
    def sufficient(self) -> bool:
        return not self.approvals_left

    @property
    def approver_usernames(self) -> List[str]:
        return [who["user"]["username"] for who in self.info["approved_by"]]

    @property
    def approver_ids(self) -> List[int]:
        """Return the uids of the approvers."""
        return [who["user"]["id"] for who in self.info["approved_by"]]

    def reapprove(self) -> None:
        """Impersonates the approvers and re-approves the merge_request as them.

        The idea is that we want to get the approvers, push the rebased branch
        (which may invalidate approvals, depending on GitLab settings) and then
        restore the approval status.
        """
        self.approve(self)

    def approve(self, obj: Union["Approvals", "mb_merge_request.MergeRequest"]) -> None:
        """Approve an object which can be a merge_request or an approval."""
        if self._api.version().release >= (9, 2, 2):
            approve_url = f"/projects/{obj.project_id}/merge_requests/{obj.iid}/approve"
        else:
            # GitLab botched the v4 api before 9.2.3
            approve_url = f"/projects/{obj.project_id}/merge_requests/{obj.id}/approve"

        for uid in self.approver_ids:
            self._api.call(gitlab.POST(approve_url), sudo=uid)


class CustomApprovals(Approvals):
    """Allows a limited way to manage approvals on CE."""

    def __init__(
        self,
        api: gitlab.Api,
        info: Dict[str, Any],
        allowed_approvers: List[str],
        approvals_required: int = 1,
    ):
        super().__init__(api, info)
        self._allowed_approvers = allowed_approvers
        self._approvals_required = approvals_required

    @property
    def allowed_approvals_usernames(self) -> List[str]:
        """Filter approver_usernames to only those allowed."""
        return [
            username
            for username in self.approver_usernames
            if username in self._allowed_approvers
        ]

    @property
    def approvals_left(self) -> int:
        return max(0, self._approvals_required - len(self.allowed_approvals_usernames))

    @property
    def sufficient(self) -> bool:
        return not self.approvals_left
