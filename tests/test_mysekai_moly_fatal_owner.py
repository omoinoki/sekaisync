"""Bounded subprocess regressions for shared-bundle fatal-owner settlement."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock
from urllib.parse import urlsplit

from sekaisync import crawler, mysekai_moly
from tests.test_mysekai_moly_completeness_review import _MolyPublicFixture


ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "MOLY_FATAL_OWNER_CHILD_RESULT="


def _run_child(mode, workers):
    owner = unittest.TestCase()
    fixture = _MolyPublicFixture(owner)
    original_fetch = fixture.fetch
    peer_waiting = threading.Event()
    shared_futures = []
    fetch_calls = []
    decoder_calls = []
    peer_exception_ids = []
    failure = (KeyboardInterrupt("injected decoder interrupt") if mode == "decode-interrupt" else
               OSError("injected local network failure") if mode == "network" else
               RuntimeError("injected fatal bundle owner"))
    original_future = crawler.Future

    class ObservedSharedFuture(original_future):
        def __init__(self):
            super().__init__()
            shared_futures.append(self)

        def result(self, *args, **kwargs):
            if not self.done() and threading.current_thread().name.startswith("ThreadPoolExecutor"):
                peer_waiting.set()
            try:
                return super().result(*args, **kwargs)
            except BaseException as error:
                owner.assertIs(error, failure)
                peer_exception_ids.append(id(error))
                raise

    def fail_after_peer_wait():
        if workers > 1:
            owner.assertTrue(peer_waiting.wait(3), "Peer did not wait on the real shared bundle Future")
        raise failure

    def fetch(url):
        if urlsplit(url).path.startswith("/moly/catalog-store/"):
            fetch_calls.append(url)
            if mode in {"fetch-runtime", "network"}:
                fail_after_peer_wait()
        return original_fetch(url)

    def fatal_decoder(text, expected_hash):
        decoder_calls.append(expected_hash)
        fail_after_peer_wait()

    fixture.fetch = fetch
    decoder_patch = (mock.patch.object(mysekai_moly, "decode_bundle", side_effect=fatal_decoder)
                     if mode.startswith("decode-") else nullcontext())
    propagated = None
    pages = None
    try:
        with mock.patch.object(crawler, "Future", ObservedSharedFuture), decoder_patch:
            try:
                _, pages = fixture.crawl(workers=workers)
            except BaseException as error:
                owner.assertIs(error, failure)
                propagated = type(error).__name__
        owner.assertEqual(len(fetch_calls), 1, "All candidate talks must share exactly one failing fetch")
        owner.assertEqual(len(shared_futures), 1)
        owner.assertTrue(shared_futures[0].done(), "Owner left the shared promise pending")
        owner.assertIs(shared_futures[0].exception(), failure)
        if workers > 1:
            owner.assertTrue(peer_waiting.is_set())
            owner.assertTrue(peer_exception_ids, "Peer did not receive the original failure")
            owner.assertTrue(all(identity == id(failure) for identity in peer_exception_ids))
        if mode == "network":
            owner.assertIsNone(propagated)
            owner.assertEqual(pages, [], "Ordinary network errors must remain local acquisition failures")
        else:
            owner.assertEqual(propagated, type(failure).__name__)
        if mode.startswith("decode-"):
            owner.assertEqual(len(decoder_calls), 1)
        result = dict(mode=mode, workers=workers, propagated=propagated, shared_future_done=True,
            shared_exception_is_original=True, peer_wait_confirmed=peer_waiting.is_set(),
            peer_original_exception_deliveries=len(peer_exception_ids), bundle_fetches=len(fetch_calls),
            decoder_calls=len(decoder_calls), saved_body_count=len(pages) if pages is not None else None)
    finally:
        owner.doCleanups()
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)


class MolyFatalOwnerPublicTests(unittest.TestCase):
    def child_case(self, mode, workers):
        command = [sys.executable, "-X", "utf8", "-m", "tests.test_mysekai_moly_fatal_owner",
                   "--child", mode, str(workers)]
        environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            completed = subprocess.run(command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", timeout=12, check=False)
        except subprocess.TimeoutExpired as error:
            self.fail(f"Public crawl child hung past 12 seconds: {mode}/{workers}; output={error.stdout!r}")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        rows = [line.removeprefix(RESULT_PREFIX) for line in completed.stdout.splitlines()
                if line.startswith(RESULT_PREFIX)]
        self.assertEqual(len(rows), 1, completed.stdout)
        result = json.loads(rows[0])
        self.assertTrue(result["shared_future_done"])
        self.assertTrue(result["shared_exception_is_original"])
        self.assertEqual(result["bundle_fetches"], 1)
        if workers > 1:
            self.assertTrue(result["peer_wait_confirmed"])
            self.assertGreaterEqual(result["peer_original_exception_deliveries"], 1)
        return result

    def test_fetch_runtime_error_single_worker_propagates(self):
        self.assertEqual(self.child_case("fetch-runtime", 1)["propagated"], "RuntimeError")

    def test_fetch_runtime_error_wakes_peer_and_propagates(self):
        self.assertEqual(self.child_case("fetch-runtime", 2)["propagated"], "RuntimeError")

    def test_decoder_runtime_error_wakes_peer_and_propagates(self):
        self.assertEqual(self.child_case("decode-runtime", 2)["propagated"], "RuntimeError")

    def test_decoder_keyboard_interrupt_wakes_peer_and_propagates(self):
        self.assertEqual(self.child_case("decode-interrupt", 2)["propagated"], "KeyboardInterrupt")

    def test_shared_network_failure_fetches_once_and_stays_local(self):
        result = self.child_case("network", 2)
        self.assertIsNone(result["propagated"])
        self.assertEqual(result["saved_body_count"], 0)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        _run_child(sys.argv[2], int(sys.argv[3]))
    else:
        unittest.main()
