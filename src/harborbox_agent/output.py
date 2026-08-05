from dataclasses import dataclass


@dataclass
class OutputBudget:
    limit: int
    used: int = 0
    truncated: bool = False

    def take(self, text: str) -> str:
        encoded = text.encode("utf-8", errors="replace")
        remaining = max(0, self.limit - self.used)
        if len(encoded) <= remaining:
            self.used += len(encoded)
            return text
        self.truncated = True
        self.used = self.limit
        return encoded[:remaining].decode("utf-8", errors="ignore")
