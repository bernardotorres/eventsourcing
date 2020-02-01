import os
import traceback
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Dict, List, Optional, Tuple, Type

import ray

from eventsourcing.application.process import (
    ProcessApplication,
    Prompt,
    PromptToPull,
    is_prompt,
)
from eventsourcing.application.simple import ApplicationWithConcreteInfrastructure
from eventsourcing.domain.model.decorators import retry
from eventsourcing.domain.model.events import subscribe, unsubscribe
from eventsourcing.exceptions import (
    OperationalError,
    ProgrammingError,
    RecordConflictError,
)
from eventsourcing.infrastructure.base import DEFAULT_PIPELINE_ID
from eventsourcing.system.definition import AbstractSystemRunner, System
from eventsourcing.system.rayhelpers import RayNotificationLog, RayPrompt
from eventsourcing.system.runner import DEFAULT_POLL_INTERVAL

ray.init()


def start_ray_system():
    pass
    # ray.init(ignore_reinit_error=True)


def shutdown_ray_system():
    pass
    # ray.shutdown()


class RayRunner(AbstractSystemRunner):
    """
    Uses actor model framework to run a system of process applications.
    """

    def __init__(
        self,
        system: System,
        pipeline_ids=(DEFAULT_PIPELINE_ID,),
        poll_interval: Optional[int] = None,
        setup_tables: bool = False,
        sleep_for_setup_tables: int = 0,
        db_uri: Optional[str] = None,
        **kwargs
    ):
        super(RayRunner, self).__init__(system=system, **kwargs)
        self.pipeline_ids = list(pipeline_ids)
        self.poll_interval = poll_interval
        self.setup_tables = setup_tables or system.setup_tables
        self.sleep_for_setup_tables = sleep_for_setup_tables
        self.db_uri = db_uri
        self.ray_processes: Dict[Tuple[str, int], RayProcess] = {}

    def start(self):
        """
        Starts all the actors to run a system of process applications.
        """
        # Check we have the infrastructure classes we need.
        for process_class in self.system.process_classes.values():
            if not isinstance(process_class, ApplicationWithConcreteInfrastructure):
                if not self.infrastructure_class:
                    raise ProgrammingError("infrastructure_class is not set")
                elif not issubclass(
                    self.infrastructure_class, ApplicationWithConcreteInfrastructure
                ):
                    raise ProgrammingError(
                        "infrastructure_class is not a subclass of {}".format(
                            ApplicationWithConcreteInfrastructure
                        )
                    )

        # Get the DB_URI.
        # Todo: Support different URI for different application classes.
        env_vars = {}
        db_uri = self.db_uri or os.environ.get("DB_URI")

        if db_uri is not None:
            env_vars["DB_URI"] = db_uri

        assert env_vars.get(
            "DB_URI"
        ), "DB_URI not set: Ray runner doesn't work with in-memory database at the mo"

        # Start processes.
        for pipeline_id in self.pipeline_ids:
            for process_name, process_class in self.system.process_classes.items():
                ray_process_id = RayProcess.remote(
                    application_process_class=process_class,
                    infrastructure_class=self.infrastructure_class,
                    env_vars=env_vars,
                    poll_interval=self.poll_interval,
                    pipeline_id=pipeline_id,
                    setup_tables=self.setup_tables,
                )
                self.ray_processes[(process_name, pipeline_id)] = ray_process_id

        init_ids = []

        for key, ray_process in self.ray_processes.items():
            process_name, pipeline_id = key
            upstream_names = self.system.upstream_names[process_name]
            downstream_names = self.system.downstream_names[process_name]
            downstream_processes = {
                name: self.ray_processes[(name, pipeline_id)]
                for name in downstream_names
            }

            upstream_logs = {}
            for upstream_name in upstream_names:
                upstream_process = self.ray_processes[(upstream_name, pipeline_id)]
                notification_log = RayNotificationLog(upstream_process, 5, ray.get)
                upstream_logs[upstream_name] = notification_log

            init_ids.append(
                ray_process.init.remote(upstream_logs, downstream_processes)
            )

        ray.get(init_ids)

        run_ids = []
        for ray_process in self.ray_processes.values():
            run_ids.append(ray_process.run.remote())
        ray.get(run_ids)

    def get_ray_process(self, process_name, pipeline_id=DEFAULT_PIPELINE_ID):
        assert isinstance(process_name, str)
        return self.ray_processes[(process_name, pipeline_id)]

    def close(self):
        super(RayRunner, self).close()
        processes = self.ray_processes.values()
        stop_ids = [p.stop.remote() for p in processes]
        ray.get(stop_ids, timeout=6)

    def call(self, process_name, pipeline_id, method_name, *args, **kwargs):
        paxosprocess0 = self.get_ray_process(process_name, pipeline_id)
        ray_id = paxosprocess0.call.remote(method_name, *args, **kwargs)
        return ray.get(ray_id)


