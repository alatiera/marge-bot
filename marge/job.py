# pylint: disable=too-many-locals,too-many-branches,too-many-statements
import dataclasses
import datetime
import enum
import logging as log
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, cast

import requests

from . import git, gitlab, interval
from .approvals import Approvals, CustomApprovals
from .branch import Branch
from .interval import IntervalUnion
from .merge_request import MergeRequest, MergeRequestRebaseFailed
from .pipeline import Pipeline
from .project import Project
from .user import User


class MergeJob:
    def __init__(
        self,
        *,
        api: gitlab.Api,
        user: User,
        project: Project,
        repo: git.Repo,
        options: "MergeJobOptions",
    ):
        self._api = api
        self._user = user
        self._project = project
        self._repo = repo
        self._options = options
        self._merge_timeout = datetime.timedelta(minutes=5)

        # Normally we trust GitLab to tell us if approval criteria are met
        self.approvals_factory: Callable[
            [gitlab.Api, Dict[str, Any]], Approvals
        ] = Approvals
        custom_allowed = options.custom_allowed_approvers
        if (
            custom_allowed is not None
            and custom_allowed
            and options.custom_required_approvals
        ):
            # But sometimes we provide this logic instead
            self.approvals_factory = lambda api, info: CustomApprovals(
                api,
                info,
                allowed_approvers=custom_allowed,
                approvals_required=options.custom_required_approvals,
            )

    @property
    def repo(self) -> git.Repo:
        return self._repo

    @property
    def project(self) -> Project:
        return self._project

    @property
    def opts(self) -> "MergeJobOptions":
        return self._options

    def execute(self, exc_comment: Optional[str] = None) -> None:
        raise NotImplementedError

    def ensure_mergeable_mr(self, merge_request: MergeRequest) -> None:
        merge_request.refetch_info()
        log.info("Ensuring MR !%s is mergeable", merge_request.iid)
        log.debug("Ensuring MR %r is mergeable", merge_request)

        if merge_request.draft:
            raise CannotMerge("Sorry, I can't merge requests marked as Draft!")

        if merge_request.squash and self._options.requests_commit_tagging:
            raise CannotMerge(
                "Sorry, merging requests marked as auto-squash would ruin my commit tagging!"
            )

        approvals = merge_request.fetch_approvals(self.approvals_factory)
        if not approvals.sufficient:
            raise CannotMerge(
                "Insufficient approvals "
                f"(have: {approvals.approver_usernames} missing: {approvals.approvals_left})"
            )

        if not merge_request.blocking_discussions_resolved:
            raise CannotMerge(
                "Sorry, I can't merge requests which have unresolved discussions!"
            )

        state = merge_request.state
        if state not in ("opened", "reopened", "locked"):
            if state in ("merged", "closed"):
                raise SkipMerge(f"The merge request is already {state}!")
            raise CannotMerge(f"The merge request is in an unknown state: {state}")

        if self.during_merge_embargo():
            raise SkipMerge("Merge embargo!")

        if self._user.id not in merge_request.assignee_ids:
            raise SkipMerge("It is not assigned to me anymore!")

    def add_trailers(self, merge_request: MergeRequest) -> Optional[str]:
        log.info("Adding trailers for MR !%s", merge_request.iid)

        # add Reviewed-by
        should_add_reviewers = (
            self._options.add_reviewers
            and self._options.fusion is not Fusion.gitlab_rebase
        )
        reviewers = (
            _get_reviewer_names_and_emails(
                merge_request.fetch_commits(),
                merge_request.fetch_approvals(self.approvals_factory),
                self._api,
            )
            if should_add_reviewers
            else None
        )
        sha = None
        if reviewers is not None:
            sha = self._repo.tag_with_trailer(
                trailer_name="Reviewed-by",
                trailer_values=reviewers,
                branch=merge_request.source_branch,
                start_commit="origin/" + merge_request.target_branch,
                keep_trailers=self._options.keep_reviewers,
            )

        # add Tested-by
        should_add_tested = (
            self._options.add_tested
            and self._project.only_allow_merge_if_pipeline_succeeds
            and self._options.fusion is Fusion.rebase
        )

        tested_by = (
            [f"{self._user.name} <{merge_request.web_url}>"]
            if should_add_tested
            else None
        )
        if tested_by is not None:
            sha = self._repo.tag_with_trailer(
                trailer_name="Tested-by",
                trailer_values=tested_by,
                branch=merge_request.source_branch,
                start_commit=merge_request.source_branch + "^",
                keep_trailers=False,
            )

        # add Part-of
        should_add_parts_of = (
            self._options.add_part_of
            and self._options.fusion is not Fusion.gitlab_rebase
        )
        part_of = f"<{merge_request.web_url}>" if should_add_parts_of else None
        if part_of is not None:
            sha = self._repo.tag_with_trailer(
                trailer_name="Part-of",
                trailer_values=[part_of],
                branch=merge_request.source_branch,
                start_commit="origin/" + merge_request.target_branch,
                keep_trailers=False,
            )
        return sha

    def get_mr_ci_status(
        self, merge_request: MergeRequest, commit_sha: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        if commit_sha is None:
            commit_sha = merge_request.sha

        pipelines = Pipeline.pipelines_by_merge_request(
            merge_request.target_project_id,
            merge_request.iid,
            self._api,
        )
        current_pipeline = next(
            iter(pipeline for pipeline in pipelines if pipeline.sha == commit_sha), None
        )

        if current_pipeline:
            ci_status = current_pipeline.status
            pipeline_msg = f"See pipeline {current_pipeline.web_url}."
        else:
            log.warning(
                "No pipeline listed for %s on branch %s",
                commit_sha,
                merge_request.source_branch,
            )
            ci_status = None
            pipeline_msg = "No pipeline associated."

        return ci_status, pipeline_msg

    def wait_for_ci_to_pass(
        self, merge_request: MergeRequest, commit_sha: Optional[str] = None
    ) -> None:
        time_0 = datetime.datetime.utcnow()
        waiting_time_in_secs = 10

        if commit_sha is None:
            commit_sha = merge_request.sha

        log.info(
            "Waiting for CI to pass for MR !%s - %s",
            merge_request.iid,
            merge_request.web_url,
        )
        if TYPE_CHECKING:
            assert self._options.ci_timeout is not None
        consecutive_errors = 0
        while True:
            try:
                ci_status, pipeline_msg = self.get_mr_ci_status(
                    merge_request, commit_sha=commit_sha
                )
            except (gitlab.InternalServerError, requests.exceptions.Timeout):
                consecutive_errors += 1
                if consecutive_errors > 5:
                    raise

                time.sleep(waiting_time_in_secs)
                continue

            if ci_status == "success":
                log.info("CI for MR !%s passed. %s", merge_request.iid, pipeline_msg)
                return

            if ci_status == "skipped":
                log.info("CI for MR !%s skipped. %s", merge_request.iid, pipeline_msg)
                return

            if ci_status == "failed":
                raise CannotMerge(f"CI failed! {pipeline_msg}")

            if ci_status in ("canceling", "canceled"):
                raise CannotMerge(f"Someone canceled the CI. {pipeline_msg}")

            if ci_status not in ("created", "pending", "running"):
                log.warning("Suspicious CI status: %r. %s", ci_status, pipeline_msg)

            # Do we have enough time left to wait for another loop?
            time_after_wait = datetime.datetime.utcnow() + datetime.timedelta(
                seconds=waiting_time_in_secs
            )
            if time_after_wait - time_0 < self._options.ci_timeout:
                log.debug(
                    "Waiting for %s secs before polling CI status again",
                    waiting_time_in_secs,
                )
                time.sleep(waiting_time_in_secs)
                continue

            break

        raise CannotMerge(f"CI is taking too long. {pipeline_msg}")

    def wait_for_merge_status_to_resolve(self, merge_request: MergeRequest) -> None:
        """
        This function is for polling the async `detailed_merge_status` field in
        merge_request API response. We suspected that the lag `detailed_merge_status`
        prevents MRs to be merged, and the fix did work for some users.

        But we are not sure if this is the root cause and if this is a proper fix. As there're some
        evidence that suggest gitlab will always check the mergeability synchronously while merging MRs.
        See more https://github.com/smarkets/marge-bot/pull/265#issuecomment-724147901
        """
        attempts = 3
        waiting_time_in_secs = 5

        log.info(
            "Waiting for MR !%s to have detailed_merge_status mergeable",
            merge_request.iid,
        )
        for attempt in range(attempts):
            merge_request.refetch_info()
            detailed_merge_status = merge_request.detailed_merge_status

            if detailed_merge_status == "mergeable":
                log.info(
                    "MR !%s can be merged on attempt %d", merge_request.iid, attempt
                )
                return

            if detailed_merge_status in (
                "unchecked",
                "checking",
                "approvals_syncing",
            ):
                log.info(
                    "MR !%s merge status is '%s' on attempt %d.",
                    merge_request.iid,
                    detailed_merge_status,
                    attempt,
                )

            else:
                log.info(
                    "MR !%s cannot be merged on attempt %d with status %s",
                    merge_request.iid,
                    attempt,
                    detailed_merge_status,
                )
                raise CannotMerge(
                    f"MR has status '{detailed_merge_status}' and cannot be merged."
                )

            time.sleep(waiting_time_in_secs)

    def unassign_from_mr(self, merge_request: MergeRequest) -> None:
        log.info("Unassigning from MR !%s", merge_request.iid)
        author_id = merge_request.author_id
        if author_id is not None and author_id != self._user.id:
            merge_request.assign_to(author_id)
        else:
            merge_request.unassign()

    def during_merge_embargo(self) -> bool:
        now = datetime.datetime.utcnow()
        if TYPE_CHECKING:
            assert self.opts.embargo is not None
        return self.opts.embargo.covers(now)

    def maybe_reapprove(
        self, merge_request: MergeRequest, approvals: Approvals
    ) -> None:
        # Re-approve the merge request, in case us pushing it has removed approvals.
        if self.opts.reapprove:
            # approving is not idempotent, so we need to check first that there
            # are no approvals, otherwise we'll get a failure on trying to
            # re-instate the previous approvals
            def sufficient_approvals() -> bool:
                return merge_request.fetch_approvals(self.approvals_factory).sufficient

            # Make sure we don't race by ensuring approvals have reset since the push
            waiting_time_in_secs = 5
            if TYPE_CHECKING:
                assert self._options.approval_timeout is not None
            approval_timeout_in_secs = self._options.approval_timeout.total_seconds()
            iterations = round(approval_timeout_in_secs / waiting_time_in_secs)
            log.info("Checking if approvals have reset")
            while sufficient_approvals() and iterations:
                log.debug(
                    "Approvals haven't reset yet, sleeping for %s secs",
                    waiting_time_in_secs,
                )
                time.sleep(waiting_time_in_secs)
                iterations -= 1
            if not sufficient_approvals():
                approvals.reapprove()

    def fetch_source_project(
        self, merge_request: MergeRequest
    ) -> Tuple[Project, Optional[str], str]:
        remote = "origin"
        remote_url = None
        source_project = self.get_source_project(merge_request)
        if source_project is not self._project:
            remote = "source"
            remote_url = source_project.ssh_url_to_repo
            self._repo.fetch(
                remote_name=remote,
                remote_url=remote_url,
            )
        return source_project, remote_url, remote

    def get_source_project(self, merge_request: MergeRequest) -> Project:
        source_project = self._project
        if merge_request.source_project_id != self._project.id:
            source_project = Project.fetch_by_id(
                merge_request.source_project_id,
                api=self._api,
            )
        return source_project

    def get_target_project(self, merge_request: MergeRequest) -> Project:
        return Project.fetch_by_id(merge_request.target_project_id, api=self._api)

    def fuse(
        self,
        source: str,
        target: str,
        source_repo_url: Optional[str] = None,
        local: bool = False,
    ) -> str:
        # NOTE: this leaves git switched to branch_a
        strategies = {
            Fusion.rebase: self._repo.rebase,
            Fusion.merge: self._repo.merge,
            Fusion.gitlab_rebase: self._repo.rebase,  # we rebase locally to know sha
        }

        strategy = strategies[self._options.fusion]
        return cast(
            str,
            strategy(
                source,
                target,
                source_repo_url=source_repo_url,
                local=local,
            ),  # type: ignore[operator]
        )

    def update_from_target_branch_and_push(
        self,
        merge_request: MergeRequest,
        source_repo_url: Optional[str] = None,
        skip_ci: bool = False,
        add_trailers: bool = True,
    ) -> Tuple[str, str, str]:
        """Updates `source_branch` on `target_branch`, optionally add trailers and push.
        The update strategy can either be rebase or merge. The default is rebase.

        Returns
        -------
        (sha_of_target_branch, sha_after_update, sha_after_rewrite)
        """
        repo = self._repo
        source_branch = merge_request.source_branch
        target_branch = merge_request.target_branch
        assert source_repo_url != repo.remote_url
        if source_repo_url is None and source_branch == target_branch:
            raise CannotMerge("source and target branch seem to coincide!")

        branch_update_done = commits_rewrite_done = False
        try:
            initial_mr_sha = merge_request.sha
            updated_sha = self.fuse(
                source_branch,
                target_branch,
                source_repo_url=source_repo_url,
            )
            branch_update_done = True
            # The fuse above fetches origin again, so we are now safe to fetch
            # the sha from the remote target branch.
            target_sha = repo.get_commit_hash("origin/" + target_branch)
            if updated_sha == target_sha:
                raise CannotMerge(
                    f"these changes already exist in branch `{target_branch}`"
                )
            final_sha = self.add_trailers(merge_request) if add_trailers else None
            final_sha = final_sha or updated_sha
            commits_rewrite_done = True
            branch_was_modified = final_sha != initial_mr_sha
            self.synchronize_mr_with_local_changes(
                merge_request,
                branch_was_modified,
                source_repo_url,
                skip_ci=skip_ci,
            )
        except git.GitError as err:
            # A failure to clean up probably means something is fucked with the git repo
            # and likely explains any previous failure, so it will better to just
            # raise a GitError
            if source_branch != self.project.default_branch:
                repo.checkout_branch(self.project.default_branch)
                repo.remove_branch(source_branch)

            if not branch_update_done:
                raise CannotMerge(
                    "got conflicts while rebasing, your problem now..."
                ) from err
            if not commits_rewrite_done:
                raise CannotMerge("failed on filter-branch; check my logs!") from err
            raise
        return target_sha, updated_sha, final_sha

    def synchronize_mr_with_local_changes(
        self,
        merge_request: MergeRequest,
        branch_was_modified: bool,
        source_repo_url: Optional[str] = None,
        skip_ci: bool = False,
    ) -> None:
        if self._options.fusion is Fusion.gitlab_rebase:
            self.synchronize_using_gitlab_rebase(merge_request)
        else:
            self.push_force_to_mr(
                merge_request,
                branch_was_modified,
                source_repo_url=source_repo_url,
                skip_ci=skip_ci,
            )

    def push_force_to_mr(
        self,
        merge_request: MergeRequest,
        branch_was_modified: bool,
        source_repo_url: Optional[str] = None,
        skip_ci: bool = False,
    ) -> None:
        try:
            self._repo.push(
                merge_request.source_branch,
                source_repo_url=source_repo_url,
                force=True,
                skip_ci=skip_ci,
            )
        except git.GitError as err:

            def fetch_remote_branch() -> Branch:
                return Branch.fetch_by_name(
                    merge_request.source_project_id,
                    merge_request.source_branch,
                    self._api,
                )

            if branch_was_modified and fetch_remote_branch().protected:
                raise CannotMerge("Sorry, I can't modify protected branches!") from err

            change_type = "merged" if self.opts.fusion == Fusion.merge else "rebased"
            raise CannotMerge(
                f"Failed to push {change_type} changes, check my logs!"
            ) from err

    def synchronize_using_gitlab_rebase(
        self, merge_request: MergeRequest, expected_sha: Optional[str] = None
    ) -> None:
        expected_sha = expected_sha or self._repo.get_commit_hash()
        try:
            merge_request.rebase()
        except MergeRequestRebaseFailed as err:
            raise CannotMerge(
                f"GitLab failed to rebase the branch saying: {err.args[0]}"
            ) from err
        except TimeoutError as err:
            raise CannotMerge(
                "GitLab was taking too long to rebase the branch..."
            ) from err
        except gitlab.ApiError as err:
            branch = Branch.fetch_by_name(
                merge_request.source_project_id,
                merge_request.source_branch,
                self._api,
            )
            if branch.protected:
                raise CannotMerge("Sorry, I can't modify protected branches!") from err
            raise

        if merge_request.sha != expected_sha:
            raise GitLabRebaseResultMismatch(
                gitlab_sha=merge_request.sha,
                expected_sha=expected_sha,
            )


def _get_reviewer_names_and_emails(
    commits: List[Dict[str, Any]], approvals: Approvals, api: gitlab.Api
) -> List[str]:
    """Return a list ['A. Prover <a.prover@example.com', ...]` for `merge_request.`"""
    uids = approvals.approver_ids
    users = [User.fetch_by_id(uid, api) for uid in uids]
    self_reviewed = {commit["author_email"] for commit in commits} & {
        user.email for user in users
    }
    if self_reviewed and len(users) <= 1:
        raise CannotMerge("Commits require at least one independent reviewer.")
    return [f"{user.name} <{user.email}>" for user in users]


# pylint: disable=invalid-name
@enum.unique
class Fusion(enum.Enum):
    merge = 0
    rebase = 1
    gitlab_rebase = 2


@dataclasses.dataclass
class MergeJobOptions:
    add_tested: bool = False
    add_part_of: bool = False
    add_reviewers: bool = False
    keep_reviewers: bool = False
    reapprove: bool = False
    approval_timeout: Optional[datetime.timedelta] = dataclasses.field(
        default_factory=lambda: datetime.timedelta(seconds=0)
    )
    embargo: Optional[interval.IntervalUnion] = dataclasses.field(
        default_factory=IntervalUnion.empty
    )
    ci_timeout: Optional[datetime.timedelta] = dataclasses.field(
        default_factory=lambda: datetime.timedelta(minutes=15)
    )
    fusion: Fusion = Fusion.rebase
    use_no_ff_batches: bool = False
    use_merge_commit_batches: bool = False
    skip_ci_batches: bool = False
    guarantee_final_pipeline: bool = False
    custom_allowed_approvers: Optional[List[str]] = None
    custom_required_approvals: int = 0

    def __post_init__(self) -> None:
        # Set any fields with None values to the result of its default_factory
        # callable.
        for field_ in dataclasses.fields(self):
            if getattr(self, field_.name) is None:
                if field_.default_factory != dataclasses.MISSING:
                    setattr(self, field_.name, field_.default_factory())

    @property
    def requests_commit_tagging(self) -> bool:
        return self.add_tested or self.add_part_of or self.add_reviewers


class CannotMerge(Exception):
    @property
    def reason(self) -> str:
        args = self.args
        if not args:
            return "Unknown reason!"

        return cast(str, args[0])


class SkipMerge(CannotMerge):
    pass


class GitLabRebaseResultMismatch(CannotMerge):
    def __init__(self, gitlab_sha: str, expected_sha: str) -> None:
        super().__init__(
            "GitLab rebase ended up with a different commit:"
            f"I expected {expected_sha} but they got {gitlab_sha}"
        )
