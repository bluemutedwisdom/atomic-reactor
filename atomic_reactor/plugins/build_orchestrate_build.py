"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, division

from collections import namedtuple
from copy import deepcopy
from multiprocessing.pool import ThreadPool

import yaml
import json
import os
from operator import attrgetter
import random
from string import ascii_letters
import time
import logging
from datetime import timedelta
import datetime as dt
import copy

from atomic_reactor.build import BuildResult
from atomic_reactor.plugin import BuildStepPlugin
from atomic_reactor.plugins.pre_reactor_config import get_config
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild
from atomic_reactor.util import get_preferred_label, df_parser, get_build_json
from atomic_reactor.constants import PLUGIN_ADD_FILESYSTEM_KEY, PLUGIN_BUILD_ORCHESTRATE_KEY
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from osbs.conf import Configuration
from osbs.constants import BUILD_FINISHED_STATES


ClusterInfo = namedtuple('ClusterInfo', ('cluster', 'platform', 'osbs', 'load'))
WORKSPACE_KEY_BUILD_INFO = 'build_info'
WORKSPACE_KEY_UPLOAD_DIR = 'koji_upload_dir'
WORKSPACE_KEY_OVERRIDE_KWARGS = 'override_kwargs'
FIND_CLUSTER_RETRY_DELAY = 15.0
FAILURE_RETRY_DELAY = 10.0
MAX_CLUSTER_FAILS = 20


def get_worker_build_info(workflow, platform):
    """
    Obtain worker build information for a given platform
    """
    workspace = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
    return workspace[WORKSPACE_KEY_BUILD_INFO][platform]


def get_koji_upload_dir(workflow):
    """
    Obtain koji_upload_dir value used for worker builds
    """
    workspace = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
    return workspace[WORKSPACE_KEY_UPLOAD_DIR]


def override_build_kwarg(workflow, k, v):
    """
    Override a build-kwarg for all worker builds
    """
    key = OrchestrateBuildPlugin.key

    workspace = workflow.plugin_workspace.setdefault(key, {})
    override_kwargs = workspace.setdefault(WORKSPACE_KEY_OVERRIDE_KWARGS, {})
    override_kwargs[k] = v


class UnknownPlatformException(Exception):
    """ No clusters could be found for a platform """


class AllClustersFailedException(Exception):
    """ Each cluster has reached max_cluster_fails """


class ClusterRetryContext(object):
    def __init__(self, max_cluster_fails):
        # how many times this cluster has failed
        self.fails = 0

        # datetime at which attempts can resume
        self.retry_at = dt.datetime.utcfromtimestamp(0)

        # the number of fail counts before this cluster is considered dead
        self.max_cluster_fails = max_cluster_fails

    @property
    def failed(self):
        """Is this cluster considered dead?"""
        return self.fails >= self.max_cluster_fails

    @property
    def in_retry_wait(self):
        """Should we wait before trying this cluster again?"""
        return dt.datetime.now() < self.retry_at

    def try_again_later(self, seconds):
        """Put this cluster in retry-wait (or consider it dead)"""
        if not self.failed:
            self.fails += 1
            self.retry_at = (dt.datetime.now() + timedelta(seconds=seconds))


def wait_for_any_cluster(contexts):
    """
    Wait until any of the clusters are out of retry-wait

    :param contexts: List[ClusterRetryContext]
    :raises: AllClustersFailedException if no more retry attempts allowed
    """
    try:
        earliest_retry_at = min(ctx.retry_at for ctx in contexts.values()
                                if not ctx.failed)
    except ValueError:  # can't take min() of empty sequence
        raise AllClustersFailedException(
            "Could not find appropriate cluster for worker build."
        )

    time_until_next = earliest_retry_at - dt.datetime.now()
    time.sleep(max(timedelta(seconds=0), time_until_next).seconds)


