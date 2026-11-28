import time
import unittest
from types import SimpleNamespace
from harness.engine import Harness
from harness.config import settings
from harness.tests.fakes import MockRobot

class CameraClockTests(unittest.TestCase):
    def validate(self,offset):
        observation=MockRobot().observe()
        observation['frames']['head']['captured_at']=time.time()+offset
        Harness._validate_observation(SimpleNamespace(c=settings()),observation)
    def test_small_future_clock_offset_is_accepted(self):self.validate(.25)
    def test_large_future_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'head'):self.validate(2)
    def test_stale_image_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError,'head'):self.validate(-4)

if __name__=='__main__':unittest.main()
