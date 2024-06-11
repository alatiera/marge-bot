from unittest.mock import Mock

from marge.branch import Branch
from marge.gitlab import DELETE, Api


# pylint: disable=attribute-defined-outside-init
class TestBranch:
    def setup_method(self) -> None:
        self.api = Mock(Api)

    def test_delete_by_name_should_escape_branch_name(self) -> None:
        Branch.delete_by_name(
            project_id=923,
            branch="branch/with/slashes",
            api=self.api,
        )
        self.api.call.assert_called_once_with(
            DELETE("projects/923/repository/branches/branch%2Fwith%2Fslashes")
        )

    def test_delete_by_name_without_slashes(
        self,
    ) -> None:
        Branch.delete_by_name(
            project_id=923,
            branch="branch-without_slashes",
            api=self.api,
        )
        self.api.call.assert_called_once_with(
            DELETE("projects/923/repository/branches/branch-without_slashes")
        )
