#!/usr/bin/env python
"""
Schedule containerized avocado-cloud tests for Alibaba Cloud.
"""

import argparse
import logging
import toml
import json
import subprocess
import shutil
import threading
import os
import time
import random

REPO_PATH = os.path.split(os.path.realpath(__file__))[0]

LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

ARG_PARSER = argparse.ArgumentParser(description="Schedule containerized \
avocado-cloud tests for Alibaba Cloud.")
ARG_PARSER.add_argument(
    '--config',
    dest='config',
    action='store',
    help='Toml file for test scheduler configuration.',
    default='./config.toml',
    required=False)
ARG_PARSER.add_argument(
    '--tasklist',
    dest='tasklist',
    action='store',
    help='Toml file for the task list.',
    default='./tasklist.toml',
    required=False)

ARGS = ARG_PARSER.parse_args()

TEMPLATE_PATH = './templates'


class TestScheduler():
    """Schedule the containerized avocado-cloud testing."""

    def __init__(self):
        # Load and parse user config
        with open(ARGS.config, 'r') as f:
            _data = toml.load(f)
            config = _data.get('scheduler', {})
            LOG.debug(f'{ARGS.config}: {config}')

        self.logpath = config.get('log_path', os.path.join(REPO_PATH, 'logs'))
        self.dry_run = config.get('dry_run', False)
        self.max_threads = config.get('max_threads', 4)
        self.max_retries_testcase = config.get('max_retries_testcase', 2)
        self.max_retries_resource = config.get('max_retries_resource', 10)

        LOG.debug(f'User config: logpath: {self.logpath}')
        LOG.debug(f'User config: dry_run: {self.dry_run}')
        LOG.debug(f'User config: max_threads: {self.max_threads}')
        LOG.debug(
            f'User config: max_retries_testcase: {self.max_retries_testcase}')
        LOG.debug(
            f'User config: max_retries_resource: {self.max_retries_resource}')

        # Load tasks
        try:
            with open(f'{ARGS.tasklist}', 'r') as f:
                tasks = toml.load(f)
        except Exception as ex:
            LOG.error(f'Failed to load tasks from {ARGS.tasklist}: {ex}')
            exit(1)

        for k, v in tasks.items():
            v.setdefault('status')
            v.setdefault('remaining_retries_testcase',
                         self.max_retries_testcase)
            v.setdefault('remaining_retries_resource',
                         self.max_retries_resource)

        LOG.debug(f'Loaded {len(tasks)} task(s): {tasks}')

        self.tasks = tasks
        self.lock = threading.Lock()

        # Save tasks
        self._save_tasks()

        # Create Producer and Consumer
        self.queue = []
        self.threads = []
        self.producer = threading.Thread(
            target=self.producer, name='Producer', daemon=True)
        self.consumer = threading.Thread(
            target=self.consumer, name='Consumer', daemon=False)

    def producer(self):
        while True:
            time.sleep(1)
            is_save_needed = False

            self.lock.acquire(timeout=60)
            for k, v in self.tasks.items():
                if v.get('status') in (None, 'NEEDRETRY'):
                    self.queue.append(k)
                    v['status'] = 'WAITING'
                    is_save_needed = True
            self.lock.release()

            if is_save_needed:
                self._save_tasks()

    def consumer(self):
        # Start after the Producer
        time.sleep(2)

        while True:
            time.sleep(1)

            # Run tasks if possible
            if len(self.threads) < self.max_threads and len(self.queue) > 0:
                flavor = self.queue.pop(0)
                t = threading.Thread(target=self.run_task,
                                     args=(flavor,), name='RunTask')
                t.start()
                self.threads.append(t)

            # Clean up finished tasks
            self.threads = [x for x in self.threads if x.is_alive()]
            LOG.debug(f'Function consumer: Tasks in Queue: {len(self.queue)}; '
                      f'Running Threads: {len(self.threads)}')

            # Exits if no more tasks
            if len(self.threads) == 0 and len(self.queue) == 0:
                # Wait new task for a while
                time.sleep(10)

                if len(self.queue) == 0:
                    LOG.info('Consumer exits since there are no more tasks '
                             'to process.')
                    break

    def start(self):
        self.producer.start()
        self.consumer.start()

    def stop(self):
        self.consumer.join()
        return 0

    def update_task(self, flavor, ask_for_retry=False,
                    retry_counter_name='remaining_retries_testcase', **args):
        """Update the status for a specified task."""

        # Lock
        self.lock.acquire(timeout=60)

        # General update
        _dict = self.tasks[flavor]
        _dict.update(args)

        if ask_for_retry:
            if _dict.get(retry_counter_name, 0) > 0:
                # Save varibles to rebuild the task info
                _remaining_retries_testcase = _dict.get(
                    'remaining_retries_testcase', 0)
                _remaining_retries_resource = _dict.get(
                    'remaining_retries_resource', 0)

                # Append current entry to the history entries
                _history = _dict.pop('history', [])
                _history.append(_dict.copy())

                # Rebuild the task info
                _dict.clear()
                _dict['status'] = 'NEEDRETRY'
                _dict['remaining_retries_testcase'] = _remaining_retries_testcase
                _dict['remaining_retries_resource'] = _remaining_retries_resource
                _dict[retry_counter_name] -= 1
                _dict['history'] = _history

        # Unlock
        self.lock.release()

        LOG.debug(f'Function update_task({flavor}) self.tasks: {self.tasks}')
        self._save_tasks()

    def _save_tasks(self):
        """Save to the tasklist file."""
        self.lock.acquire(timeout=60)
        try:
            with open(f'{ARGS.tasklist}', 'w') as f:
                toml.dump(self.tasks, f)
        except Exception as ex:
            LOG.warning(f'Failed to save tasks to {ARGS.tasklist}: {ex}')
            return 1
        finally:
            self.lock.release()

        return 0

    def run_task(self, flavor):
        LOG.info(f'Task for "{flavor}" is started.')
        start_sec = time.time()
        time_start = time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(start_sec))

        self.update_task(flavor, status='RUNNING', time_start=time_start)
        ts = time.strftime('%y%m%d%H%M%S', time.localtime(start_sec))
        logname = f'task_{ts}_{flavor}.log'
        cmd = f'nohup {REPO_PATH}/executor.py --config {ARGS.config} \
            --flavor {flavor} > {self.logpath}/{logname}'

        if self.dry_run:
            time.sleep(random.random() * 3 + 2)
            return_code = random.choice(
                [0, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 31, 32, 33, 41])
        else:
            res = subprocess.run(cmd, shell=True)
            return_code = res.returncode

        stop_sec = time.time()
        time_stop = time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(stop_sec))
        time_used = round((stop_sec - start_sec), 2)

        # - 0  - Test executed and passed (test_passed)
        # - 11 - Test error due to general error (test_general_error)
        # - 12 - Test error due to container error (test_container_error)
        # - 13 - Test error due to log delivery error (test_log_delivery_error)
        # - 14 - Test failed due to general error (test_failed_general)
        # - 15 - Test failed due to error cases (test_failed_error_cases)
        # - 16 - Test failed due to failure cases (test_failed_failure_cases)
        # - 21 - General failure while getting AZ (flavor_general_error)
        # - 22 - Flavor is out of stock (flavor_no_stock)
        # - 23 - Possible AZs are not enabled (flavor_azone_disabled)
        # - 24 - Eligible AZs are occupied (flavor_azone_occupied)
        # - 31 - General failure while getting container (container_error)
        # - 32 - Cannot get idle container (container_all_busy)
        # - 33 - Lock or Unlock container failed (container_lock_error)
        # - 41 - General failure while provisioning data (provision_error)

        code_to_status = {
            0: 'test_passed',
            11: 'test_general_error',
            12: 'test_container_error',
            13: 'test_log_delivery_error',
            14: 'test_failed_general',
            15: 'test_failed_error_cases',
            16: 'test_failed_failure_cases',
            21: 'flavor_general_error',
            22: 'flavor_no_stock',
            23: 'flavor_azone_disabled',
            24: 'flavor_azone_occupied',
            31: 'container_error',
            32: 'container_all_busy',
            33: 'container_lock_error',
            41: 'provision_error'
        }

        status_code = code_to_status.get(return_code, 'unknown_status')

        if return_code in (12, 23, 24, 31, 32, 33):
            # Need to retry for resouces
            _ask_for_retry = True
            _retry_counter_name = 'remaining_retries_resource'
        elif return_code in (15,):
            # Need to retry for testcase
            _ask_for_retry = True
            _retry_counter_name = 'remaining_retries_testcase'
        else:
            # No need to retry
            _ask_for_retry = False
            _retry_counter_name = None

        # Update the task info
        res = self.update_task(
            flavor,
            ask_for_retry=_ask_for_retry,
            retry_counter_name=_retry_counter_name,
            status='FINISHED',
            return_code=return_code,
            status_code=status_code,
            time_stop=time_stop,
            time_used=time_used,
            test_log=logname)

        LOG.info(f'Task for "{flavor}" is finished (Status: {status_code}).')

        return return_code

    def post_process(self, code):
        LOG.debug(f'Got return code {code} in post_process function.')


if __name__ == '__main__':

    ts = TestScheduler()
    ts.start()
    ts.stop()


exit(0)