@ray.remote
class RayProcess:
    def __init__(
        self,
        application_process_class: Type[ProcessApplication],
        infrastructure_class: Type[ApplicationWithConcreteInfrastructure],
        env_vars: dict = None,
        pipeline_id: int = DEFAULT_PIPELINE_ID,
        poll_interval: int = None,
        setup_tables: bool = False,
    ):
        self.application_process_class = application_process_class
        self.infrastructure_class = infrastructure_class
        self.daemon = True
        self.pipeline_id = pipeline_id
        self.poll_interval = poll_interval or DEFAULT_POLL_INTERVAL
        self.setup_tables = setup_tables
        self.push_prompt_queue = Queue(maxsize=100)
        self.upstream_event_queue = Queue(maxsize=100)
        self.prompted_names = set()
        self.prompted_names_lock = Lock()
        self.has_been_prompted = Event()
        self.has_been_stopped = Event()
        self.event_reading_lock = Lock()
        if env_vars is not None:
            os.environ.update(env_vars)

    def init(self, upstream_logs: dict, downstream_processes: dict) -> None:
        self.upstream_logs = upstream_logs
        self.downstream_processes = downstream_processes

        # Subscribe to broadcast prompts published by the process application.
        subscribe(handler=self.enqueue_prompt, predicate=is_prompt)

        # Construct process application class.
        process_class = self.application_process_class
        if not isinstance(process_class, ApplicationWithConcreteInfrastructure):
            if self.infrastructure_class:
                process_class = process_class.mixin(self.infrastructure_class)
            else:
                raise ProgrammingError("infrastructure_class is not set")

        # Construct process application object.
        self.process: ProcessApplication = process_class(
            pipeline_id=self.pipeline_id, setup_table=self.setup_tables
        )
        # print(getpid(), "Created application process: %s" % self.process)

        for upstream_name, ray_notification_log in self.upstream_logs.items():
            # Make the process follow the upstream notification log.
            self.process.follow(upstream_name, ray_notification_log)

    def follow(self, upstream_name, ray_notification_log):
        # print("Received follow: ", upstream_name, ray_notification_log)
        # self.tmpdict[upstream_name] = ray_notification_log
        self.process.follow(upstream_name, ray_notification_log)

    def run(self) -> None:
        self.reset_readers()
        self.pull_notifications_thread = Thread(target=self.process_notifications)
        self.pull_notifications_thread.setDaemon(True)
        self.pull_notifications_thread.start()
        self.process_events_thread = Thread(target=self.process_events)
        self.process_events_thread.setDaemon(True)
        self.process_events_thread.start()
        self.push_prompts_thread = Thread(target=self.push_prompts)
        self.push_prompts_thread.setDaemon(True)
        self.push_prompts_thread.start()

    @retry(OperationalError, max_attempts=10, wait=0.1)
    def get_notification_log_section(self, section_id):
        return self.process.notification_log[section_id]

    @retry(OperationalError, max_attempts=10, wait=0.1)
    def call(self, application_method_name, *args, **kwargs):
        # print("Calling", application_method_name, args, kwargs)
        if self.process:
            method = getattr(self.process, application_method_name)
            return method(*args, **kwargs)
        else:
            raise Exception(
                "Can't call method '%s' before process exists" % application_method_name
            )

    def prompt(self, prompt: List[Prompt] = None):
        if isinstance(prompt, PromptToPull):
            with self.prompted_names_lock:
                self.prompted_names.add(prompt.process_name)
                self.has_been_prompted.set()

    def process_notifications(self) -> None:
        # Loop until stop event is set.
        while not self.has_been_stopped.is_set():
            # Wait until prompted.
            if self.has_been_prompted.wait(timeout=1):
                # Have been prompted.
                is_timeout = False
            else:
                # Timed out waiting to be prompted.
                is_timeout = True

            # Check if process has been stopped since waiting to be prompted.
            if self.has_been_stopped.is_set():
                break

            if is_timeout:
                prompted_names = self.upstream_logs.keys()
            else:
                # Get the prompted names.
                with self.prompted_names_lock:
                    self.has_been_prompted.clear()
                    prompted_names, self.prompted_names = self.prompted_names, set()

            # Get new upstream domain events.
            with self.event_reading_lock:

                for prompted_name in prompted_names:

                    # Get the notifications.
                    upstream_name = prompted_name
                    reader = self.process.readers[upstream_name]
                    notifications = reader.read()

                    for notification in notifications:
                        # Check causal dependencies.
                        self.process.check_causal_dependencies(
                            upstream_name, notification.get("causal_dependencies")
                        )

                        # Get event from notification.
                        event = self.process.get_event_from_notification(notification)

                        # Put the event on the queue.
                        self.upstream_event_queue.put(
                            (event, notification["id"], upstream_name)
                        )

    def process_events(self):
        while not self.has_been_stopped.is_set():
            try:
                item = self.upstream_event_queue.get(timeout=1)
                domain_event, notification_id, upstream_name = item
            except Empty:
                if self.has_been_stopped.is_set():
                    break
            else:
                self.upstream_event_queue.task_done()
                if self.has_been_stopped.is_set():
                    break
                try:
                    # print("Processing upstream event:", (domain_event,
                    # notification_id, upstream_name))

                    new_events = self.process.process_upstream_event(
                        domain_event, notification_id, upstream_name
                    )
                    # print("New events:", new_events)
                except Exception as e:
                    print(traceback.format_exc())
                    with self.event_reading_lock:
                        self.reset_readers()
                        with self.upstream_event_queue.mutex:
                            self.upstream_event_queue.queue.clear()
                        with self.prompted_names_lock:
                            for upstream_name in self.upstream_logs.keys():
                                self.prompted_names.add(upstream_name)
                            self.has_been_prompted.set()
                else:
                    if new_events:
                        prompt = RayPrompt(self.process.name,
                                           self.process.pipeline_id)
                        self.push_prompt_queue.put(prompt)

    def reset_readers(self):
        for upstream_name in self.process.readers:
            self.process.set_reader_position_from_tracking_records(upstream_name)

    def enqueue_prompt(self, prompts):
        self.push_prompt_queue.put(prompts[0])

    def push_prompts(self) -> None:
        while not self.has_been_stopped.is_set():
            try:
                prompt = self.push_prompt_queue.get(timeout=10)
            except Empty:
                if self.has_been_stopped.is_set():
                    break
            else:
                self.push_prompt_queue.task_done()
                if self.has_been_stopped.is_set():
                    break
                prompt_response_ids = []
                for downstream_name, ray_process in self.downstream_processes.items():
                    prompt_response_ids.append(ray_process.prompt.remote(prompt))
                    if self.has_been_stopped.is_set():
                        break
                ray.get(prompt_response_ids)

    def run_process(self, prompt: Optional[Prompt] = None) -> None:
        try:
            self._run_process(prompt)
        except:
            print(
                traceback.format_exc() + "\nException was ignored so that actor can "
                                         "continue running."
            )

    @retry((RecordConflictError, OperationalError, KeyError), 50, wait=0.1)
    def _run_process(self, prompt: Optional[Prompt] = None) -> None:
        self.process.run(prompt)

    def stop(self):
        self.has_been_stopped.set()
        self.push_prompt_queue.put(None)
        self.has_been_prompted.set()
        self.pull_notifications_thread.join(timeout=3)
        self.push_prompts_thread.join(timeout=3)
        unsubscribe(handler=self.enqueue_prompt, predicate=is_prompt)
