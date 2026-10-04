from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    title: str
    body: str
