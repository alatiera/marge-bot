# pylint: disable=too-many-locals
import contextlib
import dataclasses
import functools
from datetime import timedelta
from unittest.mock import ANY, patch

import pytest

import marge.commit
import marge.git
import marge.gitlab
import marge.interval
import marge.job
import marge.project
import marge.single_merge_job
import marge.user
from marge.gitlab import GET, POST, PUT
from marge.job import Fusion
from marge.merge_request import MergeRequest
from tests import test_commit
from tests.git_repo_mock import RepoMock
from tests.gitlab_api_mock import Api, Error, MockLab, Ok
from tests.test_project import INFO as TEST_PROJECT_INFO

INITIAL_MR_SHA = test_commit.INFO["id"]


def _commit(commit_id, status):
    return {
        "id": commit_id,
        "short_id": commit_id,
        "author_name": "J. Bond",
        "author_email": "jbond@mi6.gov.uk",
        "message": "Shaken, not stirred",
        "status": status,
    }


def _branch(name, protected=False):
    return {
        "name": name,
        "protected": protected,
    }


def _pipeline(sha1, status, ref="useless_new_feature"):
    return {
        "id": 47,
        "status": status,
        "ref": ref,
        "sha": sha1,
        "web_url": "https://link/pipelines/87",
    }


class SingleJobMockLab(MockLab):
    def __init__(
        self,
        *,
        initial_master_sha,
        rewritten_sha,
        gitlab_url=None,
        fork=False,
        expect_gitlab_rebase=False,
        merge_request_options=None,
        guarantee_final_pipeline=False,
    ):
        super().__init__(
            initial_master_sha,
            gitlab_url,
            fork=fork,
            merge_request_options=merge_request_options,
        )
        api = self.api
        self.rewritten_sha = rewritten_sha
        if expect_gitlab_rebase:
            api.add_transition(
                PUT(
                    f'/projects/{self.merge_request_info["project_id"]}/merge_requests/'
                    f'{self.merge_request_info["iid"]}/rebase',
                ),
                Ok(True),
                from_state="initial",
                to_state="rebase-in-progress",
            )
            api.add_merge_request(
                dict(self.merge_request_info, rebase_in_progress=True),
                from_state="rebase-in-progress",
                to_state="rebase-finished",
            )
            api.add_merge_request(
                dict(
                    self.merge_request_info,
                    rebase_in_progress=False,
                    sha=rewritten_sha,
                ),
                from_state="rebase-finished",
                to_state="pushed",
            )

        if guarantee_final_pipeline:
            # Corresponds to the `merge_request.trigger_pipeline()` call.
            api.add_transition(
                POST(
                    f"/projects/1234/merge_requests/{self.merge_request_info['iid']}/pipelines"
                ),
                Ok({}),
                to_state="final_pipeline_triggered",
            )
            # Corresponds to `pipelines_by_branch()` by `wait_for_ci_to_pass`.
            api.add_pipelines(
                self.merge_request_info["source_project_id"],
                _pipeline(
                    sha1=rewritten_sha,
                    status="success",
                    ref=self.merge_request_info["source_branch"],
                ),
                from_state=["final_pipeline_triggered"],
                to_state="pushed",
            )

        api.add_pipelines(
            self.merge_request_info["source_project_id"],
            _pipeline(
                sha1=rewritten_sha,
                status="running",
                ref=self.merge_request_info["source_branch"],
            ),
            from_state="pushed",
            to_state="passed",
        )
        api.add_pipelines(
            self.merge_request_info["source_project_id"],
            _pipeline(
                sha1=rewritten_sha,
                status="success",
                ref=self.merge_request_info["source_branch"],
            ),
            from_state=["passed", "merged"],
        )
        source_project_id = self.merge_request_info["source_project_id"]
        api.add_transition(
            GET(
                f"/projects/{source_project_id}/repository/branches/"
                f'{self.merge_request_info["source_branch"]}',
            ),
            Ok({"commit": _commit(commit_id=rewritten_sha, status="running")}),
            from_state="pushed",
        )
        api.add_transition(
            GET(
                f"/projects/{source_project_id}/repository/branches/"
                f'{self.merge_request_info["source_branch"]}',
            ),
            Ok({"commit": _commit(commit_id=rewritten_sha, status="success")}),
            from_state="passed",
        )
        api.add_transition(
            PUT(
                f'/projects/1234/merge_requests/{self.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Ok({}),
            from_state=["passed", "skipped"],
            to_state="merged",
        )
        api.add_merge_request(
            dict(self.merge_request_info, state="merged"), from_state="merged"
        )
        api.add_transition(
            GET(
                f"/projects/1234/repository/branches/"
                f'{self.merge_request_info["target_branch"]}',
            ),
            Ok({"commit": {"id": self.rewritten_sha}}),
            from_state="merged",
        )
        api.expected_note(
            self.merge_request_info,
            "My job would be easier if people didn't jump the queue and push directly... *sigh*",
            from_state=["pushed_but_master_moved", "merge_rejected"],
        )
        api.expected_note(
            self.merge_request_info,
            "Unexpected exception in bot while handling this MR.",
        )

    def push_updated(self, remote_url, remote_branch, old_sha, new_sha):
        source_project = self.forked_project_info or self.project_info
        assert remote_url == source_project["ssh_url_to_repo"]
        assert remote_branch == self.merge_request_info["source_branch"]
        assert old_sha == INITIAL_MR_SHA
        assert new_sha == self.rewritten_sha
        self.api.state = "pushed"

    @contextlib.contextmanager
    def expected_failure(self, message):
        author_assigned = False

        def assign_to_author():
            nonlocal author_assigned
            author_assigned = True

        self.api.add_transition(
            PUT(
                f'/projects/1234/merge_requests/{self.merge_request_info["iid"]}',
                args={"assignee_id": self.author_id},
            ),
            assign_to_author,
        )
        error_note = f"I couldn't merge this branch: {message}"
        self.api.expected_note(self.merge_request_info, error_note)

        yield

        assert author_assigned
        assert error_note in self.api.notes


class TestUpdateAndAccept:  # pylint: disable=too-many-public-methods
    @dataclasses.dataclass
    class Mocks:
        mocklab: SingleJobMockLab
        api: Api
        job: marge.single_merge_job.SingleMergeJob

    @pytest.fixture(params=[True, False])
    def fork(self, request):
        return request.param

    @pytest.fixture(params=list(Fusion))
    def fusion(self, request):
        return request.param

    @pytest.fixture(params=[True, False])
    def add_tested(self, request):
        return request.param

    @pytest.fixture(params=[True, False])
    def add_part_of(self, request):
        return request.param

    @pytest.fixture(params=[False])  # TODO: Needs support in mocklab
    def add_reviewers(self, request):
        return request.param

    @pytest.fixture(params=[True, False])
    def guarantee_final_pipeline(self, request):
        return request.param

    @pytest.fixture()
    def options_factory(
        self, fusion, add_tested, add_reviewers, add_part_of, guarantee_final_pipeline
    ):
        def make_options(**kwargs):
            fixture_opts = {
                "fusion": fusion,
                "add_tested": add_tested,
                "add_part_of": add_part_of,
                "add_reviewers": add_reviewers,
                "guarantee_final_pipeline": guarantee_final_pipeline,
            }
            assert not set(fixture_opts).intersection(kwargs)
            kwargs.update(fixture_opts)
            return marge.job.MergeJobOptions(**kwargs)

        yield make_options

    @pytest.fixture()
    def update_sha(self, fusion):
        def new_sha(new, old):
            pats = {
                marge.job.Fusion.rebase: "rebase(%s onto %s)",
                marge.job.Fusion.merge: "merge(%s with %s)",
                marge.job.Fusion.gitlab_rebase: "rebase(%s onto %s)",
            }
            return pats[fusion] % (new, old)

        yield new_sha

    @pytest.fixture()
    def rewrite_sha(self, fusion, add_tested, add_reviewers, add_part_of):
        def new_sha(sha):
            # NB. The order matches the one used in the Git mock to run filters
            if add_tested and fusion == marge.job.Fusion.rebase:
                sha = f"add-tested-by({sha})"

            if add_reviewers and fusion != marge.job.Fusion.gitlab_rebase:
                sha = f"add-reviewed-by({sha})"

            if add_part_of and fusion != marge.job.Fusion.gitlab_rebase:
                sha = f"add-part-of({sha})"

            return sha

        yield new_sha

    @pytest.fixture(autouse=True)
    def patch_sleep(self):
        with patch("time.sleep"):
            yield

    @pytest.fixture()
    def mocklab_factory(self, fork, fusion, guarantee_final_pipeline):
        expect_rebase = fusion is Fusion.gitlab_rebase
        return functools.partial(
            SingleJobMockLab,
            fork=fork,
            expect_gitlab_rebase=expect_rebase,
            guarantee_final_pipeline=guarantee_final_pipeline,
        )

    @pytest.fixture()
    def mocks_factory(self, mocklab_factory, options_factory, update_sha, rewrite_sha):
        # pylint: disable=too-many-locals
        def make_mocks(
            initial_master_sha=None,
            rewritten_sha=None,
            extra_opts=None,
            extra_mocklab_opts=None,
            on_push=None,
        ):
            options = options_factory(**(extra_opts or {}))
            initial_master_sha = initial_master_sha or "505050505e"

            if not rewritten_sha:
                rewritten_sha = rewrite_sha(
                    update_sha(INITIAL_MR_SHA, initial_master_sha)
                )

            mocklab = mocklab_factory(
                initial_master_sha=initial_master_sha,
                rewritten_sha=rewritten_sha,
                **(extra_mocklab_opts or {}),
            )
            api = mocklab.api

            project_id = mocklab.project_info["id"]
            merge_request_iid = mocklab.merge_request_info["iid"]

            project = marge.project.Project.fetch_by_id(project_id, api)
            forked_project = None
            if mocklab.forked_project_info:
                forked_project_id = mocklab.forked_project_info["id"]
                forked_project = marge.project.Project.fetch_by_id(
                    forked_project_id, api
                )

            merge_request = MergeRequest.fetch_by_iid(
                project_id, merge_request_iid, api
            )

            def assert_can_push(*_args, **_kwargs):
                assert options.fusion is not Fusion.gitlab_rebase

            callback = on_push or mocklab.push_updated
            repo = RepoMock.init_for_merge_request(
                merge_request=merge_request,
                initial_target_sha=mocklab.initial_master_sha,
                project=project,
                forked_project=forked_project,
            )
            repo.mock_impl.on_push_callbacks.append(assert_can_push)
            repo.mock_impl.on_push_callbacks.append(callback)

            user = marge.user.User.myself(api)
            job = marge.single_merge_job.SingleMergeJob(
                api=api,
                user=user,
                project=project,
                merge_request=merge_request,
                repo=repo,
                options=options,
            )
            return self.Mocks(mocklab=mocklab, api=api, job=job)

        yield make_mocks

    @pytest.fixture()
    def mocks(self, mocks_factory):
        yield mocks_factory()

    def test_succeeds_first_time(self, mocks):
        mocks.job.execute()
        assert mocks.api.state == "merged"
        assert mocks.api.notes == []

    def test_succeeds_with_updated_branch(self, mocks):
        mocks.api.add_transition(
            GET(
                f"/projects/1234/repository/branches/"
                f'{mocks.mocklab.merge_request_info["source_branch"]}'
            ),
            Ok({"commit": {"id": mocks.mocklab.rewritten_sha}}),
            from_state="initial",
            to_state="pushed",
        )
        mocks.job.execute()

        assert mocks.api.state == "merged"
        assert mocks.api.notes == []

    def test_succeeds_if_skipped(self, mocks):
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(sha1=mocks.mocklab.rewritten_sha, status="running"),
            from_state="pushed",
            to_state="skipped",
        )
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(sha1=mocks.mocklab.rewritten_sha, status="skipped"),
            from_state=["skipped", "merged"],
        )
        # `pipelines_by_branch()` by `wait_for_ci_to_pass`.
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(
                sha1=mocks.mocklab.rewritten_sha,
                status="skipped",
                ref=mocks.mocklab.merge_request_info["source_branch"],
            ),
            from_state=["final_pipeline_triggered"],
            to_state="passed",
        )
        mocks.job.execute()

        assert mocks.api.state == "merged"
        assert mocks.api.notes == []

    def test_succeeds_if_source_is_master(self, mocks_factory):
        mocks = mocks_factory(
            extra_mocklab_opts={
                "merge_request_options": {
                    "source_branch": "master",
                    "target_branch": "production",
                },
            },
        )
        mocks.api.add_transition(
            GET(
                f"/projects/1234/repository/branches/"
                f'{mocks.mocklab.merge_request_info["source_branch"]}',
            ),
            Ok({"commit": {"id": mocks.mocklab.rewritten_sha}}),
            from_state="initial",
            to_state="pushed",
        )
        mocks.job.execute()

        assert mocks.api.state == "merged"
        assert mocks.api.notes == []

    def test_fails_if_ci_fails(self, mocks):
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(sha1=mocks.mocklab.rewritten_sha, status="running"),
            from_state="pushed",
            to_state="failed",
        )
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(sha1=mocks.mocklab.rewritten_sha, status="failed"),
            from_state=["failed"],
        )
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(
                sha1=mocks.mocklab.rewritten_sha,
                status="failed",
                ref=mocks.mocklab.merge_request_info["source_branch"],
            ),
            from_state=["final_pipeline_triggered"],
            to_state="failed",
        )

        expected_message = "CI failed! See pipeline https://link/pipelines/87."
        with mocks.mocklab.expected_failure(expected_message):
            mocks.job.execute()

        assert mocks.api.state == "failed"

    def test_fails_if_ci_canceled(self, mocks):
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(sha1=mocks.mocklab.rewritten_sha, status="running"),
            from_state="pushed",
            to_state="canceled",
        )
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(sha1=mocks.mocklab.rewritten_sha, status="canceled"),
            from_state=["canceled"],
        )
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(
                sha1=mocks.mocklab.rewritten_sha,
                status="canceled",
                ref=mocks.mocklab.merge_request_info["source_branch"],
            ),
            from_state=["final_pipeline_triggered"],
            to_state="canceled",
        )

        expected_message = (
            "Someone canceled the CI. See pipeline https://link/pipelines/87."
        )
        with mocks.mocklab.expected_failure(expected_message):
            mocks.job.execute()

        assert mocks.api.state == "canceled"

    def test_fails_on_not_acceptable_if_master_did_not_move(self, mocks):
        new_branch_head_sha = "99ba110035"
        mocks.api.add_transition(
            GET(
                f'/projects/{mocks.mocklab.merge_request_info["source_project_id"]}/'
                f"repository/branches/useless_new_feature",
            ),
            Ok({"commit": _commit(commit_id=new_branch_head_sha, status="success")}),
            from_state="pushed",
            to_state="pushed_but_head_changed",
        )
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(
                sha1=new_branch_head_sha,
                status="success",
                ref=mocks.mocklab.merge_request_info["source_branch"],
            ),
            from_state=["pushed_but_head_changed"],
        )

        with mocks.mocklab.expected_failure(
            "Someone pushed to branch while we were trying to merge"
        ):
            mocks.job.execute()

        assert mocks.api.state == "pushed_but_head_changed"
        assert mocks.api.notes == [
            "I couldn't merge this branch: Someone pushed to branch while we were trying to merge",
        ]

    def test_fails_if_branch_is_protected(self, mocks_factory, fusion):
        def reject_push(*_args, **_kwargs):
            raise marge.git.GitError()

        mocks = mocks_factory(on_push=reject_push)
        mocks.api.add_transition(
            GET(
                f'/projects/{mocks.mocklab.merge_request_info["source_project_id"]}/'
                f"repository/branches/useless_new_feature",
            ),
            Ok(_branch("useless_new_feature", protected=True)),
            from_state="initial",
            to_state="protected",
        )

        if fusion is Fusion.gitlab_rebase:
            mocks.api.add_transition(
                PUT(
                    f'/projects/{mocks.mocklab.merge_request_info["project_id"]}/'
                    f'merge_requests/{mocks.mocklab.merge_request_info["iid"]}/rebase',
                ),
                Error(
                    marge.gitlab.MethodNotAllowed(
                        405, {"message": "405 Method Not Allowed"}
                    )
                ),
                from_state="initial",
            )

        with mocks.mocklab.expected_failure(
            "Sorry, I can't modify protected branches!"
        ):
            mocks.job.execute()

        assert mocks.api.state == "protected"

    def test_second_time_if_master_moved(
        self, mocks_factory, fusion, update_sha, rewrite_sha
    ):
        initial_master_sha = "eaeaea9e9e"
        moved_master_sha = "fafafa"
        first_rewritten_sha = rewrite_sha(
            update_sha(INITIAL_MR_SHA, initial_master_sha)
        )
        second_rewritten_sha = rewrite_sha(
            update_sha(first_rewritten_sha, moved_master_sha)
        )

        # pylint: disable=unused-argument
        def push_effects(remote_url, remote_branch, old_sha, new_sha):
            nonlocal target_branch, remote_target_repo

            if mocks.api.state == "initial":
                assert old_sha == INITIAL_MR_SHA
                assert new_sha == first_rewritten_sha
                mocks.api.state = "pushed_but_master_moved"
                remote_target_repo.set_ref(target_branch, moved_master_sha)
            elif mocks.api.state == "merge_rejected":
                assert new_sha == second_rewritten_sha
                mocks.api.state = "pushed"

        mocks = mocks_factory(
            initial_master_sha=initial_master_sha,
            rewritten_sha=second_rewritten_sha,
            on_push=push_effects,
        )

        source_project_info = (
            mocks.mocklab.forked_project_info or mocks.mocklab.project_info
        )
        target_project_info = mocks.mocklab.project_info

        source_project_url = source_project_info["ssh_url_to_repo"]
        target_project_url = target_project_info["ssh_url_to_repo"]

        source_branch = mocks.mocklab.merge_request_info["source_branch"]
        target_branch = mocks.mocklab.merge_request_info["target_branch"]

        remote_source_repo = mocks.job.repo.mock_impl.remote_repos[source_project_url]
        remote_target_repo = mocks.job.repo.mock_impl.remote_repos[target_project_url]

        mocks.api.add_merge_request(
            dict(
                mocks.mocklab.merge_request_info,
                sha=first_rewritten_sha,
            ),
            from_state=["pushed_but_master_moved", "merge_rejected"],
        )
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(sha1=first_rewritten_sha, status="success"),
            from_state=["pushed_but_master_moved", "merge_rejected"],
        )
        mocks.api.add_transition(
            PUT(
                f"/projects/1234/merge_requests/"
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": first_rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Error(marge.gitlab.NotAcceptable()),
            from_state="pushed_but_master_moved",
            to_state="merge_rejected",
        )
        mocks.api.add_transition(
            GET(
                f'/projects/{mocks.mocklab.merge_request_info["source_project_id"]}/'
                f"repository/branches/useless_new_feature",
            ),
            Ok({"commit": _commit(commit_id=first_rewritten_sha, status="success")}),
            from_state="pushed_but_master_moved",
        )
        mocks.api.add_transition(
            GET("/projects/1234/repository/branches/master"),
            Ok({"commit": _commit(commit_id=moved_master_sha, status="success")}),
            from_state="merge_rejected",
        )
        # Overwrite original `guarantee_final_pipeline` transition: no need in
        # the state changing here.
        mocks.api.add_transition(
            POST(
                f"/projects/1234/merge_requests/{mocks.mocklab.merge_request_info['iid']}/pipelines"
            ),
            Ok({}),
        )
        # Register additional pipeline check introduced by
        # `guarantee_final_pipeline`.
        mocks.api.add_pipelines(
            mocks.mocklab.merge_request_info["source_project_id"],
            _pipeline(
                sha1=first_rewritten_sha,
                status="success",
                ref=mocks.mocklab.merge_request_info["source_branch"],
            ),
            from_state=["pushed_but_master_moved", "merge_rejected"],
        )
        if fusion is Fusion.gitlab_rebase:
            rebase_url = (
                f'/projects/{mocks.mocklab.merge_request_info["project_id"]}/'
                f'merge_requests/{mocks.mocklab.merge_request_info["iid"]}/rebase'
            )

            mocks.api.add_transition(
                PUT(rebase_url),
                Ok(True),
                from_state="initial",
                to_state="pushed_but_master_moved",
                side_effect=lambda: (
                    remote_source_repo.set_ref(source_branch, first_rewritten_sha),
                    remote_target_repo.set_ref(target_branch, moved_master_sha),
                ),
            )
            mocks.api.add_transition(
                PUT(rebase_url),
                Ok(True),
                from_state="merge_rejected",
                to_state="rebase-in-progress",
                side_effect=lambda: remote_source_repo.set_ref(
                    source_branch, second_rewritten_sha
                ),
            )

        mocks.job.execute()
        assert mocks.api.state == "merged"
        assert mocks.api.notes == [
            "My job would be easier if people didn't jump the queue and push directly... *sigh*",
        ]

    def test_handles_races_for_merging(self, mocks):
        rewritten_sha = mocks.mocklab.rewritten_sha
        mocks.api.add_transition(
            PUT(
                f"/projects/1234/merge_requests/"
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Error(marge.gitlab.NotFound(404, {"message": "404 Branch Not Found"})),
            from_state="passed",
            to_state="someone_else_merged",
        )
        mocks.api.add_merge_request(
            dict(mocks.mocklab.merge_request_info, state="merged"),
            from_state="someone_else_merged",
        )
        mocks.job.execute()
        assert mocks.api.state == "someone_else_merged"
        assert mocks.api.notes == []

    @pytest.mark.parametrize("only_allow_merge_if_pipeline_succeeds", [True, False])
    def test_calculates_merge_when_pipeline_succeeds_correctly(
        self, mocks, only_allow_merge_if_pipeline_succeeds
    ):
        rewritten_sha = mocks.mocklab.rewritten_sha
        project_info = dict(TEST_PROJECT_INFO)
        project_info[
            "only_allow_merge_if_pipeline_succeeds"
        ] = only_allow_merge_if_pipeline_succeeds
        mocks.api.add_project(project_info)
        mocks.api.add_transition(
            PUT(
                f'/projects/{project_info["id"]}/merge_requests/'
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": only_allow_merge_if_pipeline_succeeds,
                },
            ),
            Ok(True),
            to_state="merged",
        )
        mocks.job.execute()
        assert mocks.api.state == "merged"

    def test_handles_request_becoming_draft_after_push(self, mocks):
        rewritten_sha = mocks.mocklab.rewritten_sha
        mocks.api.add_transition(
            PUT(
                f"/projects/1234/merge_requests/"
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Error(
                marge.gitlab.MethodNotAllowed(
                    405, {"message": "405 Method Not Allowed"}
                )
            ),
            from_state="passed",
            to_state="now_is_draft",
        )
        mocks.api.add_merge_request(
            dict(mocks.mocklab.merge_request_info, draft=True),
            from_state="now_is_draft",
        )
        message = "The request was marked as Draft as I was processing it (maybe a Draft commit?)"
        with mocks.mocklab.expected_failure(message):
            mocks.job.execute()
        assert mocks.api.state == "now_is_draft"
        assert mocks.api.notes == [f"I couldn't merge this branch: {message}"]

    def test_guesses_git_hook_error_on_merge_refusal(self, mocks):
        rewritten_sha = mocks.mocklab.rewritten_sha
        mocks.api.add_transition(
            PUT(
                f"/projects/1234/merge_requests/"
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Error(
                marge.gitlab.MethodNotAllowed(
                    405, {"message": "405 Method Not Allowed"}
                )
            ),
            from_state="passed",
            to_state="rejected_by_git_hook",
        )
        mocks.api.add_merge_request(
            dict(mocks.mocklab.merge_request_info, state="reopened"),
            from_state="rejected_by_git_hook",
        )
        message = (
            "GitLab refused to merge this branch. I suspect that a Push Rule or a git-hook "
            "is rejecting my commits; maybe my email needs to be white-listed?"
        )
        with mocks.mocklab.expected_failure(message):
            mocks.job.execute()
        assert mocks.api.state == "rejected_by_git_hook"
        assert mocks.api.notes == [f"I couldn't merge this branch: {message}"]

    def test_assumes_unresolved_discussions_on_merge_refusal(self, mocks):
        rewritten_sha = mocks.mocklab.rewritten_sha
        mocks.api.add_transition(
            PUT(
                f"/projects/1234/merge_requests/"
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Error(
                marge.gitlab.MethodNotAllowed(
                    405, {"message": "405 Method Not Allowed"}
                )
            ),
            from_state="passed",
            to_state="unresolved_discussions",
        )
        mocks.api.add_merge_request(
            dict(mocks.mocklab.merge_request_info),
            from_state="unresolved_discussions",
        )
        message = (
            "Gitlab refused to merge this request and I don't know why! "
            "Maybe you have unresolved discussions?"
        )
        with mocks.mocklab.expected_failure(message):
            with patch.dict(
                mocks.mocklab.project_info,
                only_allow_merge_if_all_discussions_are_resolved=True,
            ):
                mocks.job.execute()
        assert mocks.api.state == "unresolved_discussions"
        assert mocks.api.notes == [f"I couldn't merge this branch: {message}"]

    def test_discovers_if_someone_closed_the_merge_request(self, mocks):
        rewritten_sha = mocks.mocklab.rewritten_sha
        mocks.api.add_transition(
            PUT(
                f"/projects/1234/merge_requests/"
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Error(
                marge.gitlab.MethodNotAllowed(
                    405, {"message": "405 Method Not Allowed"}
                )
            ),
            from_state="passed",
            to_state="oops_someone_closed_it",
        )
        mocks.api.add_merge_request(
            dict(mocks.mocklab.merge_request_info, state="closed"),
            from_state="oops_someone_closed_it",
        )
        message = "Someone closed the merge request while I was attempting to merge it."
        with mocks.mocklab.expected_failure(message):
            mocks.job.execute()
        assert mocks.api.state == "oops_someone_closed_it"
        assert mocks.api.notes == [f"I couldn't merge this branch: {message}"]

    def test_tells_explicitly_that_gitlab_refused_to_merge(self, mocks):
        rewritten_sha = mocks.mocklab.rewritten_sha
        mocks.api.add_transition(
            PUT(
                f"/projects/1234/merge_requests/"
                f'{mocks.mocklab.merge_request_info["iid"]}/merge',
                {
                    "sha": rewritten_sha,
                    "should_remove_source_branch": True,
                    "merge_when_pipeline_succeeds": True,
                },
            ),
            Error(
                marge.gitlab.MethodNotAllowed(
                    405, {"message": "405 Method Not Allowed"}
                )
            ),
            from_state="passed",
            to_state="rejected_for_mysterious_reasons",
        )
        message = "Gitlab refused to merge this request and I don't know why!"
        with mocks.mocklab.expected_failure(message):
            mocks.job.execute()
        assert mocks.api.state == "rejected_for_mysterious_reasons"
        assert mocks.api.notes == [f"I couldn't merge this branch: {message}"]

    def test_wont_merge_draft_stuff(self, mocks):
        draft_merge_request = dict(mocks.mocklab.merge_request_info, draft=True)
        mocks.api.add_merge_request(draft_merge_request, from_state="initial")

        with mocks.mocklab.expected_failure(
            "Sorry, I can't merge requests marked as Draft!"
        ):
            mocks.job.execute()

        assert mocks.api.state == "initial"
        assert mocks.api.notes == [
            "I couldn't merge this branch: Sorry, I can't merge requests marked as Draft!",
        ]

    def test_wont_merge_branches_with_autosquash_if_rewriting(self, mocks):
        autosquash_merge_request = dict(mocks.mocklab.merge_request_info, squash=True)
        mocks.api.add_merge_request(autosquash_merge_request, from_state="initial")

        admin_user = dict(mocks.mocklab.user_info, is_admin=True)
        mocks.api.add_user(admin_user, is_current=True)

        if mocks.job.opts.requests_commit_tagging:
            message = "Sorry, merging requests marked as auto-squash would ruin my commit tagging!"
            with mocks.mocklab.expected_failure(message):
                mocks.job.execute()
            assert mocks.api.state == "initial"
        else:
            mocks.job.execute()
            assert mocks.api.state == "merged"

    @patch("marge.job.log", autospec=True)
    def test_waits_for_approvals(self, mock_log, mocks_factory):
        five_secs = timedelta(seconds=5)
        mocks = mocks_factory(
            extra_opts={"approval_timeout": five_secs, "reapprove": True}
        )
        mocks.job.execute()

        mock_log.info.assert_any_call("Checking if approvals have reset")
        mock_log.debug.assert_any_call(
            "Approvals haven't reset yet, sleeping for %s secs", ANY
        )
        assert mocks.api.state == "merged"

    def test_fails_if_changes_already_exist(self, mocks):
        source_project_info = (
            mocks.mocklab.forked_project_info or mocks.mocklab.project_info
        )
        source_project_url = source_project_info["ssh_url_to_repo"]
        target_project_url = mocks.mocklab.project_info["ssh_url_to_repo"]
        remote_source_repo = mocks.job.repo.mock_impl.remote_repos[source_project_url]
        remote_target_repo = mocks.job.repo.mock_impl.remote_repos[target_project_url]
        source_branch = mocks.mocklab.merge_request_info["source_branch"]
        target_branch = mocks.mocklab.merge_request_info["target_branch"]

        remote_target_repo.set_ref(
            target_branch, remote_source_repo.get_ref(source_branch)
        )
        expected_message = f"these changes already exist in branch `{target_branch}`"

        with mocks.mocklab.expected_failure(expected_message):
            mocks.job.execute()

        assert mocks.api.state == "initial"
        assert mocks.api.notes == [f"I couldn't merge this branch: {expected_message}"]
