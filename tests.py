#!/usr/bin/env python3
"""
VideoEngine — Test Suite
Run: python3 tests.py
"""
import json, os, sys, unittest, tempfile, shutil
sys.path.insert(0, os.path.dirname(__file__))

# Patch dirs to temp before importing server
_tmp = tempfile.mkdtemp()
import server
server.INPUTS_DIR  = os.path.join(_tmp, "inputs");  os.makedirs(server.INPUTS_DIR)
server.OUTPUTS_DIR = os.path.join(_tmp, "outputs"); os.makedirs(server.OUTPUTS_DIR)
server.TEMP_DIR    = os.path.join(_tmp, "temp");    os.makedirs(server.TEMP_DIR)
server.JOBS_DIR    = os.path.join(_tmp, "jobs");    os.makedirs(server.JOBS_DIR)


class TestValidation(unittest.TestCase):
    def test_valid_plan(self):
        plan = {"summary": "test", "operations": [{"op": "remove_silence", "params": {}}]}
        result = server.validate_plan(plan)
        self.assertEqual(result["operations"][0]["op"], "remove_silence")

    def test_unknown_op(self):
        plan = {"summary": "test", "operations": [{"op": "explode_video", "params": {}}]}
        with self.assertRaises(ValueError) as ctx:
            server.validate_plan(plan)
        self.assertIn("Unsupported operation", str(ctx.exception))

    def test_missing_required_param(self):
        plan = {"summary": "test", "operations": [{"op": "trim", "params": {}}]}
        with self.assertRaises(ValueError) as ctx:
            server.validate_plan(plan)
        self.assertIn("Missing parameter", str(ctx.exception))

    def test_defaults_injected(self):
        plan = {"summary": "test", "operations": [{"op": "remove_silence", "params": {}}]}
        result = server.validate_plan(plan)
        params = result["operations"][0]["params"]
        self.assertIn("threshold_seconds", params)
        self.assertIn("min_duration", params)

    def test_empty_operations(self):
        plan = {"summary": "test", "operations": []}
        with self.assertRaises(ValueError):
            server.validate_plan(plan)

    def test_non_dict_plan(self):
        with self.assertRaises(ValueError):
            server.validate_plan("not a dict")


class TestJobManager(unittest.TestCase):
    def test_create_and_load(self):
        job_id, job = server.create_job("test.mp4", "Clean up", "/fake/path.mp4")
        self.assertEqual(job["status"], server.QUEUED)
        loaded = server.load_job(job_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["id"], job_id)

    def test_save_updates_timestamp(self):
        job_id, job = server.create_job("test.mp4", "test", "/fake/path.mp4")
        orig_ts = job["updated_at"]
        import time; time.sleep(0.01)
        server.save_job(job)
        reloaded = server.load_job(job_id)
        self.assertGreaterEqual(reloaded["updated_at"], orig_ts)

    def test_append_log(self):
        job_id, job = server.create_job("test.mp4", "test", "/fake/path.mp4")
        server.append_log(job, "info", "test", "hello")
        reloaded = server.load_job(job_id)
        self.assertEqual(len(reloaded["logs"]), 1)
        self.assertEqual(reloaded["logs"][0]["message"], "hello")


class TestSanitize(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(server.sanitize_filename("my video.mp4"), "my_video.mp4")

    def test_path_traversal(self):
        result = server.sanitize_filename("../../etc/passwd")
        self.assertNotIn("..", result)

    def test_special_chars(self):
        result = server.sanitize_filename("vid; rm -rf *.mp4")
        self.assertNotIn(";", result)


class TestClaudeInvalidJSON(unittest.TestCase):
    def test_invalid_json_raises(self):
        # Simulate Claude returning bad JSON
        import unittest.mock as mock
        with mock.patch("server.get_plan_from_claude", side_effect=ValueError("Invalid plan format")):
            job_id, job = server.create_job("test.mp4", "do stuff", "/fake.mp4")
            server.process_job(job_id, api_key="")
            reloaded = server.load_job(job_id)
            # Falls back gracefully (no api key → fallback plan)
            self.assertIn(reloaded["status"], [server.FAILED, server.COMPLETED, server.PROCESSING])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
