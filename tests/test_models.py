import unittest

from maestro.models import Mode, Role, Status, Verdict, Workflow


def make_workflow(**kwargs):
    defaults = dict(
        prompt="Add a health check endpoint",
        repo_path="/tmp/repo",
        base_branch="main",
        agents={"plan": "claude", "execute": "codex", "review": "claude"},
    )
    defaults.update(kwargs)
    return Workflow(**defaults)


class WorkflowSerialisation(unittest.TestCase):
    def test_round_trips_through_json_dict(self):
        wf = make_workflow(mode=Mode.AUTONOMOUS, max_rounds=3)
        wf.branch = "maestro/add-a-health-check-endpoint-abc123"
        wf.status = Status.REVIEWING
        wf.verdict = Verdict.APPROVE
        wf.plan_text = "step one"
        step = wf.add_step(Role.PLAN)
        step.status = Status.DONE
        step.exit_code = 0

        restored = Workflow.from_dict(wf.to_dict())

        self.assertEqual(restored.id, wf.id)
        self.assertEqual(restored.mode, Mode.AUTONOMOUS)
        self.assertEqual(restored.status, Status.REVIEWING)
        self.assertEqual(restored.verdict, Verdict.APPROVE)
        self.assertEqual(restored.max_rounds, 3)
        self.assertEqual(len(restored.steps), 1)
        self.assertEqual(restored.steps[0].role, Role.PLAN)
        self.assertEqual(restored.steps[0].agent, "claude")

    def test_handles_absent_verdict(self):
        restored = Workflow.from_dict(make_workflow().to_dict())
        self.assertIsNone(restored.verdict)


class WorkflowBehaviour(unittest.TestCase):
    def test_agent_for_role(self):
        wf = make_workflow()
        self.assertEqual(wf.agent_for(Role.EXECUTE), "codex")

    def test_add_step_records_current_round_and_agent(self):
        wf = make_workflow()
        wf.round = 2
        step = wf.add_step(Role.REVIEW)
        self.assertEqual(step.round, 2)
        self.assertEqual(step.agent, "claude")

    def test_last_step_returns_most_recent_for_role(self):
        wf = make_workflow()
        wf.add_step(Role.EXECUTE)
        wf.round = 2
        second = wf.add_step(Role.EXECUTE)
        self.assertIs(wf.last_step(Role.EXECUTE), second)
        self.assertIsNone(wf.last_step(Role.PLAN))

    def test_summary_truncates_and_collapses_whitespace(self):
        wf = make_workflow(prompt="a\n  b   c" + "x" * 80)
        self.assertLessEqual(len(wf.summary), 50)
        self.assertNotIn("\n", wf.summary)

    def test_is_terminal(self):
        wf = make_workflow()
        self.assertFalse(wf.is_terminal)
        wf.status = Status.MERGED
        self.assertTrue(wf.is_terminal)


if __name__ == "__main__":
    unittest.main()
