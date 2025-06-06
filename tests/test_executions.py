from time import sleep
from unittest.mock import patch

from rq.executions import Execution, ExecutionRegistry
from rq.job import Job
from rq.queue import Queue
from rq.utils import current_timestamp, now
from rq.worker import Worker
from tests import RQTestCase
from tests.fixtures import long_running_job, say_hello, start_worker_process


class TestRegistry(RQTestCase):
    """Test the execution registry."""

    def setUp(self):
        super().setUp()
        self.queue = Queue(connection=self.connection)

    def test_equality(self):
        """Test equality between Execution objects"""
        job = self.queue.enqueue(say_hello)
        pipeline = self.connection.pipeline()
        execution_1 = Execution.create(job=job, ttl=100, pipeline=pipeline)
        execution_2 = Execution.create(job=job, ttl=100, pipeline=pipeline)
        pipeline.execute()
        self.assertNotEqual(execution_1, execution_2)
        fetched_execution = Execution.fetch(id=execution_1.id, job_id=job.id, connection=self.connection)
        self.assertEqual(execution_1, fetched_execution)

    def test_add_delete_executions(self):
        """Test adding and deleting executions"""
        job = self.queue.enqueue(say_hello)
        pipeline = self.connection.pipeline()
        execution = Execution.create(job=job, ttl=100, pipeline=pipeline)
        pipeline.execute()
        created_at = execution.created_at
        composite_key = execution.composite_key
        self.assertTrue(execution.composite_key.startswith(job.id))  # Composite key is prefixed by job ID
        self.assertLessEqual(self.connection.ttl(execution.key), 100)

        execution = Execution.fetch(id=execution.id, job_id=job.id, connection=self.connection)
        self.assertEqual(execution.created_at.timestamp(), created_at.timestamp())
        self.assertEqual(execution.composite_key, composite_key)
        self.assertEqual(execution.last_heartbeat.timestamp(), created_at.timestamp())

        execution.delete(job=job, pipeline=pipeline)
        pipeline.execute()

        self.assertFalse(self.connection.exists(execution.key))

    def test_execution_registry(self):
        """Test the ExecutionRegistry class"""
        job = self.queue.enqueue(say_hello)
        registry = ExecutionRegistry(job_id=job.id, connection=self.connection)

        pipeline = self.connection.pipeline()
        execution = Execution.create(job=job, ttl=100, pipeline=pipeline)
        pipeline.execute()

        self.assertEqual(self.connection.zcard(registry.key), 1)
        # Registry key TTL should be execution TTL + some buffer time (60 at the moment)
        self.assertTrue(158 <= self.connection.ttl(registry.key) <= 160)

        execution.delete(pipeline=pipeline, job=job)
        pipeline.execute()
        self.assertEqual(self.connection.zcard(registry.key), 0)

    def test_ttl(self):
        """Execution registry and job execution should follow heartbeat TTL"""
        job = self.queue.enqueue(say_hello, timeout=-1)
        worker = Worker([self.queue], connection=self.connection)
        execution = worker.prepare_execution(job=job)
        self.assertGreaterEqual(self.connection.ttl(job.execution_registry.key), worker.get_heartbeat_ttl(job))
        self.assertGreaterEqual(self.connection.ttl(execution.key), worker.get_heartbeat_ttl(job))

    def test_heartbeat(self):
        """Test heartbeat should refresh execution as well as registry TTL"""
        job = self.queue.enqueue(say_hello, timeout=1)
        worker = Worker([self.queue], connection=self.connection)
        execution = worker.prepare_execution(job=job)

        # The actual TTL should be 150 seconds
        self.assertTrue(1 < self.connection.ttl(job.execution_registry.key) < 160)
        self.assertTrue(1 < self.connection.ttl(execution.key) < 160)
        with self.connection.pipeline() as pipeline:
            worker.execution.heartbeat(job.started_job_registry, 200, pipeline)
            pipeline.execute()

        # The actual TTL should be 260 seconds for registry and 200 seconds for execution
        self.assertTrue(200 <= self.connection.ttl(job.execution_registry.key) <= 260)
        self.assertTrue(200 <= self.connection.ttl(execution.key) < 260)

    def test_registry_cleanup(self):
        """ExecutionRegistry.cleanup() should remove expired executions."""
        job = self.queue.enqueue(say_hello)
        worker = Worker([self.queue], connection=self.connection)
        worker.prepare_execution(job=job)

        registry = job.execution_registry
        registry.cleanup()

        self.assertEqual(len(registry), 1)

        registry.cleanup(current_timestamp() + 100)
        self.assertEqual(len(registry), 1)

        # If we pass in a timestamp past execution's TTL, it should be removed.
        # Expiration should be about 150 seconds (worker.get_heartbeat_ttl(job) + 60)
        registry.cleanup(current_timestamp() + 200)
        self.assertEqual(len(registry), 0)

    def test_delete_registry(self):
        """ExecutionRegistry.delete() should delete registry and its executions."""
        job = self.queue.enqueue(say_hello)
        worker = Worker([self.queue], connection=self.connection)
        execution = worker.prepare_execution(job=job)

        self.assertIn(execution.job_id, job.started_job_registry.get_job_ids())

        registry = job.execution_registry
        pipeline = self.connection.pipeline()
        registry.delete(job=job, pipeline=pipeline)
        pipeline.execute()

        self.assertNotIn(execution.job_id, job.started_job_registry.get_job_ids())
        self.assertFalse(self.connection.exists(registry.key))

    def test_get_execution_ids(self):
        """ExecutionRegistry.get_execution_ids() should return a list of execution IDs"""
        job = self.queue.enqueue(say_hello)
        worker = Worker([self.queue], connection=self.connection)

        execution = worker.prepare_execution(job=job)
        execution_2 = worker.prepare_execution(job=job)

        registry = job.execution_registry
        self.assertEqual(set(registry.get_execution_ids()), {execution.id, execution_2.id})

    def test_execution_added_to_started_job_registry(self):
        """Ensure worker adds execution to started job registry"""
        job = self.queue.enqueue(long_running_job, timeout=3)
        Worker([self.queue], connection=self.connection)

        # Start worker process in background with 1 second monitoring interval
        process = start_worker_process(
            self.queue.name, worker_name='w1', connection=self.connection, burst=True, job_monitoring_interval=1
        )

        sleep(0.5)
        # Execution should be registered in started job registry
        execution = job.get_executions()[0]
        self.assertEqual(len(job.get_executions()), 1)
        self.assertIn(execution.job_id, job.started_job_registry.get_job_ids())

        last_heartbeat = execution.last_heartbeat
        last_heartbeat = now()
        self.assertTrue(30 < self.connection.ttl(execution.key) < 200)

        sleep(2)
        # During execution, heartbeat should be updated, this test is flaky on MacOS
        execution.refresh()
        self.assertNotEqual(execution.last_heartbeat, last_heartbeat)
        process.join(10)

        # When job is done, execution should be removed from started job registry
        self.assertNotIn(execution.composite_key, job.started_job_registry.get_job_ids())
        self.assertEqual(job.get_status(), 'finished')

    def test_fetch_execution(self):
        """Ensure Execution.fetch() fetches the correct execution"""
        job = self.queue.enqueue(say_hello)
        worker = Worker([self.queue], connection=self.connection)
        execution = worker.prepare_execution(job=job)

        fetched_execution = Execution.fetch(id=execution.id, job_id=job.id, connection=self.connection)
        self.assertEqual(execution, fetched_execution)

        self.connection.delete(execution.key)
        # Execution.fetch raises ValueError if execution is not found
        with self.assertRaises(ValueError):
            Execution.fetch(id=execution.id, job_id=job.id, connection=self.connection)

    def test_init_from_composite_key(self):
        """Ensure the from_composite_key can correctly parse job_id and execution_id"""
        composite_key = 'job_id:execution_id'
        execution = Execution.from_composite_key(composite_key, connection=self.connection)
        self.assertEqual(execution.job_id, 'job_id')
        self.assertEqual(execution.id, 'execution_id')

    def test_job_auto_fetch(self):
        """Ensure that if the job is not set, the Job.fetch is not called"""
        job = self.queue.enqueue(say_hello)
        execution = Execution('execution_id', job.id, connection=self.connection)

        with patch.object(Job, 'fetch') as mock:
            mock.return_value = Job(id=job.id, connection=self.connection)
            # the first call would fetch the job
            first_fetch = execution.job
            self.assertEqual(first_fetch.id, job.id)
            self.assertEqual(mock.call_count, 1)
            self.assertNotEqual(id(job), id(first_fetch))

            # the second call should return the same object
            second_fetch = execution.job
            self.assertEqual(second_fetch.id, job.id)
            # call count remains the same
            self.assertEqual(mock.call_count, 1)
            self.assertEqual(id(first_fetch), id(second_fetch))
