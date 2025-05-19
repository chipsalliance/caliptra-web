from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Section:
    name: str
    level: int
    parent: Optional['Section'] = None
    content: list[str | Table] = field(default_factory=list)
    children: list['Section'] = field(default_factory=list)

    @staticmethod
    def find_parent(current: 'Section', level: int) -> 'Section':
        while level <= current.level:
            current = current.parent
        return current

    def find_subsection(self, name: str) -> 'Section':
        stack: list['Section'] = [self]
        while len(stack) > 0:
            current = stack.pop()
            if current.name == name:
                return current
            stack.extend(current.children)
        raise ValueError(f'Subsection "{name}" does not exist')

    def __post_init__(self):
        if self.parent is not None:
            self.parent.children.append(self)

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'level': self.level,
            'content': [x if isinstance(x, str) else x.to_dict() for x in self.content],
            'children': [x.to_dict() for x in self.children],
        }
