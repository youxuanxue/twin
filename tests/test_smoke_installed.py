from unittest import TestCase

from tests import smoke_installed


class SmokeInstalledTest(TestCase):
    def test_action_token_option_preserves_a_leading_dash_token(self) -> None:
        self.assertEqual(
            smoke_installed._action_token_option("--generated-token"),
            "--action-token=--generated-token",
        )