class WorkerBuildInfo(object):

    def __init__(self, build, cluster_info, logger):
        self.build = build
        self.cluster = cluster_info.cluster
        self.osbs = cluster_info.osbs
        self.platform = cluster_info.platform
        self.log = logging.LoggerAdapter(logger, {'arch': self.platform})

        self.monitor_exception = None

    @property
    def name(self):
        return self.build.get_build_name() if self.build else 'N/A'

    def wait_to_finish(self):
        self.build = self.osbs.wait_for_build_to_finish(self.name)
        return self.build

    def watch_logs(self):
        for line in self.osbs.get_build_logs(self.name, follow=True):
            self.log.info(line)

    def get_annotations(self):
        build_annotations = self.build.get_annotations() or {}
        annotations = {
            'build': {
                'cluster-url': self.osbs.os_conf.get_openshift_base_uri(),
                'namespace': self.osbs.os_conf.get_namespace(),
                'build-name': self.name,
            },
            'digests': json.loads(
                build_annotations.get('digests', '[]')),
            'plugins-metadata': json.loads(
                build_annotations.get('plugins-metadata', '{}')),
        }

        if 'metadata_fragment' in build_annotations and \
           'metadata_fragment_key' in build_annotations:
            annotations['metadata_fragment'] = build_annotations['metadata_fragment']
            annotations['metadata_fragment_key'] = build_annotations['metadata_fragment_key']

        return annotations

    def get_fail_reason(self):
        fail_reason = {}
        if self.monitor_exception:
            fail_reason['general'] = repr(self.monitor_exception)
        elif not self.build:
            fail_reason['general'] = 'build not started'

        if not self.build:
            return fail_reason

        build_annotations = self.build.get_annotations() or {}
        metadata = json.loads(build_annotations.get('plugins-metadata', '{}'))
        if self.monitor_exception:
            fail_reason['general'] = repr(self.monitor_exception)

        try:
            fail_reason.update(metadata['errors'])
        except KeyError:
            try:
                build_name = self.build.get_build_name()
                pod = self.osbs.get_pod_for_build(build_name)
                fail_reason['pod'] = pod.get_failure_reason()
            except (OsbsException, AttributeError):
                # Catch AttributeError here because osbs-client < 0.41
                # doesn't have this method
                pass

        return fail_reason

    def cancel_build(self):
        if self.build and not self.build.is_finished():
            self.osbs.cancel_build(self.name)


