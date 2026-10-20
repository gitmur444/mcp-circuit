"""MCP Circuit adapter for GitHub: nothing to launch, the target is github.com. Tasks are checked by the answer."""
from project import TargetBase


class Target(TargetBase):
    has_app = False

    def check(self, i, spec, answer=None):
        """spec: {"all": [...]} — every value must appear in the answer (case-insensitive)."""
        text = (answer or "").lower()
        text += " " + text.replace(",", "")  # 1,288 = 1288
        return all(str(v).lower() in text for v in spec["all"])
