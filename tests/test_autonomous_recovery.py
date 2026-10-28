import unittest
from unittest.mock import MagicMock
from core.execution_engine import ExecutionEngine, StepOutcome
from core.shell_manager import ShellExecutionError, ShellResult
from core.goal_engine import GoalEngine, GoalStep

class TestAutonomousRecovery(unittest.TestCase):
    def test_execution_engine_catches_shell_error(self):
        # Mock dependencies
        goal_engine = MagicMock(spec=GoalEngine)
        goal_engine.store = MagicMock()
        goal_engine.store.get_goal.return_value = MagicMock()
        goal_engine.get_next_step.return_value = GoalStep(step_id="1", description="test", tool="test_tool")
        
        # Mock tool_fn to raise ShellExecutionError
        def failing_tool(tool, input, meta):
            raise ShellExecutionError(ShellResult(1, "", "", error="Executable not found"))
            
        engine = ExecutionEngine(goal_engine, tool_fn=failing_tool)
        
        # Execute
        result = engine.execute_next_step("goal1")
        
        # Verify
        self.assertEqual(result.outcome, StepOutcome.FAILURE)
        self.assertIn("Shell failed: MISSING_EXECUTABLE", result.error)
        goal_engine.fail_step.assert_called()

if __name__ == "__main__":
    unittest.main()
