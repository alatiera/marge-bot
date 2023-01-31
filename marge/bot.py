import dataclasses
import datetime
import logging as log
import re
import time
from tempfile import TemporaryDirectory
from typing import List, Optional, Dict

import threading

from . import batch_job, git, gitlab, job
from . import merge_request as merge_request_module
from . import single_merge_job, store
from . import user as user_module
from .project import AccessLevel, Project

MergeRequest = merge_request_module.MergeRequest


class Bot:
    def __init__(self, *, api: gitlab.Api, config: "BotConfig"):
        self._api = api
        self._config = config

        user = config.user
        opts = config.merge_opts

        if not user.is_admin:
            assert (
                not opts.reapprove
            ), f"{user.username} is not an admin, can't impersonate!"
            assert (
                not opts.add_reviewers
            ), f"{user.username} is not an admin, can't lookup Reviewed-by: email addresses "

    def start(self) -> None:
        with TemporaryDirectory() as root_dir:
            repo_manager: store.RepoManager
            if self._config.use_https:
                repo_manager = store.HttpsRepoManager(
                    user=self.user,
                    root_dir=root_dir,
                    auth_token=self._config.auth_token,
                    timeout=self._config.git_timeout,
                    reference=self._config.git_reference_repo,
                )
            else:
                repo_manager = store.SshRepoManager(
                    user=self.user,
                    root_dir=root_dir,
                    ssh_key_file=self._config.ssh_key_file,
                    timeout=self._config.git_timeout,
                    reference=self._config.git_reference_repo,
                )
            self._run(repo_manager)

    @property
    def user(self) -> user_module.User:
        return self._config.user

    @property
    def api(self) -> gitlab.Api:
        return self._api

    def _run(self, repo_manager: store.RepoManager) -> None:
        time_to_sleep_between_projects_in_secs = (
            1 if not self._config.concurrent_projects else 30
        )
        min_time_to_sleep_after_iterating_all_projects_in_secs = (
            30 if not self._config.concurrent_projects else 300
        )

        project_threads: Dict[str, threading.Thread] = {}

        while True:
            projects = self._get_projects()

            # Purge dead threads so they can restart
            project_threads = dict(filter(lambda thread: thread[1].is_alive(), project_threads.items()))
            # Quit threads for removed projects
            # ... no built in thread quiting, assume these will exit naturally

            for project in projects:
                if self._config.concurrent_projects:
                    if project.path_with_namespace not in project_threads:
                        log.info('Starting to watch for %s', project.path_with_namespace)
                        project_threads[project.path_with_namespace] = threading.Thread(
                            target=self._process_project,
                            args=(repo_manager, time_to_sleep_between_projects_in_secs, project, True),
                            daemon=True
                        )
                        project_threads[project.path_with_namespace].start()
                else:
                    self._process_project(
                        repo_manager,
                        time_to_sleep_between_projects_in_secs,
                        project,
                        False
                    )

            if self._config.cli:
                return

            if self._config.concurrent_projects:
                log.info('Watching %s repos of %s projects', len(project_threads), len(projects))

            times_slept = len(projects) if not self._config.concurrent_projects else 0
            already_slept = time_to_sleep_between_projects_in_secs * times_slept
            big_sleep = max(0, min_time_to_sleep_after_iterating_all_projects_in_secs - already_slept)
            log.info('Sleeping for %s seconds...', big_sleep)
            time.sleep(big_sleep)

    def _get_projects(self) -> List[Project]:
        log.info("Finding out my current projects...")
        my_projects = Project.fetch_all_mine(self._api)
        project_regexp = self._config.project_regexp
        filtered_projects = [
            p for p in my_projects if project_regexp.match(p.path_with_namespace)
        ]
        log.debug(
            "Projects that match project_regexp: %s",
            [p.path_with_namespace for p in filtered_projects],
        )
        filtered_out = set(my_projects) - set(filtered_projects)
        if filtered_out:
            log.debug(
                "Projects that do not match project_regexp: %s",
                [p.path_with_namespace for p in filtered_out],
            )
        return filtered_projects

    def _process_project(
        self,
        repo_manager: store.RepoManager,
        time_to_sleep_between_projects_in_secs: int,
        project: Project,
        forever: bool,
    ) -> None:
        try:
            while True:
                project_name = project.path_with_namespace

                if project.access_level < AccessLevel.reporter:
                    log.warning("Don't have enough permissions to browse merge requests in %s!", project_name)
                    continue

                merge_requests = self._get_merge_requests(project, project_name)
                self._process_merge_requests(repo_manager, project, merge_requests)
                time.sleep(time_to_sleep_between_projects_in_secs)

                if not forever:
                    return

        except Exception:
            log.exception("Project error (%s)!", project_name)
            raise

    def _get_merge_requests(
        self, project: Project, project_name: str
    ) -> List[MergeRequest]:
        log.info("Fetching merge requests assigned to me in %s...", project_name)
        my_merge_requests = MergeRequest.fetch_all_open_for_user(
            project_id=project.id,
            user=self.user,
            api=self._api,
            merge_order=self._config.merge_order,
        )
        branch_regexp = self._config.branch_regexp
        filtered_mrs = [
            mr for mr in my_merge_requests if branch_regexp.match(mr.target_branch)
        ]
        log.debug(
            "MRs that match branch_regexp: %s", [mr.web_url for mr in filtered_mrs]
        )
        filtered_out = set(my_merge_requests) - set(filtered_mrs)
        if filtered_out:
            log.debug(
                "MRs that do not match branch_regexp: %s",
                [mr.web_url for mr in filtered_out],
            )
        source_branch_regexp = self._config.source_branch_regexp
        source_filtered_mrs = [
            mr for mr in filtered_mrs if source_branch_regexp.match(mr.source_branch)
        ]
        log.debug(
            "MRs that match source_branch_regexp: %s",
            [mr.web_url for mr in source_filtered_mrs],
        )
        source_filtered_out = set(filtered_mrs) - set(source_filtered_mrs)
        if source_filtered_out:
            log.debug(
                "MRs that do not match source_branch_regexp: %s",
                [mr.web_url for mr in source_filtered_out],
            )
        return source_filtered_mrs

    def _process_merge_requests(
        self,
        repo_manager: store.RepoManager,
        project: Project,
        merge_requests: List[MergeRequest],
    ) -> None:
        if not merge_requests:
            log.info("Nothing to merge at this point...")
            return

        try:
            repo = repo_manager.repo_for_project(project)
        except git.GitError:
            log.exception("Couldn't initialize repository for project!")
            raise

        log.info("Got %s requests to merge;", len(merge_requests))
        if self._config.batch and len(merge_requests) > 1:
            log.info(
                "Attempting to merge as many MRs as possible using BatchMergeJob..."
            )
            batch_merge_job = batch_job.BatchMergeJob(
                api=self._api,
                user=self.user,
                project=project,
                merge_requests=merge_requests,
                repo=repo,
                options=self._config.merge_opts,
                batch_branch_name=self._config.batch_branch_name,
            )
            try:
                batch_merge_job.execute(exc_comment=self._config.exc_comment)
                return
            except batch_job.CannotBatch as err:
                log.warning("BatchMergeJob aborted: %s", err)
            except job.CannotMerge as err:
                log.warning("BatchMergeJob failed: %s", err)
                return
            except git.GitError as err:
                log.exception("BatchMergeJob failed: %s", err)
        log.info("Attempting to merge the oldest MR...")
        merge_request = merge_requests[0]
        merge_job = self._get_single_job(
            project=project,
            merge_request=merge_request,
            repo=repo,
            options=self._config.merge_opts,
        )
        merge_job.execute(exc_comment=self._config.exc_comment)

    def _get_single_job(
        self,
        project: Project,
        merge_request: MergeRequest,
        repo: git.Repo,
        options: job.MergeJobOptions,
    ) -> single_merge_job.SingleMergeJob:
        return single_merge_job.SingleMergeJob(
            api=self._api,
            user=self.user,
            project=project,
            merge_request=merge_request,
            repo=repo,
            options=options,
        )


@dataclasses.dataclass
class BotConfig:
    user: user_module.User
    use_https: bool
    auth_token: str
    ssh_key_file: Optional[str]
    project_regexp: "re.Pattern[str]"
    concurrent_projects: bool
    merge_order: str
    merge_opts: job.MergeJobOptions
    git_timeout: datetime.timedelta
    git_reference_repo: str
    branch_regexp: "re.Pattern[str]"
    source_branch_regexp: "re.Pattern[str]"
    batch: bool
    cli: bool
    batch_branch_name: str
    exc_comment: Optional[str]


MergeJobOptions = job.MergeJobOptions
Fusion = job.Fusion