class OrchestrateBuildPlugin(BuildStepPlugin):
    """
    Start and monitor worker builds for each platform

    This plugin will find the best suited worker cluster to
    be used for each platform. It does so by calculating the
    current load of active builds on each cluster and choosing
    the one with smallest load.

    The list of available worker clusters is retrieved by fetching
    the result provided by reactor_config plugin.

    If any of the worker builds fail, this plugin will return a
    failed BuildResult. Although, it does wait for all worker builds
    to complete in any case.

    If all worker builds succeed, then this plugin returns a
    successful BuildResult, but with a remote image result. The
    image is built in the worker builds which is likely a different
    host than the one running this build. This means that the local
    docker daemon has no knowledge of the built image.

    If build_image is defined it is passed to the worker build,
    but there is still possibility to have build_imagestream inside
    osbs.conf in the secret, and that would take precendence over
    build_image from kwargs
    """

    CONTAINER_FILENAME = 'container.yaml'
    UNREACHABLE_CLUSTER_LOAD = object()

    key = PLUGIN_BUILD_ORCHESTRATE_KEY

    def __init__(self, tasker, workflow, platforms, build_kwargs,
                 osbs_client_config=None, worker_build_image=None,
                 config_kwargs=None,
                 find_cluster_retry_delay=FIND_CLUSTER_RETRY_DELAY,
                 failure_retry_delay=FAILURE_RETRY_DELAY,
                 max_cluster_fails=MAX_CLUSTER_FAILS):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param platforms: list<str>, platforms to build
        :param build_kwargs: dict, keyword arguments for starting worker builds
        :param osbs_client_config: str, path to directory containing osbs.conf
        :param worker_build_image: str, the builder image to use for worker builds
                                  (not used, image is inherited from the orchestrator)
        :param config_kwargs: dict, keyword arguments to override worker configuration
        :param find_cluster_retry_delay: the delay in seconds to try again reaching a cluster
        :param failure_retry_delay: the delay in seconds to try again starting a build
        :param max_cluster_fails: the maximum number of times a cluster can fail before being
                                  ignored
        """
        super(OrchestrateBuildPlugin, self).__init__(tasker, workflow)
        self.platforms = set(platforms)
        self.build_kwargs = build_kwargs
        self.osbs_client_config = osbs_client_config
        self.config_kwargs = config_kwargs or {}
        self.find_cluster_retry_delay = find_cluster_retry_delay
        self.failure_retry_delay = failure_retry_delay
        self.max_cluster_fails = max_cluster_fails
        self.koji_upload_dir = self.get_koji_upload_dir()
        self.fs_task_id = self.get_fs_task_id()
        self.release = self.get_release()

        if worker_build_image:
            self.log.warning('worker_build_image is deprecated')

        self.worker_builds = []

    def make_list(self, value):
        if not isinstance(value, list):
            value = [value]
        return value

    def get_platforms(self):
        build_file_dir = self.workflow.source.get_build_file_path()[1]
        excluded_platforms = set()
        container_path = os.path.join(build_file_dir, self.CONTAINER_FILENAME)
        if os.path.exists(container_path):
            with open(container_path) as f:
                data = yaml.load(f)
                if data is None or 'platforms' not in data or data['platforms'] is None:
                    return self.platforms
                excluded_platforms = set(self.make_list(data['platforms'].get('not', [])))
                only_platforms = set(self.make_list(data['platforms'].get('only', [])))
                if only_platforms:
                    self.platforms = self.platforms & only_platforms
        return self.platforms - excluded_platforms

    def get_current_builds(self, osbs):
        field_selector = ','.join(['status!={status}'.format(status=status.capitalize())
                                   for status in BUILD_FINISHED_STATES])
        with osbs.retries_disabled():
            return len(osbs.list_builds(field_selector=field_selector))

    def get_cluster_info(self, cluster, platform):
        kwargs = deepcopy(self.config_kwargs)
        kwargs['conf_section'] = cluster.name
        if self.osbs_client_config:
            kwargs['conf_file'] = os.path.join(self.osbs_client_config, 'osbs.conf')

        conf = Configuration(**kwargs)
        osbs = OSBS(conf, conf)

        current_builds = self.get_current_builds(osbs)

        load = current_builds / cluster.max_concurrent_builds
        self.log.debug('enabled cluster %s for platform %s has load %s and active builds %s/%s',
                       cluster.name, platform, load, current_builds, cluster.max_concurrent_builds)
        return ClusterInfo(cluster, platform, osbs, load)

    def get_clusters(self, platform, retry_contexts, all_clusters):
        ''' return clusters sorted by load. '''

        possible_cluster_info = {}
        candidates = set(copy.copy(all_clusters))
        while candidates and not possible_cluster_info:
            wait_for_any_cluster(retry_contexts)

            for cluster in sorted(candidates, key=attrgetter('priority')):
                ctx = retry_contexts[cluster.name]
                if ctx.in_retry_wait:
                    continue
                if ctx.failed:
                    continue
                try:
                    cluster_info = self.get_cluster_info(cluster, platform)
                    possible_cluster_info[cluster] = cluster_info
                except OsbsException:
                    ctx.try_again_later(self.find_cluster_retry_delay)
            candidates -= set([c for c in candidates if retry_contexts[c.name].failed])

        ret = sorted(possible_cluster_info.values(), key=lambda c: c.cluster.priority)
        ret = sorted(ret, key=lambda c: c.load)
        return ret

    def get_release(self):
        labels = df_parser(self.workflow.builder.df_path, workflow=self.workflow).labels
        return get_preferred_label(labels, 'release')

    @staticmethod
    def get_koji_upload_dir():
        """
        Create a path name for uploading files to

        :return: str, path name expected to be unique
        """
        dir_prefix = 'koji-upload'
        random_chars = ''.join([random.choice(ascii_letters)
                                for _ in range(8)])
        unique_fragment = '%r.%s' % (time.time(), random_chars)
        return os.path.join(dir_prefix, unique_fragment)

    def get_worker_build_kwargs(self, release, platform, koji_upload_dir,
                                task_id):
        build_kwargs = deepcopy(self.build_kwargs)

        build_kwargs.pop('architecture', None)

        build_kwargs['release'] = release
        build_kwargs['platform'] = platform
        build_kwargs['koji_upload_dir'] = koji_upload_dir
        build_kwargs['is_auto'] = is_rebuild(self.workflow)
        if task_id:
            build_kwargs['filesystem_koji_task_id'] = task_id

        return build_kwargs

    def _apply_repositories(self, annotations):
        unique = set()
        primary = set()

        for build_info in self.worker_builds:
            if not build_info.build:
                continue
            repositories = build_info.build.get_repositories() or {}
            unique.update(repositories.get('unique', []))
            primary.update(repositories.get('primary', []))

        if unique or primary:
            annotations['repositories'] = {
                'unique': sorted(list(unique)),
                'primary': sorted(list(primary)),
            }

    def _make_labels(self):
        labels = {}
        koji_build_id = None
        ids = set([build_info.build.get_koji_build_id()
                   for build_info in self.worker_builds
                   if build_info.build])
        self.log.debug('all koji-build-ids: %s', ids)
        if ids:
            koji_build_id = ids.pop()

        if koji_build_id:
            labels['koji-build-id'] = koji_build_id

        return labels

    def get_fs_task_id(self):
        task_id = None

        fs_result = self.workflow.prebuild_results.get(PLUGIN_ADD_FILESYSTEM_KEY)
        if fs_result is None:
            return None

        try:
            task_id = int(fs_result['filesystem-koji-task-id'])
        except KeyError:
            self.log.error("%s: expected filesystem-koji-task-id in result",
                           PLUGIN_ADD_FILESYSTEM_KEY)
            raise
        except (ValueError, TypeError):
            self.log.exception("%s: returned an invalid task ID: %r",
                               PLUGIN_ADD_FILESYSTEM_KEY, task_id)
            raise

        self.log.debug("%s: got filesystem_koji_task_id of %d",
                       PLUGIN_ADD_FILESYSTEM_KEY, task_id)

        return task_id

    def do_worker_build(self, cluster_info):
        workspace = self.workflow.plugin_workspace.get(self.key, {})
        override_kwargs = workspace.get(WORKSPACE_KEY_OVERRIDE_KWARGS, {})

        build = None

        try:
            kwargs = self.get_worker_build_kwargs(self.release, cluster_info.platform,
                                                  self.koji_upload_dir, self.fs_task_id)
            kwargs.update(override_kwargs)
            with cluster_info.osbs.retries_disabled():
                build = cluster_info.osbs.create_worker_build(**kwargs)
        except OsbsException:
            self.log.exception('%s - failed to create worker build.',
                               cluster_info.platform)
            raise
        except Exception:
            self.log.exception('%s - failed to create worker build',
                               cluster_info.platform)

        build_info = WorkerBuildInfo(build=build, cluster_info=cluster_info, logger=self.log)
        self.worker_builds.append(build_info)

        if build_info.build:
            try:
                self.log.info('%s - created build %s on cluster %s.', cluster_info.platform,
                              build_info.name, cluster_info.cluster.name)
                build_info.watch_logs()
                build_info.wait_to_finish()
            except Exception as e:
                build_info.monitor_exception = e
                self.log.exception('%s - failed to monitor worker build',
                                   cluster_info.platform)

                # Attempt to cancel it rather than leave it running
                # unmonitored.
                try:
                    build_info.cancel_build()
                except OsbsException:
                    pass

    def select_and_start_cluster(self, platform):
        ''' Choose a cluster and start a build on it '''

        config = get_config(self.workflow)
        clusters = config.get_enabled_clusters_for_platform(platform)

        if not clusters:
            raise UnknownPlatformException('No clusters found for platform {}!'
                                           .format(platform))

        retry_contexts = {
            cluster.name: ClusterRetryContext(self.max_cluster_fails)
            for cluster in clusters
        }

        while True:
            try:
                possible_cluster_info = self.get_clusters(platform,
                                                          retry_contexts,
                                                          clusters)
            except AllClustersFailedException as ex:
                cluster = ClusterInfo(None, platform, None, None)
                build_info = WorkerBuildInfo(build=None,
                                             cluster_info=cluster,
                                             logger=self.log)
                build_info.monitor_exception = repr(ex)
                self.worker_builds.append(build_info)
                return

            for cluster_info in possible_cluster_info:
                ctx = retry_contexts[cluster_info.cluster.name]
                try:
                    self.log.info('Attempting to start build for platform %s on cluster %s',
                                  platform, cluster_info.cluster.name)
                    self.do_worker_build(cluster_info)
                    return
                except OsbsException:
                    ctx.try_again_later(self.failure_retry_delay)
                    # this will put the cluster in retry-wait when get_clusters runs

    def set_build_image(self):
        """
        Overrides build_image for worker, to be same as in orchestrator build
        """
        spec = get_build_json().get("spec")
        try:
            build_name = spec['strategy']['customStrategy']['from']['name']
            build_kind = spec['strategy']['customStrategy']['from']['kind']
        except KeyError:
            raise RuntimeError("Build object is malformed, failed to fetch buildroot image")

        if build_kind == 'DockerImage':
            self.config_kwargs['build_image'] = build_name
        else:
            raise RuntimeError("Build kind isn't 'DockerImage' but %s" % build_kind)

    def run(self):
        self.set_build_image()
        platforms = self.get_platforms()

        thread_pool = ThreadPool(len(platforms))
        result = thread_pool.map_async(self.select_and_start_cluster, platforms)

        try:
            result.get()
        # Always clean up worker builds on any error to avoid
        # runaway worker builds (includes orchestrator build cancellation)
        except Exception:
            thread_pool.terminate()
            self.log.info('build cancelled, cancelling worker builds')
            if self.worker_builds:
                ThreadPool(len(self.worker_builds)).map(
                    lambda bi: bi.cancel_build(), self.worker_builds)
            while not result.ready():
                result.wait(1)
            raise
        else:
            thread_pool.close()
            thread_pool.join()

        annotations = {'worker-builds': {
            build_info.platform: build_info.get_annotations()
            for build_info in self.worker_builds if build_info.build
        }}

        self._apply_repositories(annotations)

        labels = self._make_labels()

        fail_reasons = {
            build_info.platform: build_info.get_fail_reason()
            for build_info in self.worker_builds
            if not build_info.build or not build_info.build.is_succeeded()
        }

        workspace = self.workflow.plugin_workspace.setdefault(self.key, {})
        workspace[WORKSPACE_KEY_UPLOAD_DIR] = self.koji_upload_dir
        workspace[WORKSPACE_KEY_BUILD_INFO] = {build_info.platform: build_info
                                               for build_info in self.worker_builds}

        if fail_reasons:
            return BuildResult(fail_reason=json.dumps(fail_reasons),
                               annotations=annotations, labels=labels)

        return BuildResult.make_remote_image_result(annotations, labels=labels)
